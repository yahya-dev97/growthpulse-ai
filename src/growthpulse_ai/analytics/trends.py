"""Relationships between findings, without claiming that one causes another.

Grouping records that findings occur together and share metric inputs. Those metrics can
move together arithmetically, which says nothing about why they moved.

Persistence assessment checks where the latest window sits against a sustained finding's
baseline. A sustained finding is never reported as resolved.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from growthpulse_ai.analytics.anomalies import (
    AnomalyThresholds,
    DetectionResult,
    Direction,
    Finding,
    RuleId,
    Severity,
)
from growthpulse_ai.analytics.kpis import (
    ConversionBasis,
    ConversionKpis,
    calculate_kpis,
    compare_metric,
)
from growthpulse_ai.analytics.noise import NoiseGuardOutcome, evaluate_noise_guard
from growthpulse_ai.analytics.periods import PeriodComparison, ReportingPeriod, records_in
from growthpulse_ai.schema import AdPerformanceRecord

RULE_INPUTS = {
    RuleId.COST_PER_CONVERSION_INCREASE: frozenset({"spend", "conversions"}),
    RuleId.CONVERSION_RATE_DECLINE: frozenset({"conversions", "clicks"}),
    RuleId.ROAS_DETERIORATION: frozenset({"conversion_value", "spend"}),
    RuleId.SPEND_WITHOUT_CONVERSION_GROWTH: frozenset({"spend", "conversions"}),
    RuleId.SPEND_WITH_ZERO_CONVERSIONS: frozenset({"spend", "conversions"}),
}

GROUP_NOTE = (
    "These findings occur in the same campaign, conversion basis, and window, and their "
    "metrics share inputs, so they can move together arithmetically. Grouping does not "
    "establish that one caused another."
)
_SEVERITY_RANK = {Severity.HIGH: 2, Severity.MEDIUM: 1, Severity.INFO: 0}


@dataclass(frozen=True, slots=True)
class FindingGroup:
    group_id: str
    relationship: str
    platform: str
    account_id: str
    campaign_id: str
    basis_id: str
    comparison_label: str
    baseline_period: ReportingPeriod
    current_period: ReportingPeriod
    highest_severity: Severity
    finding_ids: tuple[str, ...]
    rule_ids: tuple[RuleId, ...]
    shared_inputs: tuple[str, ...]
    note: str


def _connected(findings: Sequence[Finding]) -> list[list[Finding]]:
    """Split findings into components linked by shared metric inputs."""
    remaining = sorted(findings, key=lambda finding: finding.finding_id)
    components = []
    while remaining:
        component = [remaining.pop(0)]
        inputs = set(RULE_INPUTS[component[0].rule_id])
        growing = True
        while growing:
            linked = [f for f in remaining if inputs & RULE_INPUTS[f.rule_id]]
            growing = bool(linked)
            for finding in linked:
                remaining.remove(finding)
                component.append(finding)
                inputs |= RULE_INPUTS[finding.rule_id]
        components.append(component)
    return components


def _group(component: list[Finding]) -> FindingGroup:
    finding_ids = tuple(sorted(finding.finding_id for finding in component))
    canonical = json.dumps(finding_ids, separators=(",", ":"))
    all_inputs = [item for finding in component for item in RULE_INPUTS[finding.rule_id]]
    first = component[0]
    return FindingGroup(
        group_id=hashlib.sha256(canonical.encode()).hexdigest(),
        relationship="shared_metric_inputs",
        platform=first.platform,
        account_id=first.account_id,
        campaign_id=first.campaign_id,
        basis_id=first.conversion_basis.basis_id,
        comparison_label=first.comparison_label,
        baseline_period=first.baseline_period,
        current_period=first.current_period,
        highest_severity=max((f.severity for f in component), key=_SEVERITY_RANK.__getitem__),
        finding_ids=finding_ids,
        rule_ids=tuple(sorted(finding.rule_id for finding in component)),
        shared_inputs=tuple(sorted({i for i in all_inputs if all_inputs.count(i) > 1})),
        note=GROUP_NOTE,
    )


def group_related_findings(findings: Iterable[Finding]) -> tuple[FindingGroup, ...]:
    """Group active adverse findings. Only groups of two or more are returned."""
    buckets: defaultdict[tuple, list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.direction is Direction.ADVERSE:
            key = (
                finding.data_source,
                finding.platform,
                finding.account_id,
                finding.campaign_id,
                finding.conversion_basis.basis_id,
                finding.baseline_period,
                finding.current_period,
            )
            buckets[key].append(finding)
    groups = [
        _group(component)
        for bucket in buckets.values()
        for component in _connected(bucket)
        if len(component) > 1
    ]
    return tuple(sorted(groups, key=lambda group: group.group_id))


class PersistenceStatus(StrEnum):
    RECENT_CHANGE_CONTINUING = "recent_change_continuing"
    PLATEAUED_STILL_ELEVATED = "plateaued_still_elevated"
    NOT_CONFIRMED_ELEVATED = "not_confirmed_elevated"
    NOT_ASSESSABLE = "not_assessable"


PERSISTENCE_NOTES = {
    PersistenceStatus.RECENT_CHANGE_CONTINUING: ("The recent comparison also flags this metric."),
    PersistenceStatus.PLATEAUED_STILL_ELEVATED: (
        "The recent comparison shows no further change, but the latest window remains beyond "
        "the threshold against the longer baseline. The absence of a recent finding does not "
        "mean this has resolved."
    ),
    PersistenceStatus.NOT_CONFIRMED_ELEVATED: (
        "The latest window does not clear both the threshold and the noise guard against the "
        "longer baseline. This does not confirm that the issue has resolved."
    ),
    PersistenceStatus.NOT_ASSESSABLE: (
        "The latest window could not be compared with the longer baseline."
    ),
}


@dataclass(frozen=True, slots=True)
class PersistenceAssessment:
    assessment_id: str
    status: PersistenceStatus
    resolution_confirmed: bool
    sustained_finding_id: str
    recent_finding_id: str | None
    rule_id: RuleId
    platform: str
    account_id: str
    campaign_id: str
    basis_id: str
    metric: str
    sustained_baseline_period: ReportingPeriod
    sustained_current_period: ReportingPeriod
    latest_period: ReportingPeriod
    sustained_baseline_value: Decimal | int | None
    sustained_current_value: Decimal | int | None
    latest_value: Decimal | int | None
    latest_percent_change_vs_baseline: Decimal | None
    note: str


def _still_beyond_threshold(
    finding: Finding, latest: ConversionKpis, t: AnomalyThresholds
) -> tuple[bool, Decimal | int | None, Decimal | None]:
    value = getattr(latest, finding.metric)
    change = compare_metric(finding.metric, finding.baseline_value, value)
    percent, absolute = change.percent_change, change.absolute_change
    if percent is None or absolute is None:
        return False, value, percent
    if finding.rule_id is RuleId.COST_PER_CONVERSION_INCREASE:
        beyond = percent >= t.cost_increase_percent and absolute >= t.min_cost_increase_absolute
    elif finding.rule_id is RuleId.CONVERSION_RATE_DECLINE:
        beyond = percent <= -t.conversion_rate_decline_percent
    else:
        beyond = percent <= -t.roas_decline_percent
    guard = evaluate_noise_guard(
        finding.baseline_value,
        value,
        finding.evidence["baseline_conversions"],
        latest.platform_attributed_conversions,
        t.noise_guard_width,
        t.noise_guard_high_severity_width,
        is_high_severity=False,
    )
    return beyond and guard.outcome is not NoiseGuardOutcome.SUPPRESSED, value, percent


def _latest_kpis(
    records: Sequence[AdPerformanceRecord], finding: Finding, period: ReportingPeriod
) -> ConversionKpis | None:
    rows = [
        record
        for record in records_in(records, period)
        if record.platform.value == finding.platform
        and record.account_id == finding.account_id
        and record.campaign_id == finding.campaign_id
        and ConversionBasis.from_record(record).basis_id == finding.conversion_basis.basis_id
    ]
    if {row.reporting_date for row in rows} != set(period.dates):
        return None
    return calculate_kpis(rows).conversions_by_basis[0]


_ASSESSED_RULES = {
    RuleId.COST_PER_CONVERSION_INCREASE,
    RuleId.CONVERSION_RATE_DECLINE,
    RuleId.ROAS_DETERIORATION,
}


def assess_persistence(
    records: Sequence[AdPerformanceRecord],
    recent: PeriodComparison,
    recent_result: DetectionResult,
    sustained: PeriodComparison,
    sustained_result: DetectionResult,
    thresholds: AnomalyThresholds,
) -> tuple[PersistenceAssessment, ...]:
    """Assess each sustained deterioration against the latest window."""
    recent_by_key = {
        (f.rule_id, f.platform, f.account_id, f.campaign_id, f.conversion_basis.basis_id): f
        for f in recent_result.findings
    }
    assessments = []
    for finding in sustained_result.findings:
        if finding.rule_id not in _ASSESSED_RULES:
            continue
        basis_id = finding.conversion_basis.basis_id
        key = (finding.rule_id, finding.platform, finding.account_id, finding.campaign_id, basis_id)
        recent_finding = recent_by_key.get(key)
        latest = _latest_kpis(records, finding, recent.current)

        value = percent = None
        status = PersistenceStatus.NOT_ASSESSABLE
        if latest is not None:
            beyond, value, percent = _still_beyond_threshold(finding, latest, thresholds)
            if recent_finding is not None:
                status = PersistenceStatus.RECENT_CHANGE_CONTINUING
            elif percent is None:
                status = PersistenceStatus.NOT_ASSESSABLE
            elif beyond:
                status = PersistenceStatus.PLATEAUED_STILL_ELEVATED
            else:
                status = PersistenceStatus.NOT_CONFIRMED_ELEVATED

        identity = [
            finding.finding_id,
            recent.current.start.isoformat(),
            recent.current.end.isoformat(),
        ]
        canonical = json.dumps(identity, separators=(",", ":"))
        assessments.append(
            PersistenceAssessment(
                assessment_id=hashlib.sha256(canonical.encode()).hexdigest(),
                status=status,
                resolution_confirmed=False,
                sustained_finding_id=finding.finding_id,
                recent_finding_id=recent_finding.finding_id if recent_finding else None,
                rule_id=finding.rule_id,
                platform=finding.platform,
                account_id=finding.account_id,
                campaign_id=finding.campaign_id,
                basis_id=basis_id,
                metric=finding.metric,
                sustained_baseline_period=sustained.baseline,
                sustained_current_period=sustained.current,
                latest_period=recent.current,
                sustained_baseline_value=finding.baseline_value,
                sustained_current_value=finding.current_value,
                latest_value=value,
                latest_percent_change_vs_baseline=percent,
                note=PERSISTENCE_NOTES[status],
            )
        )
    return tuple(sorted(assessments, key=lambda item: item.assessment_id))
