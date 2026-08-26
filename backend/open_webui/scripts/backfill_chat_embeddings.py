"""Backfill / keep-fresh sweep for chat message embeddings.

Walks ``chat_message`` (user + assistant turns, non-subagent chats), extracts the
embeddable content for each — user/assistant prose with ``<details>`` reasoning/tool
scaffolding stripped, plus any attached images from ``meta.files`` — embeds it with the
VL model (one vector per message; image messages fuse text+images), and upserts into
``chat_message_embedding``.

Resumable + idempotent: only processes messages whose embedding is missing or whose
source content changed (``content_hash`` = md5 of content + files json). Empty turns
(pure tool/scaffolding with no prose and no image) are filtered out in SQL so they're
never reprocessed.

Run as a one-shot backfill::  python -m open_webui.scripts.backfill_chat_embeddings
The same ``run_sweep`` is called periodically by the keep-fresh background task.
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlparse, unquote

import psycopg2
import psycopg2.extras

from open_webui.utils import chat_embedder as ce

_DETAILS_RE = re.compile(r"<details.*?</details>", re.DOTALL)
_WS_RE = re.compile(r"\s+")

# Cap text fed to the embedder (model context is 4096 tokens; one vector can't represent a 7MB turn).
MAX_CHARS = int(os.environ.get("CHAT_EMBED_MAX_CHARS", "6000"))
# Min substantive content length to embed. Contentless fragments ("whatever", "which one")
# form a degenerate centroid that sits closer to typo/OOD queries than any real topic and
# pollutes semantic search. 14 strips the tight junk (all <=13 chars) while keeping legitimate
# short USER question-anchors (15+ chars). Mirrors _SEARCH_SEM_MIN_MSG_LEN in models/chats.py
# (the query-side gate). Messages with images are always embedded.
MIN_TEXT_CHARS = int(os.environ.get("CHAT_EMBED_MIN_CHARS", "14"))
# How many text-only messages to embed per HTTP batch: lives in chat_embedder
# (ce.CHAT_EMBED_TEXT_BATCH) so the admin UI can change it at runtime — read at
# use-site, not captured here, so a change applies to the very next sweep.

# Source-content hash used to detect edits — must match the SQL expression below.
_SRC_HASH_SQL = "md5(coalesce(cm.content,'') || coalesce((cm.meta->'files')::text,''))"

# Non-empty filter (mirrors the Python extraction): enough prose after stripping details, OR has an image.
_NONEMPTY_SQL = (
    "(length(trim(regexp_replace("
    "  CASE WHEN cm.role='assistant' THEN regexp_replace(cm.content,'<details.*?</details>','','g') ELSE cm.content END,"
    f"  '\\s+',' ','g'))) >= {MIN_TEXT_CHARS}"
    " OR EXISTS (SELECT 1 FROM jsonb_array_elements(coalesce(cm.meta->'files','[]'::jsonb)) f WHERE f->>'type'='image'))"
)


def _pg_dsn() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql://tennisbowling:tennispass@192.168.10.2:5432/openllm")
    # psycopg2 wants a plain postgresql:// DSN (strip +asyncpg / +psycopg etc.)
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    p = urlparse(url)
    return (
        f"host={p.hostname} port={p.port or 5432} dbname={(p.path or '/').lstrip('/')} "
        f"user={unquote(p.username or '')} password={unquote(p.password or '')}"
    )


def _extract_content_text(content) -> str:
    """Plain text from a message content field (str or multimodal list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _content_image_urls(content) -> list[str]:
    out = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                iu = item.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str):
                    out.append(url)
    return out


def extract_images(files) -> list[str]:
    if not isinstance(files, list):
        return []
    return [
        f["url"]
        for f in files
        if isinstance(f, dict) and f.get("type") == "image" and isinstance(f.get("url"), str)
    ]


# Message image "urls" come in two flavors: inline data-URLs / raw base64 (older or
# pasted images — usable as-is), and internal file references like
# ``/api/v1/files/<uuid>/content`` (uploaded images). The embedder needs actual image
# bytes, so a file ref MUST be resolved to base64 from the ``file`` table before use —
# otherwise the literal path string is sent as "image data" and the server 500s. This
# was the cause of the bulk of image-embedding failures.
_FILE_REF_RE = re.compile(r"/api/v1/files/([0-9a-fA-F-]{36})(?:/content)?")


