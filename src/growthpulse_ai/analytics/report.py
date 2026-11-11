"""Deterministic, JSON-compatible analytics report.

Values are unformatted: no currency symbols, percent signs, or display rounding, which
belong to presentation layers. Decimals become strings so no precision is lost to floats.
The report contains no clock reading, so the same records always give the same report.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any

from growthpulse_ai.analytics.anomalies import (
    DEFAULT_THRESHOLDS,
    AnomalyThresholds,
    DetectionResult,
    detect_anomalies,
)
from growthpulse_ai.analytics.errors import AnalyticsError
from growthpulse_ai.analytics.kpis import (
    KpiSet,
    calculate_kpis,
    compare_conversions,
    compare_delivery,
    validate_records,
)
from growthpulse_ai.analytics.periods import (
    ComparisonPurpose,
    PeriodComparison,
    ReportingPeriod,
    ensure_complete,
    latest_vs_first,
    latest_vs_previous,
    records_in,
)
from growthpulse_ai.analytics.trends import assess_persistence, group_related_findings
from growthpulse_ai.schema import AdPerformanceRecord

REPORT_SCHEMA_VERSION = "1.1"
_DECIMAL_SCALE = Decimal("0.00000001")

METRIC_DEFINITIONS = {
    "ctr": "clicks / impressions, as a fraction. Null when impressions are zero.",
    "cpc": "spend / clicks. Null when clicks are zero.",
    "platform_attributed_conversion_rate": (
        "platform-attributed conversions / clicks, as a fraction. Null when clicks are zero. "
        "Can include view-through credit."
    ),
    "cost_per_platform_attributed_conversion": (
        "spend / platform-attributed conversions. Null when conversions are zero. "
        "A proxy, not true customer acquisition cost."
    ),
    "platform_attributed_roas": (
        "platform-reported conversion value / spend. Null when spend is zero. Not realized return."
    ),
    "absolute_change": "current - baseline. Null when either value is null.",
    "percent_change": (
        "(current - baseline) / baseline, in percent. Null when the baseline is zero or null."
    ),
    "noise_guard": (
        "A change clears the guard when |ln(current / baseline)| >= width * sqrt(1/B + 1/C), "
        "where B and C are platform-attributed conversion counts. A heuristic, not a "
        "significance test or confidence interval."
    ),
}

REPORT_LIMITATIONS = (
    "Conversions are platform-attributed. Actual bookings and true customer acquisition cost "
    "are not available in this data.",
    "Conversions are reported per conversion basis and are never totalled across bases.",
    "Findings are threshold-based. No statistical significance test was performed.",
    "Findings describe what changed and do not establish causes.",
    "Reporting dates are in each account's own timezone.",
    "Recent days can be restated as late conversions are reported.",
    "Comparisons have different windows and are reported separately, not combined.",
    "Finding groups show shared metric inputs and do not establish causes.",
    "A persistence assessment never confirms that an issue has resolved.",
)


def to_json_compatible(value: Any) -> Any:
    """Convert analytics objects to plain JSON types."""
    if isinstance(value, Decimal):
        rounded = value.quantize(_DECIMAL_SCALE, rounding=ROUND_HALF_EVEN)
        return "0" if rounded.is_zero() else format(rounded.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json_compatible(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_compatible(item) for item in value]
    return value


def dumps_report(report: dict[str, Any]) -> str:
    """Serialize a report canonically, so equal reports are byte-identical."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def default_comparisons(start: date, end: date) -> tuple[PeriodComparison, ...]:
    return (latest_vs_previous(end, days=7), latest_vs_first(start, end, days=10))


def _campaign_groups(
    records: Iterable[AdPerformanceRecord],
) -> list[tuple[tuple[str, str, str], list[AdPerformanceRecord]]]:
    groups: defaultdict[tuple[str, str, str], list[AdPerformanceRecord]] = defaultdict(list)
    for record in records:
        groups[(record.platform.value, record.account_id, record.campaign_id)].append(record)
    return sorted(groups.items())


def _currency_groups(
    records: Iterable[AdPerformanceRecord],
) -> list[tuple[str, list[AdPerformanceRecord]]]:
    groups: defaultdict[str, list[AdPerformanceRecord]] = defaultdict(list)
    for record in records:
        groups[record.currency].append(record)
    return sorted(groups.items())


def _campaign_identity(rows: Sequence[AdPerformanceRecord]) -> dict[str, Any]:
    latest = max(rows, key=lambda row: (row.reporting_date, row.record_id))
    return {
        "platform": latest.platform.value,
        "account_id": latest.account_id,
        "campaign_id": latest.campaign_id,
        "campaign_name": latest.campaign_name,
        "campaign_type": latest.campaign_type.value,
    }


