"""The validator: every AI response is accepted whole or rejected whole, against evidence."""

import copy
import json
from decimal import Decimal

import pytest

from growthpulse_ai.ai_analysis.evidence import build_evidence_package
from growthpulse_ai.ai_analysis.fallback import deterministic_response
from growthpulse_ai.ai_analysis.validation import (
    CHECKS,
    number_is_supported,
    validate_response,
)
from growthpulse_ai.config import PROJECT_ROOT

SAMPLE = PROJECT_ROOT / "docs" / "samples" / "synthetic_analytics_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(SAMPLE.read_text())


@pytest.fixture(scope="module")
def package(report):
    return build_evidence_package(report)


@pytest.fixture
def response(package) -> dict:
    return copy.deepcopy(deterministic_response(package))


def checks_failed(raw, package) -> set[str]:
    result = validate_response(raw, package)
    assert not result.ok
    assert result.response is None
    return {violation.check for violation in result.violations}


def two_findings(package, *, same_currency=True, same_basis=False) -> tuple[dict, dict]:
    findings = package.ordered_findings
    for first in findings:
        for second in findings:
            if first is second:
                continue
            if (first["currency"] == second["currency"]) != same_currency:
                continue
            basis_match = (
                first["conversion_basis"]["basis_id"] == second["conversion_basis"]["basis_id"]
            )
            if basis_match == same_basis:
                return first, second
    raise AssertionError("no such pair in the sample report")


# --- accepted responses --------------------------------------------------------------


def test_the_deterministic_response_passes_every_check(package, response):
    result = validate_response(response, package)
    assert result.ok, result.violations
    assert len(CHECKS) == 15


def test_rounded_evidence_values_and_rates_as_percentages_are_supported(package, response):
    finding = package.ordered_findings[0]
    value = Decimal(finding["current_value"])
    response["observed_facts"][0]["statement"] = (
        f"The current value is {value:.2f}, or {value:.1f} at one decimal place."
    )
    response["observed_facts"][0]["finding_ids"] = [finding["finding_id"]]
    assert validate_response(response, package).ok


@pytest.mark.parametrize(
    ("written", "decimals", "allowed", "expected"),
    [
        ("38", "5", {Decimal("38.54484977")}, True),
        ("39", None, {Decimal("38.54484977")}, True),
        ("38", "6", {Decimal("38.54484977")}, False),
        ("3", None, {Decimal("3.18585925")}, False),  # one significant figure: exact only
        ("3", "19", {Decimal("3.18585925")}, True),
        ("446", None, {Decimal("446")}, True),
        ("1,093", "80", {Decimal("1093.8")}, True),
        ("4", "14", {Decimal("0.0414") * 100}, True),
        ("10", None, {Decimal("9.66")}, True),
        ("12", None, {Decimal("9.66")}, False),
    ],
)
def test_number_matching_respects_written_precision(written, decimals, allowed, expected):
    assert number_is_supported(written, decimals, allowed) is expected


# --- schema ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("observed_facts"),
        lambda r: r.update(extra_field="not allowed"),
        lambda r: r.update(observed_facts=[]),
        lambda r: r["experiments"][0].update(requires_human_approval=False),
        lambda r: r["experiments"][0].update(duration_days="14"),
        lambda r: r["experiments"][0].update(duration_days=365),
        lambda r: r["experiments"][0].update(currency="pounds"),
        lambda r: r["experiments"][0].update(status="executed"),
        lambda r: r["observed_facts"][0].update(statement="x" * 601),
        lambda r: r["observed_facts"][0].update(finding_ids=[]),
    ],
)
def test_schema_violations_are_rejected(package, response, mutate):
    mutate(response)
    assert "schema" in checks_failed(response, package)


@pytest.mark.parametrize("raw", [None, "text", [], 42, {"observed_facts": "nope"}])
def test_non_object_responses_are_rejected_without_raising(package, raw):
    assert checks_failed(raw, package) == {"schema"}