def _resolve_image_b64(cur, url):
    """Return raw base64 image bytes for one image ``url`` (or None if unresolvable).
    Data-URLs / raw base64 pass through unchanged; ``/api/v1/files/<id>`` refs are looked
    up in the ``file`` table — the ``data`` (BYTEA) column first, then the on-disk path."""
    if not isinstance(url, str) or not url:
        return None
    m = _FILE_REF_RE.search(url)
    if not m:
        return url  # already an inline data-URL / raw base64 — _prepare_image handles it
    cur.execute("SELECT data, path FROM file WHERE id = %s", (m.group(1),))
    row = cur.fetchone()
    if not row:
        return None  # file was deleted — nothing to embed
    data, path = row
    if data is not None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(data)).decode()
        if isinstance(data, str):
            return data  # already base64 / data-URL
    if path:
        try:
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except OSError:
            return None
    return None


def extract(role: str, content, content_is_json, files):
    """Return (embeddable_text, image_urls) for one message. Handles content_is_json=1
    messages (multimodal JSON content) by parsing out the text/image parts instead of
    embedding the raw JSON blob. Assistant prose has <details> scaffolding stripped."""
    images = extract_images(files)
    text_src = content or ""
    if content_is_json and isinstance(content, str):
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None
        if parsed is not None:
            text_src = _extract_content_text(parsed)
            images = images + _content_image_urls(parsed)
        else:
            text_src = ""  # unparseable — don't embed the raw json blob
    text = text_src if isinstance(text_src, str) else ""
    if role == "assistant":
        text = _DETAILS_RE.sub("", text)
    text = text.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return text, images


def _fetch_batch(cur, limit: int, last_chat: str, last_msg: str) -> list:
    # Keyset pagination by (chat_id, message_id) guarantees forward progress: rows that
    # are fetched but can't be embedded (permanent failures, malformed content) are
    # walked PAST rather than re-fetched forever (a plain WHERE-shrinks-the-set loop
    # spins infinitely once only un-embeddable rows remain).
    cur.execute(
        f"""
        SELECT cm.chat_id, cm.message_id, c.user_id, cm.role, cm.content, cm.content_is_json,
               cm.meta->'files' AS files, {_SRC_HASH_SQL} AS src_hash
        FROM chat_message cm
        JOIN chat c ON c.id = cm.chat_id
        LEFT JOIN chat_message_embedding e
               ON e.chat_id = cm.chat_id AND e.message_id = cm.message_id
        WHERE c.subagent_of IS NULL
          AND cm.role IN ('user','assistant')
          AND {_NONEMPTY_SQL}
          AND (e.message_id IS NULL OR e.content_hash <> {_SRC_HASH_SQL})
          AND (cm.chat_id, cm.message_id) > (%s, %s)
        ORDER BY cm.chat_id, cm.message_id
        LIMIT %s
        """,
        (last_chat, last_msg, limit),
    )
    return cur.fetchall()


_UPSERT = """
INSERT INTO chat_message_embedding (chat_id, message_id, user_id, content_hash, embedding, updated_at, msg_len)
VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
ON CONFLICT (chat_id, message_id) DO UPDATE SET
    user_id = EXCLUDED.user_id, content_hash = EXCLUDED.content_hash,
    embedding = EXCLUDED.embedding, updated_at = EXCLUDED.updated_at, msg_len = EXCLUDED.msg_len
"""


