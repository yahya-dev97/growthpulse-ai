from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from factories import make_days, make_record
from growthpulse_ai.analytics.anomalies import (
    AnomalyThresholds,
    Direction,
    RuleId,
    Severity,
    detect_anomalies,
)
from growthpulse_ai.analytics.periods import (
    ComparisonPurpose,
    PeriodComparison,
    ReportingPeriod,
    latest_vs_first,
    latest_vs_previous,
)
from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.analytics.trends import (
    PersistenceStatus,
    assess_persistence,
    group_related_findings,
)

START = date(2026, 10, 1)
END = date(2026, 10, 30)
RECENT = latest_vs_previous(END, days=7)
SUSTAINED = latest_vs_first(START, END, days=10)
THRESHOLDS = AnomalyThresholds()
COST = RuleId.COST_PER_CONVERSION_INCREASE
RATE = RuleId.CONVERSION_RATE_DECLINE
ROAS = RuleId.ROAS_DETERIORATION
GOOD = 500
BAD = 250


def day_row(day: date, conversions: int, **overrides) -> Any:
    """A high-volume day. Value tracks conversions, so ROAS moves with them."""
    return make_record(
        reporting_date=day.isoformat(),
        impressions=100000,
        clicks=10000,
        spend="10000.00",
        conversions=str(conversions),
        conversion_value=str(conversions * 300),
        **overrides,
    )


def series(conversions_on: Callable[[date], int], **overrides) -> list:
    days = [START + timedelta(days=offset) for offset in range(30)]
    return [day_row(day, conversions_on(day), **overrides) for day in days]


def plateau(day: date) -> int:
    """Worsens on Oct 11 and then stays flat."""
    return GOOD if day < date(2026, 10, 11) else BAD


def assess(rows: list, sustained: PeriodComparison = SUSTAINED, recent: PeriodComparison = RECENT):
    return assess_persistence(
        rows,
        recent,
        detect_anomalies(rows, recent),
        sustained,
        detect_anomalies(rows, sustained),
        THRESHOLDS,
    )


def by_rule(assessments, rule: RuleId):
    matches = [item for item in assessments if item.rule_id is rule]
    assert len(matches) == 1
    return matches[0]


# Plateaued deterioration


def test_plateau_has_no_recent_finding():
    rows = series(plateau)
    assert detect_anomalies(rows, RECENT).findings == ()
    assert detect_anomalies(rows, SUSTAINED).findings != ()


@pytest.mark.parametrize("rule", [COST, RATE, ROAS])
def test_plateau_is_not_treated_as_resolved(rule: RuleId):
    item = by_rule(assess(series(plateau)), rule)
    assert item.status is PersistenceStatus.PLATEAUED_STILL_ELEVATED
    assert item.resolution_confirmed is False
    assert item.recent_finding_id is None
    assert "does not mean this has resolved" in item.note


def test_plateau_reports_latest_value_against_long_baseline():
    item = by_rule(assess(series(plateau)), COST)
    assert item.sustained_baseline_value == Decimal(20)
    assert item.latest_value == Decimal(40)
    assert item.latest_percent_change_vs_baseline == Decimal(100)
    assert item.latest_period == RECENT.current


def test_continuing_worsening_is_linked_to_the_recent_finding():
    def worsening(day: date) -> int:
        return 120 if day >= date(2026, 10, 24) else plateau(day)

    rows = series(worsening)
    item = by_rule(assess(rows), COST)
    assert item.status is PersistenceStatus.RECENT_CHANGE_CONTINUING
    recent = next(f for f in detect_anomalies(rows, RECENT).findings if f.rule_id is COST)
    assert item.recent_finding_id == recent.finding_id


def test_recovery_is_never_confirmed_as_resolved():
    def recovered(day: date) -> int:
        return BAD if date(2026, 10, 15) <= day <= date(2026, 10, 23) else GOOD

    long_window = latest_vs_first(START, END, days=14)
    item = by_rule(assess(series(recovered), sustained=long_window), RATE)
    assert item.status is PersistenceStatus.NOT_CONFIRMED_ELEVATED
    assert item.resolution_confirmed is False
    assert "does not confirm" in item.note


