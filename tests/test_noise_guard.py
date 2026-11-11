from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from factories import make_days
from growthpulse_ai.analytics.anomalies import (
    AnomalyThresholds,
    ExclusionReason,
    Finding,
    RuleId,
    Severity,
    detect_anomalies,
)
from growthpulse_ai.analytics.errors import InvalidPeriodError
from growthpulse_ai.analytics.noise import NoiseGuardOutcome, evaluate_noise_guard
from growthpulse_ai.analytics.periods import PeriodComparison, ReportingPeriod

BASELINE_START = date(2026, 10, 1)
CURRENT_START = date(2026, 10, 8)
COMPARISON = PeriodComparison(
    "test",
    ReportingPeriod(BASELINE_START, date(2026, 10, 7)),
    ReportingPeriod(CURRENT_START, date(2026, 10, 14)),
)
GUARD = AnomalyThresholds()
NO_GUARD = AnomalyThresholds(
    noise_guard_width=Decimal(0), noise_guard_high_severity_width=Decimal(0)
)
# 35 conversions per period at this volume, with a cost of 20.
LOW = {
    "impressions": 1000,
    "clicks": 100,
    "spend": "100.00",
    "conversions": "5",
    "conversion_value": "1500.00",
}
# 3,500 conversions per period, with the same cost of 20.
HIGH = {
    "impressions": 100000,
    "clicks": 10000,
    "spend": "10000.00",
    "conversions": "500",
    "conversion_value": "150000.00",
}
COST = RuleId.COST_PER_CONVERSION_INCREASE
RATE = RuleId.CONVERSION_RATE_DECLINE
ROAS = RuleId.ROAS_DETERIORATION


def rows(steady: dict, current: dict, **shared) -> list:
    return [
        *make_days(BASELINE_START, 7, **{**steady, **shared}),
        *make_days(CURRENT_START, 7, **{**steady, **current, **shared}),
    ]


def detect(data: list, thresholds: AnomalyThresholds = GUARD):
    return detect_anomalies(data, COMPARISON, thresholds)


def only(findings: tuple[Finding, ...], rule: RuleId) -> Finding:
    matches = [finding for finding in findings if finding.rule_id is rule]
    assert len(matches) == 1
    return matches[0]


def evaluate(width: Decimal, high_width: Decimal = Decimal(0), is_high: bool = False):
    # B = C = 100, so the spread is sqrt(0.02). A doubling gives a log ratio of ln(2).
    return evaluate_noise_guard(
        Decimal(1), Decimal(2), Decimal(100), Decimal(100), width, high_width, is_high
    )


LN2_WIDTH = Decimal(2).ln() / Decimal("0.02").sqrt()
EPSILON = Decimal("0.0000000001")


# Boundaries


def test_formula_values():
    result = evaluate(Decimal("1.5"))
    assert round(result.log_ratio_magnitude, 10) == round(Decimal(2).ln(), 10)
    assert round(result.band, 10) == round(Decimal("1.5") * Decimal("0.02").sqrt(), 10)


def test_change_just_above_band_passes():
    assert evaluate(LN2_WIDTH - EPSILON).outcome is NoiseGuardOutcome.PASSED


def test_change_just_below_band_is_suppressed():
    assert evaluate(LN2_WIDTH + EPSILON).outcome is NoiseGuardOutcome.SUPPRESSED


def test_high_severity_just_above_high_band_passes():
    result = evaluate(Decimal(1), LN2_WIDTH - EPSILON, is_high=True)
    assert result.outcome is NoiseGuardOutcome.PASSED


def test_high_severity_just_below_high_band_is_downgraded():
    result = evaluate(Decimal(1), LN2_WIDTH + EPSILON, is_high=True)
    assert result.outcome is NoiseGuardOutcome.DOWNGRADED


def test_medium_severity_ignores_the_high_band():
    assert evaluate(Decimal(1), LN2_WIDTH + EPSILON).outcome is NoiseGuardOutcome.PASSED


def test_decreases_and_increases_are_treated_symmetrically():
    up = evaluate_noise_guard(
        Decimal(1), Decimal(2), Decimal(50), Decimal(50), Decimal(1), Decimal(1), False
    )
    down = evaluate_noise_guard(
        Decimal(2), Decimal(1), Decimal(50), Decimal(50), Decimal(1), Decimal(1), False
    )
    assert up.log_ratio_magnitude == down.log_ratio_magnitude


