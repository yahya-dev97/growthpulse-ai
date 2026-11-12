"""Deterministic insights, used whenever AI output is unavailable or fails validation.

The fallback builds a response in exactly the shape a provider must return, from the
evidence package alone, and is validated by the same checks. Its templates contain no
numbers: every figure is copied from a finding's evidence, and test lengths live in the
structured ``duration_days`` field. Same package in, same insights out.
"""

from decimal import Decimal

from .evidence import EvidencePackage

MAX_FACTS = 6
MAX_HYPOTHESES = 4
MAX_EXPERIMENTS = 4

# rule_id -> (hypothesis, title, proposed experiment, measurement, duration, risks, assumptions)
TEMPLATES: dict[str, dict] = {
    "cost_per_conversion_increase": {
        "hypothesis": "Auction pressure or a bid-strategy mismatch may be raising the cost of "
        "each platform-attributed conversion on {campaign}.",
        "title": "Bid-strategy split test on {campaign}",
        "experiment": "Split {campaign} traffic between the current bid strategy and one "
        "target-based alternative, using the platform's own experiment tool.",
        "measurement": "Compare cost per platform-attributed conversion between the two arms "
        "on the same conversion basis, over the same dates.",
        "duration_days": 14,
        "risks": ["A learning period may depress results in the test arm early on."],
        "assumptions": ["Conversion tracking stays unchanged for the whole test."],
    },
    "conversion_rate_decline": {
        "hypothesis": "A change in the post-click experience may be lowering the "
        "platform-attributed conversion rate on {campaign}.",
        "title": "Landing-page variant test on {campaign}",
        "experiment": "Send part of {campaign} traffic to one landing-page variant and keep "
        "the rest on the current page.",
        "measurement": "Compare platform-attributed conversion rate between the two pages on "
        "the same conversion basis.",
        "duration_days": 14,
        "risks": ["Traffic may be too low for a clear result within the test window."],
        "assumptions": ["Ad targeting and bids stay unchanged while the test runs."],
    },
    "spend_without_conversion_growth": {
        "hypothesis": "Extra spend on {campaign} may be reaching lower-intent audiences.",
        "title": "Budget hold-back test on {campaign}",
        "experiment": "Hold {campaign} budget at its baseline level in a matched test and "
        "compare against continued spend.",
        "measurement": "Compare platform-attributed conversions per unit of spend between the "
        "periods, on the same conversion basis.",
        "duration_days": 14,
        "risks": ["Holding budget back may lose conversions that would otherwise occur."],
        "assumptions": ["Seasonal demand is similar across the comparison periods."],
    },
    "spend_with_zero_conversions": {
        "hypothesis": "A conversion-tracking fault is as likely as true non-performance "
        "to explain spend with no platform-attributed conversions on {campaign}.",
        "title": "Tracking audit, then pause test, on {campaign}",
        "experiment": "Audit conversion tracking for {campaign} first. Only if tracking is "
        "intact, test pausing the campaign against keeping it live.",
        "measurement": "Confirm test conversions are recorded, then compare platform-attributed "
        "conversions across the client's other campaigns during the pause.",
        "duration_days": 7,
        "risks": ["Pausing a campaign whose tracking is broken would hide a real performer."],
        "assumptions": ["The audit can be completed before any pause is considered."],
    },
    "roas_deterioration": {
        "hypothesis": "Creative fatigue or a shift in audience mix may be lowering "
        "platform-attributed ROAS on {campaign}.",
        "title": "Creative refresh test on {campaign}",
        "experiment": "Test fresh creatives against the current ones on {campaign}, with the "
        "platform splitting traffic evenly.",
        "measurement": "Compare platform-attributed ROAS between creative sets on the same "
        "conversion basis. Platform-reported value is not realised revenue.",
        "duration_days": 14,
        "risks": ["Platform-reported conversion value may not reflect realised revenue."],
        "assumptions": ["Pricing and offers stay unchanged during the test."],
    },
    "strong_performer": {
        "hypothesis": "{campaign} may have headroom to take more budget at a similar cost "
        "per platform-attributed conversion.",
        "title": "Measured budget scaling test on {campaign}",
        "experiment": "Raise the {campaign} budget in a controlled step and hold everything "
        "else constant.",
        "measurement": "Track cost per platform-attributed conversion against the pre-test "
        "level on the same conversion basis.",
        "duration_days": 14,
        "risks": ["Efficiency often falls as budget grows, so gains may not scale."],
        "assumptions": ["Demand in the campaign's market is not already saturated."],
    },
}


