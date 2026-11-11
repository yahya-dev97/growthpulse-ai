"""Account-level breakdown of one client's analysis.

The client report itself is unchanged, so its ID stays a pure function of the records. This
breakdown is a separate deterministic document linked to that report by ``report_id``,
with its own content hash.

Incompatible metrics are never combined:

- Each account is reported on its own, in its own currency and timezone.
- Conversions are totalled only within one conversion basis (platform, event, stage and
  attribution settings). Accounts or campaigns on different bases are never added up.
- The compatibility section states which of those dimensions differ across the client's
  accounts, so a reader can see why no client-wide total is given.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from growthpulse_ai.analytics.errors import AnalyticsError
from growthpulse_ai.analytics.kpis import ConversionBasis, calculate_kpis, validate_records
from growthpulse_ai.analytics.report import dumps_report, to_json_compatible
from growthpulse_ai.schema import AdPerformanceRecord

BREAKDOWN_SCHEMA_VERSION = "1.0"

BREAKDOWN_NOTES = (
    "Each account is reported separately, in its own currency.",
    "Totals never span currencies. Convert outside this report if a single figure is needed.",
    "Conversions are totalled only within one conversion basis, which fixes the platform, "
    "conversion event, stage and attribution settings.",
    "Reporting dates are each account's local dates, so accounts in different timezones "
    "cover slightly different hours.",
)


def _period(rows: list[AdPerformanceRecord]) -> dict[str, str]:
    return {
        "start": min(row.reporting_date for row in rows).isoformat(),
        "end": max(row.reporting_date for row in rows).isoformat(),
    }


def build_account_breakdown(
    records: Iterable[AdPerformanceRecord], *, report_id: str
) -> dict[str, Any]:
    """Per-account KPIs and a compatibility summary for one client's records."""
    rows = sorted(records, key=lambda record: record.record_id)
    if not rows:
        raise AnalyticsError("Cannot build a breakdown from no records")
    validate_records(rows)

    by_account: defaultdict[tuple[str, str], list[AdPerformanceRecord]] = defaultdict(list)
    for row in rows:
        by_account[(row.platform.value, row.account_id)].append(row)

    accounts = []
    bases_by_account: dict[str, list[str]] = {}
    for (platform, account_id), group in sorted(by_account.items()):
        kpis = calculate_kpis(group)
        bases = sorted({ConversionBasis.from_record(row).basis_id for row in group})
        bases_by_account[f"{platform}:{account_id}"] = bases
        accounts.append(
            {
                "platform": platform,
                "account_id": account_id,
                "currency": kpis.currency,
                "account_timezones": kpis.account_timezones,
                "period": _period(group),
                "record_count": len(group),
                "campaign_count": len({row.campaign_id for row in group}),
                "kpis": kpis,
            }
        )

    currencies = sorted({row.currency for row in rows})
    timezones = sorted({row.account_timezone for row in rows})
    bases = sorted({basis for values in bases_by_account.values() for basis in values})
    attribution = sorted(
        {
            (
                row.platform.value,
                row.attribution.attribution_model.value if row.attribution else None,
                row.attribution.click_window_days if row.attribution else None,
                row.attribution.view_window_days if row.attribution else None,
            )
            for row in rows
        },
        key=lambda item: tuple("" if part is None else str(part) for part in item),
    )
    breakdown = to_json_compatible(
        {
            "breakdown_schema_version": BREAKDOWN_SCHEMA_VERSION,
            "report_id": report_id,
            "data_source": rows[0].data_source.value,
            "accounts": accounts,
            "compatibility": {
                "currencies": currencies,
                "account_timezones": timezones,
                "conversion_basis_ids": bases,
                "conversion_bases_by_account": bases_by_account,
                "attribution_settings": [
                    {
                        "platform": platform,
                        "attribution_model": model,
                        "click_window_days": click,
                        "view_window_days": view,
                    }
                    for platform, model, click, view in attribution
                ],
                "mixed_currencies": len(currencies) > 1,
                "mixed_timezones": len(timezones) > 1,
                "multiple_conversion_bases": len(bases) > 1,
                "mixed_attribution_settings": len(attribution) > 1,
            },
            "notes": BREAKDOWN_NOTES,
        }
    )
    breakdown["breakdown_id"] = hashlib.sha256(dumps_report(breakdown).encode()).hexdigest()
    return breakdown
