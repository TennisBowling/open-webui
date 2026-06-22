#!/usr/bin/env python3
"""Synthetic benchmark for the stream v2.1 runtime.

This does not call a model or require a running server. It drives the real
``open_webui.socket.main.emit_to_primary`` path with fake Socket.IO targets and
reports how many packets/bytes would be emitted for representative stream
traffic. The default run compares:

* ``legacy``: subscribers do not advertise v2.1 capabilities and every tab is
  treated as visible.
* ``v2.1``: visible tabs advertise compact batches/replay/acks and hidden tabs
  are marked hidden, so live token/tool/browser payloads are suppressed for them.

The benchmark is intentionally deterministic and suitable for quick regression
checks while tuning batching, compact frames, replay, visibility, and
backpressure behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


def _prepare_import_env() -> None:
    # Keep this benchmark self-contained. socket.main imports config/model modules
    # that normally bind the DB at import time; benchmark traffic does not need DB.
    os.environ.setdefault("OPEN_WEBUI_SKIP_MIGRATIONS", "true")
    os.environ.setdefault("OPEN_WEBUI_SKIP_CONFIG_DB_LOAD", "true")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://bench:bench@127.0.0.1:5432/openwebui_bench"
    )
    if os.environ.get("STREAM_BENCH_USE_REDIS", "false").lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        os.environ["WEBSOCKET_MANAGER"] = ""
        os.environ["WEBSOCKET_REDIS_URL"] = ""
    os.environ.setdefault("STREAM_RUNTIME_METRICS", "true")


_prepare_import_env()

from open_webui.socket import main as socket_main  # noqa: E402


PRESETS: dict[str, dict[str, int]] = {
    "default": {},
    "long_text": {
        "tokens": 2000,
        "token_chars": 6,
        "tool_results": 2,
        "tool_result_chars": 4096,
        "browser_frames": 0,
        "browser_frame_chars": 0,
        "visible_tabs": 1,
        "hidden_tabs": 2,
    },
    "tool_heavy": {
        "tokens": 300,
        "token_chars": 8,
        "tool_results": 40,
        "tool_result_chars": 32768,
        "browser_frames": 0,
        "browser_frame_chars": 0,
        "visible_tabs": 1,
        "hidden_tabs": 2,
    },
    "browser_heavy": {
        "tokens": 120,
        "token_chars": 8,
        "tool_results": 2,
        "tool_result_chars": 4096,
        "browser_frames": 40,
        "browser_frame_chars": 64000,
        "visible_tabs": 1,
        "hidden_tabs": 2,
    },
    "multi_tab": {
        "tokens": 600,
        "token_chars": 8,
        "tool_results": 8,
        "tool_result_chars": 16384,
        "browser_frames": 12,
        "browser_frame_chars": 48000,
        "visible_tabs": 1,
        "hidden_tabs": 5,
    },
}


@dataclass
class EmitStats:
    packets: int = 0
    bytes: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    bytes_by_type: Counter[str] = field(default_factory=Counter)
    by_target: Counter[str] = field(default_factory=Counter)

    def record(self, target: str | None, payload: dict[str, Any]) -> None:
        event_type = (payload.get("data") or {}).get("type") or "unknown"
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8", "replace"))
        self.packets += 1
        self.bytes += size
        self.by_type[event_type] += 1
        self.bytes_by_type[event_type] += size
        self.by_target[target or "<none>"] += 1

    def to_json(self) -> dict[str, Any]:
        return {
            "packets": self.packets,
            "bytes": self.bytes,
            "by_type": dict(sorted(self.by_type.items())),
            "bytes_by_type": dict(sorted(self.bytes_by_type.items())),
            "by_target": dict(sorted(self.by_target.items())),
        }


def _reset_socket_state() -> None:
    for name in (
        "STREAM_MESSAGE_TO_CHAT",
        "STREAM_ACTIVE_BY_CHAT",
        "STREAM_SUBSCRIPTION_STATE",
        "STREAM_CLIENT_ACKS",
        "STREAM_SYNC_REQUIRED_SENT",
        "STREAM_REPLAY_BUFFERS",
        "STREAM_REPLAY_BUFFER_BYTES",
        "STREAM_FIRST_DELTA_SENT",
        "TOOL_RESULT_BODIES",
        "TOOL_RESULT_BODY_SIZES",
        "TOOL_RESULT_BODY_ORDER",
        "TOOL_RESULT_BODY_SPILLS",
        "STREAM_METRICS",
        "_pending_delta_buffer",
        "_pending_delta_buffer_sizes",
        "_pending_delta_scheduled",
    ):
        obj = getattr(socket_main, name, None)
        if hasattr(obj, "clear"):
            obj.clear()
    socket_main.TOOL_RESULT_BODY_TOTAL_BYTES = 0
    for store_name in ("STREAM_VERSION", "TOOL_RESULTS", "STREAM_STATE"):
        store = getattr(socket_main, store_name, None)
        if hasattr(store, "clear"):
            store.clear()


def _subscriber_state(
    *,
    visible_count: int,
    hidden_count: int,
    compact: bool,
    track_visibility: bool,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    sids: list[str] = []
    states: dict[str, dict[str, Any]] = {}
    caps = {"compact_batch": compact, "replay": compact, "ack": compact, "visibility": track_visibility}

    for idx in range(visible_count):
        sid = f"visible-{idx}"
        sids.append(sid)
        states[sid] = {"visible": True, "capabilities": dict(caps), "updated_at": time.time()}

    for idx in range(hidden_count):
        sid = f"hidden-{idx}"
        sids.append(sid)
        # Legacy baseline intentionally treats hidden tabs as visible: old clients
        # did not report visibility, so they received all live payloads.
        states[sid] = {
            "visible": False if track_visibility else True,
            "capabilities": dict(caps),
            "updated_at": time.time(),
        }

    return sids, states


def _delta_payload(chat_id: str, message_id: str, session_id: str, version: int, text: str) -> dict:
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "session_id": session_id,
        "data": {
            "type": "chat:delta",
            "data": {
                "message_id": message_id,
                "version": version,
                "op": "text_append",
                "payload": {"block_idx": 0, "text": text},
            },
        },
    }


def _tool_result_payload(chat_id: str, message_id: str, session_id: str, idx: int, body_size: int) -> dict:
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "session_id": session_id,
        "data": {
            "type": "tool_call:result",
            "data": {
                "message_id": message_id,
                "tool_call_id": f"tool-{idx}",
                "result": "R" * body_size,
                "size": body_size,
            },
        },
    }


def _browser_frame_payload(chat_id: str, message_id: str, session_id: str, idx: int, frame_size: int) -> dict:
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "session_id": session_id,
        "data": {
            "type": "browser:frame",
            "data": {
                "message_id": message_id,
                "session": "main",
                "url": "https://example.com",
                "phase": "navigating" if idx == 0 else "loaded",
                "frame": "data:image/jpeg;base64," + ("A" * frame_size),
                "done": False,
            },
        },
    }


async def run_scenario(
    name: str,
    *,
    tokens: int,
    token_chars: int,
    tool_results: int,
    tool_result_chars: int,
    browser_frames: int,
    browser_frame_chars: int,
    visible_tabs: int,
    hidden_tabs: int,
    compact: bool,
    track_visibility: bool,
) -> dict[str, Any]:
    _reset_socket_state()
    socket_main.STREAM_RUNTIME_METRICS = True

    user_id = "bench-user"
    chat_id = f"bench-chat-{name}"
    message_id = f"bench-message-{name}"
    origin_sid = "visible-0"
    sids, subscription_state = _subscriber_state(
        visible_count=visible_tabs,
        hidden_count=hidden_tabs,
        compact=compact,
        track_visibility=track_visibility,
    )
    socket_main.STREAM_SUBSCRIPTION_STATE[chat_id] = subscription_state
    socket_main.stream_version_init(
        message_id,
        chat_id=chat_id,
        user_id=user_id,
        session_id=origin_sid,
        content_blocks=[],
    )

    stats = EmitStats()
    original_emit = socket_main.sio.emit
    original_participants = socket_main.get_session_ids_from_room

    async def fake_emit(_event, payload, to=None):
        stats.record(to, payload)

    def fake_participants(_room):
        return list(sids)

    socket_main.sio.emit = fake_emit
    socket_main.get_session_ids_from_room = fake_participants
    try:
        for idx in range(tokens):
            version = socket_main.stream_version_incr(message_id)
            await socket_main.emit_to_primary(
                user_id,
                _delta_payload(chat_id, message_id, origin_sid, version, "t" * token_chars),
            )

        for idx in range(tool_results):
            await socket_main.emit_to_primary(
                user_id,
                _tool_result_payload(chat_id, message_id, origin_sid, idx, tool_result_chars),
            )

        for idx in range(browser_frames):
            await socket_main.emit_to_primary(
                user_id,
                _browser_frame_payload(chat_id, message_id, origin_sid, idx, browser_frame_chars),
            )

        await socket_main._flush_delta_buffers_for_payload(user_id, {"chat_id": chat_id})
    finally:
        socket_main.sio.emit = original_emit
        socket_main.get_session_ids_from_room = original_participants

    replay_events = len(socket_main.STREAM_REPLAY_BUFFERS.get(message_id, []))
    replay_bytes = int(socket_main.STREAM_REPLAY_BUFFER_BYTES.get(message_id, 0) or 0)
    metrics = socket_main.get_stream_runtime_metrics()
    hidden_targets = [sid for sid in sids if sid.startswith("hidden-")]
    hidden_packets = sum(stats.by_target.get(sid, 0) for sid in hidden_targets)

    return {
        "name": name,
        "compact": compact,
        "track_visibility": track_visibility,
        "visible_tabs": visible_tabs,
        "hidden_tabs": hidden_tabs,
        "emits": stats.to_json(),
        "hidden_packets": hidden_packets,
        "replay_events": replay_events,
        "replay_bytes": replay_bytes,
        "metrics": metrics,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    legacy = await run_scenario(
        "legacy",
        tokens=args.tokens,
        token_chars=args.token_chars,
        tool_results=args.tool_results,
        tool_result_chars=args.tool_result_chars,
        browser_frames=args.browser_frames,
        browser_frame_chars=args.browser_frame_chars,
        visible_tabs=args.visible_tabs,
        hidden_tabs=args.hidden_tabs,
        compact=False,
        track_visibility=False,
    )
    optimized = await run_scenario(
        "v2.1",
        tokens=args.tokens,
        token_chars=args.token_chars,
        tool_results=args.tool_results,
        tool_result_chars=args.tool_result_chars,
        browser_frames=args.browser_frames,
        browser_frame_chars=args.browser_frame_chars,
        visible_tabs=args.visible_tabs,
        hidden_tabs=args.hidden_tabs,
        compact=True,
        track_visibility=True,
    )

    legacy_bytes = legacy["emits"]["bytes"]
    optimized_bytes = optimized["emits"]["bytes"]
    legacy_packets = legacy["emits"]["packets"]
    optimized_packets = optimized["emits"]["packets"]
    return {
        "input": vars(args),
        "scenarios": {"legacy": legacy, "v2.1": optimized},
        "savings": {
            "bytes": legacy_bytes - optimized_bytes,
            "bytes_pct": ((legacy_bytes - optimized_bytes) / legacy_bytes * 100) if legacy_bytes else 0,
            "packets": legacy_packets - optimized_packets,
            "packets_pct": ((legacy_packets - optimized_packets) / legacy_packets * 100)
            if legacy_packets
            else 0,
        },
    }


def _args_for_preset(args: argparse.Namespace, preset: str) -> argparse.Namespace:
    next_args = argparse.Namespace(**vars(args))
    for key, value in PRESETS.get(preset, {}).items():
        setattr(next_args, key, value)
    next_args.preset = preset
    return next_args


async def run_benchmark_suite(args: argparse.Namespace) -> dict[str, Any]:
    preset = getattr(args, "preset", "default") or "default"
    if preset != "all":
        return await run_benchmark(_args_for_preset(args, preset))

    results = {}
    for name in ("default", "long_text", "tool_heavy", "browser_heavy", "multi_tab"):
        results[name] = await run_benchmark(_args_for_preset(args, name))
    return {"preset": "all", "benchmarks": results}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preset",
        choices=[*PRESETS.keys(), "all"],
        default="default",
        help="Synthetic traffic profile to run. Explicit numeric flags are ignored by named presets.",
    )
    parser.add_argument("--tokens", type=int, default=300)
    parser.add_argument("--token-chars", type=int, default=8)
    parser.add_argument("--tool-results", type=int, default=8)
    parser.add_argument("--tool-result-chars", type=int, default=16_384)
    parser.add_argument("--browser-frames", type=int, default=12)
    parser.add_argument("--browser-frame-chars", type=int, default=48_000)
    parser.add_argument("--visible-tabs", type=int, default=1)
    parser.add_argument("--hidden-tabs", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def print_human(result: dict[str, Any]) -> None:
    if "benchmarks" in result:
        names = list(result["benchmarks"].keys())
        for name, item in result["benchmarks"].items():
            print_human(item)
            if name != names[-1]:
                print()
        return

    legacy = result["scenarios"]["legacy"]
    optimized = result["scenarios"]["v2.1"]
    savings = result["savings"]
    title = f"Stream runtime benchmark ({result.get('input', {}).get('preset', 'custom')})"
    print(title)
    print("=" * len(title))
    for scenario in (legacy, optimized):
        emits = scenario["emits"]
        print(
            f"{scenario['name']:>7}: packets={emits['packets']:,} bytes={emits['bytes']:,} "
            f"hidden_packets={scenario['hidden_packets']:,} replay_events={scenario['replay_events']:,}"
        )
        print(f"         by_type={emits['by_type']}")
    print(
        f"savings: packets={savings['packets']:,} ({savings['packets_pct']:.1f}%) "
        f"bytes={savings['bytes']:,} ({savings['bytes_pct']:.1f}%)"
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = asyncio.run(run_benchmark_suite(args))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
