"""Adversarial NO-REGRESSION verification of _merge_files / _file_content_key.

_merge_files is a pure function — no DB needed. We exercise the prior-good
behaviors the content-dedup patch must NOT have broken.
"""

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils.container_workspace import _merge_files, _file_content_key


def _cw(path, sha, version=1):
    return {"workspace_path": path, "sha256": sha, "version": version}


def test_office_doc_preview_file_id_survives():
    """Office-doc descriptors carry preview_file_id; content-dedup must not strip
    it, and two versions of the same docx (different sha) must BOTH survive with
    distinct names."""
    v1 = {
        "id": "uuid-1",
        "name": "report.docx",
        "preview_file_id": "preview-1",
        "container_workspace": _cw("outputs/report.docx", "sha_A", 1),
    }
    v2 = {
        "id": "uuid-2",
        "name": "report (v2).docx",
        "preview_file_id": "preview-2",
        "container_workspace": _cw("outputs/report.docx", "sha_B", 2),
    }
    out = _merge_files([], [v1, v2])
    assert len(out) == 2, out
    assert out[0]["preview_file_id"] == "preview-1"
    assert out[1]["preview_file_id"] == "preview-2"
    names = {f["name"] for f in out}
    assert names == {"report.docx", "report (v2).docx"}, names


def test_non_container_user_uploads_never_dropped():
    """User uploads (id, no container_workspace) dedup by id only; no false
    content-collapse, and distinct ones all survive."""
    u1 = {"id": "user-a", "name": "a.png"}
    u2 = {"id": "user-b", "name": "b.png"}
    # Same id arriving twice dedups via id path.
    out = _merge_files([u1], [dict(u1), u2])
    assert [f["id"] for f in out] == ["user-a", "user-b"], out


def test_container_plus_user_both_render():
    """A container file + a user file sharing nothing both render."""
    user = {"id": "user-x", "name": "photo.jpg"}
    cont = {
        "id": "cont-1",
        "name": "out.csv",
        "container_workspace": _cw("outputs/out.csv", "sha_csv"),
    }
    out = _merge_files([], [user, cont])
    assert len(out) == 2
    assert [f["id"] for f in out] == ["user-x", "cont-1"]


def test_middleware_reread_idempotent_no_second_append():
    """Middleware re-read: after import, files already contain the merged set.
    Re-merging the same imported set must NOT append again (content-key + id both
    block it)."""
    cont = {
        "id": "cont-1",
        "name": "out.csv",
        "container_workspace": _cw("outputs/out.csv", "sha_csv"),
    }
    first = _merge_files([], [cont])
    # Re-run importer with a FRESH uuid for the same content (ledger-lost re-import).
    cont_reimport = {
        "id": "cont-FRESH-uuid",
        "name": "out.csv",
        "container_workspace": _cw("outputs/out.csv", "sha_csv"),
    }
    second = _merge_files(first, [cont_reimport])
    assert len(second) == 1, second
    # First occurrence preserved (stable) — original id, not the fresh one.
    assert second[0]["id"] == "cont-1", second


def test_order_preserved_first_occurrence_kept():
    """Stable: a later identical-content descriptor must not reorder/replace the
    existing card."""
    a = {"id": "a", "name": "first.txt", "container_workspace": _cw("outputs/x.txt", "sha_x")}
    b = {"id": "b", "name": "middle.txt"}
    c = {"id": "c", "name": "last.txt", "container_workspace": _cw("outputs/y.txt", "sha_y")}
    existing = _merge_files([], [a, b, c])
    # Later import of same content as `a` with a new id + different name.
    a_dup = {"id": "a-new", "name": "RENAMED.txt", "container_workspace": _cw("outputs/x.txt", "sha_x")}
    out = _merge_files(existing, [a_dup])
    assert [f["id"] for f in out] == ["a", "b", "c"], out
    assert out[0]["name"] == "first.txt", "first occurrence must be kept, not replaced"


def test_two_versions_same_path_different_content_both_survive():
    """Same workspace_path, DIFFERENT sha (a re-generated file) -> distinct
    content keys -> both survive."""
    v1 = {"id": "v1", "name": "chart.png", "container_workspace": _cw("outputs/chart.png", "sha1")}
    v2 = {"id": "v2", "name": "chart.png", "container_workspace": _cw("outputs/chart.png", "sha2")}
    out = _merge_files([v1], [v2])
    assert len(out) == 2, out


def test_content_key_shape():
    item = {"container_workspace": _cw("outputs/a.txt", "deadbeef")}
    assert _file_content_key(item) == "cw\x00outputs/a.txt\x00deadbeef"
    assert _file_content_key({"id": "x"}) is None
    assert _file_content_key({"container_workspace": {"workspace_path": "p"}}) is None  # no sha


def test_existing_non_list_existing_treated_empty():
    out = _merge_files(None, [{"id": "a"}])
    assert [f["id"] for f in out] == ["a"]


def test_intra_batch_content_dup_collapses_keeps_first():
    """Two fresh-uuid descriptors for the SAME (path, sha) within one batch
    collapse to ONE, keeping the first."""
    a = {"id": "u1", "name": "first.csv", "container_workspace": _cw("outputs/o.csv", "shaQ")}
    b = {"id": "u2", "name": "second.csv", "container_workspace": _cw("outputs/o.csv", "shaQ")}
    out = _merge_files([], [a, b])
    assert len(out) == 1 and out[0]["id"] == "u1", out


def test_container_descriptor_without_id_still_content_dedups():
    """A container descriptor lacking an id still dedups by content key (the
    real importer always mints an id, but ensure content path is independent)."""
    a = {"name": "x.csv", "container_workspace": _cw("outputs/o.csv", "shaR")}
    b = {"name": "x.csv", "container_workspace": _cw("outputs/o.csv", "shaR")}
    out = _merge_files([], [a, b])
    assert len(out) == 1, out


def test_user_upload_then_container_same_id_dedups_by_id():
    """If a container file coincidentally has the same id as an existing user
    upload, id-dedup still fires (no duplicate card)."""
    existing = [{"id": "shared", "name": "user.png"}]
    cont = {"id": "shared", "name": "cont.csv", "container_workspace": _cw("outputs/c.csv", "shaS")}
    out = _merge_files(existing, [cont])
    assert len(out) == 1 and out[0]["name"] == "user.png", out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
