"""Write pipeline results. Every write runs inside one explicit transaction.

Every row is written with its scope passed explicitly: the client of a run, report,
finding or proposal, and the account of a record, finding or quarantined row. Nothing
relies on a database default, and nothing is ever re-filed under another account.

Three kinds of run share this module:

- **Combined** (Milestone 4 shape): ``start_run``, then ``save_run_results`` writes one
  client's records, quarantined rows, report and findings and marks the run succeeded.
- **Account sync**: ``claim_account_sync`` takes the account's lease and commits a
  running run; ``save_account_sync`` writes that account's records and quarantined rows,
  finishes the run and advances the account's watermark, all in one transaction;
  ``record_sync_failure`` marks the run failed and schedules a retry without touching the
  watermark.
- **Client analysis**: ``start_run``, then ``save_client_analysis`` writes the report,
  findings and account breakdown and finishes the run.

A failure inside any save rolls the whole transaction back. The failure is then recorded
on a fresh connection with a fixed error code, never an error message. All writes are
idempotent: records and findings update only when their values differ, and reports and
breakdowns with the same content hash are stored once.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from growthpulse_ai.accounts.assignment import assign_findings
from growthpulse_ai.accounts.models import AccountContext, AccountMismatchError, ClientContext
from growthpulse_ai.accounts.scheduling import DateWindow, FailureDecision, SyncFrequency
from growthpulse_ai.data_quality import QUALITY_EVIDENCE_VERSION, QuarantinedRow, ValidationResult
from growthpulse_ai.persistence.errors import (
    AccountBusyError,
    AccountNotSyncableError,
    RunStateError,
)
from growthpulse_ai.persistence.rows import (
    Row,
    finding_rows,
    quarantine_evidence,
    quarantine_rows,
    record_row,
    report_row,
)
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

Trigger = Literal["manual", "scheduled", "test"]
RunKind = Literal["combined", "account_sync", "client_analysis"]
ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z_]{0,63}")
CODE_VERSION_PATTERN = re.compile(r"[0-9a-f]{7,40}")
LEASE_DURATION = timedelta(minutes=30)

INSERT_RUN = """
insert into growthpulse.pipeline_runs (
    run_id, data_source, triggered_by, code_version, run_kind, client_id, ad_account_id,
    window_start, window_end, attempt, retry_of_run_id
) values (
    %(run_id)s, %(data_source)s, %(triggered_by)s, %(code_version)s, %(run_kind)s,
    %(client_id)s, %(ad_account_id)s, %(window_start)s, %(window_end)s, %(attempt)s,
    %(retry_of_run_id)s
)
"""

UPSERT_RECORD = """
insert into growthpulse.ad_performance_records as existing (
    record_id, data_source, reporting_date, platform, account_id, campaign_id,
    campaign_name, campaign_type, impressions, clicks, spend, conversions, conversion_value,
    currency, account_timezone, conversion_event_id, conversion_event_name, conversion_stage,
    attribution_model, click_window_days, view_window_days, report_time_basis, ingested_at,
    first_seen_run_id, last_changed_run_id, ad_account_id
) values (
    %(record_id)s, %(data_source)s, %(reporting_date)s, %(platform)s, %(account_id)s,
    %(campaign_id)s, %(campaign_name)s, %(campaign_type)s, %(impressions)s, %(clicks)s,
    %(spend)s, %(conversions)s, %(conversion_value)s, %(currency)s, %(account_timezone)s,
    %(conversion_event_id)s, %(conversion_event_name)s, %(conversion_stage)s,
    %(attribution_model)s, %(click_window_days)s, %(view_window_days)s,
    %(report_time_basis)s, %(ingested_at)s, %(run_id)s, %(run_id)s, %(ad_account_id)s
)
on conflict (record_id) do update set
    campaign_name = excluded.campaign_name,
    campaign_type = excluded.campaign_type,
    impressions = excluded.impressions,
    clicks = excluded.clicks,
    spend = excluded.spend,
    conversions = excluded.conversions,
    conversion_value = excluded.conversion_value,
    currency = excluded.currency,
    account_timezone = excluded.account_timezone,
    conversion_event_name = excluded.conversion_event_name,
    conversion_stage = excluded.conversion_stage,
    ingested_at = excluded.ingested_at,
    last_changed_run_id = excluded.last_changed_run_id,
    updated_at = now()
