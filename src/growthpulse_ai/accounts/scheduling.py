"""When each account syncs, what it fetches, and what happens after a failure.

Pure functions: the clock and the random source are arguments, so every decision is
reproducible in tests. Each account is scheduled on its own; one account's failure never
changes another's state. A failed run is never retried inside the same run: a retry is a
new run, scheduled here.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class SyncFrequency(StrEnum):
    DAILY = "daily"
    EVERY_6_HOURS = "every_6_hours"
    MANUAL = "manual"


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    NEEDS_ATTENTION = "needs_attention"
    PAUSED = "paused"


class FailureAction(StrEnum):
    RETRY = "retry"
    NEEDS_REAUTH = "needs_reauth"
    NEEDS_ATTENTION = "needs_attention"


# Transient problems. Everything else needs a person, so it is never retried blindly.
RETRYABLE_ERROR_CODES = frozenset(
    {"database_unavailable", "connector_unavailable", "rate_limited", "abandoned"}
)
REAUTH_ERROR_CODES = frozenset({"auth_failed"})

RETRY_BASE = timedelta(minutes=15)
RETRY_CAP = timedelta(hours=6)
RETRY_JITTER = 0.2
MAX_LOOKBACK_DAYS = 90


def _require_utc(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("Times must be timezone-aware UTC")


def local_today(now: datetime, timezone: str) -> date:
    """The calendar date in the account's timezone, which is how platforms report days."""
    _require_utc(now)
    return now.astimezone(ZoneInfo(timezone)).date()


def next_due_at(
    frequency: SyncFrequency,
    *,
    preferred_local_hour: int,
    timezone: str,
    after: datetime,
) -> datetime | None:
    """The next scheduled sync strictly after ``after``, in UTC. None for manual accounts.

    Daily syncs run at the preferred hour in the account's own timezone, so the time in UTC
    moves with daylight saving. A preferred hour skipped by a clock change runs an hour late
    that day, as the zone defines it.
    """
    _require_utc(after)
    if not 0 <= preferred_local_hour <= 23:
        raise ValueError("preferred_local_hour must be 0-23")
    if frequency is SyncFrequency.MANUAL:
        return None
    if frequency is SyncFrequency.EVERY_6_HOURS:
        return after + timedelta(hours=6)
    zone = ZoneInfo(timezone)
    day = after.astimezone(zone).date()
    for offset in range(3):
        local = datetime.combine(day + timedelta(days=offset), time(preferred_local_hour), zone)
        candidate = local.astimezone(UTC)
        if candidate > after:
            return candidate
    raise AssertionError("unreachable: a later local day always exists")


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("start must not be after end")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def sync_window(
    *,
    today_local: date,
    synced_through: date | None,
    lookback_days: int,
    backfill_start: date,
) -> DateWindow | None:
    """The dates one sync fetches, ending yesterday in the account's timezone.

    A first sync starts at backfill_start. Later syncs re-fetch the last ``lookback_days``
    days, so late conversions and platform restatements are picked up and upserted
    idempotently. None when there is nothing new to fetch.
    """
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be 1-{MAX_LOOKBACK_DAYS}")
    end = today_local - timedelta(days=1)
    if synced_through is None:
        start = backfill_start
    else:
        start = min(synced_through + timedelta(days=1), end - timedelta(days=lookback_days - 1))
        start = max(start, backfill_start)
    return DateWindow(start, end) if start <= end else None


def chunk_window(window: DateWindow, max_days: int) -> list[DateWindow]:
    """Split a long backfill into runs of at most max_days, oldest first."""
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    chunks, start = [], window.start
    while start <= window.end:
        end = min(window.end, start + timedelta(days=max_days - 1))
        chunks.append(DateWindow(start, end))
        start = end + timedelta(days=1)
    return chunks


def classify_failure(error_code: str) -> FailureAction:
    if error_code in RETRYABLE_ERROR_CODES:
        return FailureAction.RETRY
    if error_code in REAUTH_ERROR_CODES:
        return FailureAction.NEEDS_REAUTH
    return FailureAction.NEEDS_ATTENTION


def retry_delay(consecutive_failures: int, random: Callable[[], float]) -> timedelta:
    """Exponential backoff with ±20% jitter: 15 min, 30 min, 1 h, ... capped at 6 h."""
    if consecutive_failures < 1:
        raise ValueError("consecutive_failures must be at least 1")
    exponent = min(consecutive_failures - 1, 16)
    base = min(RETRY_BASE * (2**exponent), RETRY_CAP)
    factor = 1 + RETRY_JITTER * (2 * random() - 1)
    return base * factor


@dataclass(frozen=True, slots=True)
class FailureDecision:
    status: SyncStatus
    consecutive_failures: int
    next_attempt_at: datetime | None
    action: FailureAction


def decide_after_failure(
    error_code: str,
    *,
    consecutive_failures_before: int,
    max_consecutive_failures: int,
    now: datetime,
    random: Callable[[], float],
) -> FailureDecision:
    """What one account's sync state becomes after one failed run."""
    _require_utc(now)
    if not 1 <= max_consecutive_failures <= 10:
        raise ValueError("max_consecutive_failures must be 1-10")
    failures = consecutive_failures_before + 1
    action = classify_failure(error_code)
    if action is FailureAction.RETRY and failures < max_consecutive_failures:
        return FailureDecision(
            SyncStatus.RETRY_WAIT, failures, now + retry_delay(failures, random), action
        )
    if action is FailureAction.RETRY:
        action = FailureAction.NEEDS_ATTENTION
    # Reauthorisation and attention both wait for the owner; nothing is scheduled.
    return FailureDecision(SyncStatus.NEEDS_ATTENTION, failures, None, action)


def common_complete_through(watermarks: Iterable[date | None]) -> date | None:
    """The last date every included account has synced. None if any has never synced.

    A client analysis ends here, so every account in it covers the same dates.
    """
    values = list(watermarks)
    if not values or any(value is None for value in values):
        return None
    return min(value for value in values if value is not None)
