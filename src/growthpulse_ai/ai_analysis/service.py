"""Report -> evidence package -> provider -> validation -> insights artifact.

``generate_insights`` is the only path from a report to AI-written insights. A live call
happens only with a ledger, after a successful reservation, and is never retried. Any
failure, whether budget, provider, stop reason, JSON or validation, produces the
deterministic fallback with its reason recorded. A failure never raises past here.

An artifact stores the validated model output, not just its rendering, so anyone loading
it (the dashboard builder) re-validates it against a freshly built package. A stored
artifact is never trusted because it was valid once.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ..config import Settings
from .audit import RejectionAudit
from .evidence import EvidencePackage, build_evidence_package, canonical_json
from .fallback import deterministic_response
from .feasibility import assess_feasibility, reference_values, tracking_risk_campaigns
from .ledger import LedgerError, SpendLedger
from .pricing import (
    PRICING_VERSION,
    UnpricedModelError,
    input_token_upper_bound,
    max_request_cost,
    usage_cost,
)
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, user_message
from .providers import InsightProvider, ProviderError, ProviderRequest, ProviderResponse
from .schema import (
    RESPONSE_SCHEMA_VERSION,
    SUPPORTED_RESPONSE_SCHEMA_VERSIONS,
    InsightResponse,
    InsightResponseV1,
    response_json_schema,
)
from .validation import CHECKS, CHECKS_BY_VERSION, Violation, validate_response

ARTIFACT_SCHEMA_VERSION = "1.0"
MAX_RECORDED_VIOLATIONS = 50


class LiveCallBlockedError(RuntimeError):
    """A live provider was used without the safeguards a live call requires."""


@dataclass(frozen=True)
class InsightConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_output_tokens: int = 10_000
    timeout_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "InsightConfig":
        return cls(
            model=settings.anthropic_model,
            effort=settings.anthropic_effort,
            max_output_tokens=settings.ai_max_output_tokens,
            timeout_seconds=settings.ai_request_timeout_seconds,
        )


def build_request(package: EvidencePackage, config: InsightConfig) -> ProviderRequest:
    return ProviderRequest(
        model=config.model,
        system=SYSTEM_PROMPT,
        user=user_message(package),
        output_schema=response_json_schema(package),
        max_output_tokens=config.max_output_tokens,
        effort=config.effort,
        timeout_seconds=config.timeout_seconds,
    )


def request_max_cost(request: ProviderRequest) -> tuple[int, Decimal]:
    """The input-token ceiling and the most this request can cost."""
    max_input = input_token_upper_bound(
        request.system, request.user, canonical_json(request.output_schema)
    )
    return max_input, max_request_cost(request.model, max_input, request.max_output_tokens)


def _citation(finding: dict) -> dict:
    return {
        "finding_id": finding["finding_id"],
        "metric": finding["metric"],
        "metric_label": finding["metric_label"],
        "campaign_name": finding["campaign_name"],
        "platform": finding["platform"],
        "currency": finding["currency"],
        "severity": finding["severity"],
        "direction": finding["direction"],
        "comparison_label": finding["comparison_label"],
    }


LEGACY_NOTE = (
    "Legacy advisory proposal: generated under response schema 1, before experiments had to "
    "state a type, success criteria, guardrails and measurement-repair order. It passed the "
    "15 evidence checks of that schema. The assessment beside it is GrowthPulse's own, "
    "derived from the evidence, not part of the AI output."
)


def render_insights(
    response: InsightResponse | InsightResponseV1,
    package: EvidencePackage,
    schema_version: str = RESPONSE_SCHEMA_VERSION,
) -> dict:
    """Dashboard-ready insights. Every value shown beside the prose comes from evidence:
    reference values and feasibility are derived by GrowthPulse, never written by a model."""
    findings = package.findings
    tracking_risk = tracking_risk_campaigns(package)

    def cite(ids: list[str]) -> list[dict]:
        return [_citation(findings[finding_id]) for finding_id in ids]

    experiments = []
    for item in response.experiments:
        baseline = findings[item.baseline_finding_id]
        basis = baseline["conversion_basis"]
        cited_campaigns = {findings[finding_id]["campaign_id"] for finding_id in item.finding_ids}
        legacy = schema_version == "1"
        experiment_type = None if legacy else item.experiment_type
        entry = {
            "title": item.title,
            "kind": item.kind,
            "legacy": legacy,
            "legacy_note": LEGACY_NOTE if legacy else None,
            "experiment_type": experiment_type,
            "problem_statement": item.problem_statement,
            "hypothesis": item.hypothesis,
            "proposed_experiment": item.proposed_experiment,
            "target_metric": item.target_metric,
            "target_metric_label": baseline["metric_label"],
            "reference_values": reference_values(baseline),
            "attribution": {
                "basis_id": basis["basis_id"],
                "platform": basis["platform"],
                "stage": basis["stage"],
                "event_name": basis["event_name"],
                "attribution_model": basis["attribution_model"],
                "click_window_days": basis["click_window_days"],
                "view_window_days": basis["view_window_days"],
            },
            "comparison_label": baseline["comparison_label"],
            "measurement_method": item.measurement_method,
            "success_criteria": None if legacy else item.success_criteria,
            "guardrail_metrics": [] if legacy else list(item.guardrail_metrics),
            "requires_measurement_repair_first": (
                None if legacy else item.requires_measurement_repair_first
            ),
            "duration_days": item.duration_days,
            "feasibility": assess_feasibility(
                experiment_type,
                item.target_metric,
                baseline,
                item.duration_days,
                tracking_risk=bool(cited_campaigns & tracking_risk),
            ),
            "risks": list(item.risks),
            "assumptions": list(item.assumptions),
            "citations": cite(item.finding_ids),
            "requires_human_approval": True,
            "status": "proposal",
            "approval": "not_requested",
            "executed": False,
        }
        if legacy:
            entry["model_selected_reference"] = item.baseline_reference
        experiments.append(entry)
    return {
        "observed_facts": [
            {"statement": f.statement, "citations": cite(f.finding_ids)}
            for f in response.observed_facts
        ],
        "hypotheses": [
            {"statement": h.statement, "status": "untested", "citations": cite(h.finding_ids)}
            for h in response.hypotheses
        ],
        "experiments": experiments,
        "limitations": {
            "attribution": list(package.content["attribution_disclosure"]),
            "report": list(package.content["report_limitations"]),
            "model_noted": list(response.limitations),
        },
    }


def fallback_insights(package: EvidencePackage) -> dict:
    result = validate_response(deterministic_response(package), package)
    if not result.ok:  # A bug, never a data condition: fail loudly in tests and builds.
        raise AssertionError(f"deterministic fallback failed validation: {result.violations}")
    return render_insights(result.response, package)


def _reasons(violations: tuple[Violation, ...] | list[Violation]) -> list[dict]:
    return [violation.as_dict() for violation in violations[:MAX_RECORDED_VIOLATIONS]]


def _reconcile(
    ledger: SpendLedger, request_id: str, response: ProviderResponse
) -> tuple[str, Decimal | None]:
    if response.usage is None:
        ledger.hold(request_id, "usage_missing")
        return "held_unknown", None
    if response.price_modifiers:
        ledger.hold(request_id, "price_modifier:" + ",".join(response.price_modifiers))
        return "held_unknown", None
    try:
        cost = usage_cost(response.model, response.usage)
    except UnpricedModelError:
        ledger.hold(request_id, "unpriced_model")
        return "held_unknown", None
    ledger.settle(request_id, cost, response.usage)
    return "settled", cost


def generate_insights(
    report: dict,
    provider: InsightProvider,
    config: InsightConfig | None = None,
    *,
    ledger: SpendLedger | None = None,
    audit: RejectionAudit | None = None,
    now: datetime | None = None,
) -> dict:
    """Produce an insights artifact for a report. Never modifies the report."""
    config = config or InsightConfig()
    package = build_evidence_package(report)
    request = build_request(package, config)
    max_input, max_cost = request_max_cost(request)
    request_id = uuid.uuid4().hex
    cost = {
        "status": "mock_no_charge" if not provider.live else "not_attempted",
        "request_id": request_id if provider.live else None,
        "max_input_tokens": max_input,
        "max_output_tokens": request.max_output_tokens,
        "reserved_max_usd": None,
        "estimated_usd": "0" if not provider.live else None,
        "usage": None,
        "ledger_after": None,
    }
    violations: list[Violation] = []
    response: ProviderResponse | None = None

    if provider.live:
        if ledger is None:
            raise LiveCallBlockedError("a live provider needs a spend ledger")
        try:
            ledger.reserve(request_id, request.model, max_cost)
            cost["reserved_max_usd"] = str(max_cost)
        except LedgerError as error:
            violations.append(Violation("budget", "$", type(error).__name__))
    if not violations:
        try:
            response = provider.complete(request)
        except ProviderError as error:
            violations.append(Violation("provider", "$", error.code))
            if provider.live:
                if error.billable == "no":
                    ledger.release(request_id, error.code)
                    cost["status"] = "released_not_billed"
                    cost["estimated_usd"] = "0"
                else:
                    ledger.hold(request_id, error.code)
                    cost["status"] = "held_unknown"
    if response is not None:
        if response.usage is not None:
            cost["usage"] = response.usage.as_dict()
        if provider.live:
            cost["status"], estimated = _reconcile(ledger, request_id, response)
            cost["estimated_usd"] = None if estimated is None else str(estimated)
    if provider.live and ledger is not None:
        try:
            cost["ledger_after"] = ledger.summary().as_dict()
        except LedgerError:
            cost["ledger_after"] = None

    model_output = None
    if response is not None:
        if response.stop_reason != "end_turn":
            violations.append(Violation("stop_reason", "$", str(response.stop_reason)))
        else:
            try:
                model_output = json.loads(response.text)
            except json.JSONDecodeError:
                violations.append(Violation("schema", "$", "invalid_json"))
            else:
                result = validate_response(model_output, package)
                violations.extend(result.violations)

    validated = response is not None and not violations
    if validated:
        insights = render_insights(InsightResponse.model_validate(model_output), package)
    else:
        insights = fallback_insights(package)
    provider_info = {
        "name": provider.name,
        "mode": "live" if provider.live else "mock",
        "requested_model": request.model,
        "response_model": response.model if response else None,
        "message_id": response.message_id if response else None,
        "effort": request.effort,
        "max_output_tokens": request.max_output_tokens,
    }
    # A rejected response is kept only in the local audit, never in the artifact.
    audit_record = None
    if response is not None and not validated and audit is not None:
        audit_record = audit.save(
            report_id=package.report_id,
            package_id=package.package_id,
            prompt_version=PROMPT_VERSION,
            provider=provider_info,
            stop_reason=response.stop_reason,
            response_text=response.text,
            violations=_reasons(violations),
        ).as_dict()
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "report_id": package.report_id,
        "package_id": package.package_id,
        "prompt_version": PROMPT_VERSION,
        "pricing_version": PRICING_VERSION,
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "provider": provider_info,
        "outcome": "validated" if validated else "fallback",
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "validation": {"checks": list(CHECKS), "violations": _reasons(violations)},
        "model_output": model_output if validated else None,
        "rejection_audit": audit_record,
        "cost": cost,
        "insights": insights,
    }


def artifact_schema_version(artifact: dict) -> str:
    """The response schema an artifact was validated under. Older artifacts predate the
    field, and were all version 1."""
    return str(artifact.get("response_schema_version", "1"))


def revalidate_artifact(artifact: dict, report: dict) -> tuple[dict | None, str | None]:
    """Re-check a stored artifact against the current report, under the schema version it
    was generated with, and render it. Returns (insights, reason)."""
    package = build_evidence_package(report)
    version = artifact_schema_version(artifact)
    if artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        return None, "unsupported artifact version"
    if version not in SUPPORTED_RESPONSE_SCHEMA_VERSIONS:
        return None, "unsupported response schema version"
    if artifact.get("report_id") != package.report_id:
        return None, "artifact is for a different report"
    if artifact.get("package_id") != package.package_id:
        return None, "artifact was generated from different evidence"
    if artifact.get("outcome") != "validated" or artifact.get("model_output") is None:
        return None, "artifact holds no validated model output"
    result = validate_response(artifact["model_output"], package, version)
    if not result.ok:
        return None, "artifact failed re-validation"
    return render_insights(result.response, package, version), None


# Kept in a published sample. Everything else (request and message IDs, the reservation,
# the ledger snapshot, the rejection-audit record, local paths) is dropped.
PUBLISHED_PROVIDER_FIELDS = (
    "name", "mode", "requested_model", "response_model", "effort", "max_output_tokens",
)  # fmt: skip
PUBLISHED_COST_FIELDS = ("status", "estimated_usd", "usage")
SECRET_PATTERNS = ("sk-ant-", "x-api-key", "authorization", "bearer ")


class PublicationError(ValueError):
    """An artifact cannot be published as a static sample."""


def publish_artifact(artifact: dict, report: dict, source_sha256: str) -> dict:
    """A sanitised, static copy of a validated live artifact, for the dashboard.

    The model output is carried over unchanged and must re-validate under its own schema
    version. Only provenance needed to label the sample is kept.
    """
    if artifact.get("provider", {}).get("name") != "anthropic":
        raise PublicationError("only Anthropic artifacts are published as AI samples")
    if artifact.get("provider", {}).get("mode") != "live":
        raise PublicationError("only live-generated artifacts are published as AI samples")
    insights, reason = revalidate_artifact(artifact, report)
    if insights is None:
        raise PublicationError(f"the artifact does not re-validate: {reason}")
    version = artifact_schema_version(artifact)
    published = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "response_schema_version": version,
        "report_id": artifact["report_id"],
        "package_id": artifact["package_id"],
        "prompt_version": artifact["prompt_version"],
        "pricing_version": artifact["pricing_version"],
        "generated_at": artifact["generated_at"],
        "provider": {key: artifact["provider"].get(key) for key in PUBLISHED_PROVIDER_FIELDS},
        "outcome": "validated",
        "validation": {"checks": list(CHECKS_BY_VERSION[version]), "violations": []},
        "model_output": artifact["model_output"],
        "cost": {key: artifact["cost"].get(key) for key in PUBLISHED_COST_FIELDS},
        "publication": {
            "kind": "static_sample",
            "source_artifact_sha256": source_sha256,
            "note": (
                "A previously generated, validated sample, published as static data. Request "
                "identifiers, budget reservations and ledger details were removed."
            ),
        },
    }
    text = json.dumps(published).lower()
    if any(pattern in text for pattern in SECRET_PATTERNS):
        raise PublicationError("the artifact contains credential-like text")
    return published


SOURCE_LABELS = {
    "anthropic": "AI-generated by Claude: a static, previously generated sample",
    "mock": "Offline mock provider: deterministic template content, not Claude output",
    "deterministic_fallback": "Deterministic fallback: rule-based templates, not AI output",
}


def dashboard_insights(report: dict, artifact: dict | None = None) -> dict:
    """The block the dashboard shows. Static by construction: never live inference."""
    package = build_evidence_package(report)
    insights, reason, source = None, None, "deterministic_fallback"
    if artifact is not None:
        insights, reason = revalidate_artifact(artifact, report)
        if insights is not None:
            source = artifact["provider"]["name"]
    if insights is None:
        insights = fallback_insights(package)
        reason = reason or "no AI artifact has been generated for this report"
    version = (
        artifact_schema_version(artifact)
        if source != "deterministic_fallback"
        else (RESPONSE_SCHEMA_VERSION)
    )
    provenance = {
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "is_ai_output": source == "anthropic",
        "is_live_inference": False,
        "static_note": (
            "Pre-generated and stored with the dashboard data. The browser never calls the "
            "Anthropic API, and nothing on this screen is live inference."
        ),
        "report_id": package.report_id,
        "package_id": package.package_id,
        "response_schema_version": version,
        "validation_checks": list(CHECKS_BY_VERSION[version]),
        "validated": source != "deterministic_fallback",
        "fallback_reason": reason if source == "deterministic_fallback" else None,
    }
    if source != "deterministic_fallback":
        provider = artifact["provider"]
        provenance |= {
            "generated_at": artifact["generated_at"],
            "prompt_version": artifact["prompt_version"],
            "model": provider["response_model"] or provider["requested_model"],
            "mode": provider["mode"],
            "estimated_cost_usd": artifact["cost"].get("estimated_usd"),
        }
    return {"provenance": provenance, **insights}
