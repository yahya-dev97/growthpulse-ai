from dataclasses import fields, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from factories import make_days, make_record, make_row
from growthpulse_ai.analytics.anomalies import (
    AnomalyThresholds,
    Direction,
    ExclusionReason,
    Finding,
    RuleId,
    Severity,
    detect_anomalies,
)
from growthpulse_ai.analytics.errors import (
    ConflictingDeliveryError,
    IncompletePeriodError,
    InvalidPeriodError,
)
from growthpulse_ai.analytics.periods import PeriodComparison, ReportingPeriod

BASELINE_START = date(2026, 10, 1)
CURRENT_START = date(2026, 10, 8)
COMPARISON = PeriodComparison(
    "test",
    ReportingPeriod(BASELINE_START, date(2026, 10, 7)),
    ReportingPeriod(CURRENT_START, date(2026, 10, 14)),
)
# Per day: cost per conversion 20, conversion rate 5%, ROAS 15.
STEADY = {
    "impressions": 1000,
    "clicks": 100,
    "spend": "100.00",
    "conversions": "5",
    "conversion_value": "1500.00",
}


def scenario(baseline: dict | None = None, current: dict | None = None, **shared) -> list:
    return [
        *make_days(BASELINE_START, 7, **{**STEADY, **(baseline or {}), **shared}),
        *make_days(CURRENT_START, 7, **{**STEADY, **(current or {}), **shared}),
    ]


# These tests isolate each rule's own thresholds. The noise guard has its own test file.
NO_GUARD = AnomalyThresholds(
    noise_guard_width=Decimal(0), noise_guard_high_severity_width=Decimal(0)
)


def detect(rows: list, thresholds: AnomalyThresholds | None = None):
    return detect_anomalies(rows, COMPARISON, thresholds or NO_GUARD)


def rule_findings(rows: list, rule: RuleId, thresholds=None) -> list[Finding]:
    return [f for f in detect(rows, thresholds).findings if f.rule_id is rule]


def test_steady_campaign_has_no_findings():
    result = detect(scenario())
    assert result.findings == ()
    assert result.exclusions == ()


# Cost per conversion


def test_cost_increase_at_threshold_fires():
    # 125 / 5 = 25, exactly +25% on a cost of 20.
    findings = rule_findings(
        scenario(current={"spend": "125.00"}), RuleId.COST_PER_CONVERSION_INCREASE
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].percent_change == Decimal(25)
    assert findings[0].absolute_change == Decimal(5)


def test_cost_increase_below_threshold_does_not_fire():
    rows = scenario(current={"spend": "124.90"})
    assert rule_findings(rows, RuleId.COST_PER_CONVERSION_INCREASE) == []


def test_cost_increase_high_tier():
    findings = rule_findings(
        scenario(current={"spend": "150.00"}), RuleId.COST_PER_CONVERSION_INCREASE
    )
    assert findings[0].severity is Severity.HIGH


def test_cost_increase_requires_absolute_floor():
    # +50% on a cost of 2 is only +1, below the 5 floor.
    rows = scenario(
        baseline={"spend": "20.00", "conversions": "10"},
        current={"spend": "30.00", "conversions": "10"},
    )
    thresholds = replace(NO_GUARD, min_spend=Decimal(1))
    assert rule_findings(rows, RuleId.COST_PER_CONVERSION_INCREASE, thresholds) == []


def test_cost_finding_is_labeled_as_a_proxy():
    finding = rule_findings(
        scenario(current={"spend": "150.00"}), RuleId.COST_PER_CONVERSION_INCREASE
    )[0]
    assert finding.metric == "cost_per_platform_attributed_conversion"
    assert "proxy, not true CAC" in finding.metric_label
    assert any("not true customer acquisition" in text for text in finding.data_limitations)


def test_cost_rule_needs_current_conversions():
    rows = scenario(current={"conversions": "0.2", "conversion_value": "60.00"})
    assert rule_findings(rows, RuleId.COST_PER_CONVERSION_INCREASE) == []


# Conversion rate


def test_conversion_rate_decline_at_threshold_fires():
    findings = rule_findings(scenario(current={"conversions": "4"}), RuleId.CONVERSION_RATE_DECLINE)
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].percent_change == Decimal(-20)


