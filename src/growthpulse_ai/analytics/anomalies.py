"""Transparent, threshold-based anomaly rules.

Findings state what changed, never why. No rule runs a statistical test, so nothing here
claims statistical significance. Every comparison stays within one campaign and one
conversion basis, and whatever cannot be compared is listed as an exclusion.

Findings inside the noise band are kept separately as suppressed findings, with their
evidence intact.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Any

from growthpulse_ai.analytics.errors import InvalidPeriodError
from growthpulse_ai.analytics.kpis import (
    ConversionBasis,
    ConversionKpis,
    MetricChange,
    calculate_kpis,
    compare_metric,
    safe_divide,
    validate_records,
)
from growthpulse_ai.analytics.noise import (
    NOT_APPLICABLE,
    NoiseGuardOutcome,
    NoiseGuardResult,
    evaluate_noise_guard,
)
from growthpulse_ai.analytics.periods import (
    PeriodComparison,
    ReportingPeriod,
    ensure_complete,
    records_in,
)
from growthpulse_ai.schema import AdPerformanceRecord, DataSource


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


class Direction(StrEnum):
    ADVERSE = "adverse"
    POSITIVE = "positive"


class RuleId(StrEnum):
    COST_PER_CONVERSION_INCREASE = "cost_per_conversion_increase"
    CONVERSION_RATE_DECLINE = "conversion_rate_decline"
    SPEND_WITHOUT_CONVERSION_GROWTH = "spend_without_conversion_growth"
    SPEND_WITH_ZERO_CONVERSIONS = "spend_with_zero_conversions"
    STRONG_PERFORMER = "strong_performer"
    ROAS_DETERIORATION = "roas_deterioration"


class ExclusionReason(StrEnum):
    INCOMPLETE_CAMPAIGN_COVERAGE = "incomplete_campaign_coverage"
    BASIS_NOT_IN_BOTH_PERIODS = "conversion_basis_not_in_both_periods"
    INCOMPLETE_BASIS_COVERAGE = "incomplete_conversion_basis_coverage"
    ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"
    INSUFFICIENT_VOLUME = "insufficient_volume"


@dataclass(frozen=True, slots=True)
class AnomalyThresholds:
    """Rule settings. Percent values are in percent, so 25 means 25%."""

    # Volume safeguards, per period
    min_spend: Decimal = Decimal(100)
    min_clicks: int = 100
    min_baseline_conversions: Decimal = Decimal(10)
    min_current_conversions: Decimal = Decimal(3)
    # Cost per platform-attributed conversion
    cost_increase_percent: Decimal = Decimal(25)
    cost_increase_high_percent: Decimal = Decimal(50)
    min_cost_increase_absolute: Decimal = Decimal(5)
    # Conversion rate
    conversion_rate_decline_percent: Decimal = Decimal(20)
    conversion_rate_decline_high_percent: Decimal = Decimal(40)
    # Spend growing faster than conversions
    spend_increase_percent: Decimal = Decimal(20)
    proportional_conversion_ratio: Decimal = Decimal("0.5")
    spend_gap_high_percent: Decimal = Decimal(40)
    # Consecutive days of spend with zero conversions
    zero_run_min_days: int = 3
    zero_run_high_days: int = 5
    zero_run_min_spend: Decimal = Decimal(200)
    zero_run_min_expected_conversions: Decimal = Decimal(3)
    # ROAS
    roas_decline_percent: Decimal = Decimal(25)
    roas_decline_high_percent: Decimal = Decimal(50)
    min_baseline_conversion_value: Decimal = Decimal(500)
    # Strong performers, relative to peers on the same conversion basis
    strong_min_peers: int = 3
    strong_min_conversions: Decimal = Decimal(20)
    strong_max_cost_ratio_to_peers: Decimal = Decimal("0.6")
    strong_min_roas_ratio_to_peers: Decimal = Decimal("1.25")
    strong_max_cost_increase_percent: Decimal = Decimal(10)
    # Noise guard for the cost, conversion rate, and ROAS rules. Set both to 0 to disable.
    noise_guard_width: Decimal = Decimal("1.5")
    noise_guard_high_severity_width: Decimal = Decimal("2.0")

    def __post_init__(self) -> None:
        if any(getattr(self, field.name) < 0 for field in fields(self)):
            raise InvalidPeriodError("Thresholds must not be negative")
        tiers = (
            (self.cost_increase_percent, self.cost_increase_high_percent),
            (self.conversion_rate_decline_percent, self.conversion_rate_decline_high_percent),
            (self.roas_decline_percent, self.roas_decline_high_percent),
            (self.zero_run_min_days, self.zero_run_high_days),
            (self.noise_guard_width, self.noise_guard_high_severity_width),
        )
        if any(medium > high for medium, high in tiers):
            raise InvalidPeriodError("High-severity thresholds must not be below medium ones")


DEFAULT_THRESHOLDS = AnomalyThresholds()

COST_METRIC = "cost_per_platform_attributed_conversion"
RATE_METRIC = "platform_attributed_conversion_rate"
CONVERSIONS_METRIC = "platform_attributed_conversions"
ROAS_METRIC = "platform_attributed_roas"

METRIC_LABELS = {
    COST_METRIC: "Cost per platform-attributed conversion (proxy, not true CAC)",
    RATE_METRIC: "Platform-attributed conversion rate",
    CONVERSIONS_METRIC: "Platform-attributed conversions",
    ROAS_METRIC: "Platform-attributed ROAS",
    "spend": "Spend",
}

LIMIT_ATTRIBUTED = "Conversions are platform-attributed and may differ from actual bookings."
LIMIT_PROXY = (
    "Cost per platform-attributed conversion is a proxy and is not true customer acquisition cost."
)
LIMIT_NO_TEST = "This is a threshold rule. No statistical significance test was performed."
LIMIT_NO_CAUSE = "The data shows that this changed. It does not establish why."
LIMIT_LAG = "Recent days can be restated as late conversions are reported."
LIMIT_SYNTHETIC = "This finding is based on synthetic data, not real campaign performance."
LIMIT_INCREMENTALITY = "Strong attributed efficiency does not establish incremental impact."
LIMIT_VALUE = "Conversion value is platform-reported and is not realized revenue."
LIMIT_GUARD = (
    "The noise guard is a heuristic on platform-attributed conversion counts, which are not "
    "verified customers or bookings. It is not a significance test or a confidence interval."
)


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    rule_id: RuleId
    severity: Severity
    direction: Direction
    data_source: str
    platform: str
    account_id: str
    campaign_id: str
    campaign_name: str
    currency: str
    account_timezone: str
    conversion_basis: ConversionBasis
    metric: str
    metric_label: str
    baseline_kind: str
    baseline_value: Decimal | int | None
    current_value: Decimal | int | None
    absolute_change: Decimal | None
    percent_change: Decimal | None
    baseline_period: ReportingPeriod
    current_period: ReportingPeriod
    comparison_label: str
    observation: str
    evidence: dict[str, Any]
    data_limitations: tuple[str, ...]
    pre_guard_severity: Severity
    noise_guard: NoiseGuardResult


@dataclass(frozen=True, slots=True)
class Exclusion:
    platform: str
    account_id: str
    campaign_id: str
    basis_id: str | None
    reason: ExclusionReason


@dataclass(frozen=True, slots=True)
class DetectionResult:
    findings: tuple[Finding, ...]
    suppressed_findings: tuple[Finding, ...]
    exclusions: tuple[Exclusion, ...]


@dataclass(frozen=True, slots=True)
class _Subject:
    """One campaign on one conversion basis, across both periods."""

    sample: AdPerformanceRecord
    baseline: ConversionKpis
    current: ConversionKpis
    current_rows: tuple[AdPerformanceRecord, ...]
    comparison: PeriodComparison

    @property
    def basis(self) -> ConversionBasis:
        return self.current.basis


def _finding_id(subject: _Subject, rule_id: RuleId, metric: str) -> str:
    record = subject.sample
    identity = [
        record.data_source.value,
        rule_id.value,
        record.platform.value,
        record.account_id,
        record.campaign_id,
        subject.basis.basis_id,
        metric,
        subject.comparison.baseline.start.isoformat(),
        subject.comparison.baseline.end.isoformat(),
        subject.comparison.current.start.isoformat(),
        subject.comparison.current.end.isoformat(),
    ]
    canonical = json.dumps(identity, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _volume_evidence(subject: _Subject) -> dict[str, Any]:
    baseline, current = subject.baseline, subject.current
    return {
        "baseline_spend": baseline.spend,
        "current_spend": current.spend,
        "baseline_clicks": baseline.clicks,
        "current_clicks": current.clicks,
        "baseline_conversions": baseline.platform_attributed_conversions,
        "current_conversions": current.platform_attributed_conversions,
    }


def _finding(
    subject: _Subject,
    rule_id: RuleId,
    severity: Severity,
    change: MetricChange,
    observation: str,
    evidence: dict[str, Any],
    limitations: tuple[str, ...],
    direction: Direction = Direction.ADVERSE,
    baseline_kind: str = "baseline_period",
) -> Finding:
    record = subject.sample
    all_limitations = (LIMIT_ATTRIBUTED, *limitations, LIMIT_NO_TEST, LIMIT_NO_CAUSE, LIMIT_LAG)
    if record.data_source is DataSource.SYNTHETIC:
        all_limitations = (*all_limitations, LIMIT_SYNTHETIC)
    return Finding(
        finding_id=_finding_id(subject, rule_id, change.metric),
        rule_id=rule_id,
        severity=severity,
        direction=direction,
        data_source=record.data_source.value,
        platform=record.platform.value,
        account_id=record.account_id,
        campaign_id=record.campaign_id,
        campaign_name=record.campaign_name,
        currency=record.currency,
        account_timezone=record.account_timezone,
        conversion_basis=subject.basis,
        metric=change.metric,
        metric_label=METRIC_LABELS[change.metric],
        baseline_kind=baseline_kind,
        baseline_value=change.baseline,
        current_value=change.current,
        absolute_change=change.absolute_change,
        percent_change=change.percent_change,
        baseline_period=subject.comparison.baseline,
        current_period=subject.comparison.current,
        comparison_label=subject.comparison.label,
        observation=observation,
        evidence={**_volume_evidence(subject), **evidence},
        data_limitations=all_limitations,
        pre_guard_severity=severity,
        noise_guard=NOT_APPLICABLE,
    )


def _has_volume(subject: _Subject, t: AnomalyThresholds) -> bool:
    baseline, current = subject.baseline, subject.current
    return (
        min(baseline.spend, current.spend) >= t.min_spend
        and min(baseline.clicks, current.clicks) >= t.min_clicks
        and baseline.platform_attributed_conversions >= t.min_baseline_conversions
    )


def _cost_increase(subject: _Subject, t: AnomalyThresholds) -> Finding | None:
    baseline, current = subject.baseline, subject.current
    if current.platform_attributed_conversions < t.min_current_conversions:
        return None
    change = compare_metric(
        COST_METRIC,
        baseline.cost_per_platform_attributed_conversion,
        current.cost_per_platform_attributed_conversion,
    )
    if change.percent_change is None or change.absolute_change is None:
        return None
    if (
        change.percent_change < t.cost_increase_percent
        or change.absolute_change < t.min_cost_increase_absolute
    ):
        return None
    severity = (
        Severity.HIGH if change.percent_change >= t.cost_increase_high_percent else Severity.MEDIUM
    )
    return _finding(
        subject,
        RuleId.COST_PER_CONVERSION_INCREASE,
        severity,
        change,
        "Cost per platform-attributed conversion rose above the configured threshold.",
        {
            "threshold_percent": t.cost_increase_percent,
            "threshold_absolute": t.min_cost_increase_absolute,
        },
        (LIMIT_PROXY,),
    )


def _conversion_rate_decline(subject: _Subject, t: AnomalyThresholds) -> Finding | None:
    change = compare_metric(
        RATE_METRIC,
        subject.baseline.platform_attributed_conversion_rate,
        subject.current.platform_attributed_conversion_rate,
    )
    if change.percent_change is None or change.percent_change > -t.conversion_rate_decline_percent:
        return None
    severity = (
        Severity.HIGH
        if change.percent_change <= -t.conversion_rate_decline_high_percent
        else Severity.MEDIUM
    )
    return _finding(
        subject,
        RuleId.CONVERSION_RATE_DECLINE,
        severity,
        change,
        "Platform-attributed conversion rate fell below the configured threshold.",
        {"threshold_percent": t.conversion_rate_decline_percent},
        (),
    )


def _spend_without_conversion_growth(subject: _Subject, t: AnomalyThresholds) -> Finding | None:
    baseline, current = subject.baseline, subject.current
    spend = compare_metric("spend", baseline.spend, current.spend)
    conversions = compare_metric(
        CONVERSIONS_METRIC,
        baseline.platform_attributed_conversions,
        current.platform_attributed_conversions,
    )
    if spend.percent_change is None or conversions.percent_change is None:
        return None
    required_growth = spend.percent_change * t.proportional_conversion_ratio
    if spend.percent_change < t.spend_increase_percent:
        return None
    if conversions.percent_change >= required_growth:
        return None
    gap = spend.percent_change - conversions.percent_change
    return _finding(
        subject,
        RuleId.SPEND_WITHOUT_CONVERSION_GROWTH,
        Severity.HIGH if gap >= t.spend_gap_high_percent else Severity.MEDIUM,
        spend,
        "Spend grew without a proportional rise in platform-attributed conversions.",
        {
            "conversions_percent_change": conversions.percent_change,
            "required_conversions_percent_change": required_growth,
            "gap_percentage_points": gap,
            "threshold_percent": t.spend_increase_percent,
        },
        (),
    )


def _longest_zero_run(
    rows: tuple[AdPerformanceRecord, ...],
) -> list[AdPerformanceRecord]:
    """Return the earliest longest run of consecutive days with spend and no conversions."""
    longest: list[AdPerformanceRecord] = []
    run: list[AdPerformanceRecord] = []
    for row in sorted(rows, key=lambda item: item.reporting_date):
        if row.spend > 0 and row.conversions == 0:
            run.append(row)
            if len(run) > len(longest):
                longest = list(run)
        else:
            run = []
    return longest


def _spend_with_zero_conversions(subject: _Subject, t: AnomalyThresholds) -> Finding | None:
    run = _longest_zero_run(subject.current_rows)
    if len(run) < t.zero_run_min_days:
        return None
    run_spend = sum((row.spend for row in run), Decimal(0))
    if run_spend < t.zero_run_min_spend:
        return None
    baseline_cost = subject.baseline.cost_per_platform_attributed_conversion
    expected = None if baseline_cost is None else safe_divide(run_spend, baseline_cost)
    if expected is not None and expected < t.zero_run_min_expected_conversions:
        return None
    limitations = () if expected is not None else ("The baseline also had no conversions.",)
    return _finding(
        subject,
        RuleId.SPEND_WITH_ZERO_CONVERSIONS,
        Severity.HIGH if len(run) >= t.zero_run_high_days else Severity.MEDIUM,
        compare_metric(CONVERSIONS_METRIC, expected, Decimal(0)),
        "Campaign spent on consecutive days with no platform-attributed conversions.",
        {
            "zero_run_start": run[0].reporting_date,
            "zero_run_end": run[-1].reporting_date,
            "zero_run_days": len(run),
            "zero_run_spend": run_spend,
            "threshold_days": t.zero_run_min_days,
            "threshold_spend": t.zero_run_min_spend,
        },
        limitations,
        baseline_kind="expected_at_baseline_cost",
    )


def _roas_deterioration(subject: _Subject, t: AnomalyThresholds) -> Finding | None:
    baseline, current = subject.baseline, subject.current
    if baseline.platform_attributed_conversion_value < t.min_baseline_conversion_value:
        return None
    change = compare_metric(
        ROAS_METRIC, baseline.platform_attributed_roas, current.platform_attributed_roas
    )
    if change.percent_change is None or change.percent_change > -t.roas_decline_percent:
        return None
    severity = (
        Severity.HIGH if change.percent_change <= -t.roas_decline_high_percent else Severity.MEDIUM
    )
    return _finding(
        subject,
        RuleId.ROAS_DETERIORATION,
        severity,
        change,
        "Platform-attributed ROAS fell below the configured threshold.",
        {
            "baseline_conversion_value": baseline.platform_attributed_conversion_value,
            "current_conversion_value": current.platform_attributed_conversion_value,
            "threshold_percent": t.roas_decline_percent,
        },
        (LIMIT_VALUE,),
    )


def _apply_noise_guard(finding: Finding, subject: _Subject, t: AnomalyThresholds) -> Finding:
    result = evaluate_noise_guard(
        finding.baseline_value,
        finding.current_value,
        subject.baseline.platform_attributed_conversions,
        subject.current.platform_attributed_conversions,
        t.noise_guard_width,
        t.noise_guard_high_severity_width,
        is_high_severity=finding.severity is Severity.HIGH,
    )
    downgraded = result.outcome is NoiseGuardOutcome.DOWNGRADED
    return replace(
        finding,
        severity=Severity.MEDIUM if downgraded else finding.severity,
        noise_guard=result,
        data_limitations=(*finding.data_limitations, LIMIT_GUARD),
    )


_GUARDED_RULES = {
    RuleId.COST_PER_CONVERSION_INCREASE,
    RuleId.CONVERSION_RATE_DECLINE,
    RuleId.ROAS_DETERIORATION,
}

_VOLUME_GATED_RULES = (
    _cost_increase,
    _conversion_rate_decline,
    _spend_without_conversion_growth,
    _roas_deterioration,
)


def _strong_performers(subjects: list[_Subject], t: AnomalyThresholds) -> list[Finding]:
    """Flag campaigns with both low cost and high ROAS against same-basis peers."""
    peer_groups: defaultdict[tuple[str, str], list[_Subject]] = defaultdict(list)
    for subject in subjects:
        current = subject.current
        if (
            _has_volume(subject, t)
            and current.cost_per_platform_attributed_conversion is not None
            and current.platform_attributed_roas is not None
        ):
            peer_groups[(subject.basis.basis_id, subject.sample.currency)].append(subject)

    findings = []
    for group in peer_groups.values():
        for subject in group:
            peers = [other for other in group if other is not subject]
            if len(peers) < t.strong_min_peers:
                continue
            finding = _strong_finding(subject, peers, t)
            if finding:
                findings.append(finding)
    return findings


def _strong_finding(
    subject: _Subject, peers: list[_Subject], t: AnomalyThresholds
) -> Finding | None:
    current, baseline = subject.current, subject.baseline
    cost = current.cost_per_platform_attributed_conversion
    roas = current.platform_attributed_roas
    peer_cost = median(p.current.cost_per_platform_attributed_conversion for p in peers)
    peer_roas = median(p.current.platform_attributed_roas for p in peers)
    own_change = compare_metric(COST_METRIC, baseline.cost_per_platform_attributed_conversion, cost)
    if (
        current.platform_attributed_conversions < t.strong_min_conversions
        or cost > peer_cost * t.strong_max_cost_ratio_to_peers
        or roas < peer_roas * t.strong_min_roas_ratio_to_peers
        or own_change.percent_change is None
        or own_change.percent_change > t.strong_max_cost_increase_percent
    ):
        return None
    return _finding(
        subject,
        RuleId.STRONG_PERFORMER,
        Severity.INFO,
        compare_metric(COST_METRIC, peer_cost, cost),
        "Cost per platform-attributed conversion and ROAS outperformed same-basis peers.",
        {
            "peer_count": len(peers),
            "peer_median_roas": peer_roas,
            "campaign_roas": roas,
            "own_baseline_cost": baseline.cost_per_platform_attributed_conversion,
            "own_cost_percent_change": own_change.percent_change,
            "threshold_cost_ratio": t.strong_max_cost_ratio_to_peers,
            "threshold_roas_ratio": t.strong_min_roas_ratio_to_peers,
        },
        (LIMIT_PROXY, LIMIT_INCREMENTALITY, LIMIT_VALUE),
        direction=Direction.POSITIVE,
        baseline_kind="peer_median_current_period",
    )


def _covers(rows: list[AdPerformanceRecord], period: ReportingPeriod) -> bool:
    return {row.reporting_date for row in rows} == set(period.dates)


def _campaign_subjects(
    baseline_rows: list[AdPerformanceRecord],
    current_rows: list[AdPerformanceRecord],
    comparison: PeriodComparison,
    exclude: Any,
) -> list[_Subject]:
    if not _covers(baseline_rows, comparison.baseline) or not _covers(
        current_rows, comparison.current
    ):
        exclude(None, ExclusionReason.INCOMPLETE_CAMPAIGN_COVERAGE)
        return []

    baseline_kpis = calculate_kpis(baseline_rows).by_basis
    current_kpis = calculate_kpis(current_rows).by_basis
    latest = max(current_rows, key=lambda row: (row.reporting_date, row.record_id))
    subjects = []
    for basis_id in sorted(baseline_kpis.keys() | current_kpis.keys()):
        if basis_id not in baseline_kpis or basis_id not in current_kpis:
            exclude(basis_id, ExclusionReason.BASIS_NOT_IN_BOTH_PERIODS)
            continue
        basis = current_kpis[basis_id].basis
        if not basis.has_attribution:
            exclude(basis_id, ExclusionReason.ATTRIBUTION_UNAVAILABLE)
            continue

        def on_basis(rows: list[AdPerformanceRecord], wanted: str = basis_id) -> list:
            return [row for row in rows if ConversionBasis.from_record(row).basis_id == wanted]

        basis_baseline = on_basis(baseline_rows)
        basis_current = on_basis(current_rows)
        if not _covers(basis_baseline, comparison.baseline) or not _covers(
            basis_current, comparison.current
        ):
            exclude(basis_id, ExclusionReason.INCOMPLETE_BASIS_COVERAGE)
            continue
        subjects.append(
            _Subject(
                sample=latest,
                baseline=baseline_kpis[basis_id],
                current=current_kpis[basis_id],
                current_rows=tuple(basis_current),
                comparison=comparison,
            )
        )
    return subjects


_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.INFO: 2}


def detect_anomalies(
    records: Iterable[AdPerformanceRecord],
    comparison: PeriodComparison,
    thresholds: AnomalyThresholds = DEFAULT_THRESHOLDS,
) -> DetectionResult:
    """Apply every rule to each comparable campaign and conversion basis."""
    rows = sorted(records, key=lambda record: record.record_id)
    validate_records(rows)
    ensure_complete(rows, comparison.baseline)
    ensure_complete(rows, comparison.current)

    campaigns: defaultdict[tuple[str, str, str], list[AdPerformanceRecord]] = defaultdict(list)
    for row in rows:
        campaigns[(row.platform.value, row.account_id, row.campaign_id)].append(row)

    exclusions: list[Exclusion] = []
    subjects: list[_Subject] = []
    for key, campaign_rows in sorted(campaigns.items()):

        def exclude(
            basis_id: str | None,
            reason: ExclusionReason,
            campaign: tuple[str, str, str] = key,
        ) -> None:
            exclusions.append(Exclusion(*campaign, basis_id, reason))

        subjects.extend(
            _campaign_subjects(
                records_in(campaign_rows, comparison.baseline),
                records_in(campaign_rows, comparison.current),
                comparison,
                exclude,
            )
        )

    findings: list[Finding] = []
    suppressed: list[Finding] = []
    for subject in subjects:
        zero_run = _spend_with_zero_conversions(subject, thresholds)
        if zero_run:
            findings.append(zero_run)
        if not _has_volume(subject, thresholds):
            record = subject.sample
            exclusions.append(
                Exclusion(
                    record.platform.value,
                    record.account_id,
                    record.campaign_id,
                    subject.basis.basis_id,
                    ExclusionReason.INSUFFICIENT_VOLUME,
                )
            )
            continue
        for rule in _VOLUME_GATED_RULES:
            finding = rule(subject, thresholds)
            if finding is None:
                continue
            if finding.rule_id in _GUARDED_RULES:
                finding = _apply_noise_guard(finding, subject, thresholds)
            if finding.noise_guard.outcome is NoiseGuardOutcome.SUPPRESSED:
                suppressed.append(finding)
            else:
                findings.append(finding)
    findings.extend(_strong_performers(subjects, thresholds))

    def order(finding: Finding) -> tuple[int, str]:
        return (_SEVERITY_ORDER[finding.severity], finding.finding_id)

    return DetectionResult(
        findings=tuple(sorted(findings, key=order)),
        suppressed_findings=tuple(sorted(suppressed, key=order)),
        exclusions=tuple(exclusions),
    )