def run_sweep(limit: int | None = None, batch: int = 200, log=print) -> int:
    """Embed all (or up to ``limit`` scanned) stale/missing messages. Keyset-paginated
    so it always terminates. Returns the count successfully embedded.

    Skips entirely (returns 0) when the embedder is down, so an outage neither wastes
    the scan nor falsely marks good messages — work resumes automatically once it recovers."""
    if not ce.embedder_healthy_sync():
        log("  embedder not healthy — skipping sweep (will retry)")
        return 0
    conn = psycopg2.connect(_pg_dsn())
    conn.autocommit = False
    total = 0          # successfully embedded (upserted)
    fetched = 0        # rows scanned (drives the keyset + limit, ensures termination)
    last_chat, last_msg = "", ""
    try:
        while True:
            take = batch if limit is None else min(batch, limit - fetched)
            if take <= 0:
                break
            with conn.cursor() as cur:
                rows = _fetch_batch(cur, take, last_chat, last_msg)
            if not rows:
                break
            last_chat, last_msg = rows[-1][0], rows[-1][1]  # advance keyset → forward progress
            fetched += len(rows)

            text_jobs, img_jobs = [], []  # (meta, text) ; (meta, text, images)
            with conn.cursor() as rcur:
                for chat_id, message_id, user_id, role, content, content_is_json, files, src_hash in rows:
                    text, images = extract(role, content, content_is_json, files)
                    # Resolve file-ref image urls to actual base64 bytes (drop any that
                    # can't be resolved, e.g. a since-deleted file). Done BEFORE msg_len
                    # so a message whose images all vanished reclassifies as text-only.
                    images = [b for b in (_resolve_image_b64(rcur, u) for u in images) if b]
                    # msg_len gates the semantic search arm (skip contentless fragments). Image
                    # messages are always eligible -> sentinel high value so the filter never drops them.
                    msg_len = 10000 if images else len(text)
                    meta = (chat_id, message_id, user_id, src_hash, msg_len)
                    if images:
                        img_jobs.append((meta, text, images))
                    elif len(text) >= MIN_TEXT_CHARS:
                        text_jobs.append((meta, text))
                    # else: nothing embeddable — keyset has already moved past it

            now = int(time.time())
            results = []  # (meta, vec_or_None)

            # Text-only: grouped into sequential /v1/embeddings requests (one vector
            # per item — texts are never fused). Batch size is admin-configurable.
            text_batch = max(1, int(ce.CHAT_EMBED_TEXT_BATCH))
            for i in range(0, len(text_jobs), text_batch):
                chunk = text_jobs[i : i + text_batch]
                vecs = ce.embed_texts_sync([t for _, t in chunk])
                for (meta, _), vec in zip(chunk, vecs):
                    results.append((meta, vec))

            # Image messages: fuse text+images one at a time, with a text-only fallback.
            # A single "poison" image can wedge the VL embedder into emitting all-null for
            # everything after it (text included) until the server restarts. Detect it: if a
            # fused embed fails AND a text probe then shows the embedder unhealthy, THIS image
            # just wedged it — record the culprit (force-null-marked below so it's never
            # retried and can't re-wedge a later sweep), leave the remaining images PENDING,
            # and stop. The self-healing loop backs off until the embedder recovers.
            poison_meta = None
            for idx, (meta, text, images) in enumerate(img_jobs):
                vec = None
                try:
                    vec = ce.embed_fused_sync(text, images)
                except Exception as e:
                    log(f"  ! fused embed failed for {meta[0]}/{meta[1]}: {e}")
                if not ce.is_valid_vector(vec) and len(text) >= MIN_TEXT_CHARS:
                    try:
                        vec = ce.embed_texts_sync([text])[0]
                    except Exception:
                        vec = None
                results.append((meta, vec))
                # A fused failure on a previously-healthy embedder that now fails a text
                # probe means this image wedged it. Stop; don't feed it more (would deepen
                # the wedge and falsely null-mark innocent images queued behind it).
                if not ce.is_valid_vector(vec) and not ce.embedder_healthy_sync():
                    poison_meta = meta
                    log(
                        f"  ! embedder wedged on image {meta[0]}/{meta[1]} — marking culprit, "
                        f"leaving {len(img_jobs) - idx - 1} image msg(s) pending; stopping sweep"
                    )
                    break

            # A failure is permanent (bad content -> mark) only if the embedder is
            # healthy; otherwise it's a transient outage (-> skip + retry). Cheap when
            # the batch already produced a valid vector; otherwise probe directly so an
            # all-bad-content batch (e.g. all bad images) still gets marked, not looped.
            # The wedge culprit (poison_meta) is ALWAYS null-marked even on an unhealthy
            # batch — it provably broke a healthy embedder, so retrying re-wedges the next.
            batch_healthy = any(ce.is_valid_vector(v) for _, v in results) or ce.embedder_healthy_sync()
            upserts = []
            for meta, vec in results:
                cid, mid, uid, sh, mlen = meta
                if ce.is_valid_vector(vec):
                    upserts.append((cid, mid, uid, sh, ce.to_pgvector_literal(vec), now, mlen))
                elif batch_healthy or meta == poison_meta:
                    upserts.append((cid, mid, uid, sh, None, now, mlen))  # permanent failure marker
                # else: embedder down mid-sweep — leave unrowed, retry next sweep

            if upserts:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, _UPSERT, upserts, page_size=100)
                conn.commit()
            total += len(upserts)
            log(f"  embedded {total} / {fetched} scanned")
            if poison_meta is not None:
                break  # embedder wedged — stop; the rest stays pending for a healthy sweep
        return total
    finally:
        conn.close()


if __name__ == "__main__":
    t0 = time.time()
    print(f"[backfill] starting; embedder={ce.CHAT_EMBED_URL} model={ce.CHAT_EMBED_MODEL} dim={ce.CHAT_EMBED_DIM}")
    n = run_sweep(limit=None, batch=200)
    print(f"[backfill] done: {n} embeddings in {time.time()-t0:.0f}s")
