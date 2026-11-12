"""Experiment measurement quality: typed proposals, evidence-derived reference values,
honest feasibility, success criteria, guardrails, and measurement repair before any
performance test. All offline, on the synthetic sample report."""

import copy
import json
import math

import pytest

from growthpulse_ai.ai_analysis.evidence import build_evidence_package
from growthpulse_ai.ai_analysis.fallback import deterministic_response
from growthpulse_ai.ai_analysis.feasibility import (
    assess_feasibility,
    reference_values,
    tracking_risk_campaigns,
)
from growthpulse_ai.ai_analysis.service import (
    LEGACY_NOTE,
    dashboard_insights,
    render_insights,
    revalidate_artifact,
)
from growthpulse_ai.ai_analysis.validation import CHECKS_BY_VERSION, validate_response
from growthpulse_ai.config import PROJECT_ROOT

SAMPLE = PROJECT_ROOT / "docs" / "samples" / "synthetic_analytics_report.json"
PUBLISHED = (
    PROJECT_ROOT / "docs" / "samples" / "ai_insights"
    / "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601.json"
)  # fmt: skip


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(SAMPLE.read_text())


@pytest.fixture(scope="module")
def package(report):
    return build_evidence_package(report)


@pytest.fixture
def response(package) -> dict:
    return copy.deepcopy(deterministic_response(package))


def failed(raw, package, version="2") -> set[str]:
    result = validate_response(raw, package, version)
    assert not result.ok
    return {v.check for v in result.violations}


def finding(package, **match) -> dict:
    return next(f for f in package.ordered_findings if all(f.get(k) == v for k, v in match.items()))


# --- schema version 2 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("success_criteria"),
        lambda e: e.pop("guardrail_metrics"),
        lambda e: e.pop("experiment_type"),
        lambda e: e.pop("requires_measurement_repair_first"),
        lambda e: e.update(experiment_type="a_b_test"),
        lambda e: e.update(guardrail_metrics=[]),
        lambda e: e.update(guardrail_metrics=["revenue"]),
        lambda e: e.update(baseline_reference="current_value"),  # the model may not pick it
        lambda e: e.update(requires_measurement_repair_first="yes"),
    ],
)
def test_version_2_experiments_need_every_design_field(package, response, mutate):
    mutate(response["experiments"][0])
    assert "schema" in failed(response, package)


def test_a_guardrail_cannot_repeat_the_target_metric(package, response):
    experiment = response["experiments"][0]
    experiment["guardrail_metrics"] = [experiment["target_metric"]]
    assert "experiment_design_consistent" in failed(response, package)


def test_guardrails_cannot_repeat(package, response):
    response["experiments"][0]["guardrail_metrics"] = ["spend", "spend"]
    assert "experiment_design_consistent" in failed(response, package)


def test_success_criteria_numbers_must_come_from_evidence(package, response):
    response["experiments"][0]["success_criteria"] = "Success is a lift of 12.5% or more."
    assert failed(response, package) == {"numbers_supported"}


# --- measurement repair before performance tests -----------------------------------------


@pytest.fixture(scope="module")
def risky(package) -> dict:
    """A finding on a campaign that spent with no conversions: a possible tracking fault."""
    campaigns = tracking_risk_campaigns(package)
    assert campaigns, "the sample report plants a zero-conversion run"
    return next(
        f for f in package.ordered_findings
        if f["campaign_id"] in campaigns and f["rule_id"] != "spend_with_zero_conversions"
    )  # fmt: skip


def experiment_for(package, response, finding_entry, experiment_type, repair_first) -> dict:
    experiment = copy.deepcopy(response["experiments"][0])
    experiment.update(
        title=f"Design check {experiment_type}",
        finding_ids=[finding_entry["finding_id"]],
        baseline_finding_id=finding_entry["finding_id"],
        target_metric=finding_entry["metric"],
        attribution_basis_id=finding_entry["conversion_basis"]["basis_id"],
        currency=finding_entry["currency"],
        problem_statement="A campaign changed.",
        hypothesis="A change may help.",
        proposed_experiment="Test a change.",
        measurement_method="Compare arms on the same conversion basis.",
        success_criteria="The test arm improves on the target metric.",
        guardrail_metrics=[m for m in ("spend", "clicks") if m != finding_entry["metric"]],
        experiment_type=experiment_type,
        requires_measurement_repair_first=repair_first,
    )
    raw = copy.deepcopy(response)
    raw["experiments"] = [experiment]
    return raw


@pytest.mark.parametrize("experiment_type", ["randomized_experiment", "monitoring"])
def test_performance_tests_on_a_tracking_risk_campaign_must_wait_for_repair(
    package, response, risky, experiment_type
):
    blocked = experiment_for(package, response, risky, experiment_type, repair_first=False)
    assert failed(blocked, package) == {"measurement_repair_first"}
    ordered = experiment_for(package, response, risky, experiment_type, repair_first=True)
    assert validate_response(ordered, package).ok


