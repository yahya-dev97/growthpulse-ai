"""Offline reproduction of number rejections, their diagnostics, and the rejection audit.

Every fixture is the synthetic sample report or a mock response. No request reaches
Anthropic. Diagnostics explain a rejection; they never change what is accepted.
"""

import copy
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from growthpulse_ai.ai_analysis import __main__ as cli
from growthpulse_ai.ai_analysis.audit import RejectionAudit, audit_enabled
from growthpulse_ai.ai_analysis.evidence import build_evidence_package
from growthpulse_ai.ai_analysis.fallback import deterministic_response
from growthpulse_ai.ai_analysis.pricing import TokenUsage
from growthpulse_ai.ai_analysis.prompt import SYSTEM_PROMPT
from growthpulse_ai.ai_analysis.providers import MockProvider
from growthpulse_ai.ai_analysis.service import dashboard_insights, generate_insights
from growthpulse_ai.ai_analysis.validation import finding_values, validate_response
from growthpulse_ai.config import PROJECT_ROOT, Settings

SAMPLE = PROJECT_ROOT / "docs" / "samples" / "synthetic_analytics_report.json"
FAKE_KEY = "sk-ant-test-DO-NOT-LEAK-0123456789"


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(SAMPLE.read_text())


@pytest.fixture(scope="module")
def package(report):
    return build_evidence_package(report)


def finding(package, **match) -> dict:
    return next(f for f in package.ordered_findings if all(f.get(k) == v for k, v in match.items()))


def with_fact(package, statement: str, finding_ids: list[str]) -> dict:
    raw = copy.deepcopy(deterministic_response(package))
    raw["observed_facts"][5] = {"statement": statement, "finding_ids": finding_ids}
    return raw


def number_violations(raw, package) -> list[dict]:
    result = validate_response(raw, package)
    return [v.as_dict() for v in result.violations if v.check == "numbers_supported"]


@pytest.fixture(scope="module")
def ten_day(package) -> dict:
    """A Google finding on the 10-day comparison: 30-day click window, so 10 and 7 are
    not attribution windows for it."""
    return finding(package, comparison_label="latest_10_vs_first_10", platform="google_ads")


# --- reproducing the live rejection ------------------------------------------------------


def test_window_lengths_reproduce_the_live_failure_and_are_now_evidence(package, ten_day):
    """The live response failed numbers_supported twice in one fact. Writing the comparison
    window ("the latest 10 days versus the first 10 days") reproduces exactly that against
    the evidence the live request saw. Window lengths are now stated as evidence."""
    fields = {item.field: item.value for item in finding_values(ten_day)}
    assert fields["baseline_days"] == fields["current_days"] == Decimal(10)
    statement = "ROAS fell over the latest 10 days versus the first 10 days."
    assert number_violations(with_fact(package, statement, [ten_day["finding_id"]]), package) == []

    previous_evidence = {
        "baseline_days", "current_days",
    }  # fmt: skip
    old_allowed = {
        item.value for item in finding_values(ten_day) if item.field not in previous_evidence
    }
    assert Decimal(10) not in old_allowed  # the cause, under the evidence sent live


def test_a_wrong_window_length_is_still_rejected(package, ten_day):
    statement = "ROAS fell over the latest 11 days versus the first 10 days."
    (violation,) = number_violations(
        with_fact(package, statement, [ten_day["finding_id"]]), package
    )
    assert violation["diagnostic"]["value"] == "11"
    assert violation["diagnostic"]["classification"] == "unsupported_invention"


# --- each diagnostic class ----------------------------------------------------------------


def test_an_invented_number_is_identified_with_its_nearest_evidence(package, ten_day):
    statement = f"{ten_day['metric_label']} reached 987.65."
    (violation,) = number_violations(
        with_fact(package, statement, [ten_day["finding_id"]]), package
    )
    diagnostic = violation["diagnostic"]
    assert violation["path"] == "observed_facts[5].statement"
    assert diagnostic["value"] == "987.65"
    assert diagnostic["cited_finding_ids"] == [ten_day["finding_id"]]
    assert diagnostic["classification"] == "unsupported_invention"
    nearest = diagnostic["nearest_cited_values"]
    assert 0 < len(nearest) <= 5
    assert {n["finding_id"] for n in nearest} == {ten_day["finding_id"]}
    assert all({"value", "field"} <= set(n) for n in nearest)