def test_fractional_counts_are_accepted():
    result = evaluate_noise_guard(
        Decimal(1), Decimal(2), Decimal("12.5"), Decimal("7.25"), Decimal(1), Decimal(1), False
    )
    assert result.baseline_conversions == Decimal("12.5")
    assert result.outcome is NoiseGuardOutcome.PASSED


@pytest.mark.parametrize(
    ("baseline", "current", "b", "c"),
    [
        (None, Decimal(1), Decimal(1), Decimal(1)),
        (Decimal(1), None, Decimal(1), Decimal(1)),
        (Decimal(0), Decimal(1), Decimal(1), Decimal(1)),
        (Decimal(1), Decimal(0), Decimal(1), Decimal(1)),
        (Decimal(1), Decimal(1), Decimal(0), Decimal(1)),
        (Decimal(1), Decimal(1), Decimal(1), Decimal(0)),
    ],
)
def test_undefined_inputs_are_not_evaluated(baseline, current, b, c):
    result = evaluate_noise_guard(baseline, current, b, c, Decimal(1), Decimal(1), False)
    assert result.outcome is NoiseGuardOutcome.NOT_EVALUATED
    assert result.band is None


def test_zero_width_disables_the_guard():
    assert evaluate(Decimal(0)).outcome is NoiseGuardOutcome.PASSED


def test_rejects_high_width_below_width():
    with pytest.raises(InvalidPeriodError):
        AnomalyThresholds(noise_guard_width=Decimal(3))


# Low-volume suppression


@pytest.mark.parametrize(
    ("current", "rule"),
    [
        ({"spend": "130.00"}, COST),
        ({"conversions": "4"}, RATE),
        ({"conversion_value": "1100.00"}, ROAS),
    ],
)
def test_low_volume_change_is_suppressed_not_lost(current: dict, rule: RuleId):
    result = detect(rows(LOW, current))
    assert not [finding for finding in result.findings if finding.rule_id is rule]
    finding = only(result.suppressed_findings, rule)
    assert finding.noise_guard.outcome is NoiseGuardOutcome.SUPPRESSED
    assert finding.evidence
    assert finding.percent_change is not None


def test_suppressed_finding_keeps_rule_severity():
    finding = only(detect(rows(LOW, {"spend": "130.00"})).suppressed_findings, COST)
    assert finding.severity is Severity.MEDIUM
    assert finding.pre_guard_severity is Severity.MEDIUM


def test_guard_records_its_inputs():
    guard = only(detect(rows(LOW, {"spend": "130.00"})).suppressed_findings, COST).noise_guard
    assert guard.baseline_conversions == Decimal(35)
    assert guard.current_conversions == Decimal(35)
    assert guard.log_ratio_magnitude < guard.band


def test_minimum_volume_still_applies_before_the_guard():
    data = rows(
        {**LOW, "conversions": "1", "conversion_value": "300.00"},
        {"spend": "900.00", "conversions": "0.5", "conversion_value": "5.00"},
    )
    result = detect(data)
    assert result.findings == ()
    assert result.suppressed_findings == ()
    assert [e.reason for e in result.exclusions] == [ExclusionReason.INSUFFICIENT_VOLUME]


def test_rule_threshold_still_applies_before_the_guard():
    result = detect(rows(HIGH, {"spend": "11000.00"}))
    assert not [f for f in result.findings if f.rule_id is COST]
    assert not [f for f in result.suppressed_findings if f.rule_id is COST]


# Strong-signal preservation


@pytest.mark.parametrize(
    ("current", "rule"),
    [
        ({"spend": "13000.00"}, COST),
        ({"conversions": "400"}, RATE),
        ({"conversion_value": "110000.00"}, ROAS),
    ],
)
def test_high_volume_change_stays_active(current: dict, rule: RuleId):
    finding = only(detect(rows(HIGH, current)).findings, rule)
    assert finding.noise_guard.outcome is NoiseGuardOutcome.PASSED
    assert finding.severity is finding.pre_guard_severity