@pytest.mark.parametrize("experiment_type", ["measurement_repair", "diagnostic_investigation"])
def test_repairs_and_investigations_need_no_prior_repair(package, response, risky, experiment_type):
    raw = experiment_for(package, response, risky, experiment_type, repair_first=False)
    assert validate_response(raw, package).ok


def test_the_fallback_types_tracking_faults_as_repairs_and_orders_tests_after_them(package):
    raw = deterministic_response(package)
    risky_campaigns = tracking_risk_campaigns(package)
    types = {}
    for experiment in raw["experiments"]:
        source = package.findings[experiment["baseline_finding_id"]]
        types[source["rule_id"]] = experiment["experiment_type"]
        if (
            experiment["experiment_type"] == "randomized_experiment"
            and source["campaign_id"] in risky_campaigns
        ):
            assert experiment["requires_measurement_repair_first"] is True
        assert experiment["target_metric"] not in experiment["guardrail_metrics"]
    if "spend_with_zero_conversions" in types:
        assert types["spend_with_zero_conversions"] == "measurement_repair"
    assert validate_response(raw, package).ok


# --- reference values: derived from evidence, never from the model ------------------------


def test_current_baseline_and_historical_target_come_from_the_finding(package):
    entry = finding(package, metric="platform_attributed_conversion_rate")
    values = reference_values(entry)
    assert values["current"]["value"] == entry["current_value"]
    assert values["current"]["period"] == entry["current_period"]
    assert values["comparison"]["kind"] == "historical_recovery_target"
    assert values["comparison"]["value"] == entry["baseline_value"]
    assert values["comparison"]["period"] == entry["baseline_period"]
    assert values["comparison"]["currency"] == entry["currency"]


def test_a_peer_comparison_is_a_benchmark_not_a_recovery_target(package):
    entry = finding(package, rule_id="strong_performer")
    values = reference_values(entry)
    assert values["comparison"]["kind"] == "peer_benchmark"
    assert "not this campaign's own history" in values["comparison"]["note"]


def test_rendered_experiments_carry_basis_currency_window_and_metric(package):
    rendered = render_insights(
        validate_response(deterministic_response(package), package).response, package
    )
    for experiment in rendered["experiments"]:
        source = package.findings[experiment["reference_values"]["finding_id"]]
        assert experiment["attribution"]["basis_id"] == source["conversion_basis"]["basis_id"]
        assert experiment["reference_values"]["current"]["currency"] == source["currency"]
        assert experiment["comparison_label"] == source["comparison_label"]
        assert experiment["target_metric_label"] == source["metric_label"]


# --- feasibility: computed where supported, honest where not ------------------------------


def test_conversion_rate_feasibility_matches_the_two_proportion_formula(package):
    entry = finding(package, metric="platform_attributed_conversion_rate")
    result = assess_feasibility("randomized_experiment", entry["metric"], entry, 28, False)
    rate = float(entry["current_value"])
    clicks = float(entry["evidence"]["current_clicks"]) / entry["current_days"] * 28 / 2
    expected = (1.959964 + 0.841621) * math.sqrt(2 * rate * (1 - rate) / clicks) / rate
    assert result["method"] == "two_proportion_normal_approximation"
    assert result["detectable_change"]["minimum_detectable_relative_change_percent"] == (
        f"{expected * 100:,.1f}"
    )
    assert result["assessment"] in {
        "adequate_for_full_recovery_only", "underpowered_for_full_recovery",
    }  # fmt: skip
    assert any("80% power" in a for a in result["assumptions"])
    assert any("split evenly" in a for a in result["assumptions"])


def test_shorter_tests_detect_only_larger_changes_and_tiny_ones_are_not_estimated(package):
    entry = finding(package, metric="platform_attributed_conversion_rate")
    long = assess_feasibility("randomized_experiment", entry["metric"], entry, 42, False)
    short = assess_feasibility("randomized_experiment", entry["metric"], entry, 14, False)
    detect = "minimum_detectable_relative_change_percent"
    assert float(short["detectable_change"][detect].replace(",", "")) > float(
        long["detectable_change"][detect].replace(",", "")
    )
    # A week gives fewer than ten expected conversions per arm: no estimate at all.
    week = assess_feasibility("randomized_experiment", entry["metric"], entry, 7, False)
    assert week["assessment"] == "not_assessable" and week["detectable_change"] is None


@pytest.mark.parametrize(
    "metric", ["cost_per_platform_attributed_conversion", "platform_attributed_roas"]
)
def test_ratio_metrics_get_no_invented_power_or_sample_size(package, metric):
    entry = finding(package, metric=metric)
    result = assess_feasibility("randomized_experiment", metric, entry, 28, False)
    assert result["method"] == "not_assessable" and result["assessment"] == "not_assessable"
    assert result["detectable_change"] is None
    assert "variance" in result["summary"]
    assert result["projection"]["platform_attributed_conversions_per_arm"]  # volume is shown