def test_schema_errors_do_not_echo_the_rejected_input(package, response):
    response["experiments"][0]["currency"] = "SECRET-LOOKING-VALUE"
    result = validate_response(response, package)
    assert "SECRET-LOOKING-VALUE" not in repr(result.violations)


# --- evidence ----------------------------------------------------------------------


def test_unknown_finding_ids_are_rejected(package, response):
    response["observed_facts"][0]["finding_ids"] = ["0" * 64]
    assert "finding_ids_exist" in checks_failed(response, package)


def test_unsupported_numbers_are_rejected(package, response):
    response["observed_facts"][0]["statement"] += " Spend could rise by 12.34%."
    assert checks_failed(response, package) == {"numbers_supported"}


def test_a_number_from_an_uncited_finding_is_rejected(package, response):
    first, second = two_findings(package)
    response["observed_facts"][0] = {
        "statement": f"The value was {Decimal(second['evidence']['current_spend']):.2f}.",
        "finding_ids": [first["finding_id"]],
    }
    if Decimal(second["evidence"]["current_spend"]) in {
        Decimal(str(v)) for v in first["evidence"].values() if not isinstance(v, bool)
    }:
        pytest.skip("the two findings share this value")
    assert "numbers_supported" in checks_failed(response, package)


def test_derived_figures_are_rejected(package, response):
    finding = package.ordered_findings[0]
    baseline = Decimal(finding["evidence"]["baseline_spend"])
    current = Decimal(finding["evidence"]["current_spend"])
    response["observed_facts"][0] = {
        "statement": f"Spend changed by {abs(current - baseline) + Decimal('1000.01'):.2f}.",
        "finding_ids": [finding["finding_id"]],
    }
    assert "numbers_supported" in checks_failed(response, package)


def test_numbers_in_limitations_are_rejected(package, response):
    response["limitations"] = ["Results may be 20% off."]
    assert "numbers_supported" in checks_failed(response, package)


def test_dates_outside_the_cited_periods_are_rejected(package, response):
    response["observed_facts"][0]["statement"] += " The trend began on 2025-01-01."
    assert "dates_supported" in checks_failed(response, package)


def test_invented_campaigns_are_rejected(package, response):
    response["hypotheses"][0]["statement"] = "Search | Invented | Campaign may be the cause."
    assert "campaigns_known_and_cited" in checks_failed(response, package)


def test_uncited_real_campaigns_are_rejected(package, response):
    first, second = two_findings(package)
    if first["campaign_name"] == second["campaign_name"]:
        pytest.skip("same campaign")
    response["hypotheses"][0] = {
        "statement": f"{second['campaign_name']} may be affected too.",
        "finding_ids": [first["finding_id"]],
    }
    assert "campaigns_known_and_cited" in checks_failed(response, package)


def test_invented_identifiers_are_rejected(package, response):
    response["hypotheses"][0]["statement"] += " See synthetic-invented-account-999."
    assert "identifiers_known" in checks_failed(response, package)


# --- attribution and currency ------------------------------------------------------


def test_experiment_citing_two_conversion_bases_is_rejected(package, response):
    first, second = two_findings(package, same_basis=False)
    experiment = response["experiments"][0]
    experiment["finding_ids"] = [first["finding_id"], second["finding_id"]]
    experiment["baseline_finding_id"] = first["finding_id"]
    experiment["target_metric"] = first["metric"]
    experiment["attribution_basis_id"] = first["conversion_basis"]["basis_id"]
    experiment["currency"] = first["currency"]
    for key in ("problem_statement", "hypothesis"):
        experiment[key] = "Statement without figures."
    for key in ("title", "proposed_experiment"):
        experiment[key] = experiment[key].split(" on ")[0]
    assert "experiment_attribution_matches" in checks_failed(response, package)


