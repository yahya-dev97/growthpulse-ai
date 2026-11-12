"""Turn an analytics report into the evidence package an AI provider may see.

The package holds only what the report states: finding IDs, metric values, periods,
conversion bases and limitations, copied verbatim. Nothing is recomputed, and the report
is never modified. The package ID is a SHA-256 of its canonical JSON, so an artifact can
prove which evidence it was generated and validated against.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACKAGE_SCHEMA_VERSION = "1.0"
MAX_FINDINGS = 20
SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}

# Always shown with AI insights, whatever the model writes.
ATTRIBUTION_DISCLOSURE = (
    "Every conversion figure is platform-attributed: each ad platform credits conversions "
    "under its own attribution model and windows. They are not verified bookings, not "
    "incremental, and not comparable across platforms or conversion bases.",
    "Conversion value and ROAS are platform-reported, not realised revenue.",
    "Findings come from threshold rules, not significance tests. They show what changed, not why.",
)

FINDING_FIELDS = (
    "finding_id", "rule_id", "severity", "direction", "comparison_label", "metric",
    "metric_label", "campaign_id", "campaign_name", "platform", "account_id", "currency",
    "baseline_kind", "baseline_period", "current_period", "baseline_value", "current_value",
    "percent_change", "absolute_change", "observation", "evidence",
)  # fmt: skip
BASIS_FIELDS = (
    "basis_id", "platform", "stage", "event_name", "attribution_model",
    "click_window_days", "view_window_days", "report_time_basis",
)  # fmt: skip


class EvidenceError(ValueError):
    """The report cannot be turned into an evidence package."""


def canonical_json(value: Any) -> str:
    """Deterministic JSON. Non-JSON values are an error, never silently stringified."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidencePackage:
    """The evidence an insight may cite, plus lookups used by the validator."""

    content: dict
    package_id: str

    @property
    def report_id(self) -> str:
        return self.content["report_id"]

    @property
    def findings(self) -> dict[str, dict]:
        return {finding["finding_id"]: finding for finding in self.content["findings"]}

    @property
    def campaign_names(self) -> set[str]:
        return {finding["campaign_name"] for finding in self.content["findings"]}

    @property
    def identifiers(self) -> set[str]:
        """Every identifier the evidence contains, which text may quote."""
        ids = {self.report_id, self.package_id}
        for finding in self.content["findings"]:
            ids |= {
                finding["finding_id"],
                finding["campaign_id"],
                finding["account_id"],
                finding["conversion_basis"]["basis_id"],
            }
        return ids

    def to_prompt_json(self) -> str:
        return canonical_json({**self.content, "package_id": self.package_id})


def _finding(finding: dict, common_limitations: list[str]) -> dict:
    try:
        entry = {field: finding[field] for field in FINDING_FIELDS}
        basis = finding["conversion_basis"]
        entry["conversion_basis"] = {field: basis.get(field) for field in BASIS_FIELDS}
        entry["noise_guard_outcome"] = finding["noise_guard"]["outcome"]
    except (KeyError, TypeError) as error:
        raise EvidenceError(f"finding is missing {error}") from None
    entry["additional_limitations"] = [
        text for text in finding.get("data_limitations", []) if text not in common_limitations
    ]
    return entry


def build_evidence_package(report: dict, max_findings: int = MAX_FINDINGS) -> EvidencePackage:
    """Build the package from a report dictionary, without modifying it."""
    try:
        comparisons = report["comparisons"]
        active = [finding for comparison in comparisons for finding in comparison["findings"]]
        suppressed = sum(len(comparison["suppressed_findings"]) for comparison in comparisons)
        report_id = report["report_id"]
    except (KeyError, TypeError) as error:
        raise EvidenceError(f"report is missing {error}") from None
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise EvidenceError("report has no valid report ID")

    ranked = sorted(active, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["finding_id"]))
    kept = ranked[:max_findings]
    limitation_sets = [set(f.get("data_limitations", [])) for f in kept]
    common = sorted(set.intersection(*limitation_sets)) if limitation_sets else []

    content = {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "report_id": report_id,
        "report_schema_version": report.get("report_schema_version"),
        "data_source": report.get("data_source"),
        "is_synthetic": report.get("is_synthetic"),
        "data_as_of": report.get("data_as_of"),
        "true_cac_available": report.get("true_cac_available", False),
        "attribution_disclosure": list(ATTRIBUTION_DISCLOSURE),
        "report_limitations": list(report.get("limitations", [])),
        "common_finding_limitations": common,
        "findings": [_finding(finding, common) for finding in kept],
        "omitted": {
            "suppressed_by_noise_guard": suppressed,
            "beyond_finding_limit": len(ranked) - len(kept),
        },
    }
    try:
        canonical = canonical_json(content)
    except TypeError as error:
        raise EvidenceError(f"report contains a non-JSON value: {error}") from None
    # Round-trip so the package holds exactly what the prompt and the validator will see.
    return EvidencePackage(content=json.loads(canonical), package_id=sha256(canonical))