-- A re-fetch with a new ingested_at but identical values is not a change.
where (
    existing.campaign_name, existing.campaign_type, existing.impressions, existing.clicks,
    existing.spend, existing.conversions, existing.conversion_value, existing.currency,
    existing.account_timezone, existing.conversion_event_name, existing.conversion_stage
) is distinct from (
    excluded.campaign_name, excluded.campaign_type, excluded.impressions, excluded.clicks,
    excluded.spend, excluded.conversions, excluded.conversion_value, excluded.currency,
    excluded.account_timezone, excluded.conversion_event_name, excluded.conversion_stage
)
"""

UPSERT_QUARANTINE = """
insert into growthpulse.quarantined_records as existing (
    quarantine_id, data_source, connector, row_index, issue_codes, issues, payload_sha256,
    raw_payload, first_seen_run_id, last_seen_run_id, ad_account_id
) values (
    %(quarantine_id)s, %(data_source)s, %(connector)s, %(row_index)s, %(issue_codes)s,
    %(issues)s, %(payload_sha256)s, %(raw_payload)s, %(run_id)s, %(run_id)s,
    %(ad_account_id)s
)
on conflict (quarantine_id) do update set
    row_index = excluded.row_index,
    issue_codes = excluded.issue_codes,
    issues = excluded.issues,
    last_seen_run_id = excluded.last_seen_run_id,
    last_seen_at = now()
"""

# Reports are never updated. The same content hash means the same report.
INSERT_REPORT = """
insert into growthpulse.analytics_reports (
    report_id, run_id, data_source, report_schema_version, data_as_of, period_start,
    period_end, record_count, report, client_id
) values (
    %(report_id)s, %(run_id)s, %(data_source)s, %(report_schema_version)s, %(data_as_of)s,
    %(period_start)s, %(period_end)s, %(record_count)s, %(report)s, %(client_id)s
)
on conflict (report_id) do nothing
"""

SELECT_REPORT_CLIENT = """
select client_id from growthpulse.analytics_reports where report_id = %(report_id)s
"""

SELECT_FOREIGN_FINDINGS = """
select count(*) from growthpulse.findings
where finding_id = any(%(finding_ids)s) and client_id <> %(client_id)s
"""

UPSERT_FINDING = """
insert into growthpulse.findings as existing (
    finding_id, data_source, rule_id, severity, pre_guard_severity, direction, guard_outcome,
    is_suppressed, platform, account_id, campaign_id, basis_id, metric, comparison_label,
    comparison_purpose, baseline_start, baseline_end, current_start, current_end,
    percent_change, payload, first_report_id, latest_report_id, latest_data_as_of,
    client_id, ad_account_id
) values (
    %(finding_id)s, %(data_source)s, %(rule_id)s, %(severity)s, %(pre_guard_severity)s,
    %(direction)s, %(guard_outcome)s, %(is_suppressed)s, %(platform)s, %(account_id)s,
    %(campaign_id)s, %(basis_id)s, %(metric)s, %(comparison_label)s, %(comparison_purpose)s,
    %(baseline_start)s, %(baseline_end)s, %(current_start)s, %(current_end)s,
    %(percent_change)s, %(payload)s, %(report_id)s, %(report_id)s, %(data_as_of)s,
    %(client_id)s, %(ad_account_id)s
)
on conflict (finding_id) do update set
    severity = excluded.severity,
    pre_guard_severity = excluded.pre_guard_severity,
    guard_outcome = excluded.guard_outcome,
    is_suppressed = excluded.is_suppressed,
    percent_change = excluded.percent_change,
    payload = excluded.payload,
    latest_report_id = excluded.latest_report_id,
    latest_data_as_of = excluded.latest_data_as_of,
    last_seen_at = now()
-- Only a different, newer report moves a finding forward. Saving an older report again
-- never overwrites newer state.
where existing.latest_report_id <> excluded.latest_report_id
    and existing.latest_data_as_of <= excluded.latest_data_as_of