def test_latest_value_inside_the_noise_band_is_not_called_elevated():
    # An extreme width puts even a doubling inside the noise band.
    rows = series(plateau)
    wide = replace(
        THRESHOLDS, noise_guard_width=Decimal(50), noise_guard_high_severity_width=Decimal(50)
    )
    item = by_rule(
        assess_persistence(
            rows,
            RECENT,
            detect_anomalies(rows, RECENT),
            SUSTAINED,
            detect_anomalies(rows, SUSTAINED),
            wide,
        ),
        COST,
    )
    assert item.status is PersistenceStatus.NOT_CONFIRMED_ELEVATED


def test_missing_latest_days_are_not_assessable():
    # The sustained window ends before the gap, so only the latest window is affected.
    earlier = PeriodComparison(
        "earlier",
        ReportingPeriod(START, date(2026, 10, 10)),
        ReportingPeriod(date(2026, 10, 11), date(2026, 10, 20)),
        ComparisonPurpose.SUSTAINED_CHANGE,
    )
    gap = date(2026, 10, 26)
    rows = [
        *[row for row in series(plateau) if row.reporting_date != gap],
        *series(lambda _: GOOD, campaign_id="complete"),
    ]
    items = assess(rows, sustained=earlier)
    assert items
    assert {item.status for item in items} == {PersistenceStatus.NOT_ASSESSABLE}
    assert all(item.resolution_confirmed is False for item in items)


def test_undefined_latest_metric_is_not_assessable():
    def went_dark(day: date) -> int:
        return 0 if day >= date(2026, 10, 24) else plateau(day)

    item = by_rule(assess(series(went_dark)), COST)
    assert item.status is PersistenceStatus.NOT_ASSESSABLE
    assert item.latest_value is None
    assert item.resolution_confirmed is False


def test_only_deterioration_rules_are_assessed():
    rows = series(plateau)
    assert {item.rule_id for item in assess(rows)} <= {COST, RATE, ROAS}


def test_assessment_ids_are_stable_and_ordered():
    first, second = assess(series(plateau)), assess(series(plateau))
    assert first == second
    ids = [item.assessment_id for item in first]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_assessment_id_depends_on_the_latest_window():
    rows = series(plateau)
    other_recent = latest_vs_previous(date(2026, 10, 29), days=7)
    assert {item.assessment_id for item in assess(rows)}.isdisjoint(
        {item.assessment_id for item in assess(rows, recent=other_recent)}
    )


# Multiple comparison windows


def test_report_keeps_comparisons_separate():
    report = build_report(series(plateau))
    labels = {item["label"]: item for item in report["comparisons"]}
    assert labels["latest_7_vs_previous_7"]["purpose"] == "recent_change"
    assert labels["latest_10_vs_first_10"]["purpose"] == "sustained_change"
    assert labels["latest_7_vs_previous_7"]["findings"] == []
    assert labels["latest_10_vs_first_10"]["findings"]


def test_windows_produce_distinct_finding_ids():
    def always_worsening(day: date) -> int:
        return GOOD - 15 * (day - START).days

    report = build_report(series(always_worsening))
    ids = [f["finding_id"] for item in report["comparisons"] for f in item["findings"]]
    assert len(ids) == len(set(ids))


def test_report_includes_persistence_assessments():
    report = build_report(series(plateau))
    assert report["persistence_assessments"]
    assert all(a["resolution_confirmed"] is False for a in report["persistence_assessments"])


def test_custom_comparisons_have_no_persistence_assessment():
    custom = PeriodComparison(
        "custom",
        ReportingPeriod(START, date(2026, 10, 7)),
        ReportingPeriod(date(2026, 10, 24), END),
    )
    assert custom.purpose is ComparisonPurpose.CUSTOM
    assert build_report(series(plateau), comparisons=[custom])["persistence_assessments"] == []


