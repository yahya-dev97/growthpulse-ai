import json
import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from factories import make_days, make_meta_record, make_record
from growthpulse_ai.analytics.anomalies import RuleId
from growthpulse_ai.analytics.errors import (
    AnalyticsError,
    IncompletePeriodError,
    MixedDataSourceError,
)
from growthpulse_ai.analytics.periods import PeriodComparison, ReportingPeriod, latest_vs_first
from growthpulse_ai.analytics.report import build_report, dumps_report, to_json_compatible
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import (
    DEFAULT_DAYS,
    DEFAULT_START_DATE,
    SCENARIOS,
    ScenarioKind,
)
from growthpulse_ai.pipeline.sample_report import (
    SAMPLE_REPORT_PATH,
    build_sample_report,
    write_sample_report,
)

END_DATE = DEFAULT_START_DATE + timedelta(days=DEFAULT_DAYS - 1)
SHORT = PeriodComparison(
    "short",
    ReportingPeriod(date(2026, 10, 1), date(2026, 10, 3)),
    ReportingPeriod(date(2026, 10, 4), date(2026, 10, 6)),
)

PRIMARY_RULE = {
    ScenarioKind.SUSTAINED_COST_PER_CONVERSION_INCREASE: RuleId.COST_PER_CONVERSION_INCREASE,
    ScenarioKind.CONVERSION_RATE_DECLINE: RuleId.CONVERSION_RATE_DECLINE,
    ScenarioKind.STRONG_PERFORMER: RuleId.STRONG_PERFORMER,
    ScenarioKind.ZERO_CONVERSIONS: RuleId.SPEND_WITH_ZERO_CONVERSIONS,
}


def small_rows(**overrides) -> list:
    return make_days(date(2026, 10, 1), 6, **overrides)


def small_report(**overrides) -> dict[str, Any]:
    return build_report(small_rows(**overrides), comparisons=[SHORT])


@pytest.fixture(scope="module")
def sample() -> dict[str, Any]:
    return build_sample_report()


def comparison(report: dict[str, Any], label: str) -> dict[str, Any]:
    return next(item for item in report["comparisons"] if item["label"] == label)


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


# Serialization


def test_report_is_json_serializable():
    assert json.loads(dumps_report(small_report())) == small_report()


def test_report_contains_only_plain_json_types(sample: dict[str, Any]):
    allowed = (dict, list, str, int, bool, type(None))
    assert all(isinstance(value, allowed) for value in walk(sample))


def test_report_has_no_floats(sample: dict[str, Any]):
    assert not any(isinstance(value, float) for value in walk(sample))


def test_decimals_are_exact_strings():
    assert to_json_compatible(Decimal("120.50")) == "120.5"
    assert to_json_compatible(Decimal("0.123456789")) == "0.12345679"


def test_zero_is_never_negative():
    assert to_json_compatible(Decimal("-0.000000001")) == "0"


def test_large_values_avoid_exponents():
    assert to_json_compatible(Decimal("1000000")) == "1000000"


def test_values_carry_no_display_formatting(sample: dict[str, Any]):
    delivery = sample["full_period"]["portfolios"][0]["delivery"]
    assert "£" not in delivery["spend"]
    assert "%" not in delivery["ctr"]


def test_undefined_metrics_are_null():
    report = small_report(conversions="0", conversion_value="0")
    kpis = report["full_period"]["campaigns"][0]["kpis"]["conversions_by_basis"][0]
    assert kpis["cost_per_platform_attributed_conversion"] is None
    assert kpis["platform_attributed_conversion_rate"] == "0"


# Idempotence


def test_output_is_identical_across_runs():
    assert dumps_report(build_sample_report()) == dumps_report(build_sample_report())


def test_output_does_not_depend_on_input_order():
    records = list(SyntheticConnector().fetch(DEFAULT_START_DATE, END_DATE).records)
    shuffled = list(records)
    random.Random(1).shuffle(shuffled)  # noqa: S311
    assert build_report(records) == build_report(shuffled)


