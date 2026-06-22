"""
Model Pricing Models for the Cost-Calculation Feature.

Two tables back USD cost calculation:

- ``ModelPricingCatalog``: per-model rates synced from OpenRouter's bulk catalog
  (``GET https://openrouter.ai/api/v1/models``). Keyed by the OpenRouter slug.
- ``ModelPricingOverride``: admin-managed mapping keyed by the EXACT stored
  ``token_usage_event.model_id``. ``mode`` is one of:
    - ``alias``  -> use the rates of ``alias_slug`` from the catalog
    - ``manual`` -> use the explicit ``*_rate`` columns on this row
    - ``zero``   -> pin cost to $0 (e.g. truly-free local models)

Rates are stored PER-TOKEN in USD, matching the units of OpenRouter's embedded
``cost`` field, so cost is a plain multiply.

This module mirrors the sync-methods + async-proxy pattern of
``open_webui.models.analytics`` so the cost-aggregation code can resolve the
pricing map without ``await`` while still being awaited from async callers.
"""

import inspect
import logging
import time
import uuid
from typing import Dict, List, Optional

from open_webui.internal.db import Base, get_db, run_sync_db
from open_webui.env import SRC_LOG_LEVELS

from sqlalchemy import BigInteger, Boolean, Column, Float, String, JSON
from sqlalchemy.dialects.postgresql import insert as pg_insert

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# SQLAlchemy Models
####################


class ModelPricingCatalog(Base):
    __tablename__ = "model_pricing_catalog"

    slug = Column(String, primary_key=True)
    model_name = Column(String, nullable=True)
    prompt_rate = Column(Float, nullable=True)
    completion_rate = Column(Float, nullable=True)
    cache_read_rate = Column(Float, nullable=True)
    web_search_rate = Column(Float, nullable=True)
    is_free = Column(Boolean, default=False)
    raw_pricing = Column(JSON, nullable=True)
    synced_at = Column(BigInteger, nullable=True)


class ModelPricingOverride(Base):
    __tablename__ = "model_pricing_override"

    model_id = Column(String, primary_key=True)
    mode = Column(String, default="alias")  # alias | manual | zero
    alias_slug = Column(String, nullable=True)
    prompt_rate = Column(Float, nullable=True)
    completion_rate = Column(Float, nullable=True)
    cache_read_rate = Column(Float, nullable=True)
    note = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)
    created_at = Column(BigInteger, nullable=True)
    updated_at = Column(BigInteger, nullable=True)


####################
# CRUD
####################


def _catalog_to_dict(row: ModelPricingCatalog) -> dict:
    return {
        "slug": row.slug,
        "model_name": row.model_name,
        "prompt_rate": row.prompt_rate,
        "completion_rate": row.completion_rate,
        "cache_read_rate": row.cache_read_rate,
        "web_search_rate": row.web_search_rate,
        "is_free": bool(row.is_free),
        "synced_at": row.synced_at,
    }


