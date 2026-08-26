"""Unit tests for the NUL-safe JSON helpers in ``open_webui.internal.db``.

Postgres ``text``/``jsonb`` columns cannot store a raw NUL (0x00): ``jsonb``
rejects the ``\\u0000`` escape (the only legal JSON encoding of a 0x00 byte) and
``text`` rejects the raw byte. ``dumps_jsonb`` / ``strip_nul`` are the single
source of truth used both for the raw-SQL jsonb binds in ``models/chats.py`` and
as the async engine's ``json_serializer`` for every ORM ``Column(JSON)`` write.

These tests pin the behaviour that the production bug needed — including the two
gaps the previous implementation had: NUL in dict KEYS and NUL inside tuples.
"""

import json

from open_webui.internal.db import strip_nul, dumps_jsonb

NUL = chr(0)


def _clean(serialized: str) -> bool:
    """A serialized jsonb bind is safe iff it has neither a raw 0x00 byte nor the
    single-backslash ``\\u0000`` escape that Postgres jsonb rejects."""
    return NUL not in serialized and "\\u0000" not in serialized


def test_nul_in_nested_value_stripped():
    # The exact shape from the bug report: meta.error.content carrying a NUL.
    v = {"done": False, "error": {"content": "boom" + NUL + "bar"}}
    out = dumps_jsonb(v)
    assert _clean(out)
    assert json.loads(out)["error"]["content"] == "boombar"


def test_nul_in_list_element_stripped():
    out = dumps_jsonb({"results": ["a" + NUL, "b"]})
    assert _clean(out)
    assert json.loads(out)["results"] == ["a", "b"]


def test_nul_in_dict_key_stripped():
    # Regression: the previous _strip_nul_value left dict KEYS untouched, so a NUL
    # in a key re-emitted the jsonb-illegal escape and the write still failed.
    out = dumps_jsonb({"k" + NUL + "ey": "v"})
    assert _clean(out)
    assert json.loads(out) == {"key": "v"}


def test_nul_in_tuple_element_stripped():
    # Regression: tuples fell through with no branch; json.dumps serializes them as
    # arrays, so a NUL inside a tuple element survived.
    out = dumps_jsonb({"t": ("x" + NUL + "y", "z")})
    assert _clean(out)
    assert json.loads(out)["t"] == ["x" + "y", "z"]


def test_deeply_nested_mixed_containers():
    out = dumps_jsonb({"a": [{"b" + NUL: ("c" + NUL,)}]})
    assert _clean(out)
    assert json.loads(out) == {"a": [{"b": ["c"]}]}


def test_clean_value_is_byte_identical_to_plain_dumps():
    # Fast path: no NUL present -> identical to json.dumps, no extra work observable.
    v = {"hello": "world", "n": [1, 2, 3], "nested": {"x": True}}
    assert dumps_jsonb(v) == json.dumps(v)


def test_genuine_backslash_u0000_literal_is_preserved():
    # A user pasting the 6 literal characters \\u0000 (a real backslash, not a NUL
    # byte) must NOT be corrupted: json.dumps escapes the backslash to \\\\u0000,
    # which is valid jsonb. We must not blind-replace the escape substring.
    v = {"note": "see \\u0000 here"}  # the value contains backslash-u-0-0-0-0
    out = dumps_jsonb(v)
    assert NUL not in out
    # Round-trips back to the exact original text.
    assert json.loads(out)["note"] == "see \\u0000 here"


def test_strip_nul_leaves_non_string_scalars_untouched():
    assert strip_nul(5) == 5
    assert strip_nul(True) is True
    assert strip_nul(None) is None
    assert strip_nul(3.14) == 3.14


def test_strip_nul_no_nul_returns_equivalent():
    v = {"a": ["b", {"c": "d"}]}
    assert strip_nul(v) == v
