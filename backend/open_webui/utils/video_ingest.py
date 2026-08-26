"""Acquire a video file from a user-supplied URL.

Strategy is always "yt-dlp first, site-specific fallback second". yt-dlp handles
the overwhelming majority of sites (including, in the happy path, both X and
TikTok), so the fallbacks below exist only for the cases where its extractor is
blocked or stale:

* **X / Twitter** — fxtwitter and vxtwitter run their own scrapers and return
  JSON with direct ``video.twimg.com`` MP4 variants (and an HLS playlist). Both
  accept the ``/i/status/<id>`` form, so a username is never required.
* **TikTok** — TikWM returns a watermark-free MP4. Its free tier is limited to
  **1 request/second**, which is enforced here by a process-wide lock.

Short links (``vm.tiktok.com``, ``vt.tiktok.com``, ``tiktok.com/t/``, ``t.co``)
are resolved to a canonical URL *before* anything else: yt-dlp chokes on the
tracking/landing parameters those redirect through, and the redirect target is
also the only place the numeric video id is exposed.

Two hard rules in this module:

1. **Nothing may write to stdout/stderr.** yt-dlp is chatty by default and a
   full pty buffer will freeze the uvicorn event loop, so it is muted and given
   a logger that forwards to ``log``.
2. **Nothing blocking may run on the event loop.** yt-dlp is synchronous, so it
   always runs via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from open_webui.env import SRC_LOG_LEVELS
from open_webui.retrieval.web.utils import validate_url

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


class VideoIngestError(Exception):
    """User-facing ingest failure."""


ProgressFn = Callable[[str, Optional[float], Optional[str]], Any]

DOWNLOAD_TIMEOUT_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 60
REDIRECT_TIMEOUT_SECONDS = 25
MAX_REDIRECTS = 6

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TWITTER_HOSTS = {
    "x.com",
    "twitter.com",
    "www.x.com",
    "www.twitter.com",
    "mobile.x.com",
    "mobile.twitter.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "fixupx.com",
    "fixvx.com",
}
_TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
# Hosts whose links are pure redirectors — they must be resolved before use.
_SHORTLINK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "t.co", "bit.ly"}

_TWITTER_STATUS_RE = re.compile(r"/(?:status|statuses)/(\d+)")
_TIKTOK_VIDEO_RE = re.compile(r"/video/(\d+)")
_TIKTOK_SHORT_PATH_RE = re.compile(r"^/(?:t|v)/")

# TikWM free tier: 1 request/second, process-wide.
_TIKWM_MIN_INTERVAL = 1.2
_tikwm_lock = asyncio.Lock()
_tikwm_last_call = 0.0


@dataclass
class DownloadResult:
    path: Path
    title: Optional[str] = None
    # Which acquisition path actually produced the file, surfaced to the UI so a
    # fallback is visible rather than silent.
    source: str = "yt-dlp"
    fallback_used: bool = False
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    extractor: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_twitter(url: str) -> bool:
    return _host(url) in _TWITTER_HOSTS


def _is_tiktok(url: str) -> bool:
    h = _host(url)
    return h in _TIKTOK_HOSTS or h.endswith(".tiktok.com")


def validate_public_url(url: str) -> None:
    """Reject non-http(s), credentialed, and private/loopback targets (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise VideoIngestError("Video URL must use http or https.")
    if not parsed.hostname:
        raise VideoIngestError("Video URL is missing a host.")
    if parsed.username or parsed.password:
        raise VideoIngestError("Video URL must not include credentials.")
    try:
        validate_url(url)
    except ValueError as e:
        raise VideoIngestError("That video URL is not allowed.") from e


def canonicalize_url(url: str) -> str:
    """Strip tracking/query noise that makes yt-dlp reject an otherwise fine URL.

    TikTok share links carry ``?_r=1&_d=...&utm_*`` and friends; yt-dlp treats
    several of those as an unsupported landing page. The bare
    ``/@user/video/<id>`` (or ``/@/video/<id>``) form always works.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in _TIKTOK_HOSTS or host.endswith(".tiktok.com"):
        m = _TIKTOK_VIDEO_RE.search(parsed.path or "")
        if m:
            # Username is not required by the extractor; "@" alone resolves.
            return f"https://www.tiktok.com/@/video/{m.group(1)}"

    if host in _TWITTER_HOSTS:
        m = _TWITTER_STATUS_RE.search(parsed.path or "")
        if m:
            return f"https://x.com/i/status/{m.group(1)}"

    # Generic: drop the query/fragment only when it is obviously tracking.
    if parsed.query and any(
        k in parsed.query for k in ("utm_", "_r=", "_d=", "share_", "is_from_webapp")
    ):
        return urlunparse(parsed._replace(query="", fragment=""))
    return url


async def resolve_redirects(url: str, *, force: bool = False) -> str:
    """Follow HTTP redirects for short links to recover the canonical URL.

    The final page frequently answers 403 (bot protection) — that is fine and
    expected. Everything needed is in the ``Location`` chain, so a 4xx at the end
    is not treated as a failure.
    """
    host = _host(url)
    is_short = host in _SHORTLINK_HOSTS or (
        _is_tiktok(url) and _TIKTOK_SHORT_PATH_RE.match(urlparse(url).path or "")
    )
    if not (force or is_short):
        return url

    current = url
    timeout = aiohttp.ClientTimeout(total=REDIRECT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for _ in range(MAX_REDIRECTS):
                try:
                    async with session.get(
                        current,
                        headers={"User-Agent": BROWSER_UA},
                        allow_redirects=False,
                    ) as resp:
                        if resp.status in {301, 302, 303, 307, 308}:
                            loc = resp.headers.get("Location")
                            if not loc:
                                break
                            current = urljoin(current, loc)
                            continue
                        break
                except aiohttp.ClientError:
                    break
    except Exception:
        log.debug("resolve_redirects failed for %s", url, exc_info=True)

    return current


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------


class _YtdlpLogger:
    """Routes yt-dlp chatter to the app logger instead of stdout/stderr."""

    def debug(self, msg):
        if not str(msg).startswith("[download]"):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp error: %s", msg)


def _format_selector(max_height: Optional[int]) -> str:
    if not max_height:
        return "bestvideo+bestaudio/best"
    # `<=?` degrades to "best available" rather than failing when every format
    # is taller than the cap (common for 1080p-only sources).
    return (
        f"bestvideo[height<=?{max_height}]+bestaudio/"
        f"best[height<=?{max_height}]/best"
    )


def _ytdlp_download_sync(
    url: str,
    dest_dir: Path,
    *,
    max_height: Optional[int],
    max_filesize: Optional[int],
    progress_cb: Optional[Callable[[float, Optional[int], Optional[int]], None]],
) -> dict:
    import yt_dlp

    def hook(d: dict) -> None:
        if not progress_cb:
            return
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100.0) if total else None
                progress_cb(pct if pct is not None else -1.0, done, total)
            elif d.get("status") == "finished":
                progress_cb(100.0, d.get("downloaded_bytes"), d.get("total_bytes"))
        except Exception:
            pass

    opts = {
        "format": _format_selector(max_height),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "logger": _YtdlpLogger(),
        "progress_hooks": [hook],
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "restrictfilenames": True,
        "nopart": False,
        # Never let an extractor spawn an interactive prompt on the server.
        "noninteractive": True,
    }
    if max_filesize:
        opts["max_filesize"] = max_filesize

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise VideoIngestError("No video could be extracted from that URL.")
        # Playlists/entries: take the first real entry.
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        return info


def _find_downloaded(dest_dir: Path) -> Optional[Path]:
    candidates = [
        p
        for p in dest_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        return None
    # The merged output is the largest artifact left behind.
    return max(candidates, key=lambda p: p.stat().st_size)


async def _try_ytdlp(
    url: str,
    dest_dir: Path,
    *,
    max_height: Optional[int],
    max_filesize: Optional[int],
    on_progress: Optional[ProgressFn],
) -> DownloadResult:
    loop = asyncio.get_running_loop()

    def progress_cb(pct: float, done: Optional[int], total: Optional[int]) -> None:
        if not on_progress:
            return
        detail = None
        if done:
            detail = (
                f"{done / 1048576:.1f} MB of {total / 1048576:.1f} MB"
                if total
                else f"{done / 1048576:.1f} MB"
            )
        # Hop back onto the loop: the hook runs on the worker thread.
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                _maybe_await(
                    on_progress, "downloading", pct if pct >= 0 else None, detail
                )
            )
        )

    info = await asyncio.wait_for(
        asyncio.to_thread(
            _ytdlp_download_sync,
            url,
            dest_dir,
            max_height=max_height,
            max_filesize=max_filesize,
            progress_cb=progress_cb,
        ),
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )

    path = _find_downloaded(dest_dir)
    if not path or path.stat().st_size == 0:
        # Seen with `--download-sections` style range requests on HLS: yt-dlp
        # reports success but leaves a 0-byte container behind.
        raise VideoIngestError("Downloader produced an empty file.")

    return DownloadResult(
        path=path,
        title=info.get("title"),
        source="yt-dlp",
        duration=info.get("duration"),
        width=info.get("width"),
        height=info.get("height"),
        extractor=info.get("extractor_key") or info.get("extractor"),
    )


async def _maybe_await(fn: ProgressFn, *args) -> None:
    try:
        res = fn(*args)
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        log.debug("progress callback failed", exc_info=True)


# --------------------------------------------------------------------------
# Direct HTTP / HLS download
# --------------------------------------------------------------------------


async def _download_direct(
    url: str,
    dest: Path,
    *,
    max_filesize: Optional[int],
    on_progress: Optional[ProgressFn],
    referer: Optional[str] = None,
) -> None:
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer

    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                raise VideoIngestError(f"Media URL returned HTTP {resp.status}.")
            total = None
            try:
                total = int(resp.headers.get("Content-Length") or 0) or None
            except ValueError:
                pass
            if max_filesize and total and total > max_filesize:
                raise VideoIngestError("That video is larger than the allowed limit.")

            done = 0
            last_emit = 0.0
            with open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    fh.write(chunk)
                    done += len(chunk)
                    if max_filesize and done > max_filesize:
                        raise VideoIngestError(
                            "That video is larger than the allowed limit."
                        )
                    now = time.monotonic()
                    if on_progress and now - last_emit > 0.4:
                        last_emit = now
                        pct = (done / total * 100.0) if total else None
                        detail = (
                            f"{done / 1048576:.1f} MB of {total / 1048576:.1f} MB"
                            if total
                            else f"{done / 1048576:.1f} MB"
                        )
                        await _maybe_await(on_progress, "downloading", pct, detail)

    if dest.stat().st_size == 0:
        raise VideoIngestError("Downloaded file was empty.")


async def _download_hls(
    url: str, dest: Path, *, on_progress: Optional[ProgressFn]
) -> None:
    """Remux an HLS playlist to MP4. ffmpeg reads .m3u8 natively."""
    if on_progress:
        await _maybe_await(on_progress, "downloading", None, "Fetching stream…")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-user_agent",
        BROWSER_UA,
        "-i",
        url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS
    )
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        msg = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise VideoIngestError(f"Could not download the stream. {msg}".strip())


# --------------------------------------------------------------------------
# X / Twitter fallback
# --------------------------------------------------------------------------


def _pick_twitter_variant(
    variants: list[dict], max_height: Optional[int]
) -> Optional[dict]:
    """Prefer the highest-bitrate MP4 that still fits under the height cap.

    fxtwitter exposes per-resolution renditions, so picking here avoids pulling
    a 10 Mbps 1080p master just to downscale it to 720p locally.
    """
    mp4s = [
        v
        for v in variants
        if (v.get("content_type") == "video/mp4" or v.get("container") == "mp4")
        and v.get("url")
    ]
    if not mp4s:
        return None

    def height_of(v: dict) -> Optional[int]:
        m = re.search(r"/(\d+)x(\d+)/", v.get("url") or "")
        return int(m.group(2)) if m else None

    if max_height:
        fitting = [v for v in mp4s if (height_of(v) or 0) <= max_height]
        if fitting:
            return max(fitting, key=lambda v: v.get("bitrate") or 0)
    return max(mp4s, key=lambda v: v.get("bitrate") or 0)


async def _fetch_json(
    url: str, *, timeout: int = HTTP_TIMEOUT_SECONDS
) -> Optional[dict]:
    try:
        ct = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=ct, trust_env=True) as session:
            async with session.get(url, headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    return None
                return await resp.json(content_type=None)
    except Exception:
        log.debug("fetch_json failed for %s", url, exc_info=True)
        return None


async def _twitter_fallback(
    url: str,
    dest_dir: Path,
    *,
    max_height: Optional[int],
    max_filesize: Optional[int],
    on_progress: Optional[ProgressFn],
) -> DownloadResult:
    m = _TWITTER_STATUS_RE.search(urlparse(url).path or "")
    if not m:
        raise VideoIngestError("Could not find a post ID in that X/Twitter link.")
    status_id = m.group(1)

    if on_progress:
        await _maybe_await(
            on_progress, "fallback", None, "yt-dlp failed — trying fxtwitter…"
        )

    # --- fxtwitter: richest payload (per-resolution variants + HLS) ---
    data = await _fetch_json(f"https://api.fxtwitter.com/i/status/{status_id}")
    tweet = (data or {}).get("tweet") or {}
    videos = ((tweet.get("media") or {}).get("videos")) or []
    title = tweet.get("text") or None

    if videos:
        video = videos[0]
        variants = video.get("variants") or video.get("formats") or []
        chosen = _pick_twitter_variant(variants, max_height)
        target = dest_dir / "source.mp4"

        if chosen:
            await _download_direct(
                chosen["url"],
                target,
                max_filesize=max_filesize,
                on_progress=on_progress,
                referer="https://x.com/",
            )
            return DownloadResult(
                path=target,
                title=title,
                source="fxtwitter",
                fallback_used=True,
                duration=video.get("duration"),
                width=video.get("width"),
                height=video.get("height"),
            )

        # No progressive MP4 — fall back to the HLS playlist via ffmpeg.
        hls = next(
            (
                v.get("url")
                for v in variants
                if (v.get("container") == "m3u8")
                or "mpegURL" in (v.get("content_type") or "")
            ),
            None,
        ) or video.get("url")
        if hls:
            await _download_hls(hls, target, on_progress=on_progress)
            return DownloadResult(
                path=target,
                title=title,
                source="fxtwitter (HLS)",
                fallback_used=True,
                duration=video.get("duration"),
            )

    # --- vxtwitter: second opinion, different scraper ---
    if on_progress:
        await _maybe_await(on_progress, "fallback", None, "Trying vxtwitter…")

    data = await _fetch_json(f"https://api.vxtwitter.com/i/status/{status_id}")
    if data:
        media = [
            m
            for m in (data.get("media_extended") or [])
            if m.get("type") in {"video", "gif"} and m.get("url")
        ]
        urls = [m["url"] for m in media] or list(data.get("mediaURLs") or [])
        if urls:
            target = dest_dir / "source.mp4"
            await _download_direct(
                urls[0],
                target,
                max_filesize=max_filesize,
                on_progress=on_progress,
                referer="https://x.com/",
            )
            first = media[0] if media else {}
            return DownloadResult(
                path=target,
                title=data.get("text") or title,
                source="vxtwitter",
                fallback_used=True,
                duration=(first.get("duration_millis") or 0) / 1000.0 or None,
                width=(first.get("size") or {}).get("width"),
                height=(first.get("size") or {}).get("height"),
            )

    raise VideoIngestError(
        "That post doesn't appear to contain a video, or it is private/age-restricted."
    )


# --------------------------------------------------------------------------
# TikTok fallback
# --------------------------------------------------------------------------


async def _tikwm_request(url: str) -> Optional[dict]:
    """Query TikWM, honouring its 1 req/sec free-tier limit process-wide."""
    global _tikwm_last_call

    async with _tikwm_lock:
        wait = _TIKWM_MIN_INTERVAL - (time.monotonic() - _tikwm_last_call)
        if wait > 0:
            await asyncio.sleep(wait)

        payload = None
        for attempt in range(3):
            try:
                ct = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
                async with aiohttp.ClientSession(timeout=ct, trust_env=True) as session:
                    async with session.post(
                        "https://www.tikwm.com/api/",
                        headers={"User-Agent": BROWSER_UA},
                        data={"url": url, "hd": "1"},
                    ) as resp:
                        payload = await resp.json(content_type=None)
            except Exception:
                log.debug("tikwm request failed", exc_info=True)
                payload = None
            finally:
                _tikwm_last_call = time.monotonic()

            # TikWM signals success with code 0 (not 200).
            if payload and payload.get("code") == 0:
                return payload
            msg = str((payload or {}).get("msg") or "")
            if "Limit" in msg or "limit" in msg:
                await asyncio.sleep(_TIKWM_MIN_INTERVAL * (attempt + 2))
                continue
            break
        return payload if (payload and payload.get("code") == 0) else None


async def _tiktok_fallback(
    url: str,
    dest_dir: Path,
    *,
    max_filesize: Optional[int],
    on_progress: Optional[ProgressFn],
) -> DownloadResult:
    if on_progress:
        await _maybe_await(
            on_progress, "fallback", None, "yt-dlp failed — trying TikWM…"
        )

    payload = await _tikwm_request(url)
    data = (payload or {}).get("data") or {}
    # `hdplay` is the HD rendition, `play` the standard watermark-free MP4.
    media_url = data.get("hdplay") or data.get("play") or data.get("wmplay")
    if not media_url:
        raise VideoIngestError(
            "Could not retrieve this TikTok. It may be private, deleted, or region-locked."
        )
    if media_url.startswith("/"):
        media_url = f"https://www.tikwm.com{media_url}"

    target = dest_dir / "source.mp4"
    await _download_direct(
        media_url,
        target,
        max_filesize=max_filesize,
        on_progress=on_progress,
        referer="https://www.tiktok.com/",
    )
    return DownloadResult(
        path=target,
        title=data.get("title") or None,
        source="tikwm",
        fallback_used=True,
        duration=data.get("duration"),
    )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


async def download_video(
    raw_url: str,
    dest_dir: Path,
    *,
    max_height: Optional[int] = 720,
    max_filesize: Optional[int] = None,
    on_progress: Optional[ProgressFn] = None,
) -> DownloadResult:
    """Fetch ``raw_url`` into ``dest_dir``, trying yt-dlp then site fallbacks."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = (raw_url or "").strip()
    if not url:
        raise VideoIngestError("No video URL was provided.")
    if "://" not in url:
        url = f"https://{url}"
    validate_public_url(url)

    if on_progress:
        await _maybe_await(on_progress, "resolving", None, "Resolving link…")

    resolved = await resolve_redirects(url)
    if resolved != url:
        # A short link can redirect anywhere; re-check the destination.
        validate_public_url(resolved)
    target_url = canonicalize_url(resolved)

    ytdlp_error: Optional[Exception] = None
    try:
        result = await _try_ytdlp(
            target_url,
            dest_dir,
            max_height=max_height,
            max_filesize=max_filesize,
            on_progress=on_progress,
        )
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ytdlp_error = e
        log.info("yt-dlp failed for %s: %s", target_url, e)

    # Clear partial artifacts so _find_downloaded can't pick up a stale stub.
    for leftover in dest_dir.iterdir():
        try:
            leftover.unlink() if leftover.is_file() else shutil.rmtree(leftover)
        except OSError:
            pass

    try:
        if _is_twitter(target_url):
            return await _twitter_fallback(
                target_url,
                dest_dir,
                max_height=max_height,
                max_filesize=max_filesize,
                on_progress=on_progress,
            )
        if _is_tiktok(target_url):
            return await _tiktok_fallback(
                target_url,
                dest_dir,
                max_filesize=max_filesize,
                on_progress=on_progress,
            )
    except VideoIngestError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise VideoIngestError(f"Could not download that video: {e}") from e

    detail = str(ytdlp_error or "").strip()
    # yt-dlp prefixes its user-facing messages; keep them, drop the noise.
    detail = re.sub(r"^ERROR:\s*", "", detail).split("\n")[0][:300]
    raise VideoIngestError(
        f"Could not download that video. {detail}"
        if detail
        else "Could not download that video."
    )
