"""REGRESSION guard for the "repeated chunks on flaky links" bug.

At ANY instant during a v2.1 translator flush, the RAM stream state's
(content_blocks, snapshot_version) pair must be COHERENT: the content contains
exactly the deltas <= snapshot_version, no more, no less. The old flush wrote
content_blocks to the state FIRST, then emitted the versioned deltas one await
at a time, and stamped snapshot_version only at the END — so a /snapshot fetch
landing mid-flush (each emit await can block on socket backpressure, so on a
slow cellular link the window is wide) returned NEW content advertised at the
OLD version. The reattaching client then replayed deltas whose text the
snapshot already contained -> duplicated chunks, fixed only by a reload.

This drives the REAL `_wrap_event_emitter_v21` translator, records a simulated
reattach (deep-copied state content + advertised snapshot_version) at EVERY
delta-emit instant, then replays all wire deltas with version > advertised on
top of each reattach snapshot — exactly what the frontend does — and asserts
the reconstruction equals the server content EXACTLY (no duplication, no loss).
"""

import copy
import random
import string

from test.util.db import configure_test_database

configure_test_database()
import os

os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.utils.middleware import _wrap_event_emitter_v21  # noqa: E402
from open_webui.socket import main as socket_main  # noqa: E402


def _rand(n):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class _ClientMirror:
    """Applies chat:delta ops exactly as the frontend does."""

    def __init__(self, blocks=None):
        self.blocks = [dict(b) for b in (blocks or [])]

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
            attrs = pl.get("attrs")
            if isinstance(attrs, dict):
                block.update(attrs)
            self.blocks[idx] = block
        elif op == "text_append":
            idx = pl["block_idx"]
            self._ensure(idx)
            self.blocks[idx]["content"] = (
                self.blocks[idx].get("content", "") + pl["text"]
            )
        elif op == "replace":
            idx = pl.get("block_idx", 0)
            nbs = pl.get("content_blocks", [])
            if idx == 0 and len(nbs) != 1:
                self.blocks = [dict(b) for b in nbs]
            else:
                self._ensure(idx)
                if nbs:
                    self.blocks[idx] = dict(nbs[0])

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


def _drive_with_reattach_probes(seed):
    random.seed(seed)
    msg_id = f"coh-{seed}"

    captured = []  # every chat:delta data dict, wire order
    reattach_points = []  # (state content_blocks, advertised version) per emit

    async def inner_emitter(event):
        return None

    async def fake_emit_to_primary(uid, envelope):
        d = envelope.get("data") or {}
        if d.get("type") != "chat:delta":
            return
        captured.append(d["data"])
        # Simulate a client reattaching EXACTLY now: what /snapshot would serve.
        # Mirrors routers/streams.py: an in-progress state with a stamped
        # snapshot_version advertises THAT version with the stored content.
        state = socket_main.get_stream_state(msg_id)
        reattach_points.append(
            (
                copy.deepcopy(state.get("content_blocks") or []),
                state.get("snapshot_version"),
                state.get("status"),
            )
        )

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
        emitter = _wrap_event_emitter_v21(inner_emitter, metadata)
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

        for _ in range(random.randint(3, 30)):
            r = random.random()
            if r < 0.6:
                if not content_blocks or content_blocks[-1]["type"] != "text":
                    content_blocks.append({"type": "text", "content": ""})
                content_blocks[-1]["content"] += _rand(random.randint(1, 6))
            elif r < 0.8:
                if not content_blocks or content_blocks[-1]["type"] != "reasoning":
                    content_blocks.append(
                        {"type": "reasoning", "content": "", "started_at": 0}
                    )
                content_blocks[-1]["content"] += _rand(random.randint(1, 6))
            else:
                if content_blocks and content_blocks[-1]["type"] == "reasoning":
                    content_blocks[-1]["ended_at"] = 1
                    content_blocks[-1]["duration"] = 1
                content_blocks.append(
                    {"type": "tool_calls", "content": [{"id": "x"}], "started_at": 0}
                )
            flush()

        return _server_text(content_blocks), captured, reattach_points
    finally:
        socket_main.emit_to_primary = orig
        mw.emit_to_primary = mw_orig


def test_reattach_at_any_emit_instant_never_duplicates_or_loses_text():
    for seed in range(150):
        server_text, captured, reattach_points = _drive_with_reattach_probes(seed)
        for i, (snap_blocks, snap_version, status) in enumerate(reattach_points):
            # The state written before ANY emit of a content flush must be
            # stamped — an unstamped in-progress state makes /snapshot fall
            # back to the live wire counter, which is incoherent mid-flush.
            assert snap_version is not None, (
                f"seed {seed}: emit #{i} saw an in-progress state without "
                f"snapshot_version (status={status})"
            )
            client = _ClientMirror(snap_blocks)
            for d in captured:
                v = d.get("version")
                if isinstance(v, int) and v <= snap_version:
                    continue  # frontend drops deltas the snapshot already covers
                client.apply(d)
            assert client.text() == server_text, (
                f"seed {seed}: reattach at emit #{i} (advertised v{snap_version}) "
                f"reconstructed {client.text()} != server {server_text} — "
                f"(content, snapshot_version) incoherent: duplicated or lost text"
            )


if __name__ == "__main__":
    test_reattach_at_any_emit_instant_never_duplicates_or_loses_text()
    print("v2.1 snapshot coherence test passed")
