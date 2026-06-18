"""High-fidelity test: drive the REAL v2 emitter (`_wrap_event_emitter_v2`) and
the real `_emit_delta_for_blocks` translator with chat:completion events, capture
the actual `chat:delta` socket payloads, reconstruct the client, and assert the
client's text/reasoning content matches the server's content_blocks exactly.

This exercises the production translator path (shared by every v2 stream) rather
than a simplified harness — it is the regression guard for the
`_emit_delta_for_blocks` mirror-population fix (the `or []` throwaway-list bug).

DB is bound to a throwaway copy of the migrated dev DB before importing (the
socket/main store binds the engine at import); no Redis → in-memory stream state.
"""

import asyncio
import os
import random
import shutil
import string
import tempfile

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "v2_emitter_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.utils.middleware import _wrap_event_emitter_v2  # noqa: E402
from open_webui.socket import main as socket_main  # noqa: E402


def _rand(n):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class _ClientMirror:
    """Applies chat:delta ops exactly as the frontend does (text_append grows the
    block, block_open creates a typed block, replace swaps)."""

    def __init__(self):
        self.blocks = []

    def _ensure(self, idx):
        while len(self.blocks) <= idx:
            self.blocks.append({"type": "text", "content": ""})

    def apply(self, delta):
        op = delta["op"]
        pl = delta.get("payload", {}) or {}
        if op == "block_open":
            idx = pl.get("block_idx", len(self.blocks))
            while len(self.blocks) <= idx:
                self.blocks.append({"type": "text", "content": ""})
            block = {"type": pl.get("type", "text"), "content": ""}
            # Mirror the real frontend: Object.assign(block, payload.attrs).
            # This is how a `user_steer` block's static `content` attr lands on
            # the client (text/reasoning instead stream via a following
            # text_append, so their attrs carry no content).
            attrs = pl.get("attrs")
            if isinstance(attrs, dict):
                block.update(attrs)
            self.blocks[idx] = block
        elif op == "text_append":
            idx = pl["block_idx"]
            self._ensure(idx)
            self.blocks[idx]["content"] = self.blocks[idx].get("content", "") + pl["text"]
        elif op == "replace":
            idx = pl.get("block_idx", 0)
            nbs = pl.get("content_blocks", [])
            if idx == 0 and len(nbs) != 1:
                self.blocks = [dict(b) for b in nbs]
            else:
                self._ensure(idx)
                if nbs:
                    self.blocks[idx] = dict(nbs[0])
        # block_close / tool ops: no text-content change

    def text(self):
        return [
            (b.get("type"), b.get("content", ""))
            for b in self.blocks
            if b.get("type") in ("text", "reasoning") and (b.get("content") or "")
        ]


def _server_text(blocks):
    return [
        (b.get("type"), b.get("content", ""))
        for b in blocks
        if b.get("type") in ("text", "reasoning") and (b.get("content") or "")
    ]


def _drive(seed):
    random.seed(seed)
    msg_id = f"vmsg-{seed}"
    user_id = f"u-{seed}"

    captured = []  # list of chat:delta data dicts in wire order

    async def inner_emitter(event):
        # Non-chat:completion events would pass through here; we only feed
        # chat:completion so this should not receive content deltas.
        return None

    # Patch emit_to_primary to capture wire payloads synchronously.
    async def fake_emit_to_primary(uid, envelope):
        d = envelope.get("data") or {}
        if d.get("type") in ("chat:delta",):
            captured.append(d["data"])

    orig = socket_main.emit_to_primary
    socket_main.emit_to_primary = fake_emit_to_primary
    # Also patch the reference imported into middleware's module namespace.
    import open_webui.utils.middleware as mw

    mw_orig = mw.emit_to_primary
    mw.emit_to_primary = fake_emit_to_primary
    try:
        metadata = {
            "message_id": msg_id,
            "user_id": user_id,
            "chat_id": f"c-{seed}",
            "session_id": f"s-{seed}",
        }
        emitter = _wrap_event_emitter_v2(inner_emitter, metadata)

        # Build a server-side content_blocks sequence and emit chat:completion
        # flushes (the v1-shaped event the wrapper translates). This mirrors how
        # the loop calls event_emitter({"type":"chat:completion","data":{...}}).
        content_blocks = []

        def _drive_coro(coro):
            # Step the coroutine to completion without a managed event loop
            # (it only awaits our trivial fake emit_to_primary). Avoids
            # cross-test event-loop contamination from asyncio.get_event_loop().
            try:
                coro.send(None)
            except StopIteration:
                pass
            else:
                # Should complete in one step (no real I/O); drain defensively.
                while True:
                    try:
                        coro.send(None)
                    except StopIteration:
                        break

        def flush_completion():
            data = {"content_blocks": [dict(b) for b in content_blocks]}
            _drive_coro(emitter({"type": "chat:completion", "data": data}))

        for _ in range(random.randint(3, 40)):
            r = random.random()
            if r < 0.6:
                if not content_blocks or content_blocks[-1]["type"] != "text":
                    content_blocks.append({"type": "text", "content": ""})
                content_blocks[-1]["content"] += _rand(random.randint(1, 6))
                flush_completion()
            elif r < 0.8:
                if not content_blocks or content_blocks[-1]["type"] != "reasoning":
                    content_blocks.append(
                        {"type": "reasoning", "content": "", "started_at": 0}
                    )
                content_blocks[-1]["content"] += _rand(random.randint(1, 6))
                flush_completion()
            else:
                if content_blocks and content_blocks[-1]["type"] == "reasoning":
                    content_blocks[-1]["ended_at"] = 1
                    content_blocks[-1]["duration"] = 1
                content_blocks.append(
                    {"type": "tool_calls", "content": [{"id": "x"}], "started_at": 0}
                )
                flush_completion()

        client = _ClientMirror()
        for d in captured:
            client.apply(d)
        return _server_text(content_blocks), client.text(), captured
    finally:
        socket_main.emit_to_primary = orig
        mw.emit_to_primary = mw_orig


