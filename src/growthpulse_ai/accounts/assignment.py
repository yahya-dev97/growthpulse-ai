"""Map records and findings to the client account they belong to.

Records and findings name their account by ``(data_source, platform, account_id)``. Within
one client that identity is unique, so the mapping is unambiguous or it fails: nothing is
ever guessed or dropped silently.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from growthpulse_ai.accounts.models import AccountMismatchError, ClientContext
from growthpulse_ai.schema import AdPerformanceRecord


def assign_records(
    client: ClientContext, records: Iterable[AdPerformanceRecord]
) -> dict[UUID, list[AdPerformanceRecord]]:
    """Group records by the client's ad_account_id. Raises on any record from elsewhere."""
    grouped: defaultdict[UUID, list[AdPerformanceRecord]] = defaultdict(list)
    for record in records:
        if record.data_source is not client.data_source:
            raise AccountMismatchError("A record's data source differs from its client's")
        account = client.account_for(record.platform, record.account_id)
        grouped[account.ad_account_id].append(record)
    return dict(grouped)


def report_findings(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every active and suppressed finding in a report, in report order."""
    return [
        finding
        for comparison in report["comparisons"]
        for finding in (*comparison["findings"], *comparison["suppressed_findings"])
    ]


def assign_findings(client: ClientContext, report: Mapping[str, Any]) -> dict[str, UUID]:
    """finding_id -> ad_account_id for every finding in a report. Raises on a stray."""
    if report["data_source"] != client.data_source.value:
        raise AccountMismatchError("The report's data source differs from its client's")
    assigned: dict[str, UUID] = {}
    for finding in report_findings(report):
        account = client.account_for(finding["platform"], finding["account_id"])
        previous = assigned.setdefault(finding["finding_id"], account.ad_account_id)
        if previous != account.ad_account_id:
            raise AccountMismatchError("One finding ID names two different accounts")
    return assigned
