"""The structure an AI provider must return, and the JSON schema sent to enforce it.

Two layers enforce the shape:

- The **wire schema** goes to the API as a structured-output format. It uses only basic
  JSON Schema keywords, with enums built from the evidence package, so the model can only
  name finding IDs, metrics, conversion bases and currencies that exist.
- The **Pydantic models** re-check everything locally, strictly, with length and count
  limits. Nothing from a provider is trusted because the API accepted the schema.

Facts, hypotheses and experiment proposals are separate types, so the dashboard can never
present a hypothesis as an observed fact or a proposal as something done.

Two response versions exist. **Version 2** is what GrowthPulse asks for now: each
experiment states its type (diagnostic investigation, measurement repair, monitoring or
randomized experiment), success criteria, guardrail metrics and whether a measurement
repair must be verified first. It names no baseline number at all: GrowthPulse derives
the current baseline and the historical recovery target from the evidence, and computes
feasibility itself. **Version 1** is kept unchanged so artifacts generated under it are
re-validated exactly as they were validated; their experiments are shown as legacy
advisory proposals.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .evidence import EvidencePackage

RESPONSE_SCHEMA_VERSION = "2"
SUPPORTED_RESPONSE_SCHEMA_VERSIONS = ("1", "2")

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FindingIds = Annotated[list[Identifier], Field(min_length=1, max_length=6)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

BASELINE_REFERENCES = ("current_value", "baseline_value")
MIN_DURATION_DAYS, MAX_DURATION_DAYS = 7, 42

EXPERIMENT_TYPES = (
    "diagnostic_investigation",
    "measurement_repair",
    "monitoring",
    "randomized_experiment",
)
# Metrics a guardrail may name. Conversion metrics stay within the experiment's basis.
GUARDRAIL_METRICS = (
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "platform_attributed_conversions",
    "platform_attributed_conversion_rate",
    "cost_per_platform_attributed_conversion",
    "platform_attributed_conversion_value",
    "platform_attributed_roas",
)


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


class _ExperimentCore(_Strict):
    """Fields both versions share. Never applied automatically."""

    title: Title
    finding_ids: FindingIds
    kind: Literal["problem", "opportunity"]
    problem_statement: Text
    hypothesis: Text
    proposed_experiment: Text
    target_metric: Identifier
    baseline_finding_id: Identifier
    attribution_basis_id: Identifier
    currency: Currency
    measurement_method: Text
    duration_days: Annotated[int, Field(ge=MIN_DURATION_DAYS, le=MAX_DURATION_DAYS)]
    risks: Annotated[list[Text], Field(min_length=1, max_length=5)]
    assumptions: Annotated[list[Text], Field(min_length=1, max_length=5)]
    requires_human_approval: Literal[True]


class ExperimentProposalV1(_ExperimentCore):
    """Version 1: the model chose which value of the baseline finding to show."""

    baseline_reference: Literal["current_value", "baseline_value"]


class ExperimentProposal(_ExperimentCore):
    """Version 2: typed, with success criteria, guardrails and a repair dependency."""

    experiment_type: Literal[
        "diagnostic_investigation", "measurement_repair", "monitoring", "randomized_experiment"
    ]
    success_criteria: Text
    guardrail_metrics: Annotated[
        list[
            Literal[
                "spend",
                "impressions",
                "clicks",
                "ctr",
                "cpc",
                "platform_attributed_conversions",
                "platform_attributed_conversion_rate",
                "cost_per_platform_attributed_conversion",
                "platform_attributed_conversion_value",
                "platform_attributed_roas",
            ]  # fmt: skip
        ],
        Field(min_length=1, max_length=4),
    ]
    requires_measurement_repair_first: bool


class InsightResponseV1(_Strict):
    observed_facts: Annotated[list[ObservedFact], Field(min_length=1, max_length=10)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=8)]
    experiments: Annotated[list[ExperimentProposalV1], Field(max_length=6)]
    limitations: Annotated[list[Text], Field(max_length=6)]


class InsightResponse(_Strict):
    observed_facts: Annotated[list[ObservedFact], Field(min_length=1, max_length=10)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=8)]
    experiments: Annotated[list[ExperimentProposal], Field(max_length=6)]
    limitations: Annotated[list[Text], Field(max_length=6)]


RESPONSE_MODELS: dict[str, type[_Strict]] = {"1": InsightResponseV1, "2": InsightResponse}


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
    """The version 2 structured-output schema for this package. Basic keywords only."""
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
            "experiment_type": _enum(
                EXPERIMENT_TYPES,
                "diagnostic_investigation: find the cause, no test arms. measurement_repair: "
                "fix and verify tracking. monitoring: watch a metric, no intervention. "
                "randomized_experiment: a controlled split test",
            ),
            "problem_statement": statement,
            "hypothesis": statement,
            "proposed_experiment": _string("What a person would do. No numbers"),
            "target_metric": _enum({f["metric"] for f in findings}, "Metric of the baseline"),
            "baseline_finding_id": _enum(
                {f["finding_id"] for f in findings},
                "The cited finding whose metric is the target. GrowthPulse derives the "
                "current baseline and historical target from it",
            ),
            "attribution_basis_id": _enum(
                {f["conversion_basis"]["basis_id"] for f in findings},
                "The conversion basis shared by every cited finding",
            ),
            "currency": _enum({f["currency"] for f in findings}, "Shared by every cited finding"),
            "measurement_method": _string("How the result would be measured. No numbers"),
            "success_criteria": _string(
                "What result would count as success, in words. Numbers only from the cited "
                "evidence. No sample sizes, power or detectable effects"
            ),
            "guardrail_metrics": {
                "type": "array",
                "items": _enum(GUARDRAIL_METRICS, "A metric that must not deteriorate"),
                "description": "1 to 4 guardrail metrics, other than the target metric",
            },
            "requires_measurement_repair_first": {
                "type": "boolean",
                "description": "True when a cited campaign shows a possible tracking fault, "
                "so tracking must be repaired and verified before a performance test is read",
            },
            "duration_days": {
                "type": "integer",
                "description": f"Length, {MIN_DURATION_DAYS} to {MAX_DURATION_DAYS} days",
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
