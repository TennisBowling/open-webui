"""Concrete mid-stream reload/reattach simulation for the snapshot_version
decoupling (Part C of the streaming O(N^2) fix).

Models the real interplay:
  * backend native flush: emit text_append deltas (monotonic versions), write the
    RAM snapshot on a bounded cadence stamping `snapshot_version` = last emitted
    version whose text is fully contained in the snapshot.
  * frontend reattach: fetch the snapshot -> set mirror.version = snapshot_version,
    take snapshot content, then replay buffered deltas dropping version <=
    mirror.version (exactly the Chat.svelte chatDeltaHandler / requestStreamSnapshot
    logic).

The invariant under test: after reattach + replay, the client's reconstructed
text equals the server's full text — NO loss, NO duplication — for every gap
position. A violation here is the "permanent missing/duplicated text on reload"
class of bug.

Pure logic; no DB/socket. Uses the real cadence semantics from the implementation
(snapshot when chars-since >= 8192 OR 0.25s elapsed OR first flush). We drive the
char trigger deterministically by choosing snapshot points.
"""

import random
import string


def _rand(n):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class _Backend:
    """Emits text_append deltas for a single growing text block, snapshotting on a
    char cadence with snapshot_version stamped AFTER emit (content includes it)."""

    def __init__(self, snap_chars=40):
        self.full = ""  # full text so far (server truth)
        self.version = 0  # live wire version
        self.deltas = []  # (version, text) emitted, in order
        # RAM snapshot the /snapshot endpoint would serve:
        self.snap_content = ""  # content at last snapshot
        self.snap_version = 0  # snapshot_version advertised
        self._chars_since = 0
        self._snap_chars = snap_chars
        self._established = False

    def emit_token(self, tok):
        # append + emit one text_append delta
        self.full += tok
        self.version += 1
        self.deltas.append((self.version, tok))
        self._chars_since += len(tok)
        # bounded-cadence snapshot AFTER emit (content includes this token),
        # stamped with the just-emitted version.
        if not self._established or self._chars_since >= self._snap_chars:
            self.snap_content = self.full  # materialize current full text
            self.snap_version = self.version  # <= content (content == full here)
            self._chars_since = 0
            self._established = True

    def snapshot(self):
        # what GET /snapshot returns mid-stream
        return {"content": self.snap_content, "version": self.snap_version}


def _client_reattach_and_replay(snapshot, buffered_deltas):
    """Mirror Chat.svelte: set version=snapshot.version, content=snapshot.content,
    then replay buffered deltas dropping version<=mirror.version, applying
    text_append; a gap (version>mirror.version+1) would trigger another snapshot
    (we assert it does not happen pathologically)."""
    content = snapshot["content"]
    version = snapshot["version"]
    # Replay in version order (socket delivers in order; buffer preserves it).
    for v, text in buffered_deltas:
        if v <= version:
            continue  # already in snapshot content
        # In the real client a gap (v > version+1) requests another snapshot;
        # here contiguous emission guarantees v == version+1 after the drop.
        assert v == version + 1, f"unexpected gap: v={v} mirror={version}"
        content += text
        version = v
    return content


def test_reattach_at_every_gap_position_no_loss_or_dup():
    failures = []
    for seed in range(3000):
        random.seed(seed)
        be = _Backend(snap_chars=random.choice([8, 20, 40, 100]))
        n_tokens = random.randint(1, 60)
        for _ in range(n_tokens):
            be.emit_token(_rand(random.randint(1, 6)))

        # Client reattaches at a RANDOM moment: it has the snapshot as of "now",
        # and a buffer of all deltas the socket delivered with version > the
        # snapshot it will fetch. Model the worst case: client fetches the
        # snapshot, and independently has buffered every delta emitted so far
        # (the socket and the snapshot race). Reattach can happen at any point;
        # the snapshot served is the backend's current snap_{content,version}.
        snapshot = be.snapshot()
        # The client's buffered deltas: all emitted deltas (it may hold some that
        # are already in the snapshot — those must be dropped, not double-applied).
        buffered = list(be.deltas)
        reconstructed = _client_reattach_and_replay(snapshot, buffered)
        if reconstructed != be.full:
            failures.append((seed, reconstructed, be.full))

    assert not failures, f"{len(failures)} reload mismatches; first: {failures[0][:1]} got len {len(failures[0][1])} vs {len(failures[0][2])}"


def test_snapshot_version_never_exceeds_content():
    """The core invariant: snapshot_version must never advertise a version whose
    text is not fully present in snapshot_content. We check that snap_content
    always equals the concatenation of all deltas with version <= snap_version."""
    for seed in range(3000):
        random.seed(seed + 50000)
        be = _Backend(snap_chars=random.choice([8, 20, 40]))
        for _ in range(random.randint(1, 60)):
            be.emit_token(_rand(random.randint(1, 6)))
            # At EVERY emit, verify the current snapshot is self-consistent.
            expected = "".join(
                t for (v, t) in be.deltas if v <= be.snap_version
            )
            assert be.snap_content == expected, (
                f"seed {seed}: snap_content != text through snap_version "
                f"({len(be.snap_content)} vs {len(expected)})"
            )


if __name__ == "__main__":
    test_reattach_at_every_gap_position_no_loss_or_dup()
    test_snapshot_version_never_exceeds_content()
    print("reload convergence tests passed")