def test_overlapping_baseline_is_not_assessed():
    late_sustained = PeriodComparison(
        "late",
        ReportingPeriod(date(2026, 10, 20), date(2026, 10, 24)),
        ReportingPeriod(date(2026, 10, 26), END),
        ComparisonPurpose.SUSTAINED_CHANGE,
    )

    def dropped(day: date) -> int:
        return GOOD if day < date(2026, 10, 25) else BAD

    report = build_report(series(dropped), comparisons=[RECENT, late_sustained])
    sustained_section = report["comparisons"][1]
    assert sustained_section["findings"]
    assert report["persistence_assessments"] == []


# Related-finding grouping


def sustained_findings(rows: list) -> tuple:
    return detect_anomalies(rows, SUSTAINED).findings


def test_related_findings_are_grouped():
    findings = sustained_findings(series(plateau))
    groups = group_related_findings(findings)
    assert len(groups) == 1
    group = groups[0]
    assert set(group.finding_ids) == {f.finding_id for f in findings}
    assert group.relationship == "shared_metric_inputs"
    assert group.highest_severity is Severity.HIGH
    assert {"spend", "conversions"} <= set(group.shared_inputs)


def test_grouping_leaves_original_findings_unchanged():
    findings = sustained_findings(series(plateau))
    before = tuple(findings)
    group_related_findings(findings)
    assert findings == before
    assert all(f.evidence for f in findings)


def test_group_note_disclaims_causation():
    group = group_related_findings(sustained_findings(series(plateau)))[0]
    assert "does not establish that one caused another" in group.note


def test_single_finding_is_not_a_group():
    findings = sustained_findings(series(plateau))
    assert group_related_findings(findings[:1]) == ()


def test_findings_without_shared_inputs_are_not_grouped():
    findings = [f for f in sustained_findings(series(plateau)) if f.rule_id in {RATE, ROAS}]
    assert len(findings) == 2
    assert group_related_findings(findings) == ()


def test_different_campaigns_are_not_grouped():
    rows = [*series(plateau, campaign_id="a"), *series(plateau, campaign_id="b")]
    groups = group_related_findings(sustained_findings(rows))
    assert {group.campaign_id for group in groups} == {"a", "b"}
    assert len(groups) == 2


def test_different_conversion_bases_are_not_grouped():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    rows = [*series(plateau), *series(plateau, conversion_definition=second)]
    groups = group_related_findings(sustained_findings(rows))
    assert len(groups) == 2
    assert len({group.basis_id for group in groups}) == 2


def test_different_windows_are_not_grouped():
    def always_worsening(day: date) -> int:
        return GOOD - 15 * (day - START).days

    rows = series(always_worsening)
    findings = [
        *detect_anomalies(rows, RECENT).findings,
        *detect_anomalies(rows, SUSTAINED).findings,
    ]
    groups = group_related_findings(findings)
    assert sorted(group.comparison_label for group in groups) == sorted(
        [RECENT.label, SUSTAINED.label]
    )


def test_different_currencies_are_not_grouped():
    rows = [
        *series(plateau, account_id="uk", currency="GBP"),
        *series(plateau, account_id="eu", currency="EUR"),
    ]
    assert len(group_related_findings(sustained_findings(rows))) == 2


def test_positive_findings_are_not_grouped():
    finding = sustained_findings(series(plateau))[0]
    positive = replace(finding, direction=Direction.POSITIVE)
    other = replace(sustained_findings(series(plateau))[1], direction=Direction.POSITIVE)
    assert group_related_findings([positive, other]) == ()


def test_group_ids_are_stable_and_input_order_independent():
    findings = sustained_findings(series(plateau))
    assert group_related_findings(findings) == group_related_findings(tuple(reversed(findings)))


def test_report_output_is_deterministic():
    rows = series(plateau)
    assert build_report(rows) == build_report(list(reversed(rows)))


def test_report_includes_groups_and_suppressed_lists():
    days = make_days(START, 30, impressions=1000, clicks=100, spend="100.00", conversions="5")
    section = build_report(days)["comparisons"][0]
    assert section["suppressed_findings"] == []
    assert section["finding_groups"] == []
