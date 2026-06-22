# Stream Runtime Benchmark

Use the synthetic stream benchmark to compare legacy delivery with the v2.1
capability path for token deltas, tool results, browser frames, replay, and
hidden-tab suppression.

```bash
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime
```

Useful knobs:

```bash
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime \
  --tokens 1000 \
  --tool-results 20 \
  --tool-result-chars 32768 \
  --browser-frames 30 \
  --browser-frame-chars 64000 \
  --visible-tabs 1 \
  --hidden-tabs 3
```

For machine-readable output:

```bash
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --json
```

Preset traffic profiles:

```bash
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --preset long_text
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --preset tool_heavy
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --preset browser_heavy
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --preset multi_tab
PYTHONPATH=backend python3 -m open_webui.scripts.benchmark_stream_runtime --preset all --json
```

The benchmark does not require a running server or model. It sets test-safe DB
bootstrap flags, patches Socket.IO emit calls in-process, and reports packet and
byte counts for both scenarios. The `v2.1` scenario should show fewer emitted
packets/bytes when latency-budgeted batching, compact batch2 frames, replay, and
hidden-tab suppression are working.

Related live-browser producer knobs:

- `STREAM_BROWSER_FRAME_MAX_FPS`: caps the browser live-frame poll cadence after
  the caller's interval is applied. Default `2.0`, matching the existing 0.5s
  poll cadence.
- `STREAM_BROWSER_FRAME_MAX_BYTES`: omits oversized live image payloads while
  still sending browser-frame metadata. Default `0` disables the byte cap.

Related stream runtime knobs:

- `STREAM_DELTA_BATCH_ENABLED`: enables v2.1 delta batching. Default `true`.
- `STREAM_DELTA_BATCH_WINDOW_MS`: tiny coalescing window after the first visible
  delta. Default `8`.
- `STREAM_DELTA_BATCH_MAX_DELAY_MS`: hard cap for batch delay. Default `32`.
- `STREAM_DELTA_FIRST_TOKEN_IMMEDIATE`: keeps latency to first token low by
  bypassing the batch window for the first delta. Default `true`.
- `STREAM_VERSION_STORE_FLUSH_EVERY`: controls how often the in-process stream
  version cursor is written back to the shared stream-version store. Default
  `64`; snapshots and terminal states force a flush.
- `STREAM_REPLAY_BUFFER_MAX_EVENTS`: maximum replay ring entries per message.
  Default `2048`.
- `STREAM_REPLAY_BUFFER_MAX_BYTES`: maximum replay ring bytes per message.
  Default `8388608`. Redis deployments track this incrementally with a side size
  list so appends do not scan the whole buffer.
- `STREAM_REPLAY_BUFFER_TTL_SECONDS`: replay key lifetime after append. Default
  `900`.
- `STREAM_CLIENT_LAG_MAX_VERSIONS`: max unacked version gap before the server
  tells that tab to replay/snapshot instead of streaming every intermediate
  delta. Default `512`.
- `STREAM_CLIENT_ACK_INTERVAL_MS`: preferred client ack cadence sent during
  stream subscription. Default `250`.
- `STREAM_DB_CHECKPOINT_POLICY`: `periodic` or `final_only`. The legacy
  `DISABLE_STREAM_SNAPSHOT_DB_WRITES` env flag maps to `final_only`.
- `STREAM_DB_CHECKPOINT_INTERVAL_SECONDS` and `STREAM_DB_CHECKPOINT_CHAR_DELTA`:
  coarse DB checkpoint cadence for active v2.1 streams. Defaults `2.0` seconds and
  `16384` chars.
- `STREAM_TOOL_RESULT_BODY_MAX_BYTES` and
  `STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE`: live full tool-result body
  cache caps. Evicted bodies spill to `STREAM_TOOL_RESULT_BODY_SPILL_DIR`.
