import datetime as dt

from restreserve.timesync import estimate_clock_offset, wait_until

UTC = dt.timezone.utc


def make_sampler(offsets_and_rtts):
    """Build a sample() whose server clock runs `offset` ahead of local."""
    it = iter(offsets_and_rtts)

    def sample():
        offset, rtt = next(it)
        t_sent = 1000.0
        t_received = t_sent + rtt
        midpoint = (t_sent + t_received) / 2
        server = dt.datetime.fromtimestamp(midpoint + offset, tz=UTC)
        return server, t_sent, t_received

    return sample


def test_median_offset():
    sampler = make_sampler([(0.4, 0.05), (0.5, 0.05), (0.6, 0.05)])
    offset = estimate_clock_offset(sampler, samples=3)
    assert abs(offset.total_seconds() - 0.5) < 0.01


def test_discards_high_rtt_samples():
    # the 9s-offset sample has 10x the RTT and must be discarded
    sampler = make_sampler([(0.5, 0.05), (9.0, 0.8), (0.5, 0.05), (0.5, 0.06)])
    offset = estimate_clock_offset(sampler, samples=4)
    assert abs(offset.total_seconds() - 0.5) < 0.01


def test_no_usable_samples_returns_zero():
    offset = estimate_clock_offset(lambda: (None, 0.0, 0.1), samples=3)
    assert offset == dt.timedelta(0)


class FakeTime:
    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        self.t += 0.001  # busy-spin still advances
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_wait_until_reaches_target():
    fake = FakeTime(start=0.0)
    target = dt.datetime.fromtimestamp(10.0, tz=UTC)
    wait_until(target, dt.timedelta(0), now=fake.now, sleep=fake.sleep)
    assert fake.t >= 10.0
    assert fake.t < 10.5  # didn't overshoot wildly


def test_wait_until_applies_offset():
    # server runs 5s ahead -> local moment for server-time 10 is local 5
    fake = FakeTime(start=0.0)
    target = dt.datetime.fromtimestamp(10.0, tz=UTC)
    wait_until(target, dt.timedelta(seconds=5), now=fake.now, sleep=fake.sleep)
    assert 5.0 <= fake.t < 5.5


def test_wait_until_past_target_returns_immediately():
    fake = FakeTime(start=100.0)
    target = dt.datetime.fromtimestamp(10.0, tz=UTC)
    wait_until(target, dt.timedelta(0), now=fake.now, sleep=fake.sleep)
    assert fake.sleeps == []


def test_wait_until_fires_keepalive():
    fake = FakeTime(start=0.0)
    pings = []
    target = dt.datetime.fromtimestamp(120.0, tz=UTC)
    wait_until(
        target,
        dt.timedelta(0),
        keepalive=lambda: pings.append(fake.t),
        keepalive_interval_s=30.0,
        now=fake.now,
        sleep=fake.sleep,
    )
    assert len(pings) >= 2  # ~120s wait with 30s interval
