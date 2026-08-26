"""ADV3 — NO-UNRELATED-REGRESSION invariant (backend half).

Owns: the socket {type:"files"} handler + import merge path must NOT have
broken NON-container file handling. These descriptors serve MCP image
attachments, OpenAPI/tool_result_files, web_search files, and user uploads.

The fix added a content-key dedup ON TOP of id-dedup in cw._merge_files. The
content key is ONLY derived from item["container_workspace"]. Non-container
files have NO container_workspace -> content_key is None -> id-only path. The
regression we hunt: a non-container file being content-collapsed (wrongly
deduped) or dropped.

REAL non-container shapes (from middleware.py, not invented):
  - MCP image result  (line ~1225): {"type": "image", "url": <file_url>}   NO id
  - OpenAPI data-uri  (line ~1249): {"type": "data",  "content": "data:..."} NO id, NO url
  - user upload / web_search file: {"type":"file","id":<uuid>,"url":...,"name":...}

Compared against the OLD backend socket handler, which was a blind
  files = incoming; files.extend(existing)
(incoming-first, NO dedup). Parity requirement: NEVER fewer non-container files
than the old extend, NEVER a content-collapse of two genuinely-distinct
non-container files.

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest test/util/test_adv3_noncontainer.py -x -q
"""

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402

merge = cw._merge_files
key = cw._file_content_key


# ---- REAL non-container descriptor factories (mirror middleware.py) ----------
def mcp_image(url):
    # middleware.py ~1225: no id, no name, no container_workspace.
    return {"type": "image", "url": url}


def openapi_datauri(content):
    # middleware.py ~1249: no id, no url — only a data: content string.
    return {"type": "data", "content": content}


def user_upload(file_id, name="report.pdf"):
    return {"type": "file", "id": file_id, "url": f"/api/v1/files/{file_id}", "name": name}


# --- 1: NO non-container item ever produces a content-key --------------------
def test_noncontainer_never_has_content_key():
    assert key(mcp_image("https://cdn/a.png")) is None
    assert key(openapi_datauri("data:text/csv;base64,YWJj")) is None
    assert key(user_upload("u-1")) is None
    # cw present but not a dict -> None (defensive)
    assert key({"id": "x", "container_workspace": "nope"}) is None
    # cw dict missing sha -> None (id-only)
    assert key({"id": "x", "container_workspace": {"workspace_path": "outputs/q"}}) is None


# --- 2: two DISTINCT no-id MCP images both survive (no content-collapse) -----
def test_two_distinct_noid_mcp_images_accumulate():
    a = mcp_image("https://cdn/a.png")
    b = mcp_image("https://cdn/b.png")
    out = merge([], [a, b])
    assert out == [a, b], out  # both kept; never collapsed (content_key is None)
    assert len(out) == 2


# --- 3: no-id OpenAPI data-uri files: distinct contents both survive --------
def test_distinct_openapi_datauris_accumulate():
    a = openapi_datauri("data:text/csv;base64,AAAA")
    b = openapi_datauri("data:text/csv;base64,BBBB")
    out = merge([], [a, b])
    assert len(out) == 2, out
    assert out == [a, b]


# --- 4: user uploads dedup by id (true repeat) but distinct ids accumulate ---
def test_user_uploads_id_dedup_and_accumulate():
    u1 = user_upload("u-1", "a.pdf")
    u2 = user_upload("u-2", "b.pdf")
    # distinct ids accumulate
    out = merge([], [u1, u2])
    assert [f["id"] for f in out] == ["u-1", "u-2"], out
    # true repeat (same id) dedups
    out2 = merge([u1], [dict(u1)])
    assert len(out2) == 1, out2
    # same id within one incoming batch dedups
    out3 = merge([], [u1, dict(u1)])
    assert len(out3) == 1, out3


