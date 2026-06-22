"""Client for the chat-search embedding model (qwen3-vl-embedding-8b).

A standalone multimodal embedder (4096-dim) reached over HTTP, used by two paths:
- the backfill / keep-fresh sweep (sync, ``requests``) to embed stored messages, and
- the search query path (async, ``aiohttp``) to embed the user's query text.

Text-only content uses the OpenAI-compatible ``/v1/embeddings`` endpoint; messages that
carry image(s) are fused (text + images -> one vector) via the native ``/embeddings``
endpoint with one ``<__media__>`` token per image. Raw base64 only (no data-URL prefix).
"""

import math
import os
from typing import Optional

import aiohttp
import requests

# Config (env-overridable; defaults to the local VL embedder).
CHAT_EMBED_URL = os.environ.get("CHAT_EMBED_URL", "http://127.0.0.1:8085").rstrip("/")
CHAT_EMBED_MODEL = os.environ.get("CHAT_EMBED_MODEL", "qwen3-vl-embedding-8b")
CHAT_EMBED_DIM = int(os.environ.get("CHAT_EMBED_DIM", "4096"))
# Master switch for blending semantic (embedding) results into chat search.
CHAT_SEMANTIC_ENABLED = os.environ.get("ENABLE_CHAT_SEMANTIC_SEARCH", "true").lower() in ("1", "true", "yes")
# Cap images fused into one message vector (89% of image msgs have 1; max seen 45).
CHAT_EMBED_MAX_IMAGES = int(os.environ.get("CHAT_EMBED_MAX_IMAGES", "4"))


def _media_prompt(text: str, n_images: int) -> str:
    media = "\n".join(["<__media__>"] * n_images)
    text = (text or "").strip()
    if n_images and text:
        return f"{media}\n{text}"
    return media or text


def _strip_data_url(b64: str) -> str:
    """Embedder wants raw base64; strip any ``data:image/...;base64,`` prefix."""
    if "base64," in b64:
        return b64.split("base64,", 1)[1]
    return b64


def _prepare_image(b64: str, max_px: int = 512) -> str:
    """Normalize an image to a downscaled JPEG (raw base64) before embedding. The VL
    model 500s on certain PNG dimensions/formats even when small; re-encoding to a
    bounded JPEG fixes that and keeps the model stable under load. Falls back to the
    original base64 if Pillow is unavailable or the image can't be decoded."""
    raw_b64 = _strip_data_url(b64)
    try:
        import base64
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(base64.b64decode(raw_b64)))
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return raw_b64


# ── Sync (backfill / keep-fresh sweep) ──────────────────────────────────────
def _post_texts(texts: list[str], timeout: int) -> list[list[float]]:
    r = requests.post(
        f"{CHAT_EMBED_URL}/v1/embeddings",
        json={"model": CHAT_EMBED_MODEL, "input": texts},
        timeout=timeout,
    )
    r.raise_for_status()
    # Order by the response's own ``index`` rather than array order — callers zip
    # results back with the input list, so misordering would mis-pair vectors.
    data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