@pytest.mark.parametrize("experiment_type", ["diagnostic_investigation", "measurement_repair"])
def test_no_sample_size_for_investigations_or_repairs(package, experiment_type):
    entry = finding(package, metric="platform_attributed_conversion_rate")
    result = assess_feasibility(experiment_type, entry["metric"], entry, 14, False)
    assert result["method"] == "not_applicable" and result["detectable_change"] is None


def test_missing_volume_is_stated_not_guessed(package):
    entry = copy.deepcopy(finding(package, metric="platform_attributed_conversion_rate"))
    entry["evidence"].pop("current_clicks")
    result = assess_feasibility("randomized_experiment", entry["metric"], entry, 28, False)
    assert result["assessment"] == "not_assessable"
    assert any("missing" in caveat for caveat in result["caveats"])


def test_too_few_expected_events_is_not_assessed(package):
    entry = copy.deepcopy(finding(package, metric="platform_attributed_conversion_rate"))
    entry["evidence"]["current_clicks"] = 40
    entry["evidence"]["current_conversions"] = "1"
    entry["current_value"] = "0.025"
    result = assess_feasibility("randomized_experiment", entry["metric"], entry, 7, False)
    assert result["assessment"] == "not_assessable" and result["detectable_change"] is None


def test_a_tracking_risk_blocks_interpretation(package, risky):
    result = assess_feasibility("randomized_experiment", risky["metric"], risky, 21, True)
    assert result["interpretation_blocked_until_measurement_repair"] is True
    assert any("tracking" in caveat for caveat in result["caveats"])
    repair = assess_feasibility("measurement_repair", risky["metric"], risky, 7, True)
    assert repair["interpretation_blocked_until_measurement_repair"] is False


def test_feasibility_is_deterministic(package):
    entry = finding(package, metric="platform_attributed_conversion_rate")
    first = assess_feasibility("randomized_experiment", entry["metric"], entry, 21, False)
    assert first == assess_feasibility("randomized_experiment", entry["metric"], entry, 21, False)


# --- legacy (schema 1) artifacts --------------------------------------------------------


def test_the_published_sample_keeps_its_original_validation_and_is_labelled_legacy(report):
    artifact = json.loads(PUBLISHED.read_text())
    assert len(CHECKS_BY_VERSION["1"]) == 15
    insights, reason = revalidate_artifact(artifact, report)
    assert reason is None
    for experiment in insights["experiments"]:
        assert experiment["legacy"] is True and experiment["legacy_note"] == LEGACY_NOTE
        assert experiment["experiment_type"] is None
        assert experiment["success_criteria"] is None and experiment["guardrail_metrics"] == []
        assert experiment["feasibility"]["source"].startswith("GrowthPulse deterministic")
        assert any("Legacy proposal" in a for a in experiment["feasibility"]["assumptions"]) or (
            experiment["feasibility"]["method"] == "not_assessable"
        )
        assert experiment["reference_values"]["current"]["value"]


def test_the_sample_meta_proposal_is_blocked_until_tracking_is_repaired(report, package):
    insights, _ = revalidate_artifact(json.loads(PUBLISHED.read_text()), report)
    risky_campaigns = tracking_risk_campaigns(package)
    for experiment in insights["experiments"]:
        cited = {package.findings[c["finding_id"]]["campaign_id"] for c in experiment["citations"]}
        blocked = experiment["feasibility"]["interpretation_blocked_until_measurement_repair"]
        assert blocked is bool(cited & risky_campaigns)
    assert any(
        e["feasibility"]["interpretation_blocked_until_measurement_repair"]
        for e in insights["experiments"]
    )


def test_artifacts_without_a_schema_field_are_version_1(report):
    artifact = json.loads(PUBLISHED.read_text())
    artifact.pop("response_schema_version")
    insights, reason = revalidate_artifact(artifact, report)
    assert reason is None and insights["experiments"][0]["legacy"] is True


def test_an_unknown_schema_version_is_refused(report, package):
    artifact = json.loads(PUBLISHED.read_text())
    artifact["response_schema_version"] = "9"
    block = dashboard_insights(report, artifact)
    assert block["provenance"]["source"] == "deterministic_fallback"
    assert validate_response({}, package, "9").violations[0].detail == "unsupported_version"


def test_a_legacy_response_cannot_pass_as_version_2(report, package):
    """Old content is never silently presented as validated under the new rules."""
    artifact = json.loads(PUBLISHED.read_text())
    artifact["response_schema_version"] = "2"
    block = dashboard_insights(report, artifact)
    assert block["provenance"]["source"] == "deterministic_fallback"
    assert block["provenance"]["fallback_reason"] == "artifact failed re-validation"