# --- 5: PARITY vs OLD blind extend — never FEWER files for non-container ------
#     Old socket handler: files=incoming; files.extend(existing) -> no dedup,
#     incoming-first. New _merge_files: existing-first + id/content dedup.
#     For NON-container (no content key, possibly no id), the only thing that
#     can reduce count is an id collision — which the old extend did NOT dedup.
#     So we must verify: distinct non-container files are NEVER content-collapsed,
#     and the only drops are exact id repeats (a strict IMPROVEMENT, not a
#     regression that loses a real file).
def test_parity_no_real_file_lost_vs_old_extend():
    existing = [mcp_image("https://cdn/x.png"), user_upload("u-9")]
    incoming = [
        mcp_image("https://cdn/y.png"),       # new distinct no-id image
        openapi_datauri("data:application/pdf;base64,ZZ"),  # new no-id datauri
        user_upload("u-9"),                   # TRUE repeat by id -> drop ok
        user_upload("u-10"),                  # new distinct upload
    ]

    def old_extend(existing, incoming):
        files = list(incoming)
        files.extend(existing)
        return files

    old = old_extend(existing, incoming)
    new = merge(existing, incoming)

    # Every distinct (non-id-repeat) file the old path kept must still be present
    # in the new path. The only legitimately-dropped item is the u-9 id repeat.
    # Build a multiset-ish identity for comparison that does NOT collapse no-id
    # files (so a real content-collapse bug would show up as a missing item).
    def ident(f):
        return (f.get("id"), f.get("url"), f.get("content"), f.get("type"))

    old_idents = [ident(f) for f in old]
    new_idents = [ident(f) for f in new]
    # u-9 appears twice in old (existing + incoming repeat); new keeps it once.
    # Every OTHER old ident must be present in new.
    from collections import Counter

    old_c = Counter(old_idents)
    new_c = Counter(new_idents)
    u9_ident = ident(user_upload("u-9"))
    for k, n in old_c.items():
        if k == u9_ident:
            assert new_c[k] == 1, f"u-9 should be deduped to exactly 1, got {new_c[k]}"
            continue
        assert new_c[k] >= n, f"non-container file lost in new merge: {k}"
    # And no two distinct no-id files were collapsed: the two new images +
    # the datauri must all be present.
    assert ident(mcp_image("https://cdn/y.png")) in new_idents
    assert ident(openapi_datauri("data:application/pdf;base64,ZZ")) in new_idents
    assert ident(user_upload("u-10")) in new_idents


# --- 6: mixed container + non-container in one batch: container content-dedups
#     while non-container never does. Cross-contamination guard.
def test_mixed_no_cross_contamination():
    img = mcp_image("https://cdn/a.png")
    upload = user_upload("u-1")
    cont = {
        "type": "file",
        "id": "file-c1",
        "name": "out.csv",
        "container_workspace": {"workspace_path": "outputs/out.csv", "sha256": "a" * 64},
    }
    cont_reimport = {  # same content, fresh id (importer re-ran)
        "type": "file",
        "id": "file-c2",
        "name": "out.csv",
        "container_workspace": {"workspace_path": "outputs/out.csv", "sha256": "a" * 64},
    }
    out = merge([], [img, upload, cont, cont_reimport])
    ids = [f.get("id") for f in out]
    # img (no id), upload u-1, container c1; c2 collapses into c1 by content key.
    assert ids == [None, "u-1", "file-c1"], ids
    assert len(out) == 3


# --- 7: a non-container file whose URL happens to EQUAL a container file's
#     content-key string must not collide. content_key is "cw\x00..\x00.." which
#     can never equal a plain url; and a non-container url is never put in
#     seen_content. Explicitly guard the NUL-prefixed key isolation.
def test_url_cannot_collide_with_content_key():
    weird_url = "cw\x00outputs/out.csv\x00" + "a" * 64  # literally the cw key string
    noncont = {"type": "image", "url": weird_url}  # no container_workspace
    cont = {
        "type": "file",
        "id": "fc",
        "container_workspace": {"workspace_path": "outputs/out.csv", "sha256": "a" * 64},
    }
    # The container content key equals the weird url string.
    assert key(cont) == weird_url
    out = merge([], [noncont, cont])
    # Both kept: the non-container item NEVER consults seen_content (its
    # content_key is None), so the string-equality is irrelevant. No collapse.
    assert len(out) == 2, out
    assert out[0] is noncont and out[1] is cont


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
