import datetime as dt

from restreserve.models import Slot
from restreserve.slots import rank_slots


def slot(hhmm: str, table_type: str = "Dining Room", token: str | None = None) -> Slot:
    hour, minute = map(int, hhmm.split(":"))
    return Slot(
        config_token=token or f"cfg-{hhmm}-{table_type}",
        start=dt.datetime(2026, 7, 4, hour, minute),
        table_type=table_type,
    )


WINDOW = dict(window_start=dt.time(18, 30), window_end=dt.time(20, 0),
              ideal_time=dt.time(19, 0))


def starts(ranked):
    return [s.start.time().strftime("%H:%M") for s in ranked]


def test_filters_outside_window():
    ranked = rank_slots([slot("17:00"), slot("19:00"), slot("21:30")], **WINDOW)
    assert starts(ranked) == ["19:00"]


def test_window_boundaries_inclusive():
    ranked = rank_slots([slot("18:30"), slot("20:00")], **WINDOW)
    assert len(ranked) == 2


def test_ranks_by_distance_from_ideal():
    ranked = rank_slots(
        [slot("18:30"), slot("19:45"), slot("19:15"), slot("19:00")], **WINDOW
    )
    assert starts(ranked) == ["19:00", "19:15", "18:30", "19:45"]


def test_table_type_soft_preference():
    ranked = rank_slots(
        [slot("19:00", "Dining Room"), slot("19:45", "Patio")],
        **WINDOW, table_type="patio",
    )
    # patio matches the preference so it outranks the closer dining-room slot
    assert starts(ranked) == ["19:45", "19:00"]
    # but non-matching slots remain as fallback
    assert len(ranked) == 2


def test_table_type_substring_case_insensitive():
    ranked = rank_slots(
        [slot("19:00", "Indoor Dining"), slot("19:00", "Outdoor Patio Seating",
                                              token="patio")],
        **WINDOW, table_type="PATIO",
    )
    assert ranked[0].config_token == "patio"


def test_no_table_preference_ignores_type():
    ranked = rank_slots(
        [slot("19:10", "Patio"), slot("19:05", "Bar")], **WINDOW
    )
    assert starts(ranked) == ["19:05", "19:10"]


def test_excludes_attempted_tokens():
    best = slot("19:00", token="stolen")
    ranked = rank_slots([best, slot("19:30")], **WINDOW, exclude_tokens={"stolen"})
    assert starts(ranked) == ["19:30"]


def test_empty_input():
    assert rank_slots([], **WINDOW) == []


def test_all_outside_window():
    assert rank_slots([slot("12:00"), slot("23:00")], **WINDOW) == []
