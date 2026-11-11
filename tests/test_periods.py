from datetime import date

import pytest

from factories import make_days, make_record
from growthpulse_ai.analytics.errors import IncompletePeriodError, InvalidPeriodError
from growthpulse_ai.analytics.periods import (
    PeriodComparison,
    ReportingPeriod,
    ensure_complete,
    latest_vs_first,
    latest_vs_previous,
    records_in,
)

OCT = 10


def day(number: int) -> date:
    return date(2026, OCT, number)


def period(start: int, end: int) -> ReportingPeriod:
    return ReportingPeriod(day(start), day(end))


def test_period_is_inclusive():
    assert period(1, 7).days == 7
    assert period(1, 1).days == 1
    assert period(1, 3).dates == (day(1), day(2), day(3))


@pytest.mark.parametrize(("value", "expected"), [(1, True), (7, True), (8, False)])
def test_contains_boundaries(value: int, expected: bool):
    assert period(1, 7).contains(day(value)) is expected


def test_rejects_reversed_period():
    with pytest.raises(InvalidPeriodError):
        period(2, 1)


def test_records_in_includes_both_boundaries_only():
    rows = make_days(day(1), 10)
    selected = records_in(rows, period(3, 5))
    assert [row.reporting_date for row in selected] == [day(3), day(4), day(5)]


def test_latest_vs_previous_dates():
    comparison = latest_vs_previous(day(30), days=7)
    assert comparison.current == period(24, 30)
    assert comparison.baseline == period(17, 23)
    assert comparison.label == "latest_7_vs_previous_7"


def test_latest_vs_first_dates():
    comparison = latest_vs_first(day(1), day(30), days=10)
    assert comparison.baseline == period(1, 10)
    assert comparison.current == period(21, 30)


def test_adjacent_periods_are_allowed():
    assert PeriodComparison("x", period(1, 7), period(8, 14))


@pytest.mark.parametrize(("baseline", "current"), [((1, 7), (7, 13)), ((8, 14), (1, 7))])
def test_rejects_overlapping_or_reversed_comparisons(baseline, current):
    with pytest.raises(InvalidPeriodError):
        PeriodComparison("x", period(*baseline), period(*current))


def test_rejects_unequal_lengths():
    with pytest.raises(InvalidPeriodError):
        PeriodComparison("x", period(1, 7), period(8, 10))


def test_latest_vs_first_rejects_overlap_on_short_ranges():
    with pytest.raises(InvalidPeriodError):
        latest_vs_first(day(1), day(15), days=10)


@pytest.mark.parametrize("factory", [latest_vs_previous])
def test_rejects_non_positive_window(factory):
    with pytest.raises(InvalidPeriodError):
        factory(day(30), days=0)


def test_latest_vs_first_rejects_non_positive_window():
    with pytest.raises(InvalidPeriodError):
        latest_vs_first(day(1), day(30), days=0)


def test_complete_period_passes():
    ensure_complete(make_days(day(1), 7), period(1, 7))


def test_missing_day_is_incomplete():
    rows = [row for row in make_days(day(1), 7) if row.reporting_date != day(4)]
    with pytest.raises(IncompletePeriodError, match="missing 1"):
        ensure_complete(rows, period(1, 7))


def test_period_beyond_data_is_incomplete():
    with pytest.raises(IncompletePeriodError):
        ensure_complete(make_days(day(1), 7), period(1, 8))


def test_empty_period_is_incomplete():
    with pytest.raises(IncompletePeriodError):
        ensure_complete([], period(1, 7))


def test_day_ingested_before_it_ended_is_incomplete():
    row = make_record(reporting_date="2026-10-10", ingested_at="2026-10-10T12:00:00+00:00")
    with pytest.raises(IncompletePeriodError, match="not yet closed"):
        ensure_complete([row], period(10, 10))


def test_day_closure_uses_account_timezone():
    # 23:30 UTC on the 10th is 00:30 on the 11th in London during BST, so the 10th has closed.
    closed = make_record(
        reporting_date="2026-10-10",
        account_timezone="Europe/London",
        ingested_at="2026-10-10T23:30:00+00:00",
    )
    ensure_complete([closed], period(10, 10))

    still_open = make_record(
        reporting_date="2026-10-10",
        account_timezone="America/New_York",
        ingested_at="2026-10-10T23:30:00+00:00",
    )
    with pytest.raises(IncompletePeriodError):
        ensure_complete([still_open], period(10, 10))
