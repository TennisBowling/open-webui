"""Event-loop lag monitor (opt-in profiling).

A single asyncio event loop can only run one Python coroutine step at a time.
When the loop is saturated — e.g. many concurrent streaming/subagent pipelines
all doing CPU work between awaits — every *other* coroutine, including socket
delta delivery, is delayed. That delay is "event-loop lag": the gap between
when a timer was due to fire and when the loop actually got around to it.

This monitor schedules a cheap wakeup every ``interval`` seconds and records how
late it actually fired. It logs max + p95 + mean stall (ms) over a rolling
window so you can quantify saturation and prove whether a change (uvloop,
orjson, offloading blocking work) reduced it.

Entirely opt-in via ``PROFILE_LOOP_LAG=1`` — nothing imports or starts this
unless the flag is set, so there is zero overhead by default.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)


async def loop_lag_monitor(
    interval: float = 0.05,
    window_seconds: float = 10.0,
) -> None:
    """Sample event-loop scheduling drift forever; log a summary per window.

    ``interval`` is how often we wake (smaller = finer resolution, marginally
    more overhead). ``window_seconds`` is how often we emit an aggregate line.

    The measured lag for one sample is ``actual_elapsed - interval``: if we
    asked to sleep 50ms but the loop took 220ms to resume us, the loop was
    blocked ~170ms by other work during that span.
    """
    # Bound the sample buffer to one window's worth of samples so a long-running
    # monitor never grows memory.
    maxlen = max(1, int(window_seconds / interval) + 1)
    samples: deque[float] = deque(maxlen=maxlen)

    expected = time.monotonic() + interval
    window_start = time.monotonic()

    log.info(
        "loop-lag monitor started (interval=%.3fs window=%.1fs)",
        interval,
        window_seconds,
    )

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break

        now = time.monotonic()
        # How much later than scheduled did we actually resume? Clamp at 0 so a
        # slightly-early wake (rare, timer rounding) doesn't skew the stats.
        lag_ms = max(0.0, (now - expected)) * 1000.0
        samples.append(lag_ms)
        expected = now + interval

        if now - window_start >= window_seconds and samples:
            ordered = sorted(samples)
            n = len(ordered)
            p95 = ordered[min(n - 1, int(n * 0.95))]
            mx = ordered[-1]
            mean = sum(ordered) / n
            log.info(
                "loop-lag over %.0fs: max=%.1fms p95=%.1fms mean=%.1fms (n=%d)",
                window_seconds,
                mx,
                p95,
                mean,
                n,
            )
            samples.clear()
            window_start = now


def start_loop_lag_monitor(
    interval: float = 0.05,
    window_seconds: float = 10.0,
) -> Optional[asyncio.Task]:
    """Spawn the monitor as a background task on the running loop.

    Returns the Task (so the caller can cancel it on shutdown), or None if there
    is no running loop yet.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("loop-lag monitor: no running loop; not started")
        return None
    return loop.create_task(loop_lag_monitor(interval, window_seconds))
