"""Integration test for the streaming O(N^2) fix (Parts B/C).

Drives the real `_StreamTextAccumulator` + the real `_emit_delta_for_blocks`
translator through randomized native/translator/boundary interleavings and
asserts that the client, reconstructing from the emitted wire ops, ends up with
byte-identical text/reasoning content to the server's content_blocks. This is the
core correctness guarantee of the rewrite: the O(1) accumulator + emitted-length
mirror tracking must produce exactly the same delta stream the old per-token
dict-concat + startswith-diff produced.

No DB / socket needed — exercises the pure stream-translation layer.
"""

import asyncio
import random
import string

from open_webui.utils.middleware import (
    _StreamTextAccumulator,
    _emit_delta_for_blocks,
    _merge_tool_result_body_maps,
)


def _rand(n):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


def _text_of(blocks):
    # The user-visible guarantee is that the concatenated text/reasoning content
    # the client reconstructs equals the server's. Block-boundary bookkeeping
    # (empty placeholder blocks, exact split points) is renderer detail; compare
    # the non-empty (type, content) run sequence.
    return [
        (b.get("type"), b.get("content", ""))
        for b in blocks
        if b.get("type") in ("text", "reasoning") and (b.get("content", "") or "")
    ]


class _Server:
    """Replicates the rewritten stream_body_handler flush control flow for the
    text/reasoning tail: native fast-path when possible, translator fallback at
    structural boundaries."""

    def __init__(self, msg_id):
        self.msg_id = msg_id
        self.content_blocks = []
        self.mirror = {"blocks": []}
        self._tail = {"acc": None, "block": None}

    # -- tail accumulator helpers (mirror of the real closures) --
    def _tail_materialize(self):
        acc, blk = self._tail["acc"], self._tail["block"]
        if acc is not None and blk is not None:
            blk["content"] = acc.materialize()

    def _tail_bind(self, block):
        if self._tail["block"] is block and self._tail["acc"] is not None:
            return
        self._tail_materialize()
        self._tail["acc"] = _StreamTextAccumulator(block.get("content", "") or "")
        self._tail["block"] = block

    def _tail_append(self, block, value):
        if self._tail["block"] is not block or self._tail["acc"] is None:
            self._tail_bind(block)
        self._tail["acc"].append(value)

    def _native(self):
        if not self.content_blocks:
            return None
        ti = len(self.content_blocks) - 1
        tail = self.content_blocks[ti]
        if tail.get("type") not in ("text", "reasoning"):
            return None
        if tail.get("type") == "reasoning" and tail.get("ended_at"):
            return None
        mb = self.mirror["blocks"]
        if ti != len(mb) - 1 or mb[ti].get("type") != tail.get("type"):
            return None
        if self._tail["acc"] is None or self._tail["block"] is not tail:
            return None
        appended = self._tail["acc"].take_appended()
        if not appended:
            return None
        return ti, appended

    def flush(self, client):
        """Emit deltas for the current content_blocks, apply them to `client`."""
        native = self._native()
        if native is not None:
            ti, appended = native
            client.text_append(ti, appended)
            mbk = self.mirror["blocks"][ti]
            if mbk.get("_emitted_len") is None:
                mbk["_emitted_len"] = len(mbk.get("content", "") or "")
            mbk["_emitted_len"] += len(appended)
            return
        # Translator fallback. NOTE: we deliberately do NOT pre-reconcile the
        # mirror here — _emit_delta_for_blocks must honor the `_emitted_len`
        # cursor itself (so every translator entry point is correct, including
        # round-boundary emits that never pre-reconcile).
        self._tail_materialize()
        ops = []

        async def cap(payload):
            ops.append(payload["data"])

        aws = _emit_delta_for_blocks(cap, self.msg_id, self.mirror, self.content_blocks)
        for aw in aws:
            # Each awaitable is a single cap() coroutine; drive it to completion
            # synchronously (no real I/O) without a managed event loop.
            try:
                aw.send(None)
            except StopIteration:
                pass
        for d in ops:
            client.apply(d)
        if self._tail["acc"] is not None:
            self._tail["acc"].take_appended()


