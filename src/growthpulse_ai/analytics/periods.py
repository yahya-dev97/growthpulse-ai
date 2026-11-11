"""Reporting periods and period comparisons.

Dates are inclusive reporting dates in each account's own timezone and are never converted.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from growthpulse_ai.analytics.errors import IncompletePeriodError, InvalidPeriodError
from growthpulse_ai.schema import AdPerformanceRecord


@dataclass(frozen=True, slots=True)
class ReportingPeriod:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise InvalidPeriodError("Period start must not be after its end")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(self.start + timedelta(days=offset) for offset in range(self.days))

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class ComparisonPurpose(StrEnum):
    RECENT_CHANGE = "recent_change"
    SUSTAINED_CHANGE = "sustained_change"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    label: str
    baseline: ReportingPeriod
    current: ReportingPeriod
    purpose: ComparisonPurpose = ComparisonPurpose.CUSTOM

    def __post_init__(self) -> None:
        if self.baseline.end >= self.current.start:
            raise InvalidPeriodError("Baseline must end before the current period starts")
        if self.baseline.days != self.current.days:
            raise InvalidPeriodError("Compared periods must have the same number of days")


def latest_vs_previous(end: date, days: int = 7) -> PeriodComparison:
    """Compare the latest ``days`` with the ``days`` immediately before them."""
    if days < 1:
        raise InvalidPeriodError("days must be at least 1")
    current_start = end - timedelta(days=days - 1)
    baseline_end = current_start - timedelta(days=1)
    return PeriodComparison(
        label=f"latest_{days}_vs_previous_{days}",
        baseline=ReportingPeriod(baseline_end - timedelta(days=days - 1), baseline_end),
        current=ReportingPeriod(current_start, end),
        purpose=ComparisonPurpose.RECENT_CHANGE,
    )


def latest_vs_first(start: date, end: date, days: int = 10) -> PeriodComparison:
    """Compare the last ``days`` of a range with its first ``days``."""
    if days < 1:
        raise InvalidPeriodError("days must be at least 1")
    return PeriodComparison(
        label=f"latest_{days}_vs_first_{days}",
        baseline=ReportingPeriod(start, start + timedelta(days=days - 1)),
        current=ReportingPeriod(end - timedelta(days=days - 1), end),
        purpose=ComparisonPurpose.SUSTAINED_CHANGE,
    )


def records_in(
    records: Iterable[AdPerformanceRecord], period: ReportingPeriod
) -> list[AdPerformanceRecord]:
    return [record for record in records if period.contains(record.reporting_date)]


def _day_is_closed(record: AdPerformanceRecord) -> bool:
    """A day is closed once data was ingested after it ended in the account's timezone."""
    ingestion_day = record.ingested_at.astimezone(ZoneInfo(record.account_timezone)).date()
    return ingestion_day > record.reporting_date


def ensure_complete(records: Iterable[AdPerformanceRecord], period: ReportingPeriod) -> None:
    """Raise unless every day in the period has data and has fully ended."""
    rows = records_in(records, period)
    open_days = sorted({row.reporting_date for row in rows if not _day_is_closed(row)})
    if open_days:
        raise IncompletePeriodError(
            f"Period includes {len(open_days)} day(s) not yet closed, from {open_days[0]}"
        )
    observed = {row.reporting_date for row in rows}
    missing = [day for day in period.dates if day not in observed]
    if missing:
        raise IncompletePeriodError(f"Period is missing {len(missing)} day(s), from {missing[0]}")
