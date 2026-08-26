"""ffprobe/ffmpeg pipeline that turns a downloaded video into a model-ready clip.

The source is always downloaded in full and trimmed here rather than range-fetched
at download time: yt-dlp's ``--download-sections`` silently produces a 0-byte
container on fragmented (HLS/DASH) sources, which is exactly what X and TikTok
serve. Trimming locally costs a little bandwidth and is always correct.

Sizing knobs map onto what the model actually consumes. Gemini samples video at
roughly one frame per second, so re-encoding to 1 fps keeps every frame the model
would have looked at while shrinking the payload by an order of magnitude — the
duration is unchanged, so the sampling rate the model sees is unchanged too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from open_webui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


class VideoProcessingError(Exception):
    """User-facing processing failure."""


ProgressFn = Callable[[str, Optional[float], Optional[str]], Any]

PROBE_TIMEOUT_SECONDS = 60
PROCESS_TIMEOUT_SECONDS = 30 * 60

# Measured against google/gemini-3.5-flash-lite via OpenRouter: a 20-frame,
# 20-second clip reported prompt_tokens_details {video_tokens: 1320,
# audio_tokens: 500}. Used only for the composer's pre-send estimate.
TOKENS_PER_FRAME = 66
TOKENS_PER_AUDIO_SECOND = 25

QUALITY_PRESETS: dict[str, Optional[int]] = {
    "240p": 240,
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
    "source": None,  # keep native resolution
}

DEFAULT_QUALITY = "720p"
DEFAULT_FPS = 1.0


def quality_to_height(quality: Optional[str]) -> Optional[int]:
    if not quality:
        return QUALITY_PRESETS[DEFAULT_QUALITY]
    return QUALITY_PRESETS.get(str(quality).lower(), QUALITY_PRESETS[DEFAULT_QUALITY])


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size: int = 0
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None


@dataclass
class ProcessResult:
    path: Path
    info: VideoInfo
    frames: int
    estimated_tokens: int


def _parse_fraction(value: Any) -> float:
    """avg_frame_rate arrives as "30000/1001"; 0/0 means "unknown"."""
    try:
        text = str(value or "").strip()
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise VideoProcessingError(
            f"{name} is not installed on the server, so videos cannot be processed."
        )


async def probe_video(path: Path) -> VideoInfo:
    _require_binary("ffprobe")

    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoProcessingError("Timed out while inspecting the video.")

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise VideoProcessingError(f"Could not read that video file. {detail}".strip())

    try:
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise VideoProcessingError("Could not read that video file.")

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream is None:
        raise VideoProcessingError("That file does not contain a video track.")

    duration = 0.0
    for candidate in (fmt.get("duration"), video_stream.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = _parse_fraction(video_stream.get("avg_frame_rate")) or _parse_fraction(
        video_stream.get("r_frame_rate")
    )

    size = 0
    try:
        size = int(fmt.get("size") or 0) or path.stat().st_size
    except (TypeError, ValueError, OSError):
        size = path.stat().st_size if path.exists() else 0

    return VideoInfo(
        duration=max(duration, 0.0),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=fps,
        has_audio=audio_stream is not None,
        size=size,
        video_codec=video_stream.get("codec_name"),
        audio_codec=(audio_stream or {}).get("codec_name"),
    )


def resolve_trim(
    duration: float, start: Optional[float], end: Optional[float]
) -> tuple[float, float]:
    """Clamp a requested [start, end] window to the real duration.

    An out-of-range or inverted window is corrected rather than rejected: the
    composer already shows the true duration, and failing a job because someone
    typed an end time one second past the end would be needlessly hostile.
    """
    total = max(float(duration or 0.0), 0.0)
    s = max(float(start or 0.0), 0.0)
    e = float(end) if end is not None else total
    if total > 0:
        s = min(s, total)
        e = min(max(e, 0.0), total) if e > 0 else total
    if e <= s:
        e = total if total > s else s
    return s, e


def estimate_tokens(
    *, duration: float, fps: float, keep_audio: bool, has_audio: bool
) -> tuple[int, int]:
    """Return (frames, estimated prompt tokens) for a processed clip."""
    span = max(float(duration or 0.0), 0.0)
    rate = max(float(fps or DEFAULT_FPS), 0.01)
    frames = max(int(round(span * rate)), 1 if span > 0 else 0)
    tokens = frames * TOKENS_PER_FRAME
    if keep_audio and has_audio:
        tokens += int(span * TOKENS_PER_AUDIO_SECOND)
    return frames, tokens


def _build_command(
    src: Path,
    dst: Path,
    *,
    fps: float,
    max_height: Optional[int],
    start: float,
    end: float,
    keep_audio: bool,
    crf: int,
) -> list[str]:
    filters = [f"fps={fps:g}"]
    if max_height:
        # -2 keeps the aspect ratio and guarantees an even width (h264 requires
        # even dimensions). `min(...,ih)` prevents upscaling a smaller source.
        filters.append(f"scale=-2:'min({max_height},ih)':flags=bicubic")

    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    # Input-side seek is fast (keyframe index) and accurate enough here; ffmpeg
    # decodes from the preceding keyframe and drops frames before -ss.
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    duration = max(end - start, 0.0)
    if duration > 0:
        cmd += ["-t", f"{duration:.3f}"]

    cmd += [
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        # Low fps yields very long GOPs by default; force regular keyframes so
        # the file stays seekable for the in-chat preview player.
        "-g",
        "30",
    ]

    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000"]
    else:
        cmd += ["-an"]

    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


_OUT_TIME_RE = re.compile(r"out_time_ms=(\d+)")


async def process_video(
    src: Path,
    dst: Path,
    *,
    fps: float = DEFAULT_FPS,
    quality: str = DEFAULT_QUALITY,
    start: Optional[float] = None,
    end: Optional[float] = None,
    keep_audio: bool = True,
    crf: int = 28,
    on_progress: Optional[ProgressFn] = None,
    source_info: Optional[VideoInfo] = None,
) -> ProcessResult:
    """Trim/resample/rescale ``src`` into ``dst`` and report progress."""
    _require_binary("ffmpeg")

    info = source_info or await probe_video(src)
    start_s, end_s = resolve_trim(info.duration, start, end)
    span = max(end_s - start_s, 0.0)
    max_height = quality_to_height(quality)
    want_audio = bool(keep_audio and info.has_audio)

    cmd = _build_command(
        src,
        dst,
        fps=fps,
        max_height=max_height,
        start=start_s,
        end=end_s,
        keep_audio=want_audio,
        crf=crf,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump() -> None:
        """Translate ffmpeg's -progress stream into percent-of-clip."""
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if not on_progress or span <= 0:
                continue
            m = _OUT_TIME_RE.search(line.decode("utf-8", "replace"))
            if not m:
                continue
            done = int(m.group(1)) / 1_000_000.0
            pct = max(0.0, min(done / span * 100.0, 100.0))
            try:
                res = on_progress("processing", pct, f"{done:.0f}s of {span:.0f}s")
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.debug("processing progress callback failed", exc_info=True)

    async def drain_stderr() -> bytes:
        assert proc.stderr is not None
        return await proc.stderr.read()

    # `communicate()` cannot be used here: it starts its own stdout reader and
    # would race the progress pump on the same stream ("read() called while
    # another coroutine is already waiting for incoming data"). Read both pipes
    # explicitly instead, and always drain stderr so a verbose failure cannot
    # fill the pipe buffer and deadlock the child.
    pump_task = asyncio.create_task(pump())
    stderr_task = asyncio.create_task(drain_stderr())
    stderr = b""
    try:
        await asyncio.wait_for(proc.wait(), timeout=PROCESS_TIMEOUT_SECONDS)
        stderr = await stderr_task
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoProcessingError("Timed out while processing the video.")
    finally:
        for task in (pump_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(pump_task, stderr_task, return_exceptions=True)

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise VideoProcessingError(f"Could not process that video. {detail}".strip())
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoProcessingError("Processing produced an empty file.")

    out_info = await probe_video(dst)
    frames, tokens = estimate_tokens(
        duration=out_info.duration or span,
        fps=fps,
        keep_audio=want_audio,
        has_audio=out_info.has_audio,
    )
    return ProcessResult(
        path=dst, info=out_info, frames=frames, estimated_tokens=tokens
    )