def test_large_change_survives_at_low_volume():
    finding = only(detect(rows(LOW, {"spend": "400.00"})).findings, COST)
    assert finding.severity is Severity.HIGH
    assert finding.noise_guard.outcome is NoiseGuardOutcome.PASSED


def test_borderline_high_severity_is_downgraded_and_labeled():
    # +50% clears the 1.5 band at 35 conversions but not the 2.0 band.
    finding = only(detect(rows(LOW, {"spend": "150.00"})).findings, COST)
    assert finding.pre_guard_severity is Severity.HIGH
    assert finding.severity is Severity.MEDIUM
    assert finding.noise_guard.outcome is NoiseGuardOutcome.DOWNGRADED


def test_guard_does_not_change_finding_ids():
    data = rows(LOW, {"spend": "130.00"})
    guarded = only(detect(data).suppressed_findings, COST)
    unguarded = only(detect(data, NO_GUARD).findings, COST)
    assert guarded.finding_id == unguarded.finding_id


def test_disabled_guard_suppresses_nothing():
    assert detect(rows(LOW, {"spend": "130.00"}), NO_GUARD).suppressed_findings == ()


def test_width_is_configurable():
    data = rows(LOW, {"spend": "130.00"})
    relaxed = replace(GUARD, noise_guard_width=Decimal("0.5"))
    assert only(detect(data, relaxed).findings, COST).noise_guard.outcome is (
        NoiseGuardOutcome.PASSED
    )


# Zero conversions


def test_conversion_rate_falling_to_zero_is_kept_active():
    data = rows(LOW, {"conversions": "0", "conversion_value": "0"})
    finding = only(detect(data).findings, RATE)
    assert finding.noise_guard.outcome is NoiseGuardOutcome.NOT_EVALUATED
    assert finding.current_value == Decimal(0)


def test_roas_falling_to_zero_is_kept_active():
    data = rows(LOW, {"conversions": "0", "conversion_value": "0"})
    assert only(detect(data).findings, ROAS).noise_guard.outcome is (
        NoiseGuardOutcome.NOT_EVALUATED
    )


def test_zero_run_is_not_guarded():
    data = rows(LOW, {"conversions": "0", "conversion_value": "0"})
    finding = only(detect(data).findings, RuleId.SPEND_WITH_ZERO_CONVERSIONS)
    assert finding.noise_guard.outcome is NoiseGuardOutcome.NOT_APPLICABLE


# Scope and wording


def test_spend_rule_is_not_guarded():
    finding = only(
        detect(rows(LOW, {"spend": "150.00"})).findings, RuleId.SPEND_WITHOUT_CONVERSION_GROWTH
    )
    assert finding.noise_guard.outcome is NoiseGuardOutcome.NOT_APPLICABLE


def test_guarded_findings_disclaim_significance_and_verified_customers():
    finding = only(detect(rows(LOW, {"spend": "400.00"})).findings, COST)
    text = " ".join(finding.data_limitations)
    assert "not a significance test or a confidence interval" in text
    assert "not verified customers or bookings" in text


# Isolation


def test_each_basis_uses_its_own_conversion_count():
    second = {"event_id": "second", "event_name": "Second", "stage": "lead"}
    steady_second = {**HIGH, "conversions": "3", "conversion_value": "900.00"}
    data = [
        *rows(HIGH, {"conversions": "350"}),
        *rows(steady_second, {"conversions": "2.1"}, conversion_definition=second),
    ]
    result = detect(data)
    assert only(result.findings, RATE).conversion_basis.event_id == "synthetic-booking"
    assert only(result.suppressed_findings, RATE).conversion_basis.event_id == "second"


def test_currencies_are_evaluated_separately():
    data = [
        *rows(HIGH, {"spend": "13000.00"}, account_id="uk", currency="GBP"),
        *rows(LOW, {"spend": "130.00"}, account_id="eu", currency="EUR"),
    ]
    result = detect(data)
    assert only(result.findings, COST).currency == "GBP"
    assert only(result.suppressed_findings, COST).currency == "EUR"


# Determinism


def test_suppressed_order_does_not_depend_on_input_order():
    data = [
        *rows(LOW, {"spend": "130.00"}, campaign_id="a"),
        *rows(LOW, {"spend": "130.00"}, campaign_id="b"),
    ]
    assert detect(data) == detect(list(reversed(data)))