def _is_context_error(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return resp is not None and resp.status_code == 400


def embedder_healthy_sync(timeout: int = 15) -> bool:
    """True only if the embedder returns a VALID vector for a known-good probe. Used to
    distinguish 'embedder is down/garbage' (skip, retry later) from 'this specific
    message is permanently unembeddable' (e.g. a bad image — mark and move on)."""
    try:
        vecs = _post_texts(["__embedder_healthcheck__"], timeout)
        return bool(vecs) and is_valid_vector(vecs[0])
    except Exception:
        return False


def _embed_one_resilient(text: str, timeout: int) -> Optional[list[float]]:
    """Embed a single text, progressively truncating if it exceeds the model context.
    Returns None (never raises) when it overflows even truncated — so one bad text
    can't abort the whole backfill batch."""
    t = text
    for _ in range(5):
        try:
            return _post_texts([t or " "], timeout)[0]
        except requests.exceptions.HTTPError as e:
            if _is_context_error(e):
                if len(t) > 200:
                    t = t[: max(200, (len(t) * 2) // 3)]  # shrink ~33% and retry
                    continue
                return None  # can't shrink further and still overflows — give up
            raise  # genuine non-context error
    return None


def embed_texts_sync(texts: list[str], timeout: int = 600) -> list[Optional[list[float]]]:
    """Batch-embed plain texts. Resilient to the model's context limit: a batch that
    overflows is split and the offending texts are truncated. Returns vectors aligned
    with ``texts`` (None for any that couldn't be embedded)."""
    if not texts:
        return []
    try:
        return _post_texts(texts, timeout)  # fast path: whole batch fits
    except requests.exceptions.HTTPError as e:
        if not _is_context_error(e):
            raise
        if len(texts) == 1:
            return [_embed_one_resilient(texts[0], timeout)]
        mid = len(texts) // 2  # split and recurse so one long text can't fail the batch
        return embed_texts_sync(texts[:mid], timeout) + embed_texts_sync(texts[mid:], timeout)


def embed_fused_sync(text: str, images: list[str], timeout: int = 300) -> Optional[list[float]]:
    """Embed one message that has image(s): fuse text + (capped) images -> one vector.
    On context overflow, truncate the text, then drop images, until it fits."""
    imgs = [_prepare_image(b) for b in images[:CHAT_EMBED_MAX_IMAGES]]
    t = (text or "").strip()
    for attempt in range(5):
        try:
            r = requests.post(
                f"{CHAT_EMBED_URL}/embeddings",
                json={"content": [{"prompt_string": _media_prompt(t, len(imgs)), "multimodal_data": imgs}]},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()[0]["embedding"][0]
        except requests.exceptions.HTTPError as e:
            if not _is_context_error(e):
                raise
            if len(t) > 100:
                t = t[: max(0, (len(t) * 1) // 2)]  # halve the text first
            elif len(imgs) > 1:
                imgs = imgs[:-1]  # then shed images
            else:
                return None
    return None


# ── Async (search query path) ───────────────────────────────────────────────
async def aembed_query(text: str, timeout: int = 20) -> Optional[list[float]]:
    """Embed the user's (text) search query. Returns None on failure so the
    caller can degrade to lexical-only search rather than error the request."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{CHAT_EMBED_URL}/v1/embeddings",
                json={"model": CHAT_EMBED_MODEL, "input": text},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                resp.raise_for_status()
                j = await resp.json()
                vec = j["data"][0]["embedding"]
                # Validate before returning: a degraded embedder can return a 200 with
                # an all-None/garbage vector (not an exception). Returning that would
                # coerce to an all-zero query vector -> NaN cosine distance -> random
                # chats injected into the ranking. Gating here makes the caller's
                # ``if query_vector:`` fall back to clean lexical-only search instead.
                return vec if is_valid_vector(vec) else None
    except Exception:
        return None


def is_valid_vector(vec) -> bool:
    """A usable embedding: a list of exactly CHAT_EMBED_DIM finite numbers. The fused
    image endpoint occasionally returns malformed vectors (None elements) for bad
    images — those must be skipped, not stored."""
    return (
        isinstance(vec, list)
        and len(vec) == CHAT_EMBED_DIM
        and all(isinstance(x, (int, float)) and math.isfinite(x) for x in vec)
    )


def to_pgvector_literal(vec: list[float]) -> str:
    """Format a vector as a pgvector text literal: ``[f1,f2,...]`` (cast ``::vector``).
    None / non-finite components (which pgvector rejects) are coerced to 0 so this never
    raises; callers should gate on ``is_valid_vector`` first to avoid storing garbage."""
    return (
        "["
        + ",".join(
            (repr(float(x)) if (isinstance(x, (int, float)) and math.isfinite(x)) else "0")
            for x in vec
        )
        + "]"
    )


async def embedding_sweeper_loop(up_interval: int = 120, down_interval: int = 45) -> None:
    """Keep-fresh + self-healing background task for chat-message embeddings.

    The embedder is an external service that can be down and come back later. Each pass
    health-checks it first:
      - DOWN  -> back off (``down_interval``), don't scan or hammer; retry sooner so we
                 notice recovery quickly. Search degrades to lexical-only meanwhile.
      - UP    -> run a full sweep (``limit=None``) that embeds EVERYTHING still pending —
                 so when the embedder comes back after an outage, all the work that piled
                 up (new messages, anything not yet computed) gets backfilled automatically.
    The write path is synchronous, so embedding is decoupled into this sweep rather than
    coupled to message writes. NULL-marked permanent failures are skipped (not retried).
    """
    import asyncio
    import logging

    log = logging.getLogger(__name__)
    if not CHAT_SEMANTIC_ENABLED:
        return
    # Lazy import to avoid a circular import (the script imports this module).
    from open_webui.scripts.backfill_chat_embeddings import run_sweep

    was_up: Optional[bool] = None
    while True:
        try:
            healthy = (await aembed_query("__embedder_healthcheck__")) is not None
            if healthy:
                if was_up is False:
                    log.info("chat embedder recovered — backfilling pending embeddings")
                was_up = True
                # limit=None: catch up ALL pending in one go (cheap in steady state, full
                # backfill after an outage). Runs in a thread (off the event loop).
                n = await asyncio.to_thread(run_sweep, None, 200, lambda *a: None)
                if n:
                    log.info("chat embedding sweep: embedded %d pending message(s)", n)
                await asyncio.sleep(up_interval)
            else:
                if was_up is not False:
                    log.warning(
                        "chat embedder is down — semantic search degraded to lexical; retrying"
                    )
                was_up = False
                await asyncio.sleep(down_interval)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("chat embedding keep-fresh sweep failed")
            await asyncio.sleep(up_interval)

