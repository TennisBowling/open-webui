"""Drop-in orjson-backed replacement for the stdlib ``json`` module.

Use by swapping the import in a hot module::

    from open_webui.utils import fast_json as json

...then ``json.dumps`` / ``json.loads`` / ``json.dump`` / ``json.load`` /
``json.JSONDecodeError`` all keep working, but the fast path uses orjson.

Design goal: **behaviourally identical to stdlib json**, so swapping the import
can never change output or raise where stdlib wouldn't. orjson is faster but
stricter and less flexible; wherever it can't reproduce stdlib semantics we
transparently fall back to stdlib. Concretely:

dumps:
* Returns ``str`` (orjson returns ``bytes`` — we ``.decode()``).
* orjson is ALWAYS compact (``,``/``:``); stdlib defaults to ``, ``/``: ``. To
  stay byte-identical to the stdlib default we only take the orjson fast path
  when the caller's ``separators`` resolve to compact, OR when the output is
  going to be re-parsed anyway (callers that care about exact bytes — hashing,
  cache keys — must NOT use this module; we don't swap those sites). We DEFAULT
  to matching stdlib: if the caller relies on the spaced default, we fall back.
* Translates ``sort_keys`` and ``indent==2`` to orjson options. Any other
  ``indent`` (0, 4, tab, …), ``cls=``, or an unsupported combination → stdlib.
* ``default=`` is passed through to orjson; if orjson still raises TypeError
  (it calls ``default`` for fewer types than stdlib), we fall back to stdlib.
* **NaN/Infinity in ``dumps`` are emitted as ``null``** (orjson behaviour), NOT
  as stdlib's non-standard ``NaN``/``Infinity`` literals. This is a deliberate
  choice: ``null`` is spec-valid JSON (the ``NaN`` literal is not, and breaks
  strict parsers), non-finite floats essentially never appear in the chat/API
  payloads this module serializes, and a pre-scan to detect them costs MORE than
  the serialization it would guard (it erased the entire orjson speedup). If you
  need the stdlib NaN literal for a specific call, use stdlib ``json`` there.

loads:
* Accepts ``str``/``bytes``/``bytearray`` like stdlib.
* orjson rejects ``NaN``/``Infinity``/``-Infinity`` which stdlib accepts → on
  any orjson decode error we fall back to stdlib, so we never reject input
  stdlib would have parsed. ``JSONDecodeError`` is re-exported and orjson's
  subclasses it, so existing ``except json.JSONDecodeError`` keeps catching.

This module is intentionally NOT used for: migrations (run-once, high blast
radius), crypto/auth payloads, vector-DB query-string building (byte-sensitive),
or anywhere a json string is hashed / used as a cache key / compared for
equality. Keep those on stdlib ``json``.
"""

import json as _stdlib_json
import orjson

# Re-export the stdlib names so this module is a structural drop-in.
JSONDecodeError = _stdlib_json.JSONDecodeError
JSONEncoder = _stdlib_json.JSONEncoder
JSONDecoder = _stdlib_json.JSONDecoder



def loads(s, **kwargs):
    """Parse JSON. Fast path via orjson; falls back to stdlib for anything
    orjson rejects that stdlib would accept (NaN/Infinity) or for any caller
    kwargs orjson doesn't support (object_hook, parse_float, …)."""
    # Any decoder kwargs (object_hook, parse_int, cls, …) → stdlib owns those.
    if kwargs:
        return _stdlib_json.loads(s, **kwargs)
    try:
        return orjson.loads(s)
    except orjson.JSONDecodeError:
        # orjson is stricter (e.g. NaN/Infinity). Defer to stdlib so we never
        # reject input the stdlib path would have parsed. If the input is truly
        # invalid, stdlib raises json.JSONDecodeError — same type callers catch.
        return _stdlib_json.loads(s, **kwargs)
    except (TypeError, ValueError):
        return _stdlib_json.loads(s, **kwargs)


def dumps(obj, **kwargs):
    """Serialize to a ``str``. Fast path via orjson; falls back to stdlib only
    for options orjson can't reproduce (custom ``cls``, non-compact
    ``separators``, ``indent`` other than 2)."""
    # Custom encoder class → only stdlib can honor it.
    if "cls" in kwargs:
        return _stdlib_json.dumps(obj, **kwargs)

    # orjson is ALWAYS compact (","/":"). If the caller passed separators, take
    # the orjson fast path only when they ARE the compact pair; any other
    # separators (e.g. the spaced ", "/": ") must go to stdlib to stay exact.
    if "separators" in kwargs:
        if kwargs["separators"] != (",", ":"):
            return _stdlib_json.dumps(obj, **kwargs)

    option = 0
    if kwargs.get("sort_keys"):
        option |= orjson.OPT_SORT_KEYS

    indent = kwargs.get("indent", None)
    if indent is not None:
        # orjson only supports 2-space indentation. Anything else → stdlib.
        if indent == 2:
            option |= orjson.OPT_INDENT_2
        else:
            return _stdlib_json.dumps(obj, **kwargs)

    # Permit orjson's non-str key coercion to mirror stdlib (which stringifies
    # int/float/bool/None keys). Without this orjson raises on {1: 2}.
    option |= orjson.OPT_NON_STR_KEYS

    default = kwargs.get("default", None)
    try:
        return orjson.dumps(obj, default=default, option=option).decode("utf-8")
    except (TypeError, ValueError):
        # orjson calls `default` for fewer types than stdlib. Fall back so a
        # value stdlib could serialize (via its own default handling) still works.
        return _stdlib_json.dumps(obj, **kwargs)


def dump(obj, fp, **kwargs):
    """Serialize ``obj`` and write to file-like ``fp`` (text mode)."""
    fp.write(dumps(obj, **kwargs))


def load(fp, **kwargs):
    """Read and parse JSON from file-like ``fp``."""
    return loads(fp.read(), **kwargs)
