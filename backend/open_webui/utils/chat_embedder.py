"""Client for the chat-search embedding model (qwen3-vl-embedding-8b).

A standalone multimodal embedder (4096-dim) reached over HTTP, used by two paths:
- the backfill / keep-fresh sweep (sync, ``requests``) to embed stored messages, and
- the search query path (async, ``aiohttp``) to embed the user's query text.

All content — text-only and image-bearing — goes through the OpenAI-compatible
``/v1/embeddings`` endpoint (the only one llama-swap routes, via the ``model`` body
field). ``input`` items are either plain strings or ``{prompt_string, multimodal_data}``
objects; image messages are fused (text + images -> one vector) with EXACTLY one
``<__media__>`` token per image (marker count must equal multimodal_data length).
Raw base64 only (no ``data:image/...;base64,`` prefix). Response is the OpenAI shape:
``{"data": [{"index": N, "embedding": [flat floats]}]}``.
"""

import math
import os
from typing import Optional

import aiohttp
import requests

# Config (env-overridable; defaults to the local llama-swap, which routes to the
# right upstream by the ``model`` field in each /v1/embeddings request body).
CHAT_EMBED_URL = os.environ.get("CHAT_EMBED_URL", "http://127.0.0.1:8085").rstrip("/")
CHAT_EMBED_MODEL = os.environ.get("CHAT_EMBED_MODEL", "qwen3-vl-embedding-8b")
CHAT_EMBED_DIM = int(os.environ.get("CHAT_EMBED_DIM", "4096"))
# Master switch for blending semantic (embedding) results into chat search.
CHAT_SEMANTIC_ENABLED = os.environ.get("ENABLE_CHAT_SEMANTIC_SEARCH", "true").lower() in ("1", "true", "yes")
# Cap images fused into one message vector (89% of image msgs have 1; max seen 45).
CHAT_EMBED_MAX_IMAGES = int(os.environ.get("CHAT_EMBED_MAX_IMAGES", "4"))
# How often (seconds) the deferred keep-fresh sweep runs when the embedder is healthy.
# New messages are NOT embedded at write time — they accumulate until the next sweep,
# which sends them in one sequential burst (easier on the inference server).
CHAT_EMBED_SWEEP_INTERVAL = int(os.environ.get("CHAT_EMBED_SWEEP_INTERVAL", "120"))
# How many text-only messages go into one HTTP request body during a sweep (the server
# returns one vector per item; texts are never fused). Concurrency is always 1 —
# requests within a sweep are strictly sequential.
CHAT_EMBED_TEXT_BATCH = int(os.environ.get("CHAT_EMBED_TEXT_BATCH", "16"))


def apply_runtime_config(
    url: Optional[str] = None,
    model: Optional[str] = None,
    enabled: Optional[bool] = None,
    sweep_interval: Optional[int] = None,
    text_batch: Optional[int] = None,
) -> None:
    """Update the live embedder settings at runtime (the admin-config bridge).

    Both the sync backfill sweep (``ce.CHAT_EMBED_URL`` etc.) and the async query
    path read these module globals at *call* time, so reassigning them here takes
    effect immediately — no restart. Called once at startup with the persisted
    PersistentConfig values, and again whenever an admin saves the config.
    URL/model/enable plus the deferred-sweep knobs (interval + per-request text batch)
    are runtime-mutable; CHAT_EMBED_DIM is pinned to the pgvector column dimension and
    can't be changed without a schema migration.
    """
    global CHAT_EMBED_URL, CHAT_EMBED_MODEL, CHAT_SEMANTIC_ENABLED
    global CHAT_EMBED_SWEEP_INTERVAL, CHAT_EMBED_TEXT_BATCH
    if url is not None:
        CHAT_EMBED_URL = url.rstrip("/")
    if model is not None:
        CHAT_EMBED_MODEL = model
    if enabled is not None:
        CHAT_SEMANTIC_ENABLED = bool(enabled)
    if sweep_interval is not None:
        # Floor of 10s: below that the "deferred batch" degrades into a hot loop of
        # near-continuous scans, defeating the point of batching.
        CHAT_EMBED_SWEEP_INTERVAL = max(10, int(sweep_interval))
    if text_batch is not None:
        CHAT_EMBED_TEXT_BATCH = max(1, min(128, int(text_batch)))