def test_experiment_with_the_wrong_attribution_basis_is_rejected(package, response):
    experiment = response["experiments"][0]
    baseline = package.findings[experiment["baseline_finding_id"]]
    other = next(
        f
        for f in package.ordered_findings
        if f["conversion_basis"]["basis_id"] != baseline["conversion_basis"]["basis_id"]
    )
    experiment["attribution_basis_id"] = other["conversion_basis"]["basis_id"]
    assert "experiment_attribution_matches" in checks_failed(response, package)


def test_experiment_currency_must_match_its_findings(package, response):
    response["experiments"][0]["currency"] = "EUR"
    assert "experiment_currency_matches" in checks_failed(response, package)


def test_currency_symbols_must_match_the_cited_findings(package, response):
    response["observed_facts"][0]["statement"] += " Measured in €."
    assert "currency_symbols_match" in checks_failed(response, package)


def test_items_mixing_currencies_are_rejected(report):
    mixed = copy.deepcopy(report)
    findings = [f for c in mixed["comparisons"] for f in c["findings"]]
    findings[-1]["currency"] = "EUR"
    package = build_evidence_package(mixed)
    eur = next(f for f in package.ordered_findings if f["currency"] == "EUR")
    gbp = next(f for f in package.ordered_findings if f["currency"] == "GBP")
    raw = deterministic_response(package)
    raw["observed_facts"][0] = {
        "statement": "Two findings in different currencies.",
        "finding_ids": [eur["finding_id"], gbp["finding_id"]],
    }
    assert "single_currency" in checks_failed(raw, package)


def test_baseline_must_be_a_cited_finding_with_the_target_metric(package, response):
    first, second = two_findings(package)
    experiment = response["experiments"][0]
    cited = experiment["finding_ids"][0]
    outsider = next(f for f in package.ordered_findings if f["finding_id"] != cited)
    experiment["baseline_finding_id"] = outsider["finding_id"]
    assert "experiment_baseline_cited" in checks_failed(response, package)

    response2 = copy.deepcopy(deterministic_response(package))
    baseline = package.findings[response2["experiments"][0]["baseline_finding_id"]]
    other_metric = next(
        f["metric"] for f in package.ordered_findings if f["metric"] != baseline["metric"]
    )
    response2["experiments"][0]["target_metric"] = other_metric
    assert "experiment_target_metric_matches" in checks_failed(response2, package)


# --- facts, actions and duplicates -------------------------------------------------


def test_facts_may_not_state_causes(package, response):
    response["observed_facts"][0]["statement"] += " This was due to creative fatigue."
    assert checks_failed(response, package) == {"facts_state_no_causes"}


def test_hypotheses_may_suggest_causes(package, response):
    response["hypotheses"][0]["statement"] = "The change may be due to creative fatigue."
    assert validate_response(response, package).ok


@pytest.mark.parametrize(
    "claim",
    [
        "The bid change has been applied.",
        "We paused the campaign.",
        "GrowthPulse automatically adjusted the budget.",
        "The new creative is already live.",
        "The test was launched.",
    ],
)
def test_claims_that_something_was_done_are_rejected(package, response, claim):
    response["experiments"][0]["proposed_experiment"] = claim
    assert "no_execution_claims" in checks_failed(response, package)


def test_duplicate_facts_are_rejected(package, response):
    response["observed_facts"].append(copy.deepcopy(response["observed_facts"][0]))
    assert "no_duplicates" in checks_failed(response, package)


def test_duplicate_experiments_are_rejected(package, response):
    duplicate = copy.deepcopy(response["experiments"][0])
    duplicate["title"] = "A differently worded title"
    response["experiments"].append(duplicate)
    assert "no_duplicates" in checks_failed(response, package)


def test_a_finding_cited_twice_in_one_item_is_rejected(package, response):
    fact = response["observed_facts"][0]
    fact["finding_ids"] = fact["finding_ids"] * 2
    assert "no_duplicates" in checks_failed(response, package)
