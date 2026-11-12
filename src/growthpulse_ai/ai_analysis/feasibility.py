"""GrowthPulse's own, deterministic assessment of each experiment, from the evidence.

Nothing here is model output. For every experiment GrowthPulse derives:

- **Reference values.** The *current baseline* is the target metric's value in the
  finding's current window. The *historical recovery target* is its value in the earlier
  window, when the finding compares the campaign with its own history; when it compares
  with a peer median, that value is a *peer benchmark* instead, never a recovery target.
- **Feasibility.** The volume available, from the evidence, and, only where the
  arithmetic is supported, the smallest change a test of that length could detect:
  - conversion rate in a randomized split: a two-proportion normal approximation, with
    every assumption listed;
  - ratio metrics (cost per conversion, ROAS): **not assessable**, because a sample size
    needs per-click or per-day variance that the evidence doesn't contain;
  - investigations, measurement repairs and monitoring: no sample size applies.

Where the inputs are missing, the assessment says so instead of guessing. A campaign with
a possible tracking fault (spend with no conversions) blocks interpretation of any
performance test until a measurement repair has been verified.
"""

import math
from decimal import Decimal

from .evidence import EvidencePackage

SOURCE = "GrowthPulse deterministic assessment from the evidence. Not model output."
Z_TWO_SIDED_5_PERCENT = 1.959964
Z_POWER_80_PERCENT = 0.841621
MIN_EXPECTED_EVENTS = 10  # per arm, on each side of the proportion, for the approximation
TRACKING_RISK_RULES = frozenset({"spend_with_zero_conversions"})
SPLIT_TYPES = frozenset({"randomized_experiment", None})  # None: legacy, unclassified


def tracking_risk_campaigns(package: EvidencePackage) -> set[str]:
    """Campaigns whose evidence shows spend with no recorded conversions."""
    return {
        f["campaign_id"] for f in package.ordered_findings if f["rule_id"] in TRACKING_RISK_RULES
    }


def reference_values(finding: dict) -> dict:
    """The current baseline and the historical target or peer benchmark, from evidence."""
    current = {
        "label": "Current baseline",
        "value": finding["current_value"],
        "period": finding["current_period"],
        "currency": finding["currency"],
        "metric": finding["metric"],
    }
    comparison = None
    if finding.get("baseline_value") is not None:
        if finding["baseline_kind"] == "baseline_period":
            comparison = {
                "kind": "historical_recovery_target",
                "label": "Historical recovery target",
                "value": finding["baseline_value"],
                "period": finding["baseline_period"],
                "note": "The campaign's own earlier level. Returning to it is not guaranteed.",
            }
        else:
            comparison = {
                "kind": "peer_benchmark",
                "label": "Peer benchmark",
                "value": finding["baseline_value"],
                "period": finding["current_period"],
                "note": "A peer median on the same basis, not this campaign's own history.",
            }
        comparison |= {"currency": finding["currency"], "metric": finding["metric"]}
    return {"finding_id": finding["finding_id"], "current": current, "comparison": comparison}


def _number(value) -> float | None:
    try:
        number = float(Decimal(str(value)))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: float, places: int = 1) -> str:
    return f"{value:,.{places}f}"