def _override_to_dict(row: ModelPricingOverride) -> dict:
    return {
        "model_id": row.model_id,
        "mode": row.mode,
        "alias_slug": row.alias_slug,
        "prompt_rate": row.prompt_rate,
        "completion_rate": row.completion_rate,
        "cache_read_rate": row.cache_read_rate,
        "note": row.note,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


class PricingTable:
    """Sync DB operations for model pricing (wrapped by the async proxy)."""

    def upsert_catalog(self, rows: List[dict], synced_at: int) -> int:
        """Replace catalog rates for the given slugs. Upsert, never wipe.

        Each row dict: slug, model_name, prompt_rate, completion_rate,
        cache_read_rate, web_search_rate, is_free, raw_pricing.
        Returns the number of rows written.
        """
        if not rows:
            return 0
        try:
            with get_db() as db:
                count = 0
                for r in rows:
                    slug = r.get("slug")
                    if not slug:
                        continue
                    values = {
                        "slug": slug,
                        "model_name": r.get("model_name"),
                        "prompt_rate": r.get("prompt_rate"),
                        "completion_rate": r.get("completion_rate"),
                        "cache_read_rate": r.get("cache_read_rate"),
                        "web_search_rate": r.get("web_search_rate"),
                        "is_free": bool(r.get("is_free", False)),
                        "raw_pricing": r.get("raw_pricing"),
                        "synced_at": synced_at,
                    }
                    stmt = pg_insert(ModelPricingCatalog).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[ModelPricingCatalog.slug],
                        set_={k: v for k, v in values.items() if k != "slug"},
                    )
                    db.execute(stmt)
                    count += 1
                db.commit()
                return count
        except Exception as e:
            log.error(f"Error upserting pricing catalog: {e}")
            return 0

    def list_catalog(self) -> List[dict]:
        try:
            with get_db() as db:
                rows = db.query(ModelPricingCatalog).all()
                return [_catalog_to_dict(r) for r in rows]
        except Exception as e:
            log.error(f"Error listing pricing catalog: {e}")
            return []

    def catalog_synced_at(self) -> Optional[int]:
        try:
            with get_db() as db:
                row = (
                    db.query(ModelPricingCatalog.synced_at)
                    .order_by(ModelPricingCatalog.synced_at.desc())
                    .first()
                )
                return int(row[0]) if row and row[0] is not None else None
        except Exception as e:
            log.error(f"Error reading catalog synced_at: {e}")
            return None

    def list_overrides(self) -> List[dict]:
        try:
            with get_db() as db:
                rows = db.query(ModelPricingOverride).all()
                return [_override_to_dict(r) for r in rows]
        except Exception as e:
            log.error(f"Error listing pricing overrides: {e}")
            return []

    def upsert_override(
        self,
        model_id: str,
        mode: str,
        alias_slug: Optional[str] = None,
        prompt_rate: Optional[float] = None,
        completion_rate: Optional[float] = None,
        cache_read_rate: Optional[float] = None,
        note: Optional[str] = None,
        updated_by: Optional[str] = None,
    ) -> Optional[dict]:
        if not model_id or mode not in ("alias", "manual", "zero"):
            return None
        try:
            now = int(time.time())
            with get_db() as db:
                values = {
                    "model_id": model_id,
                    "mode": mode,
                    "alias_slug": alias_slug if mode == "alias" else None,
                    "prompt_rate": prompt_rate if mode == "manual" else None,
                    "completion_rate": completion_rate if mode == "manual" else None,
                    "cache_read_rate": cache_read_rate if mode == "manual" else None,
                    "note": note,
                    "updated_by": updated_by,
                    "updated_at": now,
                }
                stmt = pg_insert(ModelPricingOverride).values(created_at=now, **values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[ModelPricingOverride.model_id],
                    set_={k: v for k, v in values.items() if k != "model_id"},
                )
                db.execute(stmt)
                db.commit()
                return {"model_id": model_id, "mode": mode, "alias_slug": values["alias_slug"]}
        except Exception as e:
            log.error(f"Error upserting pricing override for {model_id}: {e}")
            return None

    def delete_override(self, model_id: str) -> bool:
        try:
            with get_db() as db:
                row = db.get(ModelPricingOverride, model_id)
                if row:
                    db.delete(row)
                    db.commit()
                return True
        except Exception as e:
            log.error(f"Error deleting pricing override for {model_id}: {e}")
            return False

    def get_pricing_map(self) -> Dict[str, Dict]:
        """Return {"catalog": {slug: rate_dict}, "overrides": {model_id: row_dict}}.

        rate_dict has prompt/completion/cache_read rate keys (per-token USD).
        """
        catalog = {}
        for r in self.list_catalog():
            catalog[r["slug"]] = {
                "prompt": r.get("prompt_rate") or 0.0,
                "completion": r.get("completion_rate") or 0.0,
                "cache_read": r.get("cache_read_rate") or 0.0,
                "is_free": r.get("is_free", False),
            }
        overrides = {o["model_id"]: o for o in self.list_overrides()}
        return {"catalog": catalog, "overrides": overrides}


class _AsyncPricingProxy:
    def __init__(self, impl: PricingTable):
        self._impl = impl

    def __getattr__(self, name):
        attr = getattr(self._impl, name)
        if not callable(attr):
            return attr

        async def _wrapped(*args, **kwargs):
            if inspect.iscoroutinefunction(attr):
                return await attr(*args, **kwargs)
            return await run_sync_db(lambda: attr(*args, **kwargs))

        return _wrapped


# Sync instance (for in-process use by sync aggregation code) and async proxy.
PricingSync = PricingTable()
Pricing = _AsyncPricingProxy(PricingSync)
