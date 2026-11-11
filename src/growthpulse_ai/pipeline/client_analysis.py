"""Build one client's consolidated report from records already synced per account.

Analysis is independent of syncing: it receives stored records and each account's latest
validation summary, never a connector. It refuses any record or summary from an account
outside the client, so one client's analysis cannot include another client's data.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from growthpulse_ai.accounts.assignment import assign_findings, assign_records
from growthpulse_ai.accounts.models import AccountMismatchError, ClientContext
from growthpulse_ai.analytics.breakdown import build_account_breakdown
from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.data_quality import combine_summaries
from growthpulse_ai.schema import AdPerformanceRecord


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
    quality_summaries: Mapping[UUID, dict[str, Any]],
) -> ClientAnalysis:
    """One report and one account breakdown for the client, from its accounts' records."""
    by_account = assign_records(client, records)
    if not by_account:
        raise ValueError("A client analysis needs at least one record")
    known = {account.ad_account_id for account in client.accounts}
    if not set(quality_summaries) <= known:
        raise AccountMismatchError("A quality summary names an account outside this client")
    missing = set(by_account) - set(quality_summaries)
    if missing:
        raise ValueError("Every account with records needs its latest quality summary")

    # Accounts that synced but produced no valid rows still count: their quarantined rows
    # belong in the client's data quality summary.
    included = sorted(set(by_account) | set(quality_summaries), key=str)
    rows = [record for account_id in included for record in by_account.get(account_id, [])]
    report = build_report(
        rows, data_quality=combine_summaries(quality_summaries[a] for a in included)
    )
    return ClientAnalysis(
        client_id=client.client_id,
        ad_account_ids=tuple(included),
        report=report,
        breakdown=build_account_breakdown(rows, report_id=report["report_id"]),
        finding_accounts=assign_findings(client, report),
    )