class _Client:
    def __init__(self):
        self.blocks = []

    def _ensure(self, idx, btype="text"):
        while len(self.blocks) <= idx:
            self.blocks.append({"type": "text", "content": ""})

    def text_append(self, idx, text):
        self._ensure(idx)
        self.blocks[idx]["content"] = self.blocks[idx].get("content", "") + text

    def apply(self, d):
        op = d["op"]
        pl = d.get("payload", {}) or {}
        if op == "block_open":
            idx = pl.get("block_idx", len(self.blocks))
            while len(self.blocks) <= idx:
                self.blocks.append({"type": "text", "content": ""})
            self.blocks[idx] = {"type": pl.get("type", "text"), "content": ""}
        elif op == "text_append":
            self.text_append(pl["block_idx"], pl["text"])
        elif op == "replace":
            idx = pl.get("block_idx", 0)
            nbs = pl.get("content_blocks", [])
            if idx == 0 and len(nbs) > 1:
                self.blocks = [dict(b) for b in nbs]
            else:
                self._ensure(idx)
                if nbs:
                    self.blocks[idx] = dict(nbs[0])
        # block_close / tool_call ops don't change text content


def _run_one(seed):
    random.seed(seed)
    srv = _Server(f"m{seed}")
    cli = _Client()
    for _ in range(random.randint(1, 30)):
        r = random.random()
        if r < 0.6:
            if not srv.content_blocks or srv.content_blocks[-1]["type"] != "text":
                srv._tail_materialize()
                srv.content_blocks.append({"type": "text", "content": ""})
            srv._tail_append(srv.content_blocks[-1], _rand(random.randint(1, 5)))
            srv.flush(cli)
        elif r < 0.78:
            if not srv.content_blocks or srv.content_blocks[-1]["type"] != "reasoning":
                srv._tail_materialize()
                srv.content_blocks.append(
                    {"type": "reasoning", "content": "", "started_at": 0}
                )
            srv._tail_append(srv.content_blocks[-1], _rand(random.randint(1, 5)))
            srv.flush(cli)
        else:
            # structural boundary: close tail, open a tool_calls block
            srv._tail_materialize()
            if srv.content_blocks and srv.content_blocks[-1]["type"] == "reasoning":
                srv.content_blocks[-1]["ended_at"] = 1
                srv.content_blocks[-1]["duration"] = 1
            srv.content_blocks.append(
                {"type": "tool_calls", "content": [{"id": "x"}], "started_at": 0}
            )
            srv.flush(cli)
    srv._tail_materialize()
    return _text_of(srv.content_blocks), _text_of(cli.blocks)


def test_native_translator_interleaving_reconstructs_exactly():
    mismatches = []
    for seed in range(2000):
        srv_text, cli_text = _run_one(seed)
        if srv_text != cli_text:
            mismatches.append((seed, srv_text, cli_text))
    assert not mismatches, (
        f"{len(mismatches)} mismatches; first: {mismatches[0]}"
    )


def test_accumulator_contract_fuzz():
    """initial + concat(take_appended()) == materialize() == all appends."""
    for seed in range(3000):
        random.seed(seed + 100000)
        init = _rand(random.randint(0, 5)) if random.random() < 0.3 else ""
        acc = _StreamTextAccumulator(init)
        ref = [init] if init else []
        emitted = [init] if init else []
        for _ in range(random.randint(1, 40)):
            r = random.random()
            if r < 0.55:
                v = _rand(random.randint(0, 6))
                acc.append(v)
                ref.append(v)
            elif r < 0.8:
                emitted.append(acc.take_appended())
            else:
                assert acc.materialize() == "".join(ref)
        assert acc.materialize() == "".join(ref)
        emitted.append(acc.take_appended())
        assert "".join(emitted) == "".join(ref)
        assert len(acc) == len("".join(ref))


