"""orjson-backed JSON serializer for the Socket.IO / Engine.IO packet layer.

Every Socket.IO packet (every ``chat:delta`` token, every status/event emit) is
JSON-encoded inside ``sio.emit`` on the single event loop. python-socketio
defaults to the stdlib ``json`` module — the largest per-token on-loop CPU item
when many streams run concurrently. orjson encodes several times faster.

python-socketio and python-engineio both call ``self.json.dumps(data,
separators=(',', ':'))`` and ``self.json.loads(s)`` and accept a ``json=``
constructor argument that they install on both layers. orjson's API differs in
two ways this shim bridges:

* ``orjson.dumps`` returns ``bytes``; socketio does ``encoded += self.json.dumps(...)``
  on a ``str``, so we must ``.decode()``.
* ``orjson.dumps`` takes an ``option`` bitset, not ``separators``/``default``.
  orjson already emits compact output (no spaces) so ``separators`` is a no-op;
  we swallow it and any other kwargs.

Parity notes vs the stdlib json it replaces:
* ``OPT_NON_STR_KEYS`` reproduces stdlib's silent coercion of non-string dict
  keys (stdlib turns ``{1: 2}`` into ``{"1": 2}``; bare orjson would raise).
* ``default=str`` is a safety net: the payloads emitted today are already all
  JSON-native (the current stdlib encoder is called with no ``default`` and does
  not crash), so this only fires where the stdlib path would itself have raised
  — strictly more robust, never less. orjson handles datetime/UUID natively
  before ``default`` is consulted.
"""

import orjson

_DUMP_OPTS = orjson.OPT_NON_STR_KEYS


def dumps(obj, *args, **kwargs) -> str:
    # *args/**kwargs absorb socketio's separators= (and anything else) for
    # signature compatibility; orjson output is already compact.
    return orjson.dumps(obj, option=_DUMP_OPTS, default=str).decode("utf-8")


def loads(s, *args, **kwargs):
    return orjson.loads(s)


class OrjsonSerializer:
    """Module-like object with ``dumps``/``loads`` for ``AsyncServer(json=...)``."""

    dumps = staticmethod(dumps)
    loads = staticmethod(loads)


# Singleton handed to both AsyncServer constructors.
orjson_serializer = OrjsonSerializer()