def test_report_id_is_stable_and_content_based():
    assert small_report()["report_id"] == small_report()["report_id"]
    assert small_report(spend="99.00")["report_id"] != small_report()["report_id"]


def test_committed_sample_matches_a_fresh_run():
    assert SAMPLE_REPORT_PATH.read_text(encoding="utf-8") == dumps_report(build_sample_report())


def test_write_sample_report_creates_file(tmp_path):
    target = tmp_path / "nested" / "report.json"
    assert write_sample_report(target) == target
    assert json.loads(target.read_text(encoding="utf-8"))["data_source"] == "synthetic"


# Contract


def test_report_labels_synthetic_data(sample: dict[str, Any]):
    assert sample["data_source"] == "synthetic"
    assert sample["is_synthetic"] is True


def test_live_data_is_not_labeled_synthetic():
    assert small_report(data_source="live")["is_synthetic"] is False


def test_report_states_true_cac_is_unavailable(sample: dict[str, Any]):
    assert sample["true_cac_available"] is False
    assert (
        "not true customer"
        in sample["metric_definitions"]["cost_per_platform_attributed_conversion"]
    )


def test_report_has_no_bare_cac_or_total_conversion_keys(sample: dict[str, Any]):
    keys = {key for value in walk(sample) if isinstance(value, dict) for key in value}
    assert not {"cac", "conversions", "cost_per_conversion", "total_conversions"} & keys


def test_report_records_thresholds_and_input(sample: dict[str, Any]):
    assert sample["thresholds"]["cost_increase_percent"] == "25"
    assert sample["input"]["record_count"] == 300


def test_data_quality_summary_is_carried(sample: dict[str, Any]):
    assert sample["data_quality"]["quarantined_rows"] == 0


def test_portfolios_are_separated_by_currency():
    rows = [
        *make_days(date(2026, 10, 1), 6, account_id="uk", currency="GBP"),
        *make_days(date(2026, 10, 1), 6, account_id="eu", currency="EUR"),
    ]
    report = build_report(rows, comparisons=[SHORT])
    portfolios = report["full_period"]["portfolios"]
    assert [p["currency"] for p in portfolios] == ["EUR", "GBP"]
    assert all(p["delivery"]["spend"] == "723" for p in portfolios)


def test_portfolio_keeps_platform_conversions_separate():
    rows = [
        *small_rows(),
        *[
            make_meta_record(reporting_date=(date(2026, 10, 1) + timedelta(days=i)).isoformat())
            for i in range(6)
        ],
    ]
    report = build_report(rows, comparisons=[SHORT])
    portfolio = report["full_period"]["portfolios"][0]
    assert len(portfolio["conversions_by_basis"]) == 2
    assert {c["basis"]["platform"] for c in portfolio["conversions_by_basis"]} == {
        "google_ads",
        "meta_ads",
    }


def test_full_period_covers_the_dataset(sample: dict[str, Any]):
    assert sample["full_period"]["period"] == {"start": "2026-10-01", "end": "2026-10-30"}
    assert len(sample["full_period"]["campaigns"]) == 10


def test_default_comparisons(sample: dict[str, Any]):
    assert [item["label"] for item in sample["comparisons"]] == [
        "latest_7_vs_previous_7",
        "latest_10_vs_first_10",
    ]


def test_full_period_spend_matches_records(sample: dict[str, Any]):
    records = SyntheticConnector().fetch(DEFAULT_START_DATE, END_DATE).records
    expected = sum(record.spend for record in records)
    assert Decimal(sample["full_period"]["portfolios"][0]["delivery"]["spend"]) == expected


def test_comparison_changes_are_reported(sample: dict[str, Any]):
    campaign = comparison(sample, "latest_10_vs_first_10")["campaigns"][0]
    names = [change["metric"] for change in campaign["changes"]["delivery"]]
    assert "spend" in names
    assert campaign["changes"]["conversions_by_basis"]


def test_empty_report_is_refused():
    with pytest.raises(AnalyticsError):
        build_report([])