def _period_kpis(records: list[AdPerformanceRecord]) -> dict[str, Any]:
    return {
        "portfolios": [calculate_kpis(rows) for _, rows in _currency_groups(records)],
        "campaigns": [
            {**_campaign_identity(rows), "kpis": calculate_kpis(rows)}
            for _, rows in _campaign_groups(records)
        ],
    }


def _changes(baseline: KpiSet, current: KpiSet) -> dict[str, Any]:
    shared = sorted(baseline.by_basis.keys() & current.by_basis.keys())
    return {
        "delivery": compare_delivery(baseline.delivery, current.delivery),
        "conversions_by_basis": {
            basis_id: compare_conversions(baseline.by_basis[basis_id], current.by_basis[basis_id])
            for basis_id in shared
        },
    }


def _pair_section(
    baseline_rows: list[AdPerformanceRecord], current_rows: list[AdPerformanceRecord]
) -> dict[str, Any]:
    baseline, current = calculate_kpis(baseline_rows), calculate_kpis(current_rows)
    return {"baseline": baseline, "current": current, "changes": _changes(baseline, current)}


def _comparison_section(
    rows: list[AdPerformanceRecord],
    comparison: PeriodComparison,
    detection: DetectionResult,
) -> dict[str, Any]:
    baseline_rows = records_in(rows, comparison.baseline)
    current_rows = records_in(rows, comparison.current)

    baseline_by_currency = dict(_currency_groups(baseline_rows))
    portfolios = [
        {"currency": currency, **_pair_section(baseline_by_currency[currency], group)}
        for currency, group in _currency_groups(current_rows)
        if currency in baseline_by_currency
    ]
    baseline_by_campaign = dict(_campaign_groups(baseline_rows))
    campaigns = [
        {**_campaign_identity(group), **_pair_section(baseline_by_campaign[key], group)}
        for key, group in _campaign_groups(current_rows)
        if key in baseline_by_campaign
    ]
    return {
        "label": comparison.label,
        "purpose": comparison.purpose,
        "baseline_period": comparison.baseline,
        "current_period": comparison.current,
        "portfolios": portfolios,
        "campaigns": campaigns,
        "findings": detection.findings,
        "suppressed_findings": detection.suppressed_findings,
        "finding_groups": group_related_findings(detection.findings),
        "exclusions": detection.exclusions,
    }


def _persistence_assessments(
    rows: list[AdPerformanceRecord],
    detections: list[tuple[PeriodComparison, DetectionResult]],
    thresholds: AnomalyThresholds,
) -> list[Any]:
    """Assess each sustained comparison against each recent one it precedes."""
    assessments: list[Any] = []
    for recent, recent_result in detections:
        if recent.purpose is not ComparisonPurpose.RECENT_CHANGE:
            continue
        for sustained, sustained_result in detections:
            if (
                sustained.purpose is ComparisonPurpose.SUSTAINED_CHANGE
                and sustained.baseline.end < recent.current.start
            ):
                assessments.extend(
                    assess_persistence(
                        rows, recent, recent_result, sustained, sustained_result, thresholds
                    )
                )
    return assessments


def build_report(
    records: Iterable[AdPerformanceRecord],
    comparisons: Sequence[PeriodComparison] | None = None,
    thresholds: AnomalyThresholds = DEFAULT_THRESHOLDS,
    data_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report. Input order does not affect the output."""
    rows = sorted(records, key=lambda record: record.record_id)
    if not rows:
        raise AnalyticsError("Cannot build a report from no records")
    validate_records(rows)

    first = min(row.reporting_date for row in rows)
    last = max(row.reporting_date for row in rows)
    full_period = ReportingPeriod(first, last)
    ensure_complete(rows, full_period)
    chosen = default_comparisons(first, last) if comparisons is None else comparisons
    detections = [
        (comparison, detect_anomalies(rows, comparison, thresholds)) for comparison in chosen
    ]

    data_source = rows[0].data_source.value
    report = to_json_compatible(
        {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "data_source": data_source,
            "is_synthetic": data_source == "synthetic",
            "true_cac_available": False,
            "data_as_of": max(row.ingested_at for row in rows),
            "input": {
                "record_count": len(rows),
                "record_ids_digest": hashlib.sha256(
                    "".join(row.record_id for row in rows).encode()
                ).hexdigest(),
            },
            "data_quality": data_quality,
            "metric_definitions": METRIC_DEFINITIONS,
            "thresholds": thresholds,
            "limitations": REPORT_LIMITATIONS,
            "full_period": {"period": full_period, **_period_kpis(rows)},
            "comparisons": [
                _comparison_section(rows, comparison, detection)
                for comparison, detection in detections
            ],
            "persistence_assessments": _persistence_assessments(rows, detections, thresholds),
        }
    )
    report["report_id"] = hashlib.sha256(dumps_report(report).encode()).hexdigest()
    return report
