"""Pure slot filtering and ranking logic (no I/O)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from restreserve.models import Slot


def rank_slots(
    slots: Sequence[Slot],
    window_start: dt.time,
    window_end: dt.time,
    ideal_time: dt.time,
    table_type: str | None = None,
    exclude_tokens: Iterable[str] = (),
) -> list[Slot]:
    """Filter slots to the time window and rank best-first.

    Ranking: slots whose table type matches the (optional, case-insensitive
    substring) preference come first; within each group, closest to
    `ideal_time` wins. Slots in `exclude_tokens` (already attempted/stolen)
    are dropped.
    """
    excluded = set(exclude_tokens)
    ideal_minutes = _minutes(ideal_time)
    pref = table_type.lower() if table_type else None

    candidates = [
        slot
        for slot in slots
        if slot.config_token not in excluded
        and window_start <= slot.start.time() <= window_end
    ]

    def key(slot: Slot) -> tuple[int, int]:
        type_rank = 0
        if pref is not None and pref not in slot.table_type.lower():
            type_rank = 1
        return (type_rank, abs(_minutes(slot.start.time()) - ideal_minutes))

    return sorted(candidates, key=key)


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute
