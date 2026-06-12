"""Plain data containers shared across modules."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Slot:
    """One bookable time slot returned by /4/find."""

    config_token: str  # slot["config"]["token"], needed for /3/details
    start: dt.datetime  # venue-local naive datetime from slot["date"]["start"]
    table_type: str  # e.g. "Dining Room"; "" if unknown
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    def __str__(self) -> str:
        kind = f" ({self.table_type})" if self.table_type else ""
        return f"{self.start:%Y-%m-%d %H:%M}{kind}"


@dataclass
class AuthSession:
    """Result of authenticating against Resy."""

    token: str  # value for the x-resy-auth-token header
    payment_method_id: int | None


@dataclass(frozen=True)
class BookingResult:
    """A confirmed (or dry-run simulated) booking."""

    resy_token: str
    reservation_id: int | None
    slot: Slot
    dry_run: bool = False
