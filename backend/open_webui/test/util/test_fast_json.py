"""Parity tests for the fast_json drop-in shim (orjson-backed json replacement).

fast_json swaps stdlib json for orjson in hot modules. It MUST stay
behaviourally identical to stdlib json (modulo the deliberate compact-default
whitespace), or a swapped module silently corrupts output. These tests pin the
contract: round-trip equality, the NaN/Infinity fallback, indent/sort_keys/
separators handling, non-string keys, bytes input, and JSONDecodeError catching.

Run: python3 -m pytest open_webui/test/util/test_fast_json.py -q
"""

import json as stdlib

from open_webui.utils import fast_json as fj


def test_loads_basic_parity():
    for s in [
        '{"a":1}', "[1,2,3]", '"hi"', "123", "true", "null",
        '{"u":"héllo 世界 🚀"}', '  {"sp":1}  ', '{"nested":{"x":[1,{"y":2}]}}',
    ]:
        assert fj.loads(s) == stdlib.loads(s), s


def test_loads_accepts_bytes():
    assert fj.loads(b'{"a":1}') == {"a": 1}


def test_loads_nan_infinity_fallback():
    # orjson rejects these; stdlib accepts. fast_json must fall back, not raise.
    import math

    assert math.isnan(fj.loads("NaN"))
    assert fj.loads("Infinity") == float("inf")
    assert fj.loads("-Infinity") == float("-inf")
    assert math.isnan(fj.loads('{"x":NaN}')["x"])


def test_loads_invalid_raises_jsondecodeerror():
    try:
        fj.loads("{bad")
        assert False, "should have raised"
    except stdlib.JSONDecodeError:
        pass  # orjson's JSONDecodeError subclasses stdlib's


def test_dumps_returns_str():
    assert isinstance(fj.dumps({"a": 1}), str)


def test_dumps_compact_byte_exact_vs_stdlib_ascii():
    # For ASCII payloads, compact-separator output is byte-identical to stdlib.
    for obj in [
        {"a": 1}, [1, 2, 3], {"b": True, "n": None},
        {"nested": {"x": [1, 2]}}, {1: 2, 3: 4},
    ]:
        assert fj.dumps(obj, separators=(",", ":")) == stdlib.dumps(
            obj, separators=(",", ":")
        ), obj


def test_dumps_non_ascii_raw_utf8():
    # Deliberate divergence: orjson emits raw UTF-8, stdlib defaults to \\uXXXX
    # escapes. Both decode to the same string; raw UTF-8 is valid JSON and is
    # already the codebase's on-disk format (see OrjsonJSON column in internal/db.py).
    out = fj.dumps({"u": "世界🚀"}, separators=(",", ":"))
    assert out == '{"u":"世界🚀"}'
    assert stdlib.loads(out) == {"u": "世界🚀"}  # round-trips identically


def test_dumps_nan_infinity_emits_null():
    # Deliberate: orjson emits null (spec-valid) rather than stdlib's non-standard
    # NaN/Infinity literals. A pre-scan to preserve the literal cost more than the
    # serialization, so we accept null. See module docstring.
    assert fj.dumps({"x": float("nan")}) == '{"x":null}'
    assert fj.dumps({"x": float("inf")}) == '{"x":null}'
    assert fj.dumps({"a": [1, float("-inf")]}) == '{"a":[1,null]}'


def test_dumps_finite_floats_fast_path():
    assert stdlib.loads(fj.dumps({"x": 1.5, "y": 2.25})) == {"x": 1.5, "y": 2.25}


def test_dumps_indent2_byte_exact():
    assert fj.dumps({"a": 1, "b": [1, 2]}, indent=2) == stdlib.dumps(
        {"a": 1, "b": [1, 2]}, indent=2
    )


def test_dumps_indent4_falls_back():
    assert fj.dumps({"a": 1}, indent=4) == stdlib.dumps({"a": 1}, indent=4)


def test_dumps_sort_keys():
    assert fj.dumps({"b": 1, "a": 2}, sort_keys=True, separators=(",", ":")) == (
        stdlib.dumps({"b": 1, "a": 2}, sort_keys=True, separators=(",", ":"))
    )


def test_dumps_custom_separators_exact():
    assert fj.dumps({"a": 1, "b": 2}, separators=(", ", ": ")) == stdlib.dumps(
        {"a": 1, "b": 2}, separators=(", ", ": ")
    )


def test_dumps_default_callable():
    assert stdlib.loads(fj.dumps({"s": {1, 2}}, default=lambda o: sorted(o))) == {
        "s": [1, 2]
    }


def test_dumps_non_str_keys_coerced():
    # stdlib stringifies int keys; orjson needs OPT_NON_STR_KEYS (shim sets it).
    assert stdlib.loads(fj.dumps({1: 2}, separators=(",", ":"))) == {"1": 2}


def test_dump_load_roundtrip_filelike():
    import io

    buf = io.StringIO()
    fj.dump({"a": [1, 2, 3]}, buf)
    buf.seek(0)
    assert fj.load(buf) == {"a": [1, 2, 3]}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL PASSED")