def test_a_correct_value_linked_to_the_wrong_finding(package):
    cited, other = package.ordered_findings[0], package.ordered_findings[1]
    value = Decimal(other["evidence"]["current_spend"])
    if value in {item.value for item in finding_values(cited)}:
        pytest.skip("both findings share this value")
    statement = f"Spend was {value:.2f}."
    (violation,) = number_violations(with_fact(package, statement, [cited["finding_id"]]), package)
    diagnostic = violation["diagnostic"]
    assert diagnostic["classification"] == "incorrect_finding_linkage"
    sources = {(m["finding_id"], m["field"]) for m in diagnostic["matching_uncited_values"]}
    assert (other["finding_id"], "evidence.current_spend") in sources


def test_a_value_from_a_finding_on_another_conversion_basis(package):
    """An attribution mismatch hidden in prose: the experiment cites a Google finding but
    quotes a Meta finding's figure."""
    raw = copy.deepcopy(deterministic_response(package))
    experiment = next(
        e
        for e in raw["experiments"]
        if package.findings[e["baseline_finding_id"]]["platform"] == "google_ads"
    )
    google = package.findings[experiment["baseline_finding_id"]]
    meta = finding(package, platform="meta_ads")
    assert google["conversion_basis"]["basis_id"] != meta["conversion_basis"]["basis_id"]
    experiment["measurement_method"] = (
        f"Compare against {Decimal(meta['evidence']['current_conversions']):.2f} conversions."
    )
    violations = number_violations(raw, package)
    assert violations, "a figure from another basis must be rejected"
    assert violations[0]["diagnostic"]["classification"] == "incorrect_finding_linkage"
    assert violations[0]["path"].endswith(".measurement_method")


def test_percentages_from_evidence_are_accepted(package):
    rate = finding(package, metric="platform_attributed_conversion_rate")
    current = Decimal(rate["current_value"]) * 100
    change = abs(Decimal(rate["percent_change"]))
    statement = f"Conversion rate fell to {current:.2f}%, a fall of {change:.1f}%."
    assert number_violations(with_fact(package, statement, [rate["finding_id"]]), package) == []


def test_derived_metrics_are_rejected_and_named(package, ten_day):
    evidence = ten_day["evidence"]
    # Cost per click is derivable (spend / clicks) but stated nowhere in the evidence. Cost
    # per conversion would be accepted here: it is this finding's own current_value.
    spend, clicks = Decimal(evidence["current_spend"]), Decimal(evidence["current_clicks"])
    gap = abs(Decimal(evidence["current_spend"]) - Decimal(evidence["baseline_spend"]))
    cases = {
        f"Spend changed by {gap:.2f}.": "difference",
        f"Each click cost {spend / clicks:.4f}.": "ratio",
    }
    for statement, transformation in cases.items():
        (violation,) = number_violations(
            with_fact(package, statement, [ten_day["finding_id"]]), package
        )
        assert violation["diagnostic"]["classification"] == "numerical_transformation"
        assert violation["diagnostic"]["transformation"] == transformation
        assert len(violation["diagnostic"]["operands"]) == 2


def test_a_non_rate_scaled_to_a_percentage_is_rejected(package):
    roas = finding(package, metric="platform_attributed_roas")
    scaled = Decimal(roas["current_value"]) * 100
    statement = f"ROAS was {scaled:.1f}%."
    (violation,) = number_violations(with_fact(package, statement, [roas["finding_id"]]), package)
    assert violation["diagnostic"]["transformation"] == "multiplied by 100"


def test_rounding_to_one_significant_figure_is_rejected_and_explained(package):
    roas = finding(package, metric="platform_attributed_roas")
    value = Decimal(roas["current_value"])
    if value == value.to_integral_value() or value >= 10:
        pytest.skip("needs a single-digit non-integer value")
    statement = f"ROAS was about {value:.0f}."
    violations = number_violations(with_fact(package, statement, [roas["finding_id"]]), package)
    if not violations:
        pytest.skip("the rounded value is exactly another evidence value")
    assert violations[0]["diagnostic"]["transformation"] in {
        "rounded to one significant figure",
        "date fragment",
    }


def test_partial_dates_are_named_as_date_fragments(package, ten_day):
    day = ten_day["current_period"]["start"].split("-")[2]
    statement = f"The current window starts on {int(day)} October."
    (violation,) = number_violations(
        with_fact(package, statement, [ten_day["finding_id"]]), package
    )
    assert violation["diagnostic"]["transformation"] == "date fragment"


