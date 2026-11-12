"""Turn an analytics report into the evidence package an AI provider may see.

The package holds only what the report states: finding IDs, metric values, periods,
conversion bases and limitations, copied verbatim. The one derived value is each
comparison window's length in days, counted from the report's own ISO dates (inclusive),
so that "the latest 10 days" is a checkable statement rather than an unsupported number.
The report is never modified. The package ID is a SHA-256 of its canonical JSON, so an
artifact can prove which evidence it was generated and validated against.

Shared values are stated once (schema 1.1): conversion bases, comparison windows and
finding-specific limitations sit in lookup tables that findings reference by key. The
``findings`` property re-joins them, so validation always sees each finding's full
evidence.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from typing import Any

PACKAGE_SCHEMA_VERSION = "1.1"
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
    "baseline_kind", "baseline_value", "current_value", "percent_change", "absolute_change",
    "observation", "evidence",
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


def period_days(period: dict) -> int:
    """Inclusive length of an ISO date period, from the report's own dates."""
    return (date.fromisoformat(period["end"]) - date.fromisoformat(period["start"])).days + 1


@dataclass(frozen=True)
class EvidencePackage:
    """The evidence an insight may cite, plus lookups used by the validator."""

    content: dict
    package_id: str

    @property
    def report_id(self) -> str:
        return self.content["report_id"]

    @cached_property
    def findings(self) -> dict[str, dict]:
        """Each finding with its basis, periods and limitations joined back in."""
        bases = self.content["conversion_bases"]
        comparisons = self.content["comparisons"]
        texts = self.content["limitation_texts"]
        joined = {}
        for finding in self.content["findings"]:
            comparison = comparisons[finding["comparison_label"]]
            entry = {key: value for key, value in finding.items() if key != "limitation_refs"}
            entry["conversion_basis"] = bases[finding["basis_id"]]
            for key in ("baseline_period", "current_period", "baseline_days", "current_days"):
                entry[key] = comparison[key]
            entry["additional_limitations"] = [texts[ref] for ref in finding["limitation_refs"]]
            joined[finding["finding_id"]] = entry
        return joined

    @property
    def ordered_findings(self) -> list[dict]:
        """Findings in package order (severity, then ID), fully joined."""
        return list(self.findings.values())

    @property
    def campaign_names(self) -> set[str]:
        return {finding["campaign_name"] for finding in self.content["findings"]}

    @property
    def identifiers(self) -> set[str]:
        """Every identifier the evidence contains, which text may quote."""
        ids = {self.report_id, self.package_id, *self.content["conversion_bases"]}
        for finding in self.content["findings"]:
            ids |= {finding["finding_id"], finding["campaign_id"], finding["account_id"]}
        return ids

    def to_prompt_json(self) -> str:
        return canonical_json({**self.content, "package_id": self.package_id})


def build_evidence_package(report: dict, max_findings: int = MAX_FINDINGS) -> EvidencePackage:
    """Build the package from a report dictionary, without modifying it."""
    try:
        report_comparisons = report["comparisons"]
        active = [(finding, c) for c in report_comparisons for finding in c["findings"]]
        suppressed = sum(len(c["suppressed_findings"]) for c in report_comparisons)
        report_id = report["report_id"]
    except (KeyError, TypeError) as error:
        raise EvidenceError(f"report is missing {error}") from None
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise EvidenceError("report has no valid report ID")

    ranked = sorted(
        active, key=lambda pair: (SEVERITY_ORDER.get(pair[0]["severity"], 9), pair[0]["finding_id"])
    )
    kept = ranked[:max_findings]
    limitation_sets = [set(f.get("data_limitations", [])) for f, _ in kept]
    common = sorted(set.intersection(*limitation_sets)) if limitation_sets else []
    extra_texts = sorted({text for s in limitation_sets for text in s} - set(common))
    refs = {text: f"L{index + 1}" for index, text in enumerate(extra_texts)}

    comparisons: dict[str, dict] = {}
    bases: dict[str, dict] = {}
    findings = []
    try:
        for finding, comparison in kept:
            label = finding["comparison_label"]
            if label != comparison["label"]:
                raise EvidenceError("a finding's comparison label differs from its comparison")
            for key in ("baseline_period", "current_period"):
                if finding[key] != comparison[key]:
                    raise EvidenceError("a finding's periods differ from its comparison")
            comparisons[label] = {
                "purpose": comparison.get("purpose"),
                "baseline_period": comparison["baseline_period"],
                "current_period": comparison["current_period"],
                "baseline_days": period_days(comparison["baseline_period"]),
                "current_days": period_days(comparison["current_period"]),
            }
            basis = finding["conversion_basis"]
            bases[basis["basis_id"]] = {field: basis.get(field) for field in BASIS_FIELDS}
            entry = {field: finding[field] for field in FINDING_FIELDS}
            entry["basis_id"] = basis["basis_id"]
            entry["noise_guard_outcome"] = finding["noise_guard"]["outcome"]
            entry["limitation_refs"] = sorted(
                refs[text] for text in set(finding.get("data_limitations", [])) - set(common)
            )
            findings.append(entry)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError(f"finding is missing or malformed: {error}") from None

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
        "limitation_texts": {ref: text for text, ref in refs.items()},
        "comparisons": comparisons,
        "conversion_bases": bases,
        "findings": findings,
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
