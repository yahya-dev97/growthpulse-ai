"""Per-account schedules, sync windows, retries and analysis readiness."""

from datetime import UTC, date, datetime, timedelta

import pytest

from growthpulse_ai.accounts.scheduling import (
    RETRY_CAP,
    DateWindow,
    FailureAction,
    SyncFrequency,
    SyncStatus,
    chunk_window,
    classify_failure,
    common_complete_through,
    decide_after_failure,
    local_today,
    next_due_at,
    retry_delay,
    sync_window,
)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# Schedules


def test_daily_sync_runs_at_the_local_hour_and_follows_daylight_saving():
    # London moves to BST on 29 March 2026, so 06:00 local becomes 05:00 UTC.
    before = next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=6, timezone="Europe/London",
        after=utc(2026, 3, 27, 7),
    )  # fmt: skip
    after = next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=6, timezone="Europe/London",
        after=utc(2026, 3, 28, 7),
    )  # fmt: skip
    assert before == utc(2026, 3, 28, 6)
    assert after == utc(2026, 3, 29, 5)


def test_daily_sync_returns_to_winter_time():
    due = next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=6, timezone="Europe/London",
        after=utc(2026, 10, 24, 6),
    )  # fmt: skip
    assert due == utc(2026, 10, 25, 6)


def test_an_hour_skipped_by_the_clock_change_runs_an_hour_late():
    due = next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=1, timezone="Europe/London",
        after=utc(2026, 3, 28, 12),
    )  # fmt: skip
    assert due == utc(2026, 3, 29, 1)  # 02:00 BST


def test_daily_sync_later_the_same_day_and_in_other_timezones():
    assert next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=9, timezone="America/New_York",
        after=utc(2026, 11, 11, 10),
    ) == utc(2026, 11, 11, 14)  # fmt: skip
    assert next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=6, timezone="Australia/Sydney",
        after=utc(2026, 11, 11, 10),
    ) == utc(2026, 11, 11, 19)  # fmt: skip


def test_due_time_is_always_strictly_later():
    moment = utc(2026, 11, 11, 6)
    due = next_due_at(
        SyncFrequency.DAILY, preferred_local_hour=6, timezone="Europe/London", after=moment
    )
    assert due == utc(2026, 11, 12, 6)


def test_six_hourly_and_manual_schedules():
    moment = utc(2026, 11, 11, 6)
    assert next_due_at(
        SyncFrequency.EVERY_6_HOURS, preferred_local_hour=0, timezone="UTC", after=moment
    ) == moment + timedelta(hours=6)
    assert (
        next_due_at(SyncFrequency.MANUAL, preferred_local_hour=0, timezone="UTC", after=moment)
        is None
    )


@pytest.mark.parametrize("after", [datetime(2026, 11, 11, 6), utc(2026, 11, 11, 6).astimezone()])
def test_schedules_need_utc_times(after: datetime):
    if after.utcoffset() == timedelta(0):
        pytest.skip("the local zone is UTC on this machine")
    with pytest.raises(ValueError, match="UTC"):
        next_due_at(SyncFrequency.DAILY, preferred_local_hour=6, timezone="UTC", after=after)


def test_preferred_hour_is_validated():
    with pytest.raises(ValueError, match="0-23"):
        next_due_at(
            SyncFrequency.DAILY, preferred_local_hour=24, timezone="UTC", after=utc(2026, 1, 1)
        )


def test_local_today_uses_the_account_timezone():
    moment = utc(2026, 11, 11, 23, 30)
    assert local_today(moment, "Europe/London") == date(2026, 11, 11)
    assert local_today(moment, "Australia/Sydney") == date(2026, 11, 12)
    assert local_today(moment, "America/Los_Angeles") == date(2026, 11, 11)


# Sync windows


def test_first_sync_backfills_to_yesterday():
    window = sync_window(
        today_local=date(2026, 11, 11), synced_through=None, lookback_days=7,
        backfill_start=date(2026, 8, 1),
    )  # fmt: skip
    assert window == DateWindow(date(2026, 8, 1), date(2026, 11, 10))


def test_later_syncs_refetch_the_lookback_window():
    window = sync_window(
        today_local=date(2026, 11, 11), synced_through=date(2026, 11, 10), lookback_days=7,
        backfill_start=date(2026, 8, 1),
    )  # fmt: skip
    assert window == DateWindow(date(2026, 11, 4), date(2026, 11, 10))


def test_a_sync_after_a_gap_starts_where_the_last_one_ended():
    window = sync_window(
        today_local=date(2026, 11, 11), synced_through=date(2026, 10, 20), lookback_days=7,
        backfill_start=date(2026, 8, 1),
    )  # fmt: skip
    assert window == DateWindow(date(2026, 10, 21), date(2026, 11, 10))


def test_lookback_never_reaches_before_backfill_start():
    window = sync_window(
        today_local=date(2026, 11, 11), synced_through=date(2026, 11, 10), lookback_days=30,
        backfill_start=date(2026, 11, 1),
    )  # fmt: skip
    assert window == DateWindow(date(2026, 11, 1), date(2026, 11, 10))


