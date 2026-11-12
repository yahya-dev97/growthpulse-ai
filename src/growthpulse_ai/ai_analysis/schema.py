"""The structure an AI provider must return, and the JSON schema sent to enforce it.

Two layers enforce the shape:

- The **wire schema** goes to the API as a structured-output format. It uses only basic
  JSON Schema keywords, with enums built from the evidence package, so the model can only
  name finding IDs, metrics, conversion bases and currencies that exist.
- The **Pydantic models** re-check everything locally, strictly, with length and count
  limits. Nothing from a provider is trusted because the API accepted the schema.

Facts, hypotheses and experiment proposals are separate types, so the dashboard can never
present a hypothesis as an observed fact or a proposal as something done.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .evidence import EvidencePackage

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FindingIds = Annotated[list[Identifier], Field(min_length=1, max_length=6)]

BASELINE_REFERENCES = ("current_value", "baseline_value")
MIN_DURATION_DAYS, MAX_DURATION_DAYS = 7, 42


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ObservedFact(_Strict):
    """Something the evidence shows directly. No causes."""

    statement: Text
    finding_ids: FindingIds


class Hypothesis(_Strict):
    """A possible explanation. Untested until an experiment runs."""

    statement: Text
    finding_ids: FindingIds


class ExperimentProposal(_Strict):
    """An advisory experiment a person may choose to run. Never applied automatically."""

    title: Title
    finding_ids: FindingIds
    kind: Literal["problem", "opportunity"]
    problem_statement: Text
    hypothesis: Text
    proposed_experiment: Text
    target_metric: Identifier
    baseline_finding_id: Identifier
    baseline_reference: Literal["current_value", "baseline_value"]
    attribution_basis_id: Identifier
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    measurement_method: Text
    duration_days: Annotated[int, Field(ge=MIN_DURATION_DAYS, le=MAX_DURATION_DAYS)]
    risks: Annotated[list[Text], Field(min_length=1, max_length=5)]
    assumptions: Annotated[list[Text], Field(min_length=1, max_length=5)]
    requires_human_approval: Literal[True]


class InsightResponse(_Strict):
    observed_facts: Annotated[list[ObservedFact], Field(min_length=1, max_length=10)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=8)]
    experiments: Annotated[list[ExperimentProposal], Field(max_length=6)]
    limitations: Annotated[list[Text], Field(max_length=6)]


def _object(properties: dict, description: str | None = None) -> dict:
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


def _enum(values: set[str] | tuple[str, ...], description: str) -> dict:
    return {"type": "string", "enum": sorted(values), "description": description}


def response_json_schema(package: EvidencePackage) -> dict:
    """The structured-output schema for this package. Basic keywords only."""
    findings = package.findings.values()
    finding_ids = {
        "type": "array",
        "items": _enum({f["finding_id"] for f in findings}, "A finding ID from the evidence"),
        "description": "1 to 6 finding IDs this item is based on",
    }
    statement = _string(
        "At most 600 characters. Numbers only if copied from the cited findings' evidence"
    )
    text_list = {"type": "array", "items": _string("At most 600 characters, no numbers")}
    experiment = _object(
        {
            "title": _string("At most 120 characters, no numbers"),
            "finding_ids": finding_ids,
            "kind": _enum(("problem", "opportunity"), "Whether this addresses a problem"),
            "problem_statement": statement,
            "hypothesis": statement,
            "proposed_experiment": _string("What a person would test. No numbers"),
            "target_metric": _enum({f["metric"] for f in findings}, "Metric of the baseline"),
            "baseline_finding_id": _enum(
                {f["finding_id"] for f in findings}, "The cited finding whose value is the baseline"
            ),
            "baseline_reference": _enum(BASELINE_REFERENCES, "Which value of that finding"),
            "attribution_basis_id": _enum(
                {f["conversion_basis"]["basis_id"] for f in findings},
                "The conversion basis shared by every cited finding",
            ),
            "currency": _enum({f["currency"] for f in findings}, "Shared by every cited finding"),
            "measurement_method": _string("How the result would be measured. No numbers"),
            "duration_days": {
                "type": "integer",
                "description": f"Test length, {MIN_DURATION_DAYS} to {MAX_DURATION_DAYS} days",
            },
            "risks": {**text_list, "description": "1 to 5 risks"},
            "assumptions": {**text_list, "description": "1 to 5 assumptions"},
            "requires_human_approval": {"type": "boolean", "description": "Always true"},
        }
    )
    return _object(
        {
            "observed_facts": {
                "type": "array",
                "items": _object({"statement": statement, "finding_ids": finding_ids}),
                "description": "1 to 6 facts the evidence shows directly, with no causes",
            },
            "hypotheses": {
                "type": "array",
                "items": _object({"statement": statement, "finding_ids": finding_ids}),
                "description": "Up to 4 untested explanations",
            },
            "experiments": {
                "type": "array",
                "items": experiment,
                "description": "Up to 3 experiment proposals",
            },
            "limitations": {**text_list, "description": "Up to 3 extra caveats"},
        }
    )
