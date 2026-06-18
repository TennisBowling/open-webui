"""Tests for per-request image conversion caching + thread offload (Fix 3).

The agentic loop re-calls ``generate_chat_completion`` once per tool round with
the same input images. Previously each round re-read the image from disk and
re-ran the synchronous PIL recompression ON THE EVENT LOOP, re-encoding identical
bytes up to N times and stalling every other chat on the single worker. The fix:
cache the resolved provider ``data:`` URL per request (keyed by content+params)
and offload the blocking read/recompress to a thread.

These tests assert the cache makes the conversion happen ONCE and that the output
is byte-identical across rounds.
"""

import asyncio
import io
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "image_cache_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")

import pytest  # noqa: E402

import open_webui.routers.openai as oai  # noqa: E402


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def _fake_request():
    class _State:
        pass

    req = MagicMock()
    req.state = _State()
    return req


def _fake_file():
    f = MagicMock()
    f.id = "file-1"
    f.path = "p"
    f.meta = {"content_type": "image/png"}
    f.filename = "x.png"
    return f


def test_local_file_image_cached_across_rounds(monkeypatch):
    png = _png_bytes()
    req = _fake_request()
    file = _fake_file()

    calls = {"n": 0}
    real = oai.prepare_image_data_for_provider

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    async def run():
        with patch.object(oai, "prepare_image_data_for_provider", counting):
            m = MagicMock()
            m.__enter__ = lambda s: m
            m.__exit__ = lambda *a: False
            m.read = lambda: png
            with patch("builtins.open", return_value=m):
                u1 = await oai._resolve_local_file_image_data_url(
                    req, file, "p", optimize=True, quality=85, min_bytes=1
                )
                u2 = await oai._resolve_local_file_image_data_url(
                    req, file, "p", optimize=True, quality=85, min_bytes=1
                )
        return u1, u2

    u1, u2 = asyncio.run(run())
    assert u1 == u2  # byte-identical across rounds
    assert calls["n"] == 1  # conversion ran once; round 2 hit the cache
    assert u1.startswith("data:image/")


def test_local_file_empty_raises(monkeypatch):
    req = _fake_request()
    file = _fake_file()

    async def run():
        m = MagicMock()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda *a: False
        m.read = lambda: b""
        with patch("builtins.open", return_value=m):
            await oai._resolve_local_file_image_data_url(
                req, file, "p", optimize=False, quality=85, min_bytes=1
            )

    with pytest.raises(Exception):
        asyncio.run(run())


def test_cache_key_separates_quality_params():
    png = _png_bytes()
    req = _fake_request()
    file = _fake_file()

    async def run():
        m = MagicMock()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda *a: False
        m.read = lambda: png
        with patch("builtins.open", return_value=m):
            a = await oai._resolve_local_file_image_data_url(
                req, file, "p", optimize=True, quality=85, min_bytes=1
            )
            # Different params → different cache slot (must not collide).
            b = await oai._resolve_local_file_image_data_url(
                req, file, "p", optimize=False, quality=85, min_bytes=1
            )
        cache = oai._image_conversion_cache(req)
        return a, b, cache

    a, b, cache = asyncio.run(run())
    # Two distinct cache entries exist for the two parameter sets.
    assert len([k for k in cache if k[0] == "file"]) == 2


def test_cache_key_separates_max_dimension():
    png = _png_bytes()
    req = _fake_request()
    file = _fake_file()

    async def run():
        m = MagicMock()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda *a: False
        m.read = lambda: png
        with patch("builtins.open", return_value=m):
            await oai._resolve_local_file_image_data_url(
                req, file, "p", optimize=True, quality=85, min_bytes=1, max_dimension=2048
            )
            # Same image+params but a DIFFERENT cap → must not collide, else a
            # re-cap with a new admin setting would serve a stale conversion.
            await oai._resolve_local_file_image_data_url(
                req, file, "p", optimize=True, quality=85, min_bytes=1, max_dimension=1024
            )
        cache = oai._image_conversion_cache(req)
        return cache

    cache = asyncio.run(run())
    assert len([k for k in cache if k[0] == "file"]) == 2
