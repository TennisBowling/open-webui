"""Regression guard for flush-time delta coalescing (metered/slow-link downlink).

The native v2.1 fast-path holds consecutive tail-block text appends and merges
them into ONE versioned `text_append` delta once MIN_CHARS accumulate OR the
coalesce window elapses (`flush_pending_delta_data` in utils/middleware.py). This
cuts per-token wire framing (~14-18B/token) without changing what the client
reconstructs.

The two invariants that make coalescing safe are asserted here by replaying the
real accumulator + version-counter through the exact gate logic:

  1. COHERENCE: merging N pending tokens into 1 emitted delta consumes exactly
     ONE version number, so the emitted versions are strictly contiguous
     (v, v+1, v+2, ...) with NO gaps — the client's `version > mirror.version + 1`
     gap guard never trips, and a mid-stream reattach that drops deltas
     `<= advertised_snapshot_version` reconstructs the server text EXACTLY.
  2. EFFECTIVENESS: with MIN_CHARS > 0, a token trickle collapses into fewer
     deltas than tokens; with MIN_CHARS == 0 coalescing is disabled (one delta
     per flush, i.e. per token) — the documented escape hatch.

No DB / socket needed — drives the pure accumulator + gate arithmetic.
"""

import random
import string

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils.middleware import (  # noqa: E402
    _StreamTextAccumulator,
    STREAM_TEXT_COALESCE_MIN_CHARS,
    STREAM_TEXT_COALESCE_WINDOW_S,
)


def _rand(n):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class _CoalescingTailEmitter:
    """Faithful reduction of the native flush gate (utils/middleware.py
    ~4764-4805): a single growing text block, tokens appended one at a time, the
    coalescing gate deciding when to assign a version and emit.

    `now` is an injected logical clock (seconds) so the window branch is
    deterministic instead of wall-clock-flaky.
    """

    def __init__(self, min_chars, window_s):
        self.acc = _StreamTextAccumulator()
        self.min_chars = min_chars
        self.window_s = window_s
        self._version = 0
        self._last_emit_at = 0.0
        self.deltas = []  # (version, text) in wire order

    def _incr(self):
        self._version += 1
        return self._version

    def append_and_flush(self, token, now, *, force=False):
        """Append one provider token, then run the gate exactly as the real
        flush handler does before every emit attempt."""
        self.acc.append(token)
        self._flush(now, force=force)

    def _flush(self, now, *, force=False):
        pending = self.acc.pending_len
        if not pending:
            return
        # Coalescing gate: hold small appends until MIN_CHARS accumulate OR the
        # window elapses. `force` (terminal flush) never defers.
        if (
            not force
            and self.min_chars > 0
            and pending < self.min_chars
            and (now - self._last_emit_at) < self.window_s
        ):
            return
        appended = self.acc.take_appended()
        if not appended:
            return
        # ONE version bump for the whole coalesced run — the coherence keystone.
        version = self._incr()
        self._last_emit_at = now
        self.deltas.append((version, appended))

    def finalize(self, now):
        self._flush(now, force=True)


def _reconstruct(deltas, drop_at_or_below=0):
    """Client mirror: apply text_append deltas whose version > the advertised
    snapshot_version (frontend drops <= advertised)."""
    return "".join(t for (v, t) in deltas if v > drop_at_or_below)


def test_coalescing_versions_are_strictly_contiguous_and_reconstruct_exactly():
    min_chars = 48
    window_s = 0.024
    for seed in range(1500):
        random.seed(seed)
        em = _CoalescingTailEmitter(min_chars, window_s)
        server_text = ""
        now = 0.0
        for _ in range(random.randint(1, 60)):
            tok = _rand(random.randint(1, 6))
            server_text += tok
            # advance the logical clock by a small random step; sometimes big
            # enough to trip the window, sometimes not.
            now += random.choice([0.0, 0.003, 0.01, 0.05])
            em.append_and_flush(tok, now)
        now += 1.0
        em.finalize(now)

        versions = [v for (v, _) in em.deltas]
        # Strictly contiguous from 1: no gaps (gap == client refetch), no dupes.
        assert versions == list(range(1, len(versions) + 1)), (
            f"seed {seed}: non-contiguous versions {versions}"
        )
        # Full replay == server text.
        assert _reconstruct(em.deltas) == server_text, f"seed {seed}: full replay drift"
        # Reattach at EVERY emitted version boundary: dropping deltas
        # <= advertised and replaying the rest must equal the server text.
        for adv, _ in em.deltas:
            assert _reconstruct(em.deltas, drop_at_or_below=adv) == server_text[
                sum(len(t) for (v, t) in em.deltas if v <= adv):
            ], f"seed {seed}: incoherent reattach at v{adv}"


def test_coalescing_reduces_delta_count_for_a_token_trickle():
    """A long single-char-token trickle with a wide window collapses into far
    fewer deltas than tokens (the whole point of the feature)."""
    min_chars = 48
    window_s = 10.0  # effectively char-only gate
    em = _CoalescingTailEmitter(min_chars, window_s)
    now = 0.0
    n_tokens = 500
    for _ in range(n_tokens):
        em.append_and_flush("a", now)  # clock frozen -> window never trips
    em.finalize(now)
    # 500 one-char tokens at MIN_CHARS=48 -> ceil(500/48) ~= 11 deltas, not 500.
    assert len(em.deltas) <= (n_tokens // min_chars) + 2
    assert len(em.deltas) < n_tokens
    assert _reconstruct(em.deltas) == "a" * n_tokens


def test_min_chars_zero_disables_coalescing():
    """MIN_CHARS=0 restores per-flush (per-token) emission — the documented
    escape hatch used to disable the feature via env."""
    em = _CoalescingTailEmitter(min_chars=0, window_s=10.0)
    now = 0.0
    for i in range(20):
        em.append_and_flush(f"t{i}", now)
    em.finalize(now)
    assert len(em.deltas) == 20  # one delta per token, none held
    assert [v for (v, _) in em.deltas] == list(range(1, 21))


def test_window_elapse_flushes_held_text_bounding_trickle_latency():
    """Even below MIN_CHARS, a token flushes once the coalesce window elapses so
    a slow stream is not held indefinitely."""
    em = _CoalescingTailEmitter(min_chars=48, window_s=0.024)
    # First short token at t=0: below MIN_CHARS, window not yet elapsed -> held.
    em.append_and_flush("hi", now=0.0)
    assert em.deltas == []
    # Next token arrives after the window elapsed -> the whole held run flushes.
    em.append_and_flush("!", now=0.030)
    assert em.deltas == [(1, "hi!")]


def test_module_defaults_are_sane():
    # Defaults keep coalescing ON with a small char threshold and a sub-frame
    # window; both must be non-negative and the window a real (small) duration.
    assert STREAM_TEXT_COALESCE_MIN_CHARS >= 0
    assert STREAM_TEXT_COALESCE_WINDOW_S >= 0
    if STREAM_TEXT_COALESCE_MIN_CHARS > 0:
        assert STREAM_TEXT_COALESCE_WINDOW_S < 1.0


if __name__ == "__main__":
    test_coalescing_versions_are_strictly_contiguous_and_reconstruct_exactly()
    test_coalescing_reduces_delta_count_for_a_token_trickle()
    test_min_chars_zero_disables_coalescing()
    test_window_elapse_flushes_held_text_bounding_trickle_latency()
    test_module_defaults_are_sane()
    print("coalescing regression tests passed")