def test_report_with_a_gap_is_refused():
    rows = [row for row in small_rows() if row.reporting_date != date(2026, 10, 3)]
    with pytest.raises(IncompletePeriodError):
        build_report(rows, comparisons=[])


def test_mixed_sources_are_refused():
    rows = [make_record(data_source="synthetic"), make_record(data_source="live")]
    with pytest.raises(MixedDataSourceError):
        build_report(rows, comparisons=[])


def test_short_dataset_needs_explicit_comparisons():
    with pytest.raises(AnalyticsError):
        build_report(small_rows())


# Planted scenarios, using SCENARIOS as ground truth


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.kind.value)
def test_planted_scenario_is_detected(sample: dict[str, Any], scenario):
    findings = comparison(sample, "latest_10_vs_first_10")["findings"]
    matches = [
        finding
        for finding in findings
        if finding["campaign_id"] == f"synthetic-{scenario.campaign_key}"
        and finding["rule_id"] == PRIMARY_RULE[scenario.kind].value
    ]
    assert len(matches) == 1


def test_zero_run_matches_scenario_dates(sample: dict[str, Any]):
    scenario = next(s for s in SCENARIOS if s.kind is ScenarioKind.ZERO_CONVERSIONS)
    window = latest_vs_first(DEFAULT_START_DATE, END_DATE, days=10).current
    scenario_start = DEFAULT_START_DATE + timedelta(days=scenario.start_day)
    scenario_end = DEFAULT_START_DATE + timedelta(days=scenario.end_day)
    finding = next(
        f
        for f in comparison(sample, "latest_10_vs_first_10")["findings"]
        if f["rule_id"] == RuleId.SPEND_WITH_ZERO_CONVERSIONS.value
    )
    assert finding["evidence"]["zero_run_start"] == max(scenario_start, window.start).isoformat()
    assert finding["evidence"]["zero_run_end"] == min(scenario_end, window.end).isoformat()


@pytest.mark.parametrize(
    ("campaign", "rule"),
    [
        ("gs-mri-london", RuleId.COST_PER_CONVERSION_INCREASE),
        ("pmax-ct-national", RuleId.CONVERSION_RATE_DECLINE),
    ],
)
def test_planted_plateaus_are_not_reported_as_resolved(
    sample: dict[str, Any], campaign: str, rule: RuleId
):
    matches = [
        item
        for item in sample["persistence_assessments"]
        if item["campaign_id"] == f"synthetic-{campaign}" and item["rule_id"] == rule.value
    ]
    assert len(matches) == 1
    assert matches[0]["status"] == "plateaued_still_elevated"
    assert matches[0]["resolution_confirmed"] is False


def test_only_the_planted_campaign_is_a_strong_performer(sample: dict[str, Any]):
    expected = next(s for s in SCENARIOS if s.kind is ScenarioKind.STRONG_PERFORMER)
    for item in sample["comparisons"]:
        strong = [
            finding["campaign_id"]
            for finding in item["findings"]
            if finding["rule_id"] == RuleId.STRONG_PERFORMER.value
        ]
        assert strong == [f"synthetic-{expected.campaign_key}"]


def test_every_finding_is_fully_populated(sample: dict[str, Any]):
    required = {
        "finding_id",
        "platform",
        "account_id",
        "campaign_id",
        "metric",
        "baseline_value",
        "current_value",
        "absolute_change",
        "percent_change",
        "baseline_period",
        "current_period",
        "severity",
        "evidence",
        "data_limitations",
        "rule_id",
    }
    findings = [f for item in sample["comparisons"] for f in item["findings"]]
    assert findings
    for finding in findings:
        assert required <= finding.keys()
        assert finding["evidence"]
        assert finding["data_limitations"]


def test_finding_ids_are_unique_within_the_report(sample: dict[str, Any]):
    ids = [f["finding_id"] for item in sample["comparisons"] for f in item["findings"]]
    assert len(ids) == len(set(ids))


def test_synthetic_findings_disclose_synthetic_data(sample: dict[str, Any]):
    for item in sample["comparisons"]:
        for finding in item["findings"]:
            assert any("synthetic data" in text for text in finding["data_limitations"])