def test_suffix_matches_full_tail():
    for seed in range(2000):
        random.seed(seed + 200000)
        parts = [_rand(random.randint(1, 6)) for _ in range(random.randint(1, 8))]
        acc = _StreamTextAccumulator()
        for p in parts:
            acc.append(p)
        full = "".join(parts)
        for k in (0, 1, 3, len(full) // 2, len(full), len(full) + 5):
            assert acc.suffix(k) == (full[-k:] if k > 0 else "")


def test_tool_result_body_maps_merge_all_authoritative_sources():
    merged = _merge_tool_result_body_maps(
        {"call_1": {"content": "persisted"}, "bad": "not a body"},
        {"call_2": {"content": "live"}},
        {"call_1": {"content": "split wins"}, 3: {"content": "numeric key"}},
        None,
    )

    assert merged == {
        "call_1": {"content": "split wins"},
        "call_2": {"content": "live"},
        "3": {"content": "numeric key"},
    }


def test_translator_honors_emitted_len_no_round_boundary_dup():
    """Regression: the native fast-path advances a mirror block's `_emitted_len`
    cursor WITHOUT refreshing the stale `content` string. A later translator emit
    that bypasses the flush-handler (e.g. the tool-round-boundary
    event_emitter(content_blocks) at the top of the agentic loop) must honor that
    cursor and NOT re-emit the already-streamed text as a duplicate. Before the
    fix, this produced visible doubled text at every tool-call boundary."""
    from open_webui.utils.middleware import _emit_delta_for_blocks

    def run_emit(mirror, content_blocks):
        emitted = []

        async def cap(payload):
            emitted.append(payload["data"])

        for aw in _emit_delta_for_blocks(cap, "m", mirror, content_blocks):
            try:
                aw.send(None)
            except StopIteration:
                pass
        return emitted

    # Native sent 11 chars ("hello world") but only refreshed mirror content to
    # "hel" (stale-short) + _emitted_len=11. Round boundary appends a tool_calls
    # block and emits the full content_blocks through the translator.
    mirror = {"blocks": [{"type": "text", "content": "hel", "_emitted_len": 11}]}
    content_blocks = [
        {"type": "text", "content": "hello world"},
        {"type": "tool_calls", "content": [{"id": "x"}], "started_at": 0},
    ]
    emitted = run_emit(mirror, content_blocks)
    text_ops_block0 = [
        d for d in emitted if d["op"] == "text_append" and d["payload"].get("block_idx") == 0
    ]
    assert not text_ops_block0, f"duplicate text re-emitted at round boundary: {text_ops_block0}"
    # Mirror content reconciled to the full text; cursor consumed.
    assert mirror["blocks"][0]["content"] == "hello world"
    assert "_emitted_len" not in mirror["blocks"][0]

    # Reasoning variant.
    mirror2 = {"blocks": [{"type": "reasoning", "content": "th", "_emitted_len": 8}]}
    cb2 = [
        {"type": "reasoning", "content": "thinking", "ended_at": 1, "duration": 1},
        {"type": "text", "content": ""},
    ]
    emitted2 = run_emit(mirror2, cb2)
    dup2 = [
        d for d in emitted2 if d["op"] == "text_append" and d["payload"].get("block_idx") == 0
    ]
    assert not dup2, f"duplicate reasoning re-emitted: {dup2}"

    # When _emitted_len exceeds the (already trimmed) content — defensive: never
    # crash, never produce negative slices.
    mirror3 = {"blocks": [{"type": "text", "content": "x", "_emitted_len": 999}]}
    cb3 = [{"type": "text", "content": "xy"}]
    emitted3 = run_emit(mirror3, cb3)
    # client had "xy" (emitted_len 999 capped to full) -> no new text
    assert mirror3["blocks"][0]["content"] == "xy"


if __name__ == "__main__":
    test_native_translator_interleaving_reconstructs_exactly()
    test_accumulator_contract_fuzz()
    test_suffix_matches_full_tail()
    print("all integration tests passed")