def test_conversion_rate_decline_below_threshold_does_not_fire():
    rows = scenario(current={"conversions": "4.05"})
    assert rule_findings(rows, RuleId.CONVERSION_RATE_DECLINE) == []


def test_conversion_rate_decline_high_tier():
    findings = rule_findings(scenario(current={"conversions": "3"}), RuleId.CONVERSION_RATE_DECLINE)
    assert findings[0].severity is Severity.HIGH


def test_conversion_rate_improvement_does_not_fire():
    rows = scenario(current={"conversions": "9"})
    assert rule_findings(rows, RuleId.CONVERSION_RATE_DECLINE) == []


# Spend without conversion growth


def test_spend_rule_fires_when_conversions_lag():
    # Spend +20% needs conversions above +10%. These grow +8%.
    findings = rule_findings(
        scenario(current={"spend": "120.00", "conversions": "5.4"}),
        RuleId.SPEND_WITHOUT_CONVERSION_GROWTH,
    )
    assert len(findings) == 1
    assert findings[0].metric == "spend"
    assert findings[0].evidence["conversions_percent_change"] == Decimal(8)


def test_spend_rule_quiet_when_conversions_keep_pace():
    rows = scenario(current={"spend": "120.00", "conversions": "5.5"})
    assert rule_findings(rows, RuleId.SPEND_WITHOUT_CONVERSION_GROWTH) == []


def test_spend_rule_quiet_below_spend_threshold():
    rows = scenario(current={"spend": "119.00", "conversions": "5"})
    assert rule_findings(rows, RuleId.SPEND_WITHOUT_CONVERSION_GROWTH) == []


def test_spend_rule_high_tier_on_a_wide_gap():
    findings = rule_findings(
        scenario(current={"spend": "150.00"}), RuleId.SPEND_WITHOUT_CONVERSION_GROWTH
    )
    assert findings[0].severity is Severity.HIGH


# ROAS


def test_roas_decline_at_threshold_fires():
    findings = rule_findings(
        scenario(current={"conversion_value": "1125.00"}), RuleId.ROAS_DETERIORATION
    )
    assert len(findings) == 1
    assert findings[0].percent_change == Decimal(-25)


def test_roas_decline_below_threshold_does_not_fire():
    rows = scenario(current={"conversion_value": "1130.00"})
    assert rule_findings(rows, RuleId.ROAS_DETERIORATION) == []


def test_roas_rule_needs_baseline_value():
    rows = scenario(
        baseline={"conversion_value": "50.00"},
        current={"conversion_value": "1.00"},
    )
    assert rule_findings(rows, RuleId.ROAS_DETERIORATION) == []


def test_roas_rule_quiet_when_no_value_is_tracked():
    rows = scenario(baseline={"conversion_value": "0"}, current={"conversion_value": "0"})
    assert rule_findings(rows, RuleId.ROAS_DETERIORATION) == []


# Zero conversions


def zero_run_rows(run_days: int, spend: str = "100.00") -> list:
    rows = make_days(BASELINE_START, 7, **STEADY)
    for offset in range(7):
        metrics = dict(STEADY)
        if offset < run_days:
            metrics.update(spend=spend, conversions="0", conversion_value="0")
        day = CURRENT_START + timedelta(days=offset)
        rows.append(make_record(reporting_date=day.isoformat(), **metrics))
    return rows


def test_zero_run_fires_at_minimum_length():
    findings = rule_findings(zero_run_rows(3), RuleId.SPEND_WITH_ZERO_CONVERSIONS)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.MEDIUM
    assert finding.evidence["zero_run_days"] == 3
    assert finding.evidence["zero_run_start"] == CURRENT_START
    assert finding.evidence["zero_run_spend"] == Decimal(300)
    assert finding.baseline_value == Decimal(15)
    assert finding.current_value == Decimal(0)
    assert finding.baseline_kind == "expected_at_baseline_cost"


