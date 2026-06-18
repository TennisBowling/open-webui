"""Regression tests for the orjson Socket.IO serializer shim (perf change A2).

The shim swaps python-socketio's stdlib-json encoder for orjson on the per-token
emit hot path. These tests pin the behaviours socketio/engineio depend on, so a
future orjson upgrade or refactor can't silently break the wire format:

* dumps() returns str (socketio concatenates onto a str)
* dumps() swallows the separators= kwarg socketio passes
* output is compact and byte-identical to stdlib for ASCII payloads
* non-string dict keys are coerced to strings (stdlib parity)
* a representative chat:delta round-trips through a real socketio Packet,
  unicode intact.

Run: python3 -m pytest open_webui/test/util/test_socket_serializer.py -q
"""

import json as stdjson
import datetime

import socketio.packet as P

from open_webui.socket.serializer import orjson_serializer, dumps, loads


def test_dumps_returns_str_and_ignores_separators():
    out = dumps({"a": 1, "b": [1, 2, 3]}, separators=(",", ":"))
    assert isinstance(out, str)
    # compact, matches stdlib's compact form exactly for ASCII
    assert out == stdjson.dumps({"a": 1, "b": [1, 2, 3]}, separators=(",", ":"))


def test_non_str_keys_coerced_like_stdlib():
    # stdlib turns {1: 2} into {"1": 2}; bare orjson would raise without the
    # OPT_NON_STR_KEYS option the shim sets.
    assert loads(dumps({1: 2})) == {"1": 2}


def test_datetime_serialized_natively():
    d = datetime.datetime(2026, 6, 14, 12, 0, 0)
    assert loads(dumps({"t": d})) == {"t": "2026-06-14T12:00:00"}


def test_socketio_packet_roundtrip_unicode():
    P.Packet.json = orjson_serializer
    try:
        payload = {
            "type": "chat:delta",
            "data": {"op": "text_append", "v": 5, "text": "héllo 世界 🚀"},
        }
        enc = P.Packet(packet_type=2, data=["events", payload]).encode()
        assert isinstance(enc, str)
        decoded = P.Packet(encoded_packet=enc)
        assert decoded.data == ["events", payload]
    finally:
        P.Packet.json = stdjson


def test_ascii_wire_identical_to_stdlib():
    data = ["events", {"op": "x", "v": 1, "done": False}]

    P.Packet.json = stdjson
    enc_std = P.Packet(packet_type=2, data=data).encode()

    P.Packet.json = orjson_serializer
    try:
        enc_or = P.Packet(packet_type=2, data=data).encode()
    finally:
        P.Packet.json = stdjson

    assert enc_std == enc_or


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL PASSED")
