"""ADV2 — SOCKET-MERGE invariant.

The patched socket {type:"files"} handler delegates to
container_workspace._merge_files(existing, incoming). Invariant:
  - ACCUMULATES distinct files across multiple emits
  - DEDUPS repeats (by id, and by container content-key)
  - never drops a legit file
  - no regression for non-container tool_result_files (id+url, data-uri, etc.)
  - existing-first ordering (new appended)
  - no circular import (function-local import path)

We mirror the REAL descriptor shapes:
  - MCP image attachment: {id, url, type:"image"} — NO container_workspace
  - OpenAPI data-uri file:  {id, url:"data:...;base64,..", name} — NO container_workspace
  - Container output (real _store_output_file): ALWAYS has
        container_workspace{workspace_path, sha256, version}
"""

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402


def _merge():
    return cw._merge_files, cw._file_content_key


def _container_descriptor(file_id, ws_path, sha, version=1, name="out.csv"):
    """Shape mirroring the REAL _store_output_file descriptor."""
    return {
        "type": "file",
        "id": file_id,
        "url": f"/api/v1/files/{file_id}",
        "name": name,
        "container_workspace": {
            "workspace_path": ws_path,
            "sha256": sha,
            "version": version,
        },
    }


# --- A: two DIFFERENT non-container files in one turn both survive ----------
def test_accumulate_two_distinct_noncontainer():
    merge, _ = _merge()
    mcp_img = {"id": "img-1", "url": "https://cdn/x.png", "type": "image"}
    openapi = {
        "id": "oa-1",
        "url": "data:text/csv;base64,YWJj",
        "name": "data.csv",
        "type": "file",
    }
    # Emit 1: image only. Emit 2: openapi file only. (separate tool results)
    after1 = merge([], [mcp_img])
    after2 = merge(after1, [openapi])
    ids = [f["id"] for f in after2]
    assert ids == ["img-1", "oa-1"], ids  # both kept, existing first
    assert len(after2) == 2


# --- B: same non-container file (id, no cw) emitted twice -> dedup by id ----
def test_dedup_noncontainer_by_id():
    merge, _ = _merge()
    f = {"id": "img-9", "url": "https://cdn/y.png", "type": "image"}
    after1 = merge([], [f])
    after2 = merge(after1, [f])  # re-emit identical
    assert [x["id"] for x in after2] == ["img-9"]
    # also dedups when same id appears twice within ONE incoming batch
    one_batch = merge([], [f, dict(f)])
    assert len(one_batch) == 1


# --- C: no-id same-url item emitted twice (pre-existing url concern) --------
def test_noid_sameurl_dedups_by_url():
    merge, _ = _merge()
    # no id, no container_workspace -> identity falls back to url (mirrors the
    # frontend mergeMessageFiles id ?? url ?? content precedence), so two emits of
    # the SAME url collapse to one (FE/BE parity; no duplicate media card on reload).
    noid = {"url": "https://cdn/z.png", "type": "image"}
    after1 = merge([], [noid])
    after2 = merge(after1, [dict(noid)])
    assert len(after2) == 1, after2
    assert after2[0]["url"] == "https://cdn/z.png"


def test_noid_distinct_urls_accumulate():
    merge, _ = _merge()
    a = {"url": "https://cdn/a.png", "type": "image"}
    b = {"url": "https://cdn/b.png", "type": "image"}
    after = merge([], [a, b])
    assert len(after) == 2, after


# --- D: container re-import dedups by content-key despite fresh id ----------
def test_container_dedup_by_content_key_fresh_id():
    merge, key = _merge()
    sha = "a" * 64
    first = _container_descriptor("file-AAA", "/workspace/outputs/r.csv", sha)
    # importer re-ran -> brand new random id, SAME (workspace_path, sha256)
    second = _container_descriptor("file-BBB", "/workspace/outputs/r.csv", sha)
    assert key(first) == key(second)  # content identity collapses
    after = merge([first], [second])
    assert len(after) == 1, after
    assert after[0]["id"] == "file-AAA"  # existing kept


# --- E: container files with DIFFERENT content both accumulate -------------
def test_container_distinct_content_accumulates():
    merge, _ = _merge()
    a = _container_descriptor("f1", "/workspace/outputs/a.csv", "1" * 64)
    b = _container_descriptor("f2", "/workspace/outputs/b.csv", "2" * 64)
    # different path same sha, and same path different sha -> all distinct
    c = _container_descriptor("f3", "/workspace/outputs/a.csv", "9" * 64)
    after = merge([], [a, b, c])
    assert [x["id"] for x in after] == ["f1", "f2", "f3"], after