def test_zero_run_shorter_than_minimum_does_not_fire():
    assert rule_findings(zero_run_rows(2), RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_zero_run_high_tier():
    findings = rule_findings(zero_run_rows(5), RuleId.SPEND_WITH_ZERO_CONVERSIONS)
    assert findings[0].severity is Severity.HIGH


def test_zero_run_needs_minimum_spend():
    assert rule_findings(zero_run_rows(3, spend="60.00"), RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_zero_days_must_be_consecutive():
    rows = make_days(BASELINE_START, 7, **STEADY)
    for offset in range(7):
        metrics = dict(STEADY)
        if offset % 2 == 0:
            metrics.update(conversions="0", conversion_value="0")
        day = CURRENT_START + timedelta(days=offset)
        rows.append(make_record(reporting_date=day.isoformat(), **metrics))
    assert rule_findings(rows, RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_zero_days_without_spend_do_not_count():
    rows = scenario(
        current={"spend": "0", "clicks": 0, "conversions": "0", "conversion_value": "0"}
    )
    assert rule_findings(rows, RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_zero_spend_day_does_not_extend_a_zero_run():
    # Two days with spend and one without would pass the spend floor if the third counted.
    rows = make_days(BASELINE_START, 7, **STEADY)
    daily = ["150.00", "150.00", "0", "100.00", "100.00", "100.00", "100.00"]
    for offset, spend in enumerate(daily):
        metrics = {**STEADY, "spend": spend}
        if offset < 3:
            metrics.update(conversions="0", conversion_value="0")
        day = CURRENT_START + timedelta(days=offset)
        rows.append(make_record(reporting_date=day.isoformat(), **metrics))
    assert rule_findings(rows, RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_zero_run_needs_meaningful_expected_conversions():
    # Baseline cost of 500 means 300 of spend would be expected to bring under 3 conversions.
    rows = make_days(BASELINE_START, 7, **{**STEADY, "spend": "500.00", "conversions": "1"})
    for offset in range(7):
        metrics = {**STEADY, "conversions": "0", "conversion_value": "0"}
        if offset >= 3:
            metrics["conversions"] = "1"
        day = CURRENT_START + timedelta(days=offset)
        rows.append(make_record(reporting_date=day.isoformat(), **metrics))
    assert rule_findings(rows, RuleId.SPEND_WITH_ZERO_CONVERSIONS) == []


def test_campaign_that_never_converted_is_reported_with_a_limitation():
    zero = {"conversions": "0", "conversion_value": "0"}
    findings = rule_findings(
        scenario(baseline=zero, current=zero), RuleId.SPEND_WITH_ZERO_CONVERSIONS
    )
    assert len(findings) == 1
    assert findings[0].baseline_value is None
    assert findings[0].absolute_change is None
    assert findings[0].percent_change is None
    assert any("baseline also had no conversions" in t for t in findings[0].data_limitations)


# Low-volume safeguards


@pytest.mark.parametrize(
    "low",
    [
        {"spend": "10.00", "conversions": "5"},
        {"clicks": 10},
        {"conversions": "1", "conversion_value": "300.00"},
    ],
)
def test_low_baseline_volume_does_not_produce_findings(low: dict):
    # The current period would trigger several rules if the baseline had enough volume.
    rows = scenario(baseline=low, current={"spend": "400.00", "conversions": "2"})
    result = detect(rows)
    assert result.findings == ()
    assert [e.reason for e in result.exclusions] == [ExclusionReason.INSUFFICIENT_VOLUME]


def test_low_current_volume_is_excluded():
    rows = scenario(current={"spend": "5.00", "clicks": 5, "conversions": "1"})
    result = detect(rows)
    assert result.findings == ()
    assert result.exclusions[0].reason is ExclusionReason.INSUFFICIENT_VOLUME


def test_thresholds_are_configurable():
    rows = scenario(current={"spend": "110.00"})
    assert rule_findings(rows, RuleId.COST_PER_CONVERSION_INCREASE) == []
    relaxed = replace(
        NO_GUARD, cost_increase_percent=Decimal(5), min_cost_increase_absolute=Decimal(1)
    )
    assert len(rule_findings(rows, RuleId.COST_PER_CONVERSION_INCREASE, relaxed)) == 1


def test_rejects_negative_thresholds():
    with pytest.raises(InvalidPeriodError):
        AnomalyThresholds(min_clicks=-1)


def test_rejects_inverted_severity_tiers():
    with pytest.raises(InvalidPeriodError):
        AnomalyThresholds(cost_increase_percent=Decimal(60))


# Strong performers


def peer(campaign_id: str, spend: str, conversions: str, value: str) -> list:
    metrics = {
        **STEADY,
        "campaign_id": campaign_id,
        "spend": spend,
        "conversions": conversions,
        "conversion_value": value,
    }
    return [*make_days(BASELINE_START, 7, **metrics), *make_days(CURRENT_START, 7, **metrics)]


def peers(count: int = 3) -> list:
    return [row for i in range(count) for row in peer(f"peer-{i}", "200.00", "5", "1000.00")]


def strong(campaign_id: str = "star") -> list:
    return peer(campaign_id, "100.00", "10", "3000.00")


def test_strong_performer_is_identified():
    findings = rule_findings([*peers(), *strong()], RuleId.STRONG_PERFORMER)
    assert [f.campaign_id for f in findings] == ["star"]
    finding = findings[0]
    assert finding.severity is Severity.INFO
    assert finding.direction is Direction.POSITIVE
    assert finding.baseline_kind == "peer_median_current_period"
    assert finding.baseline_value == Decimal(40)
    assert finding.current_value == Decimal(10)
    assert finding.evidence["peer_count"] == 3


def test_strong_performer_needs_enough_peers():
    assert rule_findings([*peers(2), *strong()], RuleId.STRONG_PERFORMER) == []


def test_low_cost_with_low_value_is_not_strong():
    cheap_but_low_value = peer("cheap", "100.00", "10", "300.00")
    assert rule_findings([*peers(), *cheap_but_low_value], RuleId.STRONG_PERFORMER) == []


def test_strong_performer_needs_volume():
    # Low cost and high ROAS, but only 14 conversions against a minimum of 20.
    thin = peer("thin", "20.00", "2", "1000.00")
    assert rule_findings([*peers(), *thin], RuleId.STRONG_PERFORMER) == []


def test_strong_performer_must_not_be_deteriorating():
    worsening = [
        *make_days(
            BASELINE_START,
            7,
            **{
                **STEADY,
                "campaign_id": "worse",
                "spend": "100.00",
                "conversions": "20",
                "conversion_value": "3000.00",
            },
        ),
        *make_days(
            CURRENT_START,
            7,
            **{
                **STEADY,
                "campaign_id": "worse",
                "spend": "100.00",
                "conversions": "10",
                "conversion_value": "3000.00",
            },
        ),
    ]
    assert rule_findings([*peers(), *worsening], RuleId.STRONG_PERFORMER) == []


def test_peers_must_share_a_conversion_basis():
    other = {"event_id": "other", "event_name": "Other", "stage": "lead"}
    metrics = {"conversion_definition": other}
    other_basis_peers = [
        row
        for i in range(3)
        for row in scenario(campaign_id=f"peer-{i}", spend="200.00", **metrics)
    ]
    assert rule_findings([*other_basis_peers, *strong()], RuleId.STRONG_PERFORMER) == []


def test_peers_must_share_a_currency():
    euro_peers = [
        row
        for i in range(3)
        for row in scenario(
            account_id="euro-account", campaign_id=f"peer-{i}", spend="200.00", currency="EUR"
        )
    ]
    assert rule_findings([*euro_peers, *strong()], RuleId.STRONG_PERFORMER) == []


# Comparability and exclusions


def test_missing_attribution_is_excluded():
    result = detect(scenario(current={"spend": "400.00"}, attribution=None))
    assert result.findings == ()
    assert result.exclusions[0].reason is ExclusionReason.ATTRIBUTION_UNAVAILABLE


def test_changed_attribution_is_not_compared():
    attribution = {**make_row()["attribution"], "click_window_days": 7}
    rows = scenario(current={"spend": "400.00", "attribution": attribution})
    result = detect(rows)
    assert result.findings == ()
    assert {e.reason for e in result.exclusions} == {ExclusionReason.BASIS_NOT_IN_BOTH_PERIODS}


def test_campaign_missing_a_day_is_excluded():
    other = scenario(campaign_id="complete")
    gappy = [r for r in scenario(campaign_id="gappy") if r.reporting_date != date(2026, 10, 9)]
    result = detect([*other, *gappy])
    assert [(e.campaign_id, e.reason) for e in result.exclusions] == [
        ("gappy", ExclusionReason.INCOMPLETE_CAMPAIGN_COVERAGE)
    ]


def test_event_missing_a_day_is_excluded():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    main_rows = scenario()
    second_rows = [
        r for r in scenario(conversion_definition=second) if r.reporting_date != date(2026, 10, 9)
    ]
    result = detect([*main_rows, *second_rows])
    assert [e.reason for e in result.exclusions] == [ExclusionReason.INCOMPLETE_BASIS_COVERAGE]


def test_multi_event_campaign_is_not_double_counted():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    rows = [*scenario(), *scenario(conversion_definition=second)]
    assert detect(rows).findings == ()


def test_each_basis_is_evaluated_separately():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    rows = [
        *scenario(),
        *scenario(current={"conversions": "3"}, conversion_definition=second),
    ]
    findings = rule_findings(rows, RuleId.CONVERSION_RATE_DECLINE)
    assert [f.conversion_basis.event_id for f in findings] == ["second"]


def test_conflicting_delivery_stops_detection():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    rows = [*scenario(), *scenario(conversion_definition=second, spend="1.00")]
    with pytest.raises(ConflictingDeliveryError):
        detect(rows)


@pytest.mark.parametrize("missing_from", [BASELINE_START, CURRENT_START])
def test_incomplete_period_is_refused(missing_from: date):
    rows = [row for row in scenario() if row.reporting_date != missing_from]
    with pytest.raises(IncompletePeriodError):
        detect(rows)


# Finding contract


def adverse_finding() -> Finding:
    return rule_findings(
        scenario(current={"spend": "150.00"}), RuleId.COST_PER_CONVERSION_INCREASE
    )[0]


def test_finding_has_every_required_field():
    finding = adverse_finding()
    for field in fields(Finding):
        if field.name not in {"absolute_change", "percent_change", "baseline_value"}:
            assert getattr(finding, field.name) not in (None, "", (), {}), field.name
    assert finding.baseline_period == COMPARISON.baseline
    assert finding.current_period == COMPARISON.current
    assert finding.rule_id is RuleId.COST_PER_CONVERSION_INCREASE


def test_finding_id_is_stable_across_runs():
    assert adverse_finding().finding_id == adverse_finding().finding_id


def test_finding_id_ignores_metric_values():
    higher = rule_findings(
        scenario(current={"spend": "300.00"}), RuleId.COST_PER_CONVERSION_INCREASE
    )[0]
    assert higher.finding_id == adverse_finding().finding_id


def test_finding_id_changes_with_the_comparison():
    rows = scenario(current={"spend": "150.00"})
    other = PeriodComparison(
        "other",
        ReportingPeriod(BASELINE_START, date(2026, 10, 6)),
        ReportingPeriod(date(2026, 10, 9), date(2026, 10, 14)),
    )
    result = detect_anomalies(rows, other)
    other_finding = next(
        f for f in result.findings if f.rule_id is RuleId.COST_PER_CONVERSION_INCREASE
    )
    assert other_finding.finding_id != adverse_finding().finding_id


def test_finding_order_does_not_depend_on_input_order():
    rows = [*peers(), *strong(), *scenario(campaign_id="bad", current={"spend": "400.00"})]
    assert detect(rows) == detect(list(reversed(rows)))


def test_findings_disclose_that_no_statistical_test_ran():
    assert any("No statistical significance test" in t for t in adverse_finding().data_limitations)


def test_findings_do_not_claim_causes_or_significance():
    rows = [
        *peers(),
        *strong(),
        *scenario(
            campaign_id="bad",
            current={"spend": "400.00", "conversions": "1", "conversion_value": "10.00"},
        ),
    ]
    for finding in detect(rows).findings:
        observation = finding.observation.lower()
        for word in ("because", "caused", "due to", "significant"):
            assert word not in observation