def test_real_v2_emitter_reconstructs_text_exactly():
    mismatches = []
    for seed in range(400):
        srv, cli, _ = _drive(seed)
        if srv != cli:
            mismatches.append((seed, srv, cli))
    assert not mismatches, f"{len(mismatches)} mismatches; first: {mismatches[0]}"


def test_real_v2_emitter_version_monotonic():
    # Every emitted delta must carry a strictly increasing version (the frontend
    # drops version <= mirror.version and snapshot-gaps on version > v+1).
    for seed in range(200):
        _, _, captured = _drive(seed)
        versions = [d.get("version") for d in captured if "version" in d]
        assert versions == sorted(versions), f"seed {seed}: versions not monotonic"
        assert len(set(versions)) == len(versions), f"seed {seed}: duplicate versions"


def _drive_steer(seed):
    """Drive the REAL v2 translator through a steering sequence — assistant text,
    a tool_calls round, then a `user_steer` block injected at the boundary, then
    the assistant continues — and return (server_steer_text, client_steer_text)
    so the test can assert the steer content reached the client mirror live."""
    msg_id = f"steer-{seed}"
    captured = []

    async def inner_emitter(event):
        return None

    async def fake_emit_to_primary(uid, envelope):
        d = envelope.get("data") or {}
        if d.get("type") == "chat:delta":
            captured.append(d["data"])

    orig = socket_main.emit_to_primary
    socket_main.emit_to_primary = fake_emit_to_primary
    import open_webui.utils.middleware as mw

    mw_orig = mw.emit_to_primary
    mw.emit_to_primary = fake_emit_to_primary
    try:
        metadata = {
            "message_id": msg_id,
            "user_id": f"u-{seed}",
            "chat_id": f"c-{seed}",
            "session_id": f"s-{seed}",
        }
        emitter = _wrap_event_emitter_v2(inner_emitter, metadata)
        content_blocks = []

        def _drive_coro(coro):
            try:
                coro.send(None)
            except StopIteration:
                pass
            else:
                while True:
                    try:
                        coro.send(None)
                    except StopIteration:
                        break

        def flush():
            data = {"content_blocks": [dict(b) for b in content_blocks]}
            _drive_coro(emitter({"type": "chat:completion", "data": data}))

        # round 1: assistant text
        content_blocks.append({"type": "text", "content": "working on it"})
        flush()
        # tool_calls round
        content_blocks[-1]["content"] = "working on it"
        content_blocks.append(
            {"type": "tool_calls", "content": [{"id": "x"}], "started_at": 0, "results": [{"tool_call_id": "x", "content": "ok"}]}
        )
        flush()
        # boundary: steer injected, then a fresh empty text block (as the loop does)
        content_blocks.append({"type": "user_steer", "content": "focus on the tests"})
        content_blocks.append({"type": "text", "content": ""})
        flush()
        # assistant continues into the trailing text block
        content_blocks[-1]["content"] = "okay, refocusing"
        flush()

        client = _ClientMirror()
        for d in captured:
            client.apply(d)

        srv_steer = [
            b.get("content") for b in content_blocks if b.get("type") == "user_steer"
        ]
        cli_steer = [
            b.get("content") for b in client.blocks if b.get("type") == "user_steer"
        ]
        return srv_steer, cli_steer
    finally:
        socket_main.emit_to_primary = orig
        mw.emit_to_primary = mw_orig


def test_v2_emitter_delivers_user_steer_block_content_live():
    """REGRESSION: a `user_steer` block (mid-task steering) must reach the client
    mirror via the live v2 delta path — not only on reload. The translator emits
    it as a block_open carrying the steer text as a static `content` attr (text/
    reasoning stream via text_append; user_steer has no follow-up op)."""
    for seed in range(25):
        srv, cli = _drive_steer(seed)
        assert srv == ["focus on the tests"], f"seed {seed}: server {srv}"
        assert cli == srv, f"seed {seed}: client mirror lost the steer: {cli} != {srv}"


if __name__ == "__main__":
    test_real_v2_emitter_reconstructs_text_exactly()
    test_real_v2_emitter_version_monotonic()
    test_v2_emitter_delivers_user_steer_block_content_live()
    print("real v2 emitter tests passed")
