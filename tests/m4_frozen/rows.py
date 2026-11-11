"""Map pipeline objects to database row parameters. Pure functions, no I/O.

Reports arrive as the JSON-compatible dict built by analytics, so this package depends on
the shared schema only and never imports analytics.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from growthpulse_ai.data_quality import QuarantinedRow
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

Row = dict[str, Any]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def record_row(record: AdPerformanceRecord, run_id: UUID) -> Row:
    attribution = record.attribution
    return {
        "record_id": record.record_id,
        "data_source": record.data_source.value,
        "reporting_date": record.reporting_date,
        "platform": record.platform.value,
        "account_id": record.account_id,
        "campaign_id": record.campaign_id,
        "campaign_name": record.campaign_name,
        "campaign_type": record.campaign_type.value,
        "impressions": record.impressions,
        "clicks": record.clicks,
        "spend": record.spend,
        "conversions": record.conversions,
        "conversion_value": record.conversion_value,
        "currency": record.currency,
        "account_timezone": record.account_timezone,
        "conversion_event_id": record.conversion_definition.event_id,
        "conversion_event_name": record.conversion_definition.event_name,
        "conversion_stage": record.conversion_definition.stage.value,
        "attribution_model": attribution and attribution.attribution_model.value,
        "click_window_days": attribution and attribution.click_window_days,
        "view_window_days": attribution and attribution.view_window_days,
        "report_time_basis": attribution and attribution.report_time_basis.value,
        "ingested_at": record.ingested_at,
        "run_id": run_id,
    }


def _canonical_payload(raw: Any) -> str:
    """Serialize any raw row deterministically. Non-JSON values become strings."""
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def quarantine_rows(
    quarantined: Iterable[QuarantinedRow],
    *,
    data_source: DataSource,
    connector: str,
    run_id: UUID,
) -> list[Row]:
    """One row per distinct payload. Repeated payloads merge their issues.

    The ID depends on the payload, not its position, so the same bad row seen again in a
    later run updates the existing entry. Issues keep code, severity, and field only.
    The raw payload is stored for synthetic data only.
    """
    # payload hash -> (first row index, canonical payload, issues from every repeat)
    grouped: dict[str, tuple[int, str, set[tuple[str, str, str | None]]]] = {}
    for item in quarantined:
        canonical = _canonical_payload(item.raw)
        _, _, issues = grouped.setdefault(_sha256(canonical), (item.row_index, canonical, set()))
        issues.update((i.code.value, i.severity.value, i.field) for i in item.issues)

    rows = []
    for payload_sha256, (row_index, canonical, issues) in grouped.items():
        ordered = sorted(issues, key=lambda issue: (issue[0], issue[1], issue[2] or ""))
        rows.append(
            {
                "quarantine_id": _sha256(
                    json.dumps([data_source.value, connector, payload_sha256])
                ),
                "data_source": data_source.value,
                "connector": connector,
                "row_index": row_index,
                "issue_codes": sorted({code for code, _, _ in ordered}),
                "issues": Jsonb(
                    [{"code": code, "severity": sev, "field": fld} for code, sev, fld in ordered]
                ),
                "payload_sha256": payload_sha256,
                "raw_payload": (
                    Jsonb(json.loads(canonical)) if data_source is DataSource.SYNTHETIC else None
                ),
                "run_id": run_id,
            }
        )
    return sorted(rows, key=lambda row: row["quarantine_id"])


def report_row(report: Mapping[str, Any], run_id: UUID) -> Row:
    period = report["full_period"]["period"]
    return {
        "report_id": report["report_id"],
        "run_id": run_id,
        "data_source": report["data_source"],
        "report_schema_version": report["report_schema_version"],
        "data_as_of": datetime.fromisoformat(report["data_as_of"]),
        "period_start": date.fromisoformat(period["start"]),
        "period_end": date.fromisoformat(period["end"]),
        "record_count": report["input"]["record_count"],
        "report": Jsonb(report),
    }


def _decimal_or_none(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def finding_rows(report: Mapping[str, Any]) -> list[Row]:
    """Active and suppressed findings from every comparison in the report."""
    data_as_of = datetime.fromisoformat(report["data_as_of"])
    rows: list[Row] = []
    for comparison in report["comparisons"]:
        for finding in (*comparison["findings"], *comparison["suppressed_findings"]):
            outcome = finding["noise_guard"]["outcome"]
            rows.append(
                {
                    "finding_id": finding["finding_id"],
                    "data_source": finding["data_source"],
                    "rule_id": finding["rule_id"],
                    "severity": finding["severity"],
                    "pre_guard_severity": finding["pre_guard_severity"],
                    "direction": finding["direction"],
                    "guard_outcome": outcome,
                    "is_suppressed": outcome == "suppressed",
                    "platform": finding["platform"],
                    "account_id": finding["account_id"],
                    "campaign_id": finding["campaign_id"],
                    "basis_id": finding["conversion_basis"]["basis_id"],
                    "metric": finding["metric"],
                    "comparison_label": comparison["label"],
                    "comparison_purpose": comparison["purpose"],
                    "baseline_start": date.fromisoformat(finding["baseline_period"]["start"]),
                    "baseline_end": date.fromisoformat(finding["baseline_period"]["end"]),
                    "current_start": date.fromisoformat(finding["current_period"]["start"]),
                    "current_end": date.fromisoformat(finding["current_period"]["end"]),
                    "percent_change": _decimal_or_none(finding["percent_change"]),
                    "payload": Jsonb(finding),
                    "report_id": report["report_id"],
                    "data_as_of": data_as_of,
                }
            )
    return sorted(rows, key=lambda row: row["finding_id"])
