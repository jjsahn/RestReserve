"""Orchestration: prewarm -> wait for the drop -> poll-and-book loop."""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable

from restreserve import timesync
from restreserve.config import SniperConfig
from restreserve.models import BookingResult, Slot
from restreserve.resy_client import (
    AuthError,
    PaymentRequiredError,
    RateLimitedError,
    ResyClient,
    ResyError,
    SlotGoneError,
)
from restreserve.slots import rank_slots

log = logging.getLogger(__name__)

ATTEMPTS_PER_TICK = 3  # how many ranked slots to try before re-polling
BACKOFF_CAP_S = 5.0


class Sniper:
    def __init__(
        self,
        cfg: SniperConfig,
        client: ResyClient,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.venue_id: int | None = cfg.venue_id
        self._reauthed = False
        self._seen_slots: dict[str, Slot] = {}  # for the timeout post-mortem

    # -- phase 1: prewarm ----------------------------------------------------

    def prewarm(self) -> dt.timedelta:
        """Authenticate, resolve venue, check payment, sync clock, warm the pipe."""
        cfg = self.cfg

        if cfg.auth_token:
            log.info("using configured auth token")
            self.client.set_auth_token(cfg.auth_token)
            self.client.fetch_payment_method()
        else:
            log.info("authenticating as %s", cfg.email)
            self.client.authenticate(cfg.email, cfg.password)

        if self.venue_id is None:
            log.info("resolving venue %s (%s)", cfg.venue_slug, cfg.location)
            self.venue_id = self.client.resolve_venue(cfg.venue_slug, cfg.location)
        log.info("venue id: %d", self.venue_id)

        if self.client.payment_method_id is None:
            if cfg.dry_run:
                log.warning("no payment method on account (ok for dry run)")
            else:
                raise PaymentRequiredError(
                    "no payment method on your Resy account; most venues require "
                    "one to book. Add a card at resy.com, or use --dry-run."
                )
        else:
            log.info("payment method id: %d", self.client.payment_method_id)

        offset = timesync.estimate_clock_offset(self.client.server_date)

        # one real find: validates params, primes the connection, shows the field
        slots = self.client.find_slots(self.venue_id, cfg.date, cfg.party_size)
        log.info(
            "prewarm find: %d slot(s) currently bookable on %s for %d",
            len(slots), cfg.date, cfg.party_size,
        )
        for slot in rank_slots(
            slots, cfg.window_start, cfg.window_end, cfg.ideal_time, cfg.table_type
        )[:5]:
            log.info("  already available: %s", slot)

        log.info(
            "armed: %s party of %d, window %s-%s (ideal %s), drop %s, lead %dms",
            cfg.date, cfg.party_size, cfg.window_start, cfg.window_end,
            cfg.ideal_time, cfg.drop_time, cfg.lead_ms,
        )
        return offset

    # -- phases 2+3: wait then snipe ------------------------------------------

    def run(self) -> BookingResult | None:
        offset = self.prewarm()
        cfg = self.cfg

        target = cfg.drop_time.astimezone(dt.timezone.utc) - dt.timedelta(
            milliseconds=cfg.lead_ms
        )
        now_utc = dt.datetime.now(dt.timezone.utc) + offset
        if target <= now_utc:
            log.warning("drop time already passed (server now %s); sniping immediately",
                        now_utc.strftime("%H:%M:%S"))
        else:
            log.info("waiting until %s (server time, %dms early)...",
                     target.astimezone().strftime("%H:%M:%S.%f")[:-3], cfg.lead_ms)
            timesync.wait_until(target, offset, keepalive=self.client.warm)

        log.info(">>> snipe window open <<<")
        return self._poll_and_book()

    def _poll_and_book(self) -> BookingResult | None:
        cfg = self.cfg
        deadline = self.clock() + cfg.max_duration_s
        interval = cfg.poll_interval_ms / 1000
        attempted: set[str] = set()
        tick = 0

        while self.clock() < deadline:
            tick += 1
            tick_start = self.clock()

            try:
                slots = self.client.find_slots(self.venue_id, cfg.date, cfg.party_size)
            except RateLimitedError as e:
                backoff = e.retry_after if e.retry_after is not None else min(
                    2 ** min(tick, 2) * 1.0, BACKOFF_CAP_S
                )
                backoff = min(backoff, BACKOFF_CAP_S)
                log.warning("tick %d: rate limited, backing off %.1fs", tick, backoff)
                self.sleep(backoff)
                continue
            except AuthError:
                if not self._try_reauth():
                    return None
                continue
            except ResyError as e:
                log.warning("tick %d: find failed (%s); retrying", tick, e)
                self.sleep(interval)
                continue

            for slot in slots:
                self._seen_slots.setdefault(slot.config_token, slot)

            ranked = rank_slots(
                slots, cfg.window_start, cfg.window_end, cfg.ideal_time,
                cfg.table_type, exclude_tokens=attempted,
            )
            log.info("tick %d: %d slot(s), %d in window", tick, len(slots), len(ranked))

            for slot in ranked[:ATTEMPTS_PER_TICK]:
                result = self._attempt(slot, attempted)
                if result is not None:
                    return result

            self.sleep(max(0.0, interval - (self.clock() - tick_start)))

        self._log_post_mortem()
        return None

    def _attempt(self, slot: Slot, attempted: set[str]) -> BookingResult | None:
        """Try details -> book for one slot.

        Marks the slot in `attempted` only when it's truly gone (412), so a
        transient auth failure doesn't permanently exclude a good slot.
        """
        cfg = self.cfg
        log.info("attempting %s ...", slot)
        try:
            book_token = self.client.get_book_token(
                slot.config_token, cfg.date, cfg.party_size
            )
            if cfg.dry_run:
                log.info("[DRY RUN] would book %s — stopping here", slot)
                return BookingResult(
                    resy_token="dry-run", reservation_id=None, slot=slot, dry_run=True
                )
            payload = self.client.book(book_token, self.client.payment_method_id)
        except SlotGoneError:
            log.info("  slot gone: %s", slot)
            attempted.add(slot.config_token)
            return None
        except AuthError:
            if not self._try_reauth():
                raise
            return None  # caller moves on; next tick retries with fresh token
        result = self.client.make_booking_result(payload, slot)
        log.info("*** BOOKED %s — reservation id %s ***", slot, result.reservation_id)
        return result

    def _try_reauth(self) -> bool:
        """One automatic re-login if the token went stale mid-run."""
        if self._reauthed or not (self.cfg.email and self.cfg.password):
            log.error("auth token rejected and re-auth unavailable/exhausted")
            return False
        self._reauthed = True
        log.warning("auth token rejected; re-authenticating once")
        try:
            self.client.authenticate(self.cfg.email, self.cfg.password)
            return True
        except ResyError as e:
            log.error("re-authentication failed: %s", e)
            return False

    def _log_post_mortem(self) -> None:
        log.error(
            "timed out after %ds without booking", self.cfg.max_duration_s
        )
        if not self._seen_slots:
            log.error("no slots were ever returned — wrong date, or they never dropped")
            return
        log.error("slots seen during the window (none matched/succeeded):")
        for slot in sorted(self._seen_slots.values(), key=lambda s: s.start):
            log.error("  %s", slot)
