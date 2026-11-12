"""Check a provider's response against the evidence package before anyone sees it.

A response is accepted whole or rejected whole: one violation anywhere means the
deterministic fallback is shown instead. Every check is listed in CHECKS, and each
violation names its check, where it was found, and why, without echoing the text.

Number rule: every number written in prose must be a value from the evidence of the
findings that item cites, rounded no further than its written precision. A number with
one significant figure must match exactly, so "3" cannot stand in for a ROAS of 3.19.
Rates may be written as percentages. Anything else, including a figure derived from the
evidence such as a difference, is unsupported.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from .evidence import EvidencePackage
from .schema import ExperimentProposal, InsightResponse

CHECKS = (
    "schema",
    "finding_ids_exist",
    "numbers_supported",
    "dates_supported",
    "identifiers_known",
    "campaigns_known_and_cited",
    "single_currency",
    "currency_symbols_match",
    "experiment_baseline_cited",
    "experiment_target_metric_matches",
    "experiment_attribution_matches",
    "experiment_currency_matches",
    "facts_state_no_causes",
    "no_execution_claims",
    "no_duplicates",
)

CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}
CURRENCY_CODES = {"GBP", "EUR", "USD", "CHF", "AUD", "CAD", "JPY", "SEK", "NOK", "DKK", "PLN"}

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
HEX_IDENTIFIER = re.compile(r"\b[0-9a-f]{12,}\b")
SYNTHETIC_IDENTIFIER = re.compile(r"\bsynthetic-[a-z0-9-]+\b")
CAUSAL = re.compile(
    r"\b(because|due to|caused|causes|causing|driven by|as a result of|led to|leads to|"
    r"owing to|resulted in|result of|attributable to|thanks to)\b",
    re.IGNORECASE,
)
EXECUTION_CLAIMS = (
    re.compile(
        r"\b(has|have|had) been (applied|executed|implemented|launched|paused|activated|"
        r"deployed|approved|enabled|scheduled)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(was|were) (applied|executed|implemented|launched|activated|deployed|approved)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(we|i|growthpulse|the system|claude) (have |has )?(applied|executed|implemented|"
        r"launched|paused|changed|activated|deployed|approved|adjusted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bautomatic(ally)? (apply|applied|applies|change|changed|adjust|adjusted)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(already|now) (live|running|applied|in effect)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class Violation:
    check: str
    path: str
    detail: str
    # Structured detail for numbers_supported. Numbers and evidence references only, never
    # the statement's text, so it is safe to print and to store in an artifact.
    diagnostic: dict | None = None

    def as_dict(self) -> dict:
        result = {"check": self.check, "path": self.path, "detail": self.detail}
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic
        return result


@dataclass(frozen=True)
class ValidationResult:
    response: InsightResponse | None
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return self.response is not None and not self.violations


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return abs(number) if number.is_finite() else None


def _is_rate(metric: str) -> bool:
    return "rate" in metric or metric == "ctr"


@dataclass(frozen=True)
class EvidenceValue:
    """One quotable number: its absolute value and where in the evidence it comes from."""

    value: Decimal
    finding_id: str
    field: str

    def as_dict(self) -> dict[str, str]:
        return {"value": str(self.value), "finding_id": self.finding_id, "field": self.field}


def finding_values(finding: dict) -> list[EvidenceValue]:
    """Every value a statement citing this finding may quote, labelled by source field."""
    basis = finding["conversion_basis"]
    raw = [
        ("baseline_value", finding.get("baseline_value")),
        ("current_value", finding.get("current_value")),
        ("percent_change", finding.get("percent_change")),
        ("absolute_change", finding.get("absolute_change")),
        ("baseline_days", finding.get("baseline_days")),
        ("current_days", finding.get("current_days")),
        ("conversion_basis.click_window_days", basis.get("click_window_days")),
        ("conversion_basis.view_window_days", basis.get("view_window_days")),
        *((f"evidence.{key}", value) for key, value in finding.get("evidence", {}).items()),
    ]
    if _is_rate(finding["metric"]):
        for key in ("baseline_value", "current_value", "absolute_change"):
            number = _decimal(finding.get(key))
            if number is not None:
                raw.append((f"{key} as a percentage", number * 100))
    values = []
    for field, value in raw:
        number = _decimal(value)
        if number is not None:
            values.append(EvidenceValue(number, finding["finding_id"], field))
    return values


def finding_numbers(finding: dict) -> set[Decimal]:
    """Every value a statement citing this finding may quote."""
    return {item.value for item in finding_values(finding)}


def finding_dates(finding: dict) -> set[str]:
    dates = {
        finding["baseline_period"]["start"],
        finding["baseline_period"]["end"],
        finding["current_period"]["start"],
        finding["current_period"]["end"],
    }
    dates |= {
        value
        for value in finding.get("evidence", {}).values()
        if isinstance(value, str) and ISO_DATE.fullmatch(value)
    }
    return dates


def _significant_digits(digits: str) -> int:
    return len(digits.replace(".", "").lstrip("0"))


def _written(written: str, decimals: str | None) -> tuple[Decimal, Decimal]:
    """The written number and half a unit of its last written digit."""
    text = written.replace(",", "") + (f".{decimals}" if decimals else "")
    return Decimal(text), Decimal(5) * Decimal(10) ** -(len(decimals or "") + 1)


def number_is_supported(written: str, decimals: str | None, allowed: set[Decimal]) -> bool:
    value, half_unit = _written(written, decimals)
    if _significant_digits(str(value)) < 2:
        return value in allowed
    return any(abs(candidate - value) <= half_unit for candidate in allowed)


# --- diagnostics for an unsupported number ----------------------------------------------
#
# Diagnosis never changes acceptance: it runs only after a number has been rejected, and
# explains the most likely reason, in this order:
#   missing_evidence           the item cites no known finding, so nothing can support it
#   incorrect_finding_linkage  the value is in the evidence of a finding the item didn't cite
#   numerical_transformation   the value can be derived from the cited evidence (a date
#                              fragment, coarser rounding, a scale change, or a difference,
#                              sum, ratio or percent change of two values), but isn't stated
#   unsupported_invention      none of the above

MAX_NEAREST = 5


def _nearest(value: Decimal, candidates: list[EvidenceValue]) -> list[dict]:
    def distance(item: EvidenceValue) -> Decimal:
        return abs(item.value - value) / max(abs(value), Decimal(1))

    return [item.as_dict() for item in sorted(candidates, key=distance)[:MAX_NEAREST]]


def _transformations(cited: list[EvidenceValue]) -> list[tuple[str, Decimal, list[EvidenceValue]]]:
    derived = []
    for item in cited:
        derived.append(("multiplied by 100", item.value * 100, [item]))
        derived.append(("divided by 100", item.value / 100, [item]))
    for i, first in enumerate(cited):
        for second in cited[i + 1 :]:
            if first.value == second.value:
                continue
            derived.append(("difference", abs(first.value - second.value), [first, second]))
            derived.append(("sum", first.value + second.value, [first, second]))
            for a, b in ((first, second), (second, first)):
                if b.value:
                    derived.append(("ratio", a.value / b.value, [a, b]))
                    derived.append(("ratio as a percentage", a.value / b.value * 100, [a, b]))
                    derived.append(
                        ("percent change", abs(a.value - b.value) / b.value * 100, [b, a])
                    )
    return derived


def diagnose_number(
    written: str,
    decimals: str | None,
    cited: list[dict],
    all_findings: dict[str, dict],
) -> dict:
    value, half_unit = _written(written, decimals)
    shown = written + (f".{decimals}" if decimals else "")
    cited_ids = [f["finding_id"] for f in cited]
    cited_values = [item for f in cited for item in finding_values(f)]
    diagnostic: dict = {
        "value": shown,
        "cited_finding_ids": cited_ids,
        "nearest_cited_values": _nearest(value, cited_values),
    }
    if not cited:
        return diagnostic | {
            "classification": "missing_evidence",
            "explanation": "the item cites no known finding, so no number in it can be supported",
        }

    def close(candidate: Decimal) -> bool:
        return abs(candidate - value) <= half_unit

    # Day-of-month and year only: a bare month number is rarely how dates are written.
    dates = sorted({d for f in cited for d in finding_dates(f)})
    for iso in dates:
        year, _, day = iso.split("-")
        if not decimals and value in (Decimal(year), Decimal(day)):
            return diagnostic | {
                "classification": "numerical_transformation",
                "transformation": "date fragment",
                "explanation": f"matches part of the cited date {iso}; dates must be written in "
                "full ISO form",
            }

    uncited_values = [
        item
        for finding_id, finding in all_findings.items()
        if finding_id not in cited_ids
        for item in finding_values(finding)
    ]
    elsewhere = [
        item.as_dict()
        for item in uncited_values
        if number_is_supported(written, decimals, {item.value})
    ]
    if elsewhere:
        return diagnostic | {
            "classification": "incorrect_finding_linkage",
            "matching_uncited_values": elsewhere[:MAX_NEAREST],
            "explanation": "the value is in the evidence of a finding this item does not cite",
        }

    coarse = [item for item in cited_values if abs(item.value - value) <= half_unit]
    if coarse:
        return diagnostic | {
            "classification": "numerical_transformation",
            "transformation": "rounded to one significant figure",
            "operands": [item.as_dict() for item in coarse[:2]],
            "explanation": "a cited value rounded too far; write at least two significant figures",
        }
    # With ~20 values per finding, pairwise arithmetic lands near most small integers by
    # chance, so a derived match needs three significant figures or an exact result.
    precise = _significant_digits(str(value)) >= 3
    for name, candidate, operands in _transformations(cited_values):
        if candidate == value or (precise and close(candidate)):
            return diagnostic | {
                "classification": "numerical_transformation",
                "transformation": name,
                "operands": [item.as_dict() for item in operands],
                "explanation": "derived from cited values, but derived figures are not evidence",
            }
    return diagnostic | {
        "classification": "unsupported_invention",
        "explanation": "no cited, uncited or derived evidence value matches",
    }


class _Checker:
    def __init__(self, package: EvidencePackage):
        self.package = package
        self.findings = package.findings
        self.violations: list[Violation] = []
        self.identifiers = sorted(package.identifiers, key=len, reverse=True)
        names = set()
        for name in package.campaign_names:
            names |= {name, name.removeprefix("[SYNTHETIC] ")}
        self.campaign_names = sorted(names, key=len, reverse=True)

    def flag(self, check: str, path: str, detail: str) -> None:
        self.violations.append(Violation(check, path, detail))

    def cited(self, path: str, finding_ids: list[str]) -> list[dict]:
        if len(set(finding_ids)) != len(finding_ids):
            self.flag("no_duplicates", path, "a finding ID is cited twice")
        known = []
        for finding_id in finding_ids:
            if finding_id in self.findings:
                known.append(self.findings[finding_id])
            else:
                self.flag("finding_ids_exist", path, "cites a finding ID not in the evidence")
        currencies = {finding["currency"] for finding in known}
        if len(currencies) > 1:
            self.flag("single_currency", path, "cites findings in more than one currency")
        return known

    def text(self, path: str, text: str, findings: list[dict]) -> None:
        """Check one piece of prose against the findings its item cites."""
        allowed_campaigns = {f["campaign_name"] for f in findings}
        allowed_campaigns |= {name.removeprefix("[SYNTHETIC] ") for name in allowed_campaigns}
        remaining = text
        for name in self.campaign_names:
            if name in remaining:
                if name not in allowed_campaigns:
                    self.flag("campaigns_known_and_cited", path, "names an uncited campaign")
                remaining = remaining.replace(name, " ")
        if "|" in remaining or "[SYNTHETIC]" in remaining:
            self.flag("campaigns_known_and_cited", path, "names a campaign not in the evidence")
        for identifier in self.identifiers:
            remaining = remaining.replace(identifier, " ")
        if HEX_IDENTIFIER.search(remaining) or SYNTHETIC_IDENTIFIER.search(remaining):
            self.flag("identifiers_known", path, "quotes an identifier not in the evidence")

        allowed_dates = set().union(*(finding_dates(f) for f in findings)) if findings else set()
        for match in ISO_DATE.finditer(remaining):
            if match.group() not in allowed_dates:
                self.flag("dates_supported", path, "states a date not in the cited evidence")
        remaining = ISO_DATE.sub(" ", remaining)

        allowed = set().union(*(finding_numbers(f) for f in findings)) if findings else set()
        for match in NUMBER.finditer(remaining):
            if not number_is_supported(match.group(1), match.group(2), allowed):
                self.violations.append(
                    Violation(
                        "numbers_supported",
                        path,
                        "states a number not in the cited evidence",
                        diagnose_number(match.group(1), match.group(2), findings, self.findings),
                    )
                )

        currencies = {f["currency"] for f in findings}
        written = {CURRENCY_SYMBOLS[s] for s in CURRENCY_SYMBOLS if s in text}
        written |= {code for code in CURRENCY_CODES if re.search(rf"\b{code}\b", text)}
        if written - currencies:
            self.flag("currency_symbols_match", path, "uses a currency the cited findings do not")

        for pattern in EXECUTION_CLAIMS:
            if pattern.search(text):
                self.flag("no_execution_claims", path, "describes a change as made or applied")
                break

    def experiment(self, path: str, experiment: ExperimentProposal, findings: list[dict]) -> None:
        if experiment.baseline_finding_id not in experiment.finding_ids:
            self.flag("experiment_baseline_cited", path, "baseline finding is not cited")
        baseline = self.findings.get(experiment.baseline_finding_id)
        if baseline is None:
            self.flag("finding_ids_exist", path, "baseline finding is not in the evidence")
        elif baseline["metric"] != experiment.target_metric:
            self.flag(
                "experiment_target_metric_matches",
                path,
                "target metric differs from the baseline finding's metric",
            )
        if any(
            f["conversion_basis"]["basis_id"] != experiment.attribution_basis_id for f in findings
        ):
            self.flag(
                "experiment_attribution_matches",
                path,
                "a cited finding uses a different conversion basis",
            )
        if any(f["currency"] != experiment.currency for f in findings):
            self.flag(
                "experiment_currency_matches", path, "a cited finding uses a different currency"
            )
        fields = {
            "title": experiment.title,
            "problem_statement": experiment.problem_statement,
            "hypothesis": experiment.hypothesis,
            "proposed_experiment": experiment.proposed_experiment,
            "measurement_method": experiment.measurement_method,
        }
        for name, value in fields.items():
            self.text(f"{path}.{name}", value, findings)
        for name in ("risks", "assumptions"):
            for index, value in enumerate(getattr(experiment, name)):
                self.text(f"{path}.{name}[{index}]", value, findings)


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _duplicates(checker: _Checker, response: InsightResponse) -> None:
    for section in ("observed_facts", "hypotheses"):
        seen: set[str] = set()
        for index, item in enumerate(getattr(response, section)):
            key = _normalise(item.statement)
            if key in seen:
                checker.flag("no_duplicates", f"{section}[{index}]", "repeats an earlier item")
            seen.add(key)
    titles: set[str] = set()
    designs: set[tuple] = set()
    for index, item in enumerate(response.experiments):
        design = (
            item.target_metric,
            frozenset(item.finding_ids),
            item.baseline_finding_id,
            item.baseline_reference,
        )
        if _normalise(item.title) in titles or design in designs:
            checker.flag("no_duplicates", f"experiments[{index}]", "repeats an earlier proposal")
        titles.add(_normalise(item.title))
        designs.add(design)


def validate_response(raw: object, package: EvidencePackage) -> ValidationResult:
    """Validate a parsed JSON response. Never raises for bad input; returns violations."""
    try:
        response = InsightResponse.model_validate(raw)
    except ValidationError as error:
        violations = tuple(
            Violation("schema", ".".join(map(str, item["loc"])) or "$", item["type"])
            for item in error.errors(include_input=False, include_url=False)
        )
        return ValidationResult(None, violations)

    checker = _Checker(package)
    for section in ("observed_facts", "hypotheses"):
        for index, item in enumerate(getattr(response, section)):
            path = f"{section}[{index}]"
            findings = checker.cited(path, item.finding_ids)
            checker.text(f"{path}.statement", item.statement, findings)
            if section == "observed_facts" and CAUSAL.search(item.statement):
                checker.flag("facts_state_no_causes", path, "a fact states a cause")
    for index, experiment in enumerate(response.experiments):
        path = f"experiments[{index}]"
        findings = checker.cited(path, experiment.finding_ids)
        checker.experiment(path, experiment, findings)
    for index, text in enumerate(response.limitations):
        checker.text(f"limitations[{index}]", text, [])
    _duplicates(checker, response)

    violations = tuple(checker.violations)
    return ValidationResult(None if violations else response, violations)