def test_numbers_with_no_cited_evidence_are_missing_evidence(package):
    raw = copy.deepcopy(deterministic_response(package))
    raw["limitations"] = ["Results may vary by 25 points."]
    (violation,) = number_violations(raw, package)
    assert violation["path"] == "limitations[0]"
    assert violation["diagnostic"]["classification"] == "missing_evidence"
    assert violation["diagnostic"]["cited_finding_ids"] == []


def test_each_number_in_a_statement_is_judged_separately(package, ten_day):
    good = Decimal(ten_day["evidence"]["current_spend"])
    statement = f"Spend was {good:.2f} in the latest 10 days; it may reach 1234.56 or 9876.54."
    violations = number_violations(with_fact(package, statement, [ten_day["finding_id"]]), package)
    assert [v["diagnostic"]["value"] for v in violations] == ["1234.56", "9876.54"]
    assert all(v["path"] == "observed_facts[5].statement" for v in violations)


def test_attribution_windows_written_in_prose_are_accepted(package):
    meta = finding(package, platform="meta_ads")
    basis = meta["conversion_basis"]
    statement = (
        f"Conversions use a {basis['click_window_days']}-day click and "
        f"{basis['view_window_days']}-day view window."
    )
    assert number_violations(with_fact(package, statement, [meta["finding_id"]]), package) == []


def test_diagnostics_never_echo_the_statement(package, ten_day):
    statement = "A distinctive sentence that must stay out of diagnostics, with 4321.09."
    raw = with_fact(package, statement, [ten_day["finding_id"]])
    result = validate_response(raw, package)
    dumped = json.dumps([v.as_dict() for v in result.violations])
    assert "distinctive sentence" not in dumped
    assert "4321.09" in dumped  # the offending number is reported, nothing else


def test_diagnostics_do_not_change_acceptance(package):
    assert validate_response(deterministic_response(package), package).ok


# --- the rejection audit -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "app_env", "environ", "expected"),
    [
        ("auto", "local", {}, True),
        ("auto", "local", {"CI": "true"}, False),
        ("auto", "ci", {}, False),
        ("auto", "production", {}, False),
        ("auto", "test", {}, False),
        ("enabled", "ci", {"CI": "true"}, True),
        ("disabled", "local", {}, False),
    ],
)
def test_audit_is_off_by_default_outside_development(mode, app_env, environ, expected):
    settings = Settings(_env_file=None, app_env=app_env, ai_rejection_audit=mode)
    assert audit_enabled(settings, environ) is expected


def test_the_default_audit_directory_is_gitignored():
    directory = Settings(_env_file=None).ai_rejection_audit_dir
    assert directory.relative_to(PROJECT_ROOT).parts[0] == "data"
    assert "/data/" in (PROJECT_ROOT / ".gitignore").read_text().splitlines()


def invalid_payload(package):
    def payload(_request):
        raw = deterministic_response(package)
        raw["observed_facts"][0]["statement"] += f" Rejected-marker 4321.09 {FAKE_KEY}."
        return raw

    return payload


def test_a_rejected_response_is_audited_owner_only_and_never_in_the_artifact(
    report, package, tmp_path
):
    audit = RejectionAudit(tmp_path / "audit", retention_days=14)
    artifact = generate_insights(
        report, MockProvider(payload=invalid_payload(package)), audit=audit
    )
    assert artifact["outcome"] == "fallback"
    record = artifact["rejection_audit"]
    assert record["saved"] is True
    assert "Rejected-marker" not in json.dumps(artifact)

    path = tmp_path / "audit" / f"{record['audit_id']}.json"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "audit").stat().st_mode) == 0o700
    saved = json.loads(path.read_text())
    assert saved["response"]["observed_facts"][0]["statement"].count("Rejected-marker") == 1
    assert saved["violations"][0]["check"] == "numbers_supported"
    text = path.read_text()
    assert FAKE_KEY not in text and "[REDACTED]" in text
    assert SYSTEM_PROMPT[:40] not in text  # no prompt, headers or request metadata
    assert set(saved["provider"]) == {
        "name", "mode", "requested_model", "response_model", "message_id",
    }  # fmt: skip


