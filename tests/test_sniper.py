"""Sniper loop state machine with a mocked client and fake time."""

import datetime as dt

import pytest

from restreserve.config import SniperConfig
from restreserve.models import Slot
from restreserve.resy_client import (
    AuthError,
    RateLimitedError,
    ResyError,
    SlotGoneError,
)
from restreserve.sniper import Sniper

UTC = dt.timezone.utc


def make_slot(hhmm: str, token: str | None = None) -> Slot:
    hour, minute = map(int, hhmm.split(":"))
    return Slot(
        config_token=token or f"cfg-{hhmm}",
        start=dt.datetime(2026, 7, 4, hour, minute),
        table_type="Dining Room",
    )


def make_cfg(**kw) -> SniperConfig:
    defaults = dict(
        email="a@b.com",
        password="pw",
        venue_id=1234,
        date=dt.date(2026, 7, 4),
        party_size=2,
        window_start=dt.time(18, 30),
        window_end=dt.time(20, 0),
        ideal_time=dt.time(19, 0),
        drop_time=dt.datetime(2026, 6, 13, 13, 0, tzinfo=UTC),
        max_duration_s=10,
        poll_interval_ms=250,
    )
    defaults.update(kw)
    return SniperConfig(**defaults)


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        self.t += 0.01  # every clock read costs a little time
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeClient:
    """Scripted ResyClient stand-in.

    `find_script` is a list of per-tick results: a list of Slots, or an
    exception instance to raise. The last entry repeats forever.
    `failing_tokens` raise SlotGoneError from get_book_token.
    """

    def __init__(self, find_script, failing_tokens=(), book_error=None):
        self.find_script = list(find_script)
        self.failing_tokens = set(failing_tokens)
        self.book_error = book_error
        self.payment_method_id = 4242
        self.booked: list[str] = []
        self.details_calls: list[str] = []
        self.authenticated = 0

    def find_slots(self, venue_id, day, party_size):
        step = self.find_script.pop(0) if len(self.find_script) > 1 else self.find_script[0]
        if isinstance(step, Exception):
            raise step
        return step

    def get_book_token(self, config_token, day, party_size):
        self.details_calls.append(config_token)
        if config_token in self.failing_tokens:
            raise SlotGoneError("stolen")
        return f"book-{config_token}"

    def book(self, book_token, payment_method_id):
        if self.book_error is not None:
            error, self.book_error = self.book_error, None
            raise error
        self.booked.append(book_token)
        return {"resy_token": "rt", "reservation_id": 777}

    def make_booking_result(self, payload, slot):
        from restreserve.models import BookingResult

        return BookingResult(
            resy_token=payload["resy_token"],
            reservation_id=payload["reservation_id"],
            slot=slot,
        )

    def authenticate(self, email, password):
        self.authenticated += 1

    def warm(self):
        return 0.01


def make_sniper(cfg, client) -> tuple[Sniper, FakeTime]:
    fake = FakeTime()
    sniper = Sniper(cfg, client, clock=fake.clock, sleep=fake.sleep)
    sniper.venue_id = cfg.venue_id
    return sniper, fake


def test_books_best_slot_first_tick():
    client = FakeClient([[make_slot("19:30"), make_slot("19:00")]])
    sniper, _ = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result is not None
    assert result.slot.config_token == "cfg-19:00"
    assert client.booked == ["book-cfg-19:00"]


def test_falls_back_when_slot_stolen():
    client = FakeClient(
        [[make_slot("19:00"), make_slot("19:15")]], failing_tokens={"cfg-19:00"}
    )
    sniper, _ = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result.slot.config_token == "cfg-19:15"


def test_stolen_slot_not_retried_next_tick():
    client = FakeClient(
        [[make_slot("19:00")], [make_slot("19:00"), make_slot("19:15")]],
        failing_tokens={"cfg-19:00"},
    )
    sniper, _ = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result.slot.config_token == "cfg-19:15"
    # details for the stolen slot was tried exactly once
    assert client.details_calls.count("cfg-19:00") == 1


def test_empty_ticks_then_slots_appear():
    client = FakeClient([[], [], [make_slot("19:00")]])
    sniper, fake = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result is not None
    # paced sleeps happened for the empty ticks
    assert len([s for s in fake.sleeps if s > 0]) >= 2


def test_timeout_returns_none():
    client = FakeClient([[]])
    sniper, _ = make_sniper(make_cfg(max_duration_s=2), client)
    assert sniper._poll_and_book() is None


def test_timeout_with_out_of_window_slots_logged(caplog):
    client = FakeClient([[make_slot("22:00")]])
    sniper, _ = make_sniper(make_cfg(max_duration_s=2), client)
    assert sniper._poll_and_book() is None
    assert "22:00" in caplog.text


def test_rate_limit_honors_retry_after():
    client = FakeClient(
        [RateLimitedError("429", retry_after=2.5), [make_slot("19:00")]]
    )
    sniper, fake = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result is not None
    assert 2.5 in fake.sleeps


def test_rate_limit_backoff_capped():
    client = FakeClient([RateLimitedError("429"), [make_slot("19:00")]])
    sniper, fake = make_sniper(make_cfg(), client)
    assert sniper._poll_and_book() is not None
    assert all(s <= 5.0 for s in fake.sleeps)


def test_5xx_keeps_polling():
    client = FakeClient([ResyError("503"), [make_slot("19:00")]])
    sniper, _ = make_sniper(make_cfg(), client)
    assert sniper._poll_and_book() is not None


def test_dry_run_stops_after_details_without_booking():
    client = FakeClient([[make_slot("19:00")]])
    sniper, _ = make_sniper(make_cfg(dry_run=True), client)
    result = sniper._poll_and_book()
    assert result is not None
    assert result.dry_run is True
    assert client.details_calls == ["cfg-19:00"]
    assert client.booked == []


def test_reauth_once_on_auth_error_during_find():
    client = FakeClient([AuthError("401"), [make_slot("19:00")]])
    sniper, _ = make_sniper(make_cfg(), client)
    assert sniper._poll_and_book() is not None
    assert client.authenticated == 1


def test_second_auth_error_aborts():
    client = FakeClient([AuthError("401"), AuthError("401"), [make_slot("19:00")]])
    sniper, _ = make_sniper(make_cfg(), client)
    assert sniper._poll_and_book() is None


def test_no_reauth_without_password():
    client = FakeClient([AuthError("401"), [make_slot("19:00")]])
    cfg = make_cfg(email=None, password=None, auth_token="tok")
    sniper, _ = make_sniper(cfg, client)
    assert sniper._poll_and_book() is None
    assert client.authenticated == 0


def test_book_auth_error_triggers_reauth_and_next_tick_succeeds():
    client = FakeClient([[make_slot("19:00")]], book_error=AuthError("401"))
    sniper, _ = make_sniper(make_cfg(), client)
    result = sniper._poll_and_book()
    assert result is not None
    assert client.authenticated == 1


def test_attempts_capped_per_tick():
    slots = [make_slot(f"19:{m:02d}") for m in (0, 5, 10, 15, 20)]
    client = FakeClient(
        [slots, []], failing_tokens={s.config_token for s in slots}
    )
    sniper, _ = make_sniper(make_cfg(max_duration_s=1), client)
    sniper._poll_and_book()
    # only ATTEMPTS_PER_TICK (3) details calls on the first tick
    assert len(client.details_calls) == 3
