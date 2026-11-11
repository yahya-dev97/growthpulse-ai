"""Build and store one client's consolidated report from records already synced per account.

Analysis is independent of syncing: it reads stored records and the quality evidence of
every successful sync covering the window, and never calls a connector. It refuses any
record or evidence from an account outside the client, any window its syncs do not cover,
and any record whose currency or timezone differs from its
account, so one client's analysis can never include another client's data or silently
mix incompatible metrics. Currencies and conversion bases that legitimately differ
between accounts are kept apart by the report and flagged in the account breakdown.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg

from growthpulse_ai.accounts.assignment import assign_findings, assign_records
from growthpulse_ai.accounts.models import (
    AccountMismatchError,
    AccountStatus,
    ClientContext,
    ClientStatus,
)
from growthpulse_ai.accounts.scheduling import (
    DateWindow,
    common_complete_through,
    windows_cover,
)
from growthpulse_ai.analytics.breakdown import build_account_breakdown
from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.data_quality import QUALITY_EVIDENCE_VERSION, window_quality_summary
from growthpulse_ai.persistence.errors import ClientNotFoundError
from growthpulse_ai.persistence.load import load_records
from growthpulse_ai.persistence.store import (
    Trigger,
    mark_run_failed,
    save_client_analysis,
    start_run,
)
from growthpulse_ai.persistence.tenancy import (
    SyncEvidence,
    load_client,
    sync_evidence,
    watermarks,
)
from growthpulse_ai.pipeline.failures import (
    NoValidRecordsError,
    QualityEvidenceIncompleteError,
    classify_error,
)
from growthpulse_ai.schema import AdPerformanceRecord

logger = logging.getLogger(__name__)

Connect = Callable[[], psycopg.Connection]
DEFAULT_ANALYSIS_DAYS = 30


@dataclass(frozen=True, slots=True)
class ClientAnalysis:
    client_id: UUID
    ad_account_ids: tuple[UUID, ...]
    """Accounts included: those with records or a quality summary, sorted."""
    report: dict[str, Any]
    breakdown: dict[str, Any]
    finding_accounts: dict[str, UUID]
    """finding_id -> ad_account_id, for scoping stored findings."""


def analyse_client(
    client: ClientContext,
    records: Iterable[AdPerformanceRecord],
    quarantine_evidence: Iterable[dict[str, Any]] = (),
) -> ClientAnalysis:
    """One report and one account breakdown for the client, from its accounts' records.

    The report's data quality is computed from what is analysed, never from how it was
    synced: valid records and warnings from the (deduplicated) records themselves, and
    quarantined rows from deduplicated sync evidence. The same records and evidence give
    the same report, whatever order or overlap the syncs had.
    """
    by_account = assign_records(client, records)
    if not by_account:
        raise NoValidRecordsError("A client analysis needs at least one record")
    known = {account.ad_account_id: account for account in client.accounts}
    evidence = list(quarantine_evidence)
    if any(UUID(item["ad_account_id"]) not in known for item in evidence):
        raise AccountMismatchError("Quality evidence names an account outside this client")
    for account_id, account_records in by_account.items():
        account = known[account_id]
        if any(
            (r.currency, r.account_timezone) != (account.currency, account.timezone)
            for r in account_records
        ):
            raise AccountMismatchError("A record's currency or timezone differs from its account")

    included = sorted(set(by_account) | {UUID(item["ad_account_id"]) for item in evidence}, key=str)
    rows = [record for account_id in included for record in by_account.get(account_id, [])]
    report = build_report(rows, data_quality=window_quality_summary(rows, evidence))
    return ClientAnalysis(
        client_id=client.client_id,
        ad_account_ids=tuple(included),
        report=report,
        breakdown=build_account_breakdown(rows, report_id=report["report_id"]),
        finding_accounts=assign_findings(client, report),
    )


def window_evidence(
    scope: ClientContext, syncs: Iterable[SyncEvidence], window: DateWindow
) -> list[dict[str, Any]]:
    """Quarantine evidence for the window, after checking syncs cover it for every account.

    Only successful syncs count. An item counts when its own reporting date is inside the
    window; an item without a valid date counts when its sync's window overlaps the
    analysis window, so problems are surfaced rather than hidden. Overlaps are deduplicated
    later by quarantine ID.
    """
    by_account: dict[UUID, list[SyncEvidence]] = {a.ad_account_id: [] for a in scope.accounts}
    for sync in syncs:
        by_account[sync.ad_account_id].append(sync)
    for account_syncs in by_account.values():
        if not windows_cover([s.window for s in account_syncs], window):
            raise QualityEvidenceIncompleteError("Syncs do not cover the whole analysis window")
    selected = []
    for account_syncs in by_account.values():
        for sync in account_syncs:
            if sync.data_quality.get("evidence_version") != QUALITY_EVIDENCE_VERSION:
                raise QualityEvidenceIncompleteError("A sync stored no usable quality evidence")
            for item in sync.data_quality["quarantine"]:
                day = item["reporting_date"]
                if day is None or window.start <= date.fromisoformat(day) <= window.end:
                    selected.append(item)
    return selected


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    run_id: UUID
    client_id: UUID
    report_id: str
    window: DateWindow
    ad_account_ids: tuple[UUID, ...]
    counts: dict[str, int]


class ClientAnalysisError(Exception):
    """An analysis failed. Carries only IDs and a fixed code."""

    def __init__(self, client_id: UUID, run_id: UUID | None, error_code: str, *, recorded: bool):
        state = "recorded" if recorded else "not recorded"
        super().__init__(f"Analysis of client {client_id} failed: {error_code} ({state})")
        self.client_id = client_id
        self.run_id = run_id
        self.error_code = error_code
        self.recorded = recorded


def active_accounts_only(client: ClientContext) -> ClientContext:
    """The client limited to its active accounts: the only ones an analysis may read."""
    return ClientContext(
        client_id=client.client_id,
        name=client.name,
        data_source=client.data_source,
        accounts=tuple(a for a in client.accounts if a.status is AccountStatus.ACTIVE),
        status=client.status,
    )


def _plan(connection: psycopg.Connection, client_id: UUID, end: date | None, days: int):
    client = load_client(connection, client_id)
    if client.status is not ClientStatus.ACTIVE:
        raise ClientNotFoundError("The client is archived")
    scope = active_accounts_only(client)
    if not scope.accounts:
        raise ClientNotFoundError("The client has no active account")
    # Every active account must have synced through the end of the window; otherwise the
    # report would quietly describe incomplete data.
    marks = watermarks(connection, [a.ad_account_id for a in scope.accounts])
    complete = common_complete_through(marks.get(a.ad_account_id) for a in scope.accounts)
    if complete is None:
        raise NoValidRecordsError("An active account has never synced")
    if end is None:
        end = complete
    elif end > complete:
        raise NoValidRecordsError("An active account has not synced through the window's end")
    return scope, DateWindow(end - timedelta(days=days - 1), end)


def run_client_analysis(
    client_id: UUID,
    connect: Connect,
    *,
    triggered_by: Trigger,
    end: date | None = None,
    days: int = DEFAULT_ANALYSIS_DAYS,
    code_version: str | None = None,
    run_id: UUID | None = None,
) -> AnalysisOutcome:
    """Analyse one client's stored records and store the report, findings and breakdown.

    The window ends at ``end``, or at the last date every active account has synced.
    Running it again on unchanged data writes nothing new.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    run_id = run_id or uuid4()
    try:
        with connect() as connection:
            scope, window = _plan(connection, client_id, end, days)
            start_run(
                connection, run_id, data_source=scope.data_source, triggered_by=triggered_by,
                client_id=client_id, code_version=code_version, run_kind="client_analysis",
                window=window,
            )  # fmt: skip
    except Exception as error:
        code = classify_error(error)
        logger.error("analysis client=%s not_started code=%s", client_id, code)
        raise ClientAnalysisError(client_id, None, code, recorded=False) from None

    try:
        account_ids = [a.ad_account_id for a in scope.accounts]
        with connect() as connection:
            records = load_records(
                connection, data_source=scope.data_source, accounts=scope.accounts,
                start=window.start, end=window.end,
            )  # fmt: skip
            syncs = sync_evidence(connection, account_ids, window)
        analysis = analyse_client(scope, records, window_evidence(scope, syncs, window))
        with connect() as connection:
            counts = save_client_analysis(
                connection, run_id, client_id=client_id, data_source=scope.data_source,
                report=analysis.report, breakdown=analysis.breakdown,
                finding_accounts=analysis.finding_accounts,
            )  # fmt: skip
        counts = {"records_analysed": len(records), **counts}
    except Exception as error:
        code = classify_error(error)
        recorded = False
        try:
            with connect() as connection:
                recorded = mark_run_failed(connection, run_id, code)
        except Exception as failure:
            logger.error(
                "analysis client=%s run=%s failure_not_recorded cause=%s",
                client_id, run_id, type(failure).__name__,
            )  # fmt: skip
        logger.error("analysis client=%s run=%s failed code=%s", client_id, run_id, code)
        raise ClientAnalysisError(client_id, run_id, code, recorded=recorded) from None

    return AnalysisOutcome(
        run_id=run_id,
        client_id=client_id,
        report_id=analysis.report["report_id"],
        window=window,
        ad_account_ids=analysis.ad_account_ids,
        counts=counts,
    )
