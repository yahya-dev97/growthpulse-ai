"""Sync one advertising account: fetch, validate, store, and advance its watermark.

Each account syncs on its own, with its own run history, lease, watermark and retries.
One account's failure never touches another account. A sync never builds a report: that
is the client analysis, which reads what syncs have stored.

Steps, each in its own transaction:

1. Claim: lock the account, refuse if it is paused, archived, disabled or leased, take
   the lease and commit a running ``account_sync`` run.
2. Fetch through the account's own connector. Its account guard quarantines any row that
   names another account. A response the connector cannot confirm as complete (partial
   pages, authorisation or API errors) fails the sync; an empty result counts only when
   complete.
3. Save: records and quarantined rows (all carrying this account), the finished run and
   the advanced watermark, together. Any failure rolls all of it back.
4. On failure only: mark the run failed with a fixed code and schedule a retry, or stop
   for attention, on a fresh connection. The watermark is never moved by a failure.
"""

import logging
import random as random_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

from growthpulse_ai.accounts.models import AccountContext
from growthpulse_ai.accounts.scheduling import (
    DateWindow,
    FailureDecision,
    decide_after_failure,
    local_today,
    next_due_at,
    sync_window,
)
from growthpulse_ai.connectors.factory import ConnectorFactory
from growthpulse_ai.persistence.errors import AccountNotSyncableError
from growthpulse_ai.persistence.store import (
    SyncClaim,
    Trigger,
    claim_account_sync,
    record_sync_failure,
    save_account_sync,
)
from growthpulse_ai.persistence.tenancy import sync_inputs
from growthpulse_ai.pipeline.failures import IncompleteResponseError, classify_error

logger = logging.getLogger(__name__)

Connect = Callable[[], psycopg.Connection]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    ad_account_id: UUID
    run_id: UUID | None
    """None when there was nothing new to fetch and no run was started."""
    window: DateWindow | None
    counts: dict[str, int]
    attempt: int


class AccountSyncError(Exception):
    """A sync failed. Carries only IDs, a fixed code and what was scheduled next."""

    def __init__(
        self,
        ad_account_id: UUID,
        run_id: UUID | None,
        error_code: str,
        *,
        recorded: bool,
        decision: FailureDecision | None = None,
    ) -> None:
        state = "recorded" if recorded else "not recorded"
        super().__init__(f"Sync of account {ad_account_id} failed: {error_code} ({state})")
        self.ad_account_id = ad_account_id
        self.run_id = run_id
        self.error_code = error_code
        self.recorded = recorded
        self.decision = decision


def _default_window(connect: Connect, account: AccountContext, now: datetime) -> DateWindow | None:
    with connect() as connection:
        inputs = sync_inputs(connection, account.ad_account_id)
    if inputs is None:
        raise AccountNotSyncableError("The account is unknown or paused")
    synced_through, lookback_days, backfill_start = inputs
    return sync_window(
        today_local=local_today(now, account.timezone),
        synced_through=synced_through,
        lookback_days=lookback_days,
        backfill_start=backfill_start,
    )


def _record_failure(
    connect: Connect,
    account: AccountContext,
    claim: SyncClaim,
    code: str,
    now: datetime,
    random: Callable[[], float],
    counts: dict[str, int],
) -> tuple[bool, FailureDecision]:
    decision = decide_after_failure(
        code,
        consecutive_failures_before=claim.consecutive_failures,
        max_consecutive_failures=claim.max_consecutive_failures,
        now=now,
        random=random,
    )
    try:
        with connect() as connection:
            recorded = record_sync_failure(
                connection,
                claim.run_id,
                account=account,
                error_code=code,
                decision=decision,
                counts=counts,
            )
    except Exception as error:
        logger.error(
            "sync account=%s run=%s failure_not_recorded code=%s cause=%s",
            account.ad_account_id, claim.run_id, classify_error(error), type(error).__name__,
        )  # fmt: skip
        return False, decision
    return recorded, decision


def sync_account(
    account: AccountContext,
    factory: ConnectorFactory,
    connect: Connect,
    *,
    triggered_by: Trigger,
    window: DateWindow | None = None,
    now: Callable[[], datetime] = utc_now,
    random: Callable[[], float] = random_module.random,
    code_version: str | None = None,
    run_id: UUID | None = None,
) -> SyncOutcome:
    """Run one sync of one account. Without a window, fetch what its schedule needs.

    A window with no data is a successful sync of zero records, not a failure.
    """
    run_id = run_id or uuid4()
    started = now()
    try:
        if window is None:
            window = _default_window(connect, account, started)
            if window is None:
                return SyncOutcome(account.ad_account_id, None, None, {}, 0)
        with connect() as connection:
            claim = claim_account_sync(
                connection, run_id, account=account, window=window,
                triggered_by=triggered_by, now=started, code_version=code_version,
            )  # fmt: skip
    except Exception as error:
        code = classify_error(error)
        logger.error("sync account=%s not_started code=%s", account.ad_account_id, code)
        raise AccountSyncError(account.ad_account_id, None, code, recorded=False) from None

    progress: dict[str, int] = {}
    try:
        connector = factory.for_account(account)
        result = connector.fetch(window.start, window.end)
        progress = {
            "rows_received": len(result.records) + len(result.quarantined),
            "records_received": len(result.records),
            "quarantined_rows": len(result.quarantined),
        }
        if not connector.last_fetch_complete:
            # Never trust a partial or unconfirmed answer, empty or not: the watermark only
            # moves past days the provider is known to have returned in full.
            raise IncompleteResponseError
        finished = now()
        due = next_due_at(
            claim.frequency,
            preferred_local_hour=claim.preferred_local_hour,
            timezone=account.timezone,
            after=finished,
        )
        with connect() as connection:
            counts = save_account_sync(
                connection, run_id, account=account, connector=connector.name,
                window=window, result=result, now=finished, next_due_at=due,
            )  # fmt: skip
    except Exception as error:
        code = classify_error(error)
        recorded, decision = _record_failure(connect, account, claim, code, now(), random, progress)
        logger.error(
            "sync account=%s run=%s failed code=%s next=%s",
            account.ad_account_id, run_id, code, decision.status.value,
        )  # fmt: skip
        raise AccountSyncError(
            account.ad_account_id, run_id, code, recorded=recorded, decision=decision
        ) from None

    logger.info("sync account=%s run=%s succeeded", account.ad_account_id, run_id)
    return SyncOutcome(account.ad_account_id, run_id, window, counts, claim.attempt)