# --- F: the real reload scenario — import persisted file, re-emit dedups ----
def test_reload_import_then_reemit_no_dup():
    merge, _ = _merge()
    sha = "c" * 64
    persisted = _container_descriptor("file-XYZ", "/workspace/outputs/plot.png", sha)
    # message.files already has it (import wrote it). The {type:"files"} emit
    # carries the SAME descriptor (same id + same content).
    after = merge([persisted], [persisted])
    assert len(after) == 1, after
    # mixed: re-emit batch contains the persisted one PLUS a genuinely new file
    new = _container_descriptor("file-NEW", "/workspace/outputs/plot2.png", "d" * 64)
    after2 = merge([persisted], [persisted, new])
    assert [x["id"] for x in after2] == ["file-XYZ", "file-NEW"], after2


# --- G: container + non-container mixed in same message ---------------------
def test_mixed_container_and_noncontainer():
    merge, _ = _merge()
    img = {"id": "img-1", "url": "https://cdn/x.png", "type": "image"}
    cont = _container_descriptor("file-1", "/workspace/outputs/r.csv", "e" * 64)
    after1 = merge([], [img])
    after2 = merge(after1, [cont])
    # re-emit both -> dedup both
    after3 = merge(after2, [img, cont])
    assert [x["id"] for x in after3] == ["img-1", "file-1"], after3


# --- H: ordering — existing strictly first, new appended (not prepended) ----
def test_existing_first_ordering():
    merge, _ = _merge()
    e1 = {"id": "e1", "url": "u1"}
    e2 = {"id": "e2", "url": "u2"}
    n1 = {"id": "n1", "url": "u3"}
    after = merge([e1, e2], [n1])
    assert [x["id"] for x in after] == ["e1", "e2", "n1"], after


# --- I: defensive — existing not a list / incoming weird shapes ------------
def test_defensive_shapes():
    merge, _ = _merge()
    # existing None -> treated as []
    assert merge(None, [{"id": "a"}]) == [{"id": "a"}]
    # incoming item missing id and not-dict-ish url -> kept (no crash)
    out = merge([], [{"name": "x"}])
    assert out == [{"name": "x"}]
    # container item with cw present but missing sha -> key None -> id-dedup only
    cw_no_sha = {"id": "z", "container_workspace": {"workspace_path": "/workspace/outputs/q"}}
    after = merge([cw_no_sha], [dict(cw_no_sha)])
    assert len(after) == 1  # falls back to id-dedup


# --- K: content-version UPDATE — real _store_output_file mints fresh uuid AND
#        new sha256 for new content. Old version descriptor stays; new one is a
#        DISTINCT card (version history). Re-emit of new version dedups. --------
def test_content_version_update_accumulates_then_dedups():
    merge, key = _merge()
    v1 = _container_descriptor("uuid-v1", "/workspace/outputs/r.csv", "1" * 64, version=1)
    v2 = _container_descriptor("uuid-v2", "/workspace/outputs/r.csv", "2" * 64, version=2)
    assert key(v1) != key(v2)  # different content -> different identity
    after = merge([v1], [v2])
    assert [x["id"] for x in after] == ["uuid-v1", "uuid-v2"], after  # both versions
    after2 = merge(after, [v2])  # reload re-emit of new version
    assert [x["id"] for x in after2] == ["uuid-v1", "uuid-v2"], after2


# --- L: pathological same-id-different-content. The real importer ALWAYS mints
#        a fresh uuid4 per store (line ~750), so an id is never reused for new
#        content; id-dedup dropping the second here can only drop a true
#        duplicate-id, never a legit new file. Documents precedence only. -------
def test_same_id_different_content_id_wins():
    merge, _ = _merge()
    a = _container_descriptor("same-id", "/workspace/outputs/r.csv", "1" * 64)
    b = _container_descriptor("same-id", "/workspace/outputs/r.csv", "2" * 64)
    after = merge([a], [b])
    assert len(after) == 1  # id-dedup wins; not a real-importer scenario


# --- J: no circular import via the function-local import path ---------------
def test_no_circular_import():
    src = open(cw.__file__).read()
    # container_workspace must NOT import socket.main at module scope (the
    # function-local import in socket/main.py is one-directional).
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "socket.main" in node.module:
            raise AssertionError(f"unexpected socket.main import: {node.module}")
    # and the helper is importable on its own
    assert callable(cw._merge_files)


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    sys.exit(1 if failed else 0)
