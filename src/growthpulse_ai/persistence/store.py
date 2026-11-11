"""Write pipeline results. Every write runs inside one explicit transaction.

A run is persisted in three steps, each in its own transaction:

1. ``start_run`` commits a ``running`` row before any data is read.
2. ``save_run_results`` writes records, quarantined rows, the report and findings, and
   marks the run ``succeeded``, all in one transaction. Any failure rolls all of it back.
3. ``mark_run_failed`` runs only after a failure, on a fresh connection, and stores a
   fixed error code, never an error message.

All writes are idempotent. Re-running the same input changes no rows: records and
findings update only when their values differ, and a report with the same content hash
is stored once.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from growthpulse_ai.data_quality import QuarantinedRow
from growthpulse_ai.persistence.errors import RunStateError
from growthpulse_ai.persistence.rows import (
    Row,
    finding_rows,
    quarantine_rows,
    record_row,
    report_row,
)
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

Trigger = Literal["manual", "scheduled", "test"]
ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z_]{0,63}")
CODE_VERSION_PATTERN = re.compile(r"[0-9a-f]{7,40}")

INSERT_RUN = """
insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, code_version)
values (%(run_id)s, %(data_source)s, %(triggered_by)s, %(code_version)s)
"""

UPSERT_RECORD = """
insert into growthpulse.ad_performance_records as existing (
    record_id, data_source, reporting_date, platform, account_id, campaign_id,
    campaign_name, campaign_type, impressions, clicks, spend, conversions, conversion_value,
    currency, account_timezone, conversion_event_id, conversion_event_name, conversion_stage,
    attribution_model, click_window_days, view_window_days, report_time_basis, ingested_at,
    first_seen_run_id, last_changed_run_id
) values (
    %(record_id)s, %(data_source)s, %(reporting_date)s, %(platform)s, %(account_id)s,
    %(campaign_id)s, %(campaign_name)s, %(campaign_type)s, %(impressions)s, %(clicks)s,
    %(spend)s, %(conversions)s, %(conversion_value)s, %(currency)s, %(account_timezone)s,
    %(conversion_event_id)s, %(conversion_event_name)s, %(conversion_stage)s,
    %(attribution_model)s, %(click_window_days)s, %(view_window_days)s,
    %(report_time_basis)s, %(ingested_at)s, %(run_id)s, %(run_id)s
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
    raw_payload, first_seen_run_id, last_seen_run_id
) values (
    %(quarantine_id)s, %(data_source)s, %(connector)s, %(row_index)s, %(issue_codes)s,
    %(issues)s, %(payload_sha256)s, %(raw_payload)s, %(run_id)s, %(run_id)s
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
    period_end, record_count, report
) values (
    %(report_id)s, %(run_id)s, %(data_source)s, %(report_schema_version)s, %(data_as_of)s,
    %(period_start)s, %(period_end)s, %(record_count)s, %(report)s
)
on conflict (report_id) do nothing
"""

UPSERT_FINDING = """
insert into growthpulse.findings as existing (
    finding_id, data_source, rule_id, severity, pre_guard_severity, direction, guard_outcome,
    is_suppressed, platform, account_id, campaign_id, basis_id, metric, comparison_label,
    comparison_purpose, baseline_start, baseline_end, current_start, current_end,
    percent_change, payload, first_report_id, latest_report_id, latest_data_as_of
) values (
    %(finding_id)s, %(data_source)s, %(rule_id)s, %(severity)s, %(pre_guard_severity)s,
    %(direction)s, %(guard_outcome)s, %(is_suppressed)s, %(platform)s, %(account_id)s,
    %(campaign_id)s, %(basis_id)s, %(metric)s, %(comparison_label)s, %(comparison_purpose)s,
    %(baseline_start)s, %(baseline_end)s, %(current_start)s, %(current_end)s,
    %(percent_change)s, %(payload)s, %(report_id)s, %(report_id)s, %(data_as_of)s
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

FINISH_RUN = """
update growthpulse.pipeline_runs
set status = 'succeeded', finished_at = now(), counts = %(counts)s, report_id = %(report_id)s
where run_id = %(run_id)s and status = 'running'
"""

FAIL_RUN = """
update growthpulse.pipeline_runs
set status = 'failed', finished_at = now(), counts = %(counts)s, error_code = %(error_code)s
where run_id = %(run_id)s and status = 'running'
"""

INSERT_PROPOSAL = """
insert into growthpulse.experiment_proposals (
    idempotency_key, data_source, report_id, finding_id, group_id, title, hypothesis,
    proposed_change, primary_metric
) values (
    %(idempotency_key)s, %(data_source)s, %(report_id)s, %(finding_id)s, %(group_id)s,
    %(title)s, %(hypothesis)s, %(proposed_change)s, %(primary_metric)s
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
    code_version: str | None = None,
) -> None:
    """Commit a running row, so a later failure has something to mark as failed."""
    if code_version is not None and not CODE_VERSION_PATTERN.fullmatch(code_version):
        raise ValueError("code_version must be a lowercase hexadecimal Git commit hash")
    with connection.transaction():
        connection.execute(
            INSERT_RUN,
            {
                "run_id": run_id,
                "data_source": data_source.value,
                "triggered_by": triggered_by,
                "code_version": code_version,
            },
        )


def save_run_results(
    connection: psycopg.Connection,
    run_id: UUID,
    *,
    data_source: DataSource,
    connector: str,
    records: Sequence[AdPerformanceRecord],
    quarantined: Sequence[QuarantinedRow],
    report: dict[str, Any],
) -> dict[str, int]:
    """Write everything a run produced and mark it succeeded, all or nothing."""
    if report["data_source"] != data_source.value:
        raise ValueError("The report's data source does not match the run's data source")
    if any(record.data_source is not data_source for record in records):
        raise ValueError("Every record must match the run's data source")

    record_params = [record_row(record, run_id) for record in records]
    quarantine_params = quarantine_rows(
        quarantined, data_source=data_source, connector=connector, run_id=run_id
    )
    finding_params = finding_rows(report)

    with connection.transaction(), connection.cursor() as cursor:
        counts = {
            "records_received": len(record_params),
            "records_written": _write_many(cursor, UPSERT_RECORD, record_params),
            "quarantined_rows": len(quarantined),
            "quarantine_entries_written": _write_many(cursor, UPSERT_QUARANTINE, quarantine_params),
        }
        cursor.execute(INSERT_REPORT, report_row(report, run_id))
        counts["reports_written"] = cursor.rowcount
        counts["findings_received"] = len(finding_params)
        counts["findings_written"] = _write_many(cursor, UPSERT_FINDING, finding_params)

        cursor.execute(
            FINISH_RUN,
            {"run_id": run_id, "counts": Jsonb(counts), "report_id": report["report_id"]},
        )
        if cursor.rowcount != 1:
            # Raising inside the block rolls back every write above.
            raise RunStateError("The pipeline run is missing or no longer running")
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


@dataclass(frozen=True, slots=True)
class ExperimentProposal:
    """An advisory experiment idea. Never applied to a campaign by this system."""

    data_source: DataSource
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