def assess_feasibility(
    experiment_type: str | None,
    target_metric: str,
    finding: dict,
    duration_days: int,
    tracking_risk: bool,
) -> dict:
    """Assess one experiment. ``experiment_type`` is None for a legacy proposal."""
    evidence = finding.get("evidence", {})
    window_days = finding.get("current_days")
    clicks = _number(evidence.get("current_clicks"))
    conversions = _number(evidence.get("current_conversions"))
    caveats: list[str] = []
    volume = {
        "window": finding["current_period"],
        "window_days": window_days,
        "clicks": evidence.get("current_clicks"),
        "platform_attributed_conversions": evidence.get("current_conversions"),
    }
    result: dict = {
        "source": SOURCE,
        "volume": volume,
        "duration_days": duration_days,
        "interpretation_blocked_until_measurement_repair": bool(
            tracking_risk and experiment_type in ("randomized_experiment", "monitoring", None)
        ),
        "projection": None,
        "detectable_change": None,
        "assumptions": [],
        "caveats": caveats,
    }
    if result["interpretation_blocked_until_measurement_repair"]:
        caveats.append(
            "This campaign recorded spend with no conversions, a possible tracking fault. "
            "Repair and verify tracking before reading any performance result."
        )

    if experiment_type not in SPLIT_TYPES:
        kind = experiment_type.replace("_", " ")
        result["method"] = "not_applicable"
        result["assessment"] = "not_applicable"
        result["summary"] = f"A {kind} has no test arms, so no sample size applies."
        return result

    if experiment_type is None:
        result["assumptions"].append(
            "Legacy proposal, not classified by type. Assessed as a two-arm split, as its "
            "text describes."
        )
    if clicks is None or conversions is None or not window_days:
        result["method"] = "not_assessable"
        result["assessment"] = "not_assessable"
        result["summary"] = "The evidence has no click and conversion volume for this window."
        caveats.append("Volume for the current window is missing from the evidence.")
        return result

    per_arm_clicks = clicks / window_days * duration_days / 2
    per_arm_conversions = conversions / window_days * duration_days / 2
    result["projection"] = {
        "clicks_per_arm": _fmt(per_arm_clicks, 0),
        "platform_attributed_conversions_per_arm": _fmt(per_arm_conversions, 1),
    }
    result["assumptions"] += [
        "Traffic is split evenly between two arms at random.",
        f"Daily volume matches the average of the current {window_days}-day window.",
    ]

    if target_metric != "platform_attributed_conversion_rate":
        result["method"] = "not_assessable"
        result["assessment"] = "not_assessable"
        result["summary"] = (
            f"About {_fmt(per_arm_conversions)} platform-attributed conversions per arm over "
            f"{duration_days} days. No detectable change is estimated: this metric is a ratio, "
            "and its variance needs per-click or per-day data the evidence doesn't contain."
        )
        caveats.append("No power or sample-size estimate: the required variance is unavailable.")
        return result

    rate = _number(finding.get("current_value"))
    if rate is None or not 0 < rate < 1:
        result["method"] = "not_assessable"
        result["assessment"] = "not_assessable"
        result["summary"] = "The current conversion rate is zero or missing, so no estimate."
        caveats.append("A proportion test needs a current rate strictly between 0 and 1.")
        return result
    if min(per_arm_clicks * rate, per_arm_clicks * (1 - rate)) < MIN_EXPECTED_EVENTS:
        result["method"] = "not_assessable"
        result["assessment"] = "not_assessable"
        result["summary"] = (
            f"Too few expected conversions per arm (about {_fmt(per_arm_conversions)}) for the "
            "normal approximation. A longer test or more traffic is needed for any estimate."
        )
        caveats.append(f"Fewer than {MIN_EXPECTED_EVENTS} expected events per arm.")
        return result

    z = Z_TWO_SIDED_5_PERCENT + Z_POWER_80_PERCENT
    minimum_absolute = z * math.sqrt(2 * rate * (1 - rate) / per_arm_clicks)
    minimum_relative = minimum_absolute / rate
    result["method"] = "two_proportion_normal_approximation"
    result["assumptions"] += [
        "Clicks convert independently of one another.",
        "Two-sided test at the 5% significance level with 80% power.",
        "The current rate holds in the control arm for the whole test.",
    ]
    result["detectable_change"] = {
        "current_rate_percent": _fmt(rate * 100, 2),
        "minimum_detectable_relative_change_percent": _fmt(minimum_relative * 100),
        "minimum_detectable_rate_percent": _fmt((rate + minimum_absolute) * 100, 2),
    }
    target = _number(finding.get("baseline_value"))
    if finding.get("baseline_kind") == "baseline_period" and target is not None and target > rate:
        gap = (target - rate) / rate
        result["detectable_change"]["recovery_gap_relative_percent"] = _fmt(gap * 100)
        adequate = minimum_relative <= gap
        result["assessment"] = (
            "adequate_for_full_recovery_only" if adequate else "underpowered_for_full_recovery"
        )
        result["summary"] = (
            f"Over {duration_days} days, a test could reliably detect a rise of about "
            f"{_fmt(minimum_relative * 100, 0)}% or more (to about "
            f"{_fmt((rate + minimum_absolute) * 100, 2)}%). "
            + (
                f"A full return to the earlier {_fmt(target * 100, 2)}% would be detectable; "
                "smaller improvements would likely be missed."
                if adequate
                else f"Even a full return to the earlier {_fmt(target * 100, 2)}% might not be "
                "detected. A longer test or more traffic is needed."
            )
        )
    else:
        result["assessment"] = "detectable_change_only"
        result["summary"] = (
            f"Over {duration_days} days, a test could reliably detect a change of about "
            f"{_fmt(minimum_relative * 100, 0)}% or more. Smaller effects would likely be missed."
        )
    return result