"""

# Append-only, one per report. The same content hash means the same breakdown.
INSERT_BREAKDOWN = """
insert into growthpulse.analytics_report_breakdowns (
    report_id, client_id, breakdown_id, breakdown_schema_version, breakdown
) values (
    %(report_id)s, %(client_id)s, %(breakdown_id)s, %(breakdown_schema_version)s,
    %(breakdown)s
)
on conflict (report_id) do nothing
"""

SELECT_BREAKDOWN = """
select breakdown_id, client_id from growthpulse.analytics_report_breakdowns
where report_id = %(report_id)s
"""

FINISH_RUN = """
update growthpulse.pipeline_runs
set status = 'succeeded', finished_at = now(), counts = %(counts)s, report_id = %(report_id)s
where run_id = %(run_id)s and status = 'running'
"""

FINISH_SYNC_RUN = """
update growthpulse.pipeline_runs
set status = 'succeeded', finished_at = now(), counts = %(counts)s,
    data_quality = %(data_quality)s
where run_id = %(run_id)s and status = 'running' and run_kind = 'account_sync'
    and ad_account_id = %(ad_account_id)s
"""

FAIL_RUN = """
update growthpulse.pipeline_runs
set status = 'failed', finished_at = now(), counts = %(counts)s, error_code = %(error_code)s
where run_id = %(run_id)s and status = 'running'
"""

# Locks the account's sync state. RLS hides paused accounts from the pipeline, so a paused
# account returns no row at all.
CLAIM_SYNC_STATE = """
select s.status, s.lease_run_id, s.lease_expires_at, s.synced_through,
    s.consecutive_failures, c.enabled, c.frequency, c.preferred_local_hour, c.lookback_days,
    c.backfill_start_date, c.max_consecutive_failures, a.status, cl.status, a.client_id,
    a.data_source, a.platform, a.external_account_id,
    (select r.run_id from growthpulse.pipeline_runs as r
     where r.ad_account_id = s.ad_account_id and r.run_kind = 'account_sync'
       and r.status = 'failed'
       and r.started_at = (select max(l.started_at) from growthpulse.pipeline_runs as l
                           where l.ad_account_id = s.ad_account_id
                             and l.run_kind = 'account_sync'))
from growthpulse.account_sync_state as s
join growthpulse.account_sync_config as c on c.ad_account_id = s.ad_account_id
join growthpulse.ad_accounts as a on a.ad_account_id = s.ad_account_id
join growthpulse.clients as cl on cl.client_id = a.client_id
where s.ad_account_id = %(ad_account_id)s
for update of s
"""

TAKE_LEASE = """
update growthpulse.account_sync_state
set status = 'running', lease_run_id = %(run_id)s, lease_expires_at = %(lease_expires_at)s,
    last_attempt_at = %(now)s
where ad_account_id = %(ad_account_id)s
"""

CHECK_LEASE = """
select lease_run_id from growthpulse.account_sync_state
where ad_account_id = %(ad_account_id)s
for update
"""

# Never moves the watermark backwards: a lookback re-fetch of older days keeps it.
SYNC_SUCCEEDED = """
update growthpulse.account_sync_state
set status = 'idle',
    synced_through = greatest(coalesce(synced_through, %(window_end)s), %(window_end)s),
    last_success_at = %(now)s, consecutive_failures = 0, last_error_code = null,
    next_due_at = %(next_due_at)s, lease_run_id = null, lease_expires_at = null
where ad_account_id = %(ad_account_id)s and lease_run_id = %(run_id)s
"""

# The watermark is never touched by a failure.
SYNC_FAILED = """
update growthpulse.account_sync_state
set status = %(status)s, consecutive_failures = %(consecutive_failures)s,
    last_error_code = %(error_code)s, next_due_at = %(next_due_at)s,
    lease_run_id = null, lease_expires_at = null
