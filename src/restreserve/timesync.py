"""Clock-offset estimation against Resy's servers and precise waiting.

Resy's HTTP Date header has 1-second granularity; sampling several requests
and taking the median midpoint-offset gives ~±0.5s accuracy, which the
configurable polling lead (default 1s) absorbs. Measuring against
api.resy.com itself is the point — it's *their* clock that decides when
slots drop.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# wait_until tuning
COARSE_CHUNK_S = 1.0
FINE_THRESHOLD_S = 2.0
FINE_STEP_S = 0.05
SPIN_THRESHOLD_S = 0.05


def estimate_clock_offset(
    sample: Callable[[], tuple[dt.datetime | None, float, float]],
    samples: int = 5,
) -> dt.timedelta:
    """Estimate (server_time - local_time) from timestamped HTTP requests.

    `sample` performs one request and returns (server Date as aware UTC
    datetime or None, t_sent, t_received) where t_* are time.time() values.
    Samples with abnormally high RTT are discarded; the median of the rest
    wins. Returns timedelta(0) if no usable samples.
    """
    measurements: list[tuple[float, float]] = []  # (offset_seconds, rtt)
    for _ in range(samples):
        server, t_sent, t_received = sample()
        if server is None:
            continue
        rtt = t_received - t_sent
        midpoint = (t_sent + t_received) / 2
        offset = server.timestamp() - midpoint
        measurements.append((offset, rtt))

    if not measurements:
        log.warning("clock sync failed: no usable Date headers; assuming zero offset")
        return dt.timedelta(0)

    median_rtt = statistics.median(rtt for _, rtt in measurements)
    kept = [off for off, rtt in measurements if rtt <= median_rtt * 2]
    offset_s = statistics.median(kept)
    log.info(
        "clock offset vs Resy: %+.3fs (from %d/%d samples, median RTT %.0fms)",
        offset_s, len(kept), samples, median_rtt * 1000,
    )
    return dt.timedelta(seconds=offset_s)


def wait_until(
    target_utc: dt.datetime,
    offset: dt.timedelta,
    keepalive: Callable[[], object] | None = None,
    keepalive_interval_s: float = 30.0,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until `target_utc` *in server time* arrives.

    `offset` is (server - local); the local wall-clock moment that
    corresponds to the server hitting target is target - offset.
    Coarse sleep with periodic keepalive pings, then 50ms steps for the
    final 2s, then a busy-spin for the last 50ms.
    """
    target_local = target_utc.timestamp() - offset.total_seconds()
    last_keepalive = now()

    while True:
        remaining = target_local - now()
        if remaining <= 0:
            return
        if remaining > FINE_THRESHOLD_S:
            if keepalive is not None and now() - last_keepalive >= keepalive_interval_s:
                keepalive()
                last_keepalive = now()
            sleep(min(COARSE_CHUNK_S, remaining - FINE_THRESHOLD_S + 0.01))
        elif remaining > SPIN_THRESHOLD_S:
            sleep(FINE_STEP_S)
        else:
            # busy-spin the final moments for ms precision
            while target_local - now() > 0:
                pass
            return
