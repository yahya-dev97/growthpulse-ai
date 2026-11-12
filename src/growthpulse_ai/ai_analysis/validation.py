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

    def as_dict(self) -> dict[str, str]:
        return {"check": self.check, "path": self.path, "detail": self.detail}


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


def finding_numbers(finding: dict) -> set[Decimal]:
    """Every value a statement citing this finding may quote."""
    raw = [
        finding.get("baseline_value"),
        finding.get("current_value"),
        finding.get("percent_change"),
        finding.get("absolute_change"),
        finding["conversion_basis"].get("click_window_days"),
        finding["conversion_basis"].get("view_window_days"),
        *finding.get("evidence", {}).values(),
    ]
    values = {number for number in map(_decimal, raw) if number is not None}
    if _is_rate(finding["metric"]):
        for key in ("baseline_value", "current_value", "absolute_change"):
            number = _decimal(finding.get(key))
            if number is not None:
                values.add(number * 100)
    return values


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


def number_is_supported(written: str, decimals: str | None, allowed: set[Decimal]) -> bool:
    text = written.replace(",", "") + (f".{decimals}" if decimals else "")
    value = Decimal(text)
    if _significant_digits(text) < 2:
        return value in allowed
    half_unit = Decimal(5) * Decimal(10) ** -(len(decimals or "") + 1)
    return any(abs(candidate - value) <= half_unit for candidate in allowed)


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
                self.flag("numbers_supported", path, "states a number not in the cited evidence")

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