where ad_account_id = %(ad_account_id)s and lease_run_id = %(run_id)s
"""

INSERT_PROPOSAL = """
insert into growthpulse.experiment_proposals (
    idempotency_key, data_source, report_id, finding_id, group_id, title, hypothesis,
    proposed_change, primary_metric, client_id
) values (
    %(idempotency_key)s, %(data_source)s, %(report_id)s, %(finding_id)s, %(group_id)s,
    %(title)s, %(hypothesis)s, %(proposed_change)s, %(primary_metric)s, %(client_id)s
)
on conflict (idempotency_key) do nothing
returning proposal_id
"""

SELECT_PROPOSAL = """
select proposal_id from growthpulse.experiment_proposals
where idempotency_key = %(idempotency_key)s
"""

INSERT_PROPOSED_STATUS = """
insert into growthpulse.experiment_status_events (proposal_id, status, actor)
values (%(proposal_id)s, 'proposed', 'pipeline')
"""


def _write_many(cursor: psycopg.Cursor, query: str, rows: Sequence[Row]) -> int:
    """Run an upsert for each row and return how many rows were inserted or changed."""
    if not rows:
        return 0
    cursor.executemany(query, rows)
    return cursor.rowcount


def start_run(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    data_source: DataSource,
    triggered_by: Trigger,
    client_id: UUID,
    code_version: str | None = None,
    run_kind: RunKind = "combined",
    window: DateWindow | None = None,
) -> None:
    """Commit a running combined or client-analysis run, so a failure has something to mark.

    Account-sync runs start through ``claim_account_sync``, which also takes the lease.
    """
    if run_kind == "account_sync":
        raise ValueError("Account syncs start with claim_account_sync")
    if code_version is not None and not CODE_VERSION_PATTERN.fullmatch(code_version):
        raise ValueError("code_version must be a lowercase hexadecimal Git commit hash")
    with connection.transaction():
        connection.execute(
            INSERT_RUN,
            _run_params(
                run_id, data_source, triggered_by, code_version, run_kind, client_id, None,
                window, 1, None,
            ),
        )  # fmt: skip


def _run_params(
    run_id: UUID,
    data_source: DataSource,
    triggered_by: Trigger,
    code_version: str | None,
    run_kind: RunKind,
    client_id: UUID,
    ad_account_id: UUID | None,
    window: DateWindow | None,
    attempt: int,
    retry_of_run_id: UUID | None,
) -> Row:
    return {
        "run_id": run_id,
        "data_source": data_source.value,
        "triggered_by": triggered_by,
        "code_version": code_version,
        "run_kind": run_kind,
        "client_id": client_id,
        "ad_account_id": ad_account_id,
        "window_start": window and window.start,
        "window_end": window and window.end,
        "attempt": attempt,
        "retry_of_run_id": retry_of_run_id,
    }


def _check_report_client(cursor: psycopg.Cursor, report_id: str, client_id: UUID) -> None:
    stored = cursor.execute(SELECT_REPORT_CLIENT, {"report_id": report_id}).fetchone()
    if stored is None or stored[0] != client_id:
        raise RunStateError("The report ID already belongs to another client")


def _check_no_foreign_findings(
    cursor: psycopg.Cursor, finding_params: Sequence[Row], client_id: UUID
) -> None:
    ids = [row["finding_id"] for row in finding_params]
    if not ids:
        return
    foreign = cursor.execute(
        SELECT_FOREIGN_FINDINGS, {"finding_ids": ids, "client_id": client_id}
    ).fetchone()
    if foreign is not None and foreign[0]:
        raise RunStateError("A finding ID already belongs to another client")


def _write_report_and_findings(
    cursor: psycopg.Cursor,
    run_id: UUID,
    report: Mapping[str, Any],
    client_id: UUID,
    finding_accounts: Mapping[str, UUID],
) -> dict[str, int]:
    finding_params = finding_rows(report, client_id=client_id, finding_accounts=finding_accounts)
    cursor.execute(INSERT_REPORT, report_row(report, run_id, client_id=client_id))
    counts = {"reports_written": cursor.rowcount}
    _check_report_client(cursor, report["report_id"], client_id)
    _check_no_foreign_findings(cursor, finding_params, client_id)
    counts["findings_received"] = len(finding_params)
    counts["findings_written"] = _write_many(cursor, UPSERT_FINDING, finding_params)
    return counts


def _finish(cursor: psycopg.Cursor, run_id: UUID, counts: dict[str, int], report_id: str) -> None:
    cursor.execute(FINISH_RUN, {"run_id": run_id, "counts": Jsonb(counts), "report_id": report_id})
    if cursor.rowcount != 1:
        # Raising inside the transaction rolls back every write before it.
        raise RunStateError("The pipeline run is missing or no longer running")


def save_run_results(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    client: ClientContext,
    data_source: DataSource,
    connector: str,
    records: Sequence[AdPerformanceRecord],
    quarantined: Sequence[QuarantinedRow],
    report: dict[str, Any],
) -> dict[str, int]:
    """Combined run: write one client's results and mark the run succeeded, all or nothing.

    Every record and finding must name one of the client's accounts. Anything else raises
    ``AccountMismatchError`` before a row is written: it is never re-filed.
    """
    if report["data_source"] != data_source.value:
        raise ValueError("The report's data source does not match the run's data source")
    if any(record.data_source is not data_source for record in records):
        raise ValueError("Every record must match the run's data source")
    if client.data_source is not data_source:
        raise ValueError("The client's data source does not match the run's data source")

    record_params = [
        record_row(
            record,
            run_id,
            ad_account_id=client.account_for(record.platform, record.account_id).ad_account_id,
        )
        for record in records
    ]
    finding_accounts = assign_findings(client, report)
    quarantine_params = quarantine_rows(
        quarantined, data_source=data_source, connector=connector, run_id=run_id
    )

    with connection.transaction(), connection.cursor() as cursor:
        counts = {
            "records_received": len(record_params),
            "records_written": _write_many(cursor, UPSERT_RECORD, record_params),
            "quarantined_rows": len(quarantined),
            "quarantine_entries_written": _write_many(cursor, UPSERT_QUARANTINE, quarantine_params),
        }
        counts |= _write_report_and_findings(
            cursor, run_id, report, client.client_id, finding_accounts
        )
        _finish(cursor, run_id, counts, report["report_id"])
    return counts


def mark_run_failed(
    connection: psycopg.Connection,
    run_id: UUID,
    error_code: str,
    counts: dict[str, int] | None = None,
) -> bool:
    """Record a sanitized failure. Returns False if the run was not running."""
    if not ERROR_CODE_PATTERN.fullmatch(error_code):
        raise ValueError("error_code must be a short snake_case code")
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            FAIL_RUN,
            {"run_id": run_id, "counts": Jsonb(counts or {}), "error_code": error_code},
        )
        return cursor.rowcount == 1


# Account sync


@dataclass(frozen=True, slots=True)
class SyncClaim:
    """What a claimed sync needs: its window inputs and schedule settings."""

    run_id: UUID
    attempt: int
    retry_of_run_id: UUID | None
    synced_through: date | None
    consecutive_failures: int
    frequency: SyncFrequency
    preferred_local_hour: int
    lookback_days: int
    backfill_start_date: date
    max_consecutive_failures: int


SYNCABLE = ("onboarding", "active")


def claim_account_sync(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    account: AccountContext,
    window: DateWindow,
    triggered_by: Trigger,
    now: datetime,
    code_version: str | None = None,
    lease: timedelta = LEASE_DURATION,
) -> SyncClaim:
    """Lock the account, take its lease and commit a running sync run, all at once.

    Refuses a paused, archived or disabled account, an archived client, an account whose
    syncing stopped for attention, and an account whose lease another run still holds. An
    expired lease is taken over, and the run that held it is marked failed as
    ``abandoned``.
    """
    if code_version is not None and not CODE_VERSION_PATTERN.fullmatch(code_version):
        raise ValueError("code_version must be a lowercase hexadecimal Git commit hash")
    with connection.transaction(), connection.cursor() as cursor:
        row = cursor.execute(CLAIM_SYNC_STATE, {"ad_account_id": account.ad_account_id}).fetchone()
        if row is None:
            raise AccountNotSyncableError("The account is unknown or paused")
        (
            status, lease_run_id, lease_expires_at, synced_through, failures, enabled,
            frequency, preferred_hour, lookback_days, backfill_start, max_failures,
            account_status, client_status, client_id, data_source, platform, external_id,
            last_failed_run,
        ) = row  # fmt: skip
        if (client_id, data_source, platform, external_id) != (
            account.client_id,
            account.data_source.value,
            account.platform.value,
            account.external_account_id,
        ):
            raise AccountMismatchError("The stored account differs from the account context")
        if not enabled or account_status not in SYNCABLE or client_status != "active":
            raise AccountNotSyncableError("The account, its sync or its client is not active")
        if status == "needs_attention":
            # Stopped after repeated or non-transient failures; the owner resumes it.
            raise AccountNotSyncableError("The account's syncing needs attention first")
        if status == "running":
            if lease_expires_at is not None and lease_expires_at > now:
                raise AccountBusyError("Another sync holds this account's lease")
            cursor.execute(
                FAIL_RUN,
                {"run_id": lease_run_id, "counts": Jsonb({}), "error_code": "abandoned"},
            )
        attempt = failures + 1
        cursor.execute(
            INSERT_RUN,
            _run_params(
                run_id, account.data_source, triggered_by, code_version, "account_sync",
                account.client_id, account.ad_account_id, window, attempt,
                last_failed_run if failures else None,
            ),
        )  # fmt: skip
        cursor.execute(
            TAKE_LEASE,
            {
                "run_id": run_id,
                "lease_expires_at": now + lease,
                "now": now,
                "ad_account_id": account.ad_account_id,
            },
        )
    return SyncClaim(
        run_id=run_id,
        attempt=attempt,
        retry_of_run_id=last_failed_run if failures else None,
        synced_through=synced_through,
        consecutive_failures=failures,
        frequency=SyncFrequency(frequency),
        preferred_local_hour=preferred_hour,
        lookback_days=lookback_days,
        backfill_start_date=backfill_start,
        max_consecutive_failures=max_failures,
    )


def _hold_lease(cursor: psycopg.Cursor, account: AccountContext, run_id: UUID) -> None:
    held = cursor.execute(CHECK_LEASE, {"ad_account_id": account.ad_account_id}).fetchone()
    if held is None or held[0] != run_id:
        raise RunStateError("This run no longer holds the account's lease")


def save_account_sync(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    account: AccountContext,
    connector: str,
    window: DateWindow,
    result: ValidationResult,
    now: datetime,
    next_due_at: datetime | None,
) -> dict[str, int]:
    """Write one account's sync and advance its watermark, all or nothing.

    Every record must belong to the account; the connector's account guard has already
    quarantined anything else, and this checks again before writing.
    """
    foreign = [
        r for r in result.records if (r.data_source, r.platform, r.account_id) != account.identity
    ]
    if foreign:
        raise AccountMismatchError("A record does not belong to the account being synced")
    record_params = [
        record_row(record, run_id, ad_account_id=account.ad_account_id) for record in result.records
    ]
    quarantine_params = quarantine_rows(
        result.quarantined,
        data_source=account.data_source,
        connector=connector,
        run_id=run_id,
        ad_account_id=account.ad_account_id,
    )
    # What this sync saw, kept on its run so analyses of any window can count each bad row
    # once, however many syncs re-fetched it.
    evidence = {
        "evidence_version": QUALITY_EVIDENCE_VERSION,
        "summary": result.summary,
        "quarantine": quarantine_evidence(
            result.quarantined,
            data_source=account.data_source,
            connector=connector,
            ad_account_id=account.ad_account_id,
        ),
    }
    with connection.transaction(), connection.cursor() as cursor:
        _hold_lease(cursor, account, run_id)
        counts = {
            "records_received": len(record_params),
            "records_written": _write_many(cursor, UPSERT_RECORD, record_params),
            "quarantined_rows": len(result.quarantined),
            "quarantine_entries_written": _write_many(cursor, UPSERT_QUARANTINE, quarantine_params),
        }
        cursor.execute(
            FINISH_SYNC_RUN,
            {
                "run_id": run_id,
                "counts": Jsonb(counts),
                "data_quality": Jsonb(evidence),
                "ad_account_id": account.ad_account_id,
            },
        )
        if cursor.rowcount != 1:
            raise RunStateError("The sync run is missing or no longer running")
        cursor.execute(
            SYNC_SUCCEEDED,
            {
                "ad_account_id": account.ad_account_id,
                "run_id": run_id,
                "window_end": window.end,
                "now": now,
                "next_due_at": next_due_at,
            },
        )
        if cursor.rowcount != 1:
            raise RunStateError("The account's sync state changed during the run")
    return counts


def record_sync_failure(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    account: AccountContext,
    error_code: str,
    decision: FailureDecision,
    counts: dict[str, int] | None = None,
) -> bool:
    """Mark the sync run failed and schedule the account, leaving its watermark alone."""
    if not ERROR_CODE_PATTERN.fullmatch(error_code):
        raise ValueError("error_code must be a short snake_case code")
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            FAIL_RUN,
            {"run_id": run_id, "counts": Jsonb(counts or {}), "error_code": error_code},
        )
        failed = cursor.rowcount == 1
        cursor.execute(
            SYNC_FAILED,
            {
                "ad_account_id": account.ad_account_id,
                "run_id": run_id,
                "status": decision.status.value,
                "consecutive_failures": decision.consecutive_failures,
                "error_code": error_code,
                "next_due_at": decision.next_attempt_at,
            },
        )
        return failed and cursor.rowcount == 1


# Client analysis


def save_client_analysis(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    client_id: UUID,
    data_source: DataSource,
    report: dict[str, Any],
    breakdown: dict[str, Any],
    finding_accounts: Mapping[str, UUID],
) -> dict[str, int]:
    """Write a client's report, findings and account breakdown, and finish the run.

    A report or breakdown already stored for the same content is kept as it is; one stored
    for another client, or a breakdown that differs for the same report, is an error.
    """
    if report["data_source"] != data_source.value:
        raise ValueError("The report's data source does not match the run's")
    if breakdown["report_id"] != report["report_id"]:
        raise ValueError("The breakdown belongs to another report")
    with connection.transaction(), connection.cursor() as cursor:
        counts = _write_report_and_findings(cursor, run_id, report, client_id, finding_accounts)
        cursor.execute(
            INSERT_BREAKDOWN,
            {
                "report_id": report["report_id"],
                "client_id": client_id,
                "breakdown_id": breakdown["breakdown_id"],
                "breakdown_schema_version": breakdown["breakdown_schema_version"],
                "breakdown": Jsonb(breakdown),
            },
        )
        counts["breakdowns_written"] = cursor.rowcount
        stored = cursor.execute(SELECT_BREAKDOWN, {"report_id": report["report_id"]}).fetchone()
        if stored is None or tuple(stored) != (breakdown["breakdown_id"], client_id):
            raise RunStateError("A different breakdown is already stored for this report")
        _finish(cursor, run_id, counts, report["report_id"])
    return counts


# Experiment proposals


@dataclass(frozen=True, slots=True)
class ExperimentProposal:
    """An advisory experiment idea. Never applied to a campaign by this system."""

    data_source: DataSource
    client_id: UUID
    report_id: str
    title: str
    hypothesis: str
    proposed_change: str
    primary_metric: str
    finding_id: str | None = None
    group_id: str | None = None

    def __post_init__(self) -> None:
        if self.finding_id is None and self.group_id is None:
            raise ValueError("A proposal must reference a finding or a finding group")

    @property
    def idempotency_key(self) -> str:
        identity = [self.report_id, self.finding_id, self.group_id, self.title]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def record_experiment_proposal(
    connection: psycopg.Connection, proposal: ExperimentProposal
) -> UUID:
    """Store a proposal with its initial 'proposed' status. Saving it again is a no-op."""
    params = {
        "idempotency_key": proposal.idempotency_key,
        "data_source": proposal.data_source.value,
        "client_id": proposal.client_id,
        "report_id": proposal.report_id,
        "finding_id": proposal.finding_id,
        "group_id": proposal.group_id,
        "title": proposal.title,
        "hypothesis": proposal.hypothesis,
        "proposed_change": proposal.proposed_change,
        "primary_metric": proposal.primary_metric,
    }
    with connection.transaction(), connection.cursor() as cursor:
        created = cursor.execute(INSERT_PROPOSAL, params).fetchone()
        if created is None:
            existing = cursor.execute(SELECT_PROPOSAL, params).fetchone()
            if existing is None:
                raise RunStateError("The proposal could not be stored or found")
            return existing[0]
        cursor.execute(INSERT_PROPOSED_STATUS, {"proposal_id": created[0]})
        return created[0]