def _plain(value: str | int | None, places: int = 2) -> str | None:
    if value is None:
        return None
    return f"{abs(Decimal(str(value))):,.{places}f}"


def _value(finding: dict, key: str) -> str | None:
    value = finding.get(key)
    if value is None:
        return None
    if "rate" in finding["metric"] or finding["metric"] == "ctr":
        return f"{_plain(Decimal(str(value)) * 100)}%"
    if "cost" in finding["metric"] or finding["metric"] == "cpc":
        return f"{finding['currency']} {_plain(value)}"
    return _plain(value)


def _period(period: dict) -> str:
    return f"{period['start']} to {period['end']}"


def _campaign(finding: dict) -> str:
    return finding["campaign_name"].removeprefix("[SYNTHETIC] ")


def fact_statement(finding: dict) -> str:
    campaign, label = _campaign(finding), finding["metric_label"]
    evidence = finding.get("evidence", {})
    if finding["rule_id"] == "spend_with_zero_conversions" and "zero_run_spend" in evidence:
        return (
            f"{campaign} spent {finding['currency']} {_plain(evidence['zero_run_spend'])} with "
            f"no platform-attributed conversions from {evidence['zero_run_start']} to "
            f"{evidence['zero_run_end']}."
        )
    current = _value(finding, "current_value")
    baseline = _value(finding, "baseline_value")
    if str(finding.get("baseline_kind", "")).startswith("peer_median"):
        return (
            f"{label} for {campaign} was {current} against a peer median of {baseline} "
            f"from {_period(finding['current_period'])}."
        )
    statement = (
        f"{label} for {campaign} moved from {baseline} ({_period(finding['baseline_period'])}) "
        f"to {current} ({_period(finding['current_period'])})"
    )
    change = finding.get("percent_change")
    if change is not None:
        direction = "a fall" if Decimal(str(change)) < 0 else "a rise"
        statement += f", {direction} of {_plain(change, 1)}%"
    return statement + "."


def deterministic_response(package: EvidencePackage) -> dict:
    """A provider-shaped response built only from the evidence."""
    findings = package.content["findings"]
    facts = [
        {"statement": fact_statement(finding), "finding_ids": [finding["finding_id"]]}
        for finding in findings[:MAX_FACTS]
    ]
    hypotheses, experiments, seen = [], [], set()
    for finding in findings:
        template = TEMPLATES.get(finding["rule_id"])
        key = (finding["rule_id"], finding["campaign_id"])
        if template is None or key in seen:
            continue
        seen.add(key)
        campaign = _campaign(finding)
        if len(hypotheses) < MAX_HYPOTHESES:
            hypotheses.append(
                {
                    "statement": template["hypothesis"].format(campaign=campaign),
                    "finding_ids": [finding["finding_id"]],
                }
            )
        if len(experiments) < MAX_EXPERIMENTS:
            experiments.append(
                {
                    "title": template["title"].format(campaign=campaign),
                    "finding_ids": [finding["finding_id"]],
                    "kind": "opportunity" if finding["direction"] == "positive" else "problem",
                    "problem_statement": fact_statement(finding),
                    "hypothesis": template["hypothesis"].format(campaign=campaign),
                    "proposed_experiment": template["experiment"].format(campaign=campaign),
                    "target_metric": finding["metric"],
                    "baseline_finding_id": finding["finding_id"],
                    "baseline_reference": "current_value",
                    "attribution_basis_id": finding["conversion_basis"]["basis_id"],
                    "currency": finding["currency"],
                    "measurement_method": template["measurement"],
                    "duration_days": template["duration_days"],
                    "risks": list(template["risks"]),
                    "assumptions": list(template["assumptions"]),
                    "requires_human_approval": True,
                }
            )
    return {
        "observed_facts": facts,
        "hypotheses": hypotheses,
        "experiments": experiments,
        "limitations": [],
    }