def test_nothing_to_fetch_before_backfill_starts():
    assert sync_window(
        today_local=date(2026, 11, 11), synced_through=None, lookback_days=7,
        backfill_start=date(2026, 11, 11),
    ) is None  # fmt: skip


@pytest.mark.parametrize("days", [0, 91])
def test_lookback_is_bounded(days: int):
    with pytest.raises(ValueError, match="lookback_days"):
        sync_window(
            today_local=date(2026, 11, 11), synced_through=None, lookback_days=days,
            backfill_start=date(2026, 8, 1),
        )  # fmt: skip


def test_backfill_is_split_into_ordered_chunks():
    chunks = chunk_window(DateWindow(date(2026, 8, 1), date(2026, 10, 15)), max_days=31)
    assert [(c.start, c.end) for c in chunks] == [
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 10, 1)),
        (date(2026, 10, 2), date(2026, 10, 15)),
    ]
    assert sum(c.days for c in chunks) == 76


def test_window_rejects_reversed_dates_and_bad_chunk_size():
    with pytest.raises(ValueError):
        DateWindow(date(2026, 2, 2), date(2026, 2, 1))
    with pytest.raises(ValueError):
        chunk_window(DateWindow(date(2026, 2, 1), date(2026, 2, 2)), 0)


# Failures and retries


@pytest.mark.parametrize(
    ("code", "action"),
    [
        ("database_unavailable", FailureAction.RETRY),
        ("connector_unavailable", FailureAction.RETRY),
        ("rate_limited", FailureAction.RETRY),
        ("abandoned", FailureAction.RETRY),
        ("auth_failed", FailureAction.NEEDS_REAUTH),
        ("account_access_lost", FailureAction.NEEDS_ATTENTION),
        ("unexpected_error", FailureAction.NEEDS_ATTENTION),
        ("analytics_error", FailureAction.NEEDS_ATTENTION),
    ],
)
def test_failure_classification(code: str, action: FailureAction):
    assert classify_failure(code) is action


def test_retry_delays_double_and_stop_at_the_cap():
    middle = [retry_delay(n, lambda: 0.5) for n in range(1, 8)]
    assert middle[:6] == [timedelta(minutes=m) for m in (15, 30, 60, 120, 240, 360)]
    assert middle[6] == RETRY_CAP
    assert retry_delay(50, lambda: 0.5) == RETRY_CAP


def test_retry_jitter_stays_within_twenty_percent():
    assert retry_delay(1, lambda: 0.0) == timedelta(minutes=12)
    assert retry_delay(1, lambda: 1.0) == timedelta(minutes=18)


def test_retry_delay_needs_a_failure():
    with pytest.raises(ValueError):
        retry_delay(0, lambda: 0.5)


def test_a_transient_failure_waits_and_retries_as_a_new_run():
    now = utc(2026, 11, 11, 6)
    decision = decide_after_failure(
        "database_unavailable", consecutive_failures_before=0, max_consecutive_failures=5,
        now=now, random=lambda: 0.5,
    )  # fmt: skip
    assert decision.status is SyncStatus.RETRY_WAIT
    assert decision.consecutive_failures == 1
    assert decision.next_attempt_at == now + timedelta(minutes=15)


def test_repeated_failures_stop_for_attention():
    decision = decide_after_failure(
        "rate_limited", consecutive_failures_before=4, max_consecutive_failures=5,
        now=utc(2026, 11, 11), random=lambda: 0.5,
    )  # fmt: skip
    assert decision.status is SyncStatus.NEEDS_ATTENTION
    assert decision.action is FailureAction.NEEDS_ATTENTION
    assert decision.next_attempt_at is None


@pytest.mark.parametrize(
    ("code", "action"),
    [
        ("auth_failed", FailureAction.NEEDS_REAUTH),
        ("unexpected_error", FailureAction.NEEDS_ATTENTION),
    ],
)
def test_non_transient_failures_are_never_retried(code: str, action: FailureAction):
    decision = decide_after_failure(
        code, consecutive_failures_before=0, max_consecutive_failures=5,
        now=utc(2026, 11, 11), random=lambda: 0.5,
    )  # fmt: skip
    assert (decision.status, decision.action, decision.next_attempt_at) == (
        SyncStatus.NEEDS_ATTENTION,
        action,
        None,
    )


def test_failure_limits_are_validated():
    with pytest.raises(ValueError):
        decide_after_failure(
            "rate_limited", consecutive_failures_before=0, max_consecutive_failures=0,
            now=utc(2026, 11, 11), random=lambda: 0.5,
        )  # fmt: skip


# Client analysis readiness


def test_analysis_ends_at_the_last_date_every_account_has_synced():
    assert common_complete_through([date(2026, 11, 10), date(2026, 11, 8)]) == date(2026, 11, 8)


def test_analysis_waits_while_any_account_has_never_synced():
    assert common_complete_through([date(2026, 11, 10), None]) is None
    assert common_complete_through([]) is None