async def verify_embedder(url: str, model: Optional[str] = None, timeout: int = 60) -> int:
    """Probe an embedder URL by requesting a vector for a known-good string via the
    OpenAI-compatible ``/v1/embeddings`` endpoint. Returns the vector dimension on
    success; raises on failure (unreachable, non-2xx, or a malformed vector). Does NOT
    mutate the live config — it's a read-only reachability check for the admin 'Verify
    connection' button. Probes with the given model name (falls back to the live one)
    since llama-swap routes — and may cold-start — the upstream by that field."""
    base = (url or "").rstrip("/")
    if not base:
        raise ValueError("embedder URL is empty")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{base}/v1/embeddings",
            json={
                "model": (model or "").strip() or CHAT_EMBED_MODEL,
                "input": _build_prompt(_HEALTHCHECK_TEXT, 0),
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            j = await resp.json()
    try:
        vec = j["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("embedder response was not in the expected shape") from e
    if not (
        isinstance(vec, list)
        and vec
        and all(isinstance(x, (int, float)) and math.isfinite(x) for x in vec)
    ):
        raise ValueError("embedder returned a malformed vector")
    return len(vec)


# Instruction prefix required by qwen3-vl-embedding-8b. It grounds the sequence in
# high-probability retrieval tokens so the tokenizer doesn't start on code/formatting
# markers that break numerical stability (the cause of the all-``None`` vectors we saw
# on bare code-like inputs). Query AND stored documents use the SAME prefix so their
# vectors stay comparable — changing it requires a full index rebuild.
INSTRUCT_PREFIX = "Represent the user's input. "
# Natural-language health probe (NOT a bare code token like "__healthcheck__", which
# is exactly the kind of input that returns a null vector on this model).
_HEALTHCHECK_TEXT = "semantic search health check probe"


def _build_prompt(text: str, n_images: int) -> str:
    """Build the ``prompt_string`` for one item per the model's required schema:

      text only    -> "Represent the user's input. \\nQuery: {text}"
      text + image -> "Represent the user's input. <__media__>\\nQuery: {text}"
      image only   -> "Represent the user's input. <__media__>"

    One ``<__media__>`` marker per image (marker count MUST equal multimodal_data
    length). A naked ``<__media__>`` with no prefix is rejected, hence the prefix is
    always present. Text is placed on a ``\\nQuery:`` line after any media tokens."""
    text = (text or "").strip()
    prompt = INSTRUCT_PREFIX
    if n_images:
        prompt += " ".join(["<__media__>"] * n_images)
    if text:
        prompt += f"\nQuery: {text}"
    return prompt


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
    # Every text goes through the instruction prefix (text-only shape: no
    # multimodal_data). Batches remain one request, one vector per item.
    r = requests.post(
        f"{CHAT_EMBED_URL}/v1/embeddings",
        json={
            "model": CHAT_EMBED_MODEL,
            "input": [_build_prompt(t, 0) for t in texts],
        },
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
        vecs = _post_texts([_HEALTHCHECK_TEXT], timeout)
        return bool(vecs) and is_valid_vector(vecs[0])
    except Exception:
        return False


def _embed_one_resilient(text: str, timeout: int) -> Optional[list[float]]:
    """Embed a single text, progressively truncating if it exceeds the model context.
    Returns None (never raises) when it overflows even truncated — so one bad text
    can't abort the whole backfill batch.

    Context overflow shows up TWO ways from this model and both must trigger the
    shrink-and-retry ladder: (a) an HTTP 400, or (b) — the common case — an HTTP 200
    carrying an all-``None`` vector. Treating only the 400 case (as we used to) left
    long messages permanently marked failed even though a truncated version embeds fine."""
    t = text
    for _ in range(6):
        try:
            vec = _post_texts([t or " "], timeout)[0]
        except requests.exceptions.HTTPError as e:
            if not _is_context_error(e):
                raise  # genuine non-context error
            vec = None  # 400 overflow — fall through to the shrink logic below
        if is_valid_vector(vec):
            return vec
        if len(t) > 200:
            t = t[: max(200, (len(t) * 2) // 3)]  # shrink ~33% and retry
            continue
        return None  # can't shrink further and still returns garbage — give up
    return None


def embed_texts_sync(texts: list[str], timeout: int = 600) -> list[Optional[list[float]]]:
    """Batch-embed plain texts. Resilient to the model's context limit: a batch that
    overflows is split and the offending texts are truncated. Returns vectors aligned
    with ``texts`` (None for any that couldn't be embedded)."""
    if not texts:
        return []
    try:
        vecs = _post_texts(texts, timeout)  # fast path: whole batch fits
    except requests.exceptions.HTTPError as e:
        if not _is_context_error(e):
            raise
        if len(texts) == 1:
            return [_embed_one_resilient(texts[0], timeout)]
        mid = len(texts) // 2  # split and recurse so one long text can't fail the batch
        return embed_texts_sync(texts[:mid], timeout) + embed_texts_sync(texts[mid:], timeout)
    # The batch HTTP call can still return 200 with an all-None vector for individual
    # over-long texts (the model doesn't 400). Keep the good ones, repair only the bad
    # ones per-text (truncating) so one long message doesn't cost the whole batch.
    if all(is_valid_vector(v) for v in vecs):
        return vecs
    return [
        v if is_valid_vector(v) else _embed_one_resilient(t, timeout)
        for t, v in zip(texts, vecs)
    ]


def embed_fused_sync(text: str, images: list[str], timeout: int = 300) -> Optional[list[float]]:
    """Embed one message that has image(s): fuse text + (capped) images -> one vector.
    On context overflow, truncate the text, then drop images, until it fits. Overflow
    arrives as either an HTTP 400 or a 200 with an all-``None`` vector — both trigger
    the same shed-and-retry ladder."""
    imgs = [_prepare_image(b) for b in images[:CHAT_EMBED_MAX_IMAGES]]
    t = (text or "").strip()
    for attempt in range(5):
        try:
            r = requests.post(
                f"{CHAT_EMBED_URL}/v1/embeddings",
                json={
                    "model": CHAT_EMBED_MODEL,
                    # Invariant: _build_prompt emits exactly len(imgs) <__media__>
                    # markers — the endpoint requires marker count == image count.
                    "input": [
                        {
                            "prompt_string": _build_prompt(t, len(imgs)),
                            "multimodal_data": imgs,
                        }
                    ],
                },
                timeout=timeout,
            )
            r.raise_for_status()
            vec = r.json()["data"][0]["embedding"]
        except requests.exceptions.HTTPError as e:
            if not _is_context_error(e):
                raise
            vec = None  # 400 overflow — shed content below and retry
        if is_valid_vector(vec):
            return vec
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
                # Same instruction prefix as the stored documents — the query and the
                # index MUST share the convention or cosine similarity is meaningless.
                json={"model": CHAT_EMBED_MODEL, "input": _build_prompt(text, 0)},
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
    # Lazy import to avoid a circular import (the script imports this module).
    from open_webui.scripts.backfill_chat_embeddings import run_sweep

    was_up: Optional[bool] = None
    while True:
        try:
            # Re-check the flag every pass (NOT once before the loop): it's runtime-
            # toggleable from the admin UI now, so a boot-disabled instance must still
            # come alive when the admin flips it on — and vice versa.
            if not CHAT_SEMANTIC_ENABLED:
                await asyncio.sleep(down_interval)
                continue
            # Read the live interval each pass so an admin change to the sweep cadence
            # applies from the very next cycle (`up_interval` is superseded by the
            # runtime-configurable global; kept in the signature for compatibility).
            interval = CHAT_EMBED_SWEEP_INTERVAL
            healthy = (await aembed_query(_HEALTHCHECK_TEXT)) is not None
            if healthy:
                if was_up is False:
                    log.info("chat embedder recovered — backfilling pending embeddings")
                was_up = True
                # limit=None: catch up ALL pending in one go (cheap in steady state, full
                # backfill after an outage). Runs in a thread (off the event loop).
                n = await asyncio.to_thread(run_sweep, None, 200, lambda *a: None)
                if n:
                    log.info("chat embedding sweep: embedded %d pending message(s)", n)
                await asyncio.sleep(interval)
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
            await asyncio.sleep(CHAT_EMBED_SWEEP_INTERVAL)