def test_validated_responses_and_disabled_audits_write_nothing(report, package, tmp_path):
    audit = RejectionAudit(tmp_path / "audit", retention_days=14)
    valid = MockProvider(payload=lambda _r: deterministic_response(package))
    assert generate_insights(report, valid, audit=audit)["rejection_audit"] is None
    invalid = MockProvider(payload=invalid_payload(package))
    assert generate_insights(report, invalid, audit=None)["rejection_audit"] is None
    assert not (tmp_path / "audit").exists()


def test_non_json_output_is_audited_as_text(report, tmp_path):
    audit = RejectionAudit(tmp_path / "audit", retention_days=14)
    artifact = generate_insights(report, MockProvider(payload="not json"), audit=audit)
    saved = json.loads((tmp_path / "audit" / f"{artifact['rejection_audit']['audit_id']}.json")
                       .read_text())  # fmt: skip
    assert saved["response"] is None and saved["response_text"] == "not json"


def test_an_audit_failure_never_blocks_the_fallback(report, package, tmp_path):
    blocker = tmp_path / "audit"
    blocker.write_text("a file where the directory should be")
    artifact = generate_insights(
        report, MockProvider(payload=invalid_payload(package)), audit=RejectionAudit(blocker, 14)
    )
    assert artifact["outcome"] == "fallback" and artifact["insights"]["observed_facts"]
    assert artifact["rejection_audit"]["saved"] is False


def test_audit_files_are_never_overwritten(tmp_path):
    audit = RejectionAudit(tmp_path, retention_days=14)
    now = datetime(2026, 11, 12, tzinfo=UTC)
    kwargs = dict(
        report_id="r", package_id="p", prompt_version="v", provider={"message_id": "msg_1"},
        stop_reason="end_turn", response_text="{}", violations=[], now=now,
    )  # fmt: skip
    assert audit.save(**kwargs).saved is True
    second = audit.save(**kwargs)
    assert second.saved is False and second.reason == "FileExistsError"


def test_old_audit_files_are_pruned(tmp_path):
    audit = RejectionAudit(tmp_path, retention_days=14)
    old = tmp_path / "old.json"
    old.write_text("{}")
    stale = (datetime.now(UTC) - timedelta(days=15)).timestamp()
    os.utime(old, (stale, stale))
    fresh = tmp_path / "fresh.json"
    fresh.write_text("{}")
    assert audit.prune() == 1
    assert not old.exists() and fresh.exists()


def test_an_audited_response_can_never_reach_the_dashboard(report, package, tmp_path):
    audit = RejectionAudit(tmp_path / "audit", retention_days=14)
    artifact = generate_insights(
        report, MockProvider(payload=invalid_payload(package)), audit=audit
    )
    # Even an artifact pointing at an audited response shows only the fallback.
    saved = json.loads(
        (tmp_path / "audit" / f"{artifact['rejection_audit']['audit_id']}.json").read_text()
    )
    forged = {**artifact, "outcome": "validated", "model_output": saved["response"]}
    block = dashboard_insights(report, forged)
    assert block["provenance"]["source"] == "deterministic_fallback"
    assert "Rejected-marker" not in json.dumps(block)
    builder = (PROJECT_ROOT / "frontend" / "tools" / "build_dashboard_data.py").read_text()
    assert "ai_rejections" not in builder and "rejection_audit" not in builder
    data = (PROJECT_ROOT / "frontend" / "data" / "dashboard_data.json").read_text()
    assert "rejection_audit" not in data and "violations" not in data


def test_cli_reports_diagnostics_without_rejected_text(tmp_path, capsys, package):
    def factory(_settings):
        return MockProvider(
            payload=invalid_payload(package), live=True, usage=TokenUsage(1_000, 1_000)
        )

    settings = Settings(
        _env_file=None,
        ai_ledger_path=tmp_path / "ledger.jsonl",
        ai_live_calls_enabled=True,
        anthropic_api_key=FAKE_KEY,
        ai_rejection_audit="enabled",
        ai_rejection_audit_dir=tmp_path / "audit",
    )
    code = cli.main(
        ["generate", "--report", str(SAMPLE), "--out", str(tmp_path / "a.json"), "--live",
         "--confirm-max-cost", "0.50"],
        settings=settings,
        provider_factory=factory,
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == cli.EXIT_FALLBACK
    assert "value 4321.09: unsupported_invention" in out.out
    assert "Rejected response saved for review" in out.out
    for stream in (out.out, out.err):
        assert "Rejected-marker" not in stream and FAKE_KEY not in stream
