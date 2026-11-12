"""The insights pipeline end to end, entirely offline: no request ever reaches Anthropic.

Live-path tests use a mock provider flagged live, or the real Anthropic adapter with a
fake SDK client injected, so the ledger, gates and error handling run for real at no cost.
"""

import copy
import importlib.util
import json
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from growthpulse_ai.ai_analysis import __main__ as cli
from growthpulse_ai.ai_analysis.evidence import build_evidence_package
from growthpulse_ai.ai_analysis.fallback import deterministic_response
from growthpulse_ai.ai_analysis.ledger import SpendLedger
from growthpulse_ai.ai_analysis.pricing import (
    TokenUsage,
    input_token_upper_bound,
    max_request_cost,
    usage_cost,
)
from growthpulse_ai.ai_analysis.providers import AnthropicProvider, MockProvider, ProviderError
from growthpulse_ai.ai_analysis.service import (
    InsightConfig,
    LiveCallBlockedError,
    build_request,
    dashboard_insights,
    generate_insights,
    request_max_cost,
    revalidate_artifact,
)
from growthpulse_ai.config import PROJECT_ROOT, Settings

SAMPLE = PROJECT_ROOT / "docs" / "samples" / "synthetic_analytics_report.json"
M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"
ARTIFACT = PROJECT_ROOT / "docs" / "samples" / "ai_insights" / f"{M4_REPORT_ID}.json"
FAKE_KEY = "sk-ant-test-DO-NOT-LEAK-0123456789"
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(SAMPLE.read_text())


@pytest.fixture(scope="module")
def package(report):
    return build_evidence_package(report)


@pytest.fixture
def ledger(tmp_path) -> SpendLedger:
    return SpendLedger(tmp_path / "ledger.jsonl")


def valid_payload(package):
    return lambda _request: deterministic_response(package)


def live_mock(package, **kwargs) -> MockProvider:
    return MockProvider(payload=valid_payload(package), live=True, **kwargs)


# --- mocked success and provenance --------------------------------------------------


def test_mock_success_is_validated_free_and_touches_no_ledger(report, package, tmp_path):
    artifact = generate_insights(report, MockProvider(payload=valid_payload(package)))
    assert artifact["outcome"] == "validated"
    assert artifact["validation"]["violations"] == []
    assert artifact["provider"]["mode"] == "mock"
    assert artifact["cost"]["status"] == "mock_no_charge"
    assert artifact["cost"]["estimated_usd"] == "0"
    assert artifact["model_output"] is not None
    for experiment in artifact["insights"]["experiments"]:
        assert experiment["requires_human_approval"] is True
        assert experiment["status"] == "proposal" and experiment["executed"] is False
        assert experiment["approval"] == "not_requested"
        assert (
            experiment["baseline"]["value"]
            == package.findings[experiment["baseline"]["finding_id"]][
                experiment["baseline"]["reference"]
            ]
        )
    assert not list(tmp_path.iterdir())


def test_generation_never_modifies_the_report_or_its_id(report):
    before = copy.deepcopy(report)
    generate_insights(report, MockProvider(payload="not json"))
    assert report == before
    assert report["report_id"] == M4_REPORT_ID


def test_the_evidence_package_is_deterministic_and_matches_the_dashboard_report(report):
    first, second = build_evidence_package(report), build_evidence_package(copy.deepcopy(report))
    assert first.package_id == second.package_id
    assert first.report_id == M4_REPORT_ID
    spec = importlib.util.spec_from_file_location(
        "build_dashboard_data", PROJECT_ROOT / "frontend" / "tools" / "build_dashboard_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = json.loads((PROJECT_ROOT / "frontend" / "data" / "dashboard_data.json").read_text())
    legacy = data["clients"][0]["ai_insights"]["provenance"]
    assert legacy["report_id"] == M4_REPORT_ID
    assert legacy["package_id"] == first.package_id


def test_the_package_holds_only_report_values(report, package):
    source = {f["finding_id"]: f for c in report["comparisons"] for f in c["findings"]}
    for finding in package.content["findings"]:
        original = source[finding["finding_id"]]
        for key in ("baseline_value", "current_value", "percent_change", "evidence", "currency"):
            assert finding[key] == original[key]
        assert finding["conversion_basis"]["basis_id"] == original["conversion_basis"]["basis_id"]
    assert package.content["omitted"]["suppressed_by_noise_guard"] == 5


def test_the_committed_mock_artifact_revalidates(report):
    artifact = json.loads(ARTIFACT.read_text())
    assert artifact["provider"]["mode"] == "mock"
    insights, reason = revalidate_artifact(artifact, report)
    assert insights is not None and reason is None


# --- failures fall back --------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "check"),
    [
        ("not json at all", "schema"),
        ({"observed_facts": []}, "schema"),
    ],
)
def test_invalid_output_falls_back(report, payload, check):
    artifact = generate_insights(report, MockProvider(payload=payload))
    assert artifact["outcome"] == "fallback"
    assert artifact["model_output"] is None
    assert check in {v["check"] for v in artifact["validation"]["violations"]}
    assert artifact["insights"]["observed_facts"]  # the fallback is shown instead


def test_unsupported_numbers_fall_back(report, package):
    def payload(_request):
        raw = deterministic_response(package)
        raw["observed_facts"][0]["statement"] += " Bookings will rise 37%."
        return raw

    artifact = generate_insights(report, MockProvider(payload=payload))
    assert artifact["outcome"] == "fallback"
    assert [v["check"] for v in artifact["validation"]["violations"]] == ["numbers_supported"]
    assert "37%" not in json.dumps(artifact)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_incomplete_or_refused_responses_fall_back(report, package, stop_reason):
    artifact = generate_insights(
        report, MockProvider(payload=valid_payload(package), stop_reason=stop_reason)
    )
    assert artifact["outcome"] == "fallback"
    assert artifact["validation"]["violations"][0] == {
        "check": "stop_reason",
        "path": "$",
        "detail": stop_reason,
    }


# --- live path: budget, reconciliation, errors, retries ------------------------------


def test_a_live_provider_without_a_ledger_is_refused(report, package):
    with pytest.raises(LiveCallBlockedError):
        generate_insights(report, live_mock(package))


def test_live_success_reserves_then_settles_the_actual_cost(report, package, ledger):
    usage = TokenUsage(input_tokens=9_000, output_tokens=3_000)
    provider = live_mock(package, usage=usage)
    artifact = generate_insights(report, provider, ledger=ledger)
    assert artifact["outcome"] == "validated"
    assert artifact["cost"]["status"] == "settled"
    assert Decimal(artifact["cost"]["estimated_usd"]) == Decimal("0.096")
    _, max_cost = request_max_cost(build_request(package, InsightConfig()))
    assert artifact["cost"]["reserved_max_usd"] == str(max_cost)
    summary = ledger.summary()
    assert summary.settled_usd == Decimal("0.096") and summary.open_usd == 0
    assert artifact["cost"]["ledger_after"]["committed_usd"] == "0.096000"
    assert len(provider.calls) == 1


def test_billed_but_invalid_responses_are_still_recorded(report, package, ledger):
    provider = live_mock(
        package,
        usage=TokenUsage(input_tokens=1_000, output_tokens=10_000),
        stop_reason="max_tokens",
    )
    artifact = generate_insights(report, provider, ledger=ledger)
    assert artifact["outcome"] == "fallback"
    assert artifact["cost"]["status"] == "settled"
    assert ledger.summary().settled_usd == Decimal("0.204")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"usage": None}, "usage_missing"),
        ({"price_modifiers": ("inference_geo=us",)}, "price_modifier"),
        ({"model": "claude-unknown-model"}, "unpriced_model"),
    ],
)
def test_unknowable_costs_are_held_at_the_full_reservation(report, package, ledger, kwargs, reason):
    artifact = generate_insights(report, live_mock(package, **kwargs), ledger=ledger)
    assert artifact["cost"]["status"] == "held_unknown"
    assert artifact["cost"]["estimated_usd"] is None
    summary = ledger.summary()
    assert summary.held_usd == Decimal(artifact["cost"]["reserved_max_usd"])
    held = [json.loads(line) for line in ledger.path.read_text().splitlines()][-1]
    assert held["event"] == "hold" and held["reason"].startswith(reason)


@pytest.mark.parametrize(
    ("error", "status", "committed"),
    [
        (ProviderError("timeout", "unknown", "timed out"), "held_unknown", True),
        (ProviderError("server_error", "unknown", "500"), "held_unknown", True),
        (ProviderError("rate_limited", "no", "429"), "released_not_billed", False),
        (ProviderError("authentication", "no", "401"), "released_not_billed", False),
    ],
)
def test_provider_errors_fall_back_without_retrying(
    report, package, ledger, error, status, committed
):
    provider = live_mock(package, error=error)
    artifact = generate_insights(report, provider, ledger=ledger)
    assert len(provider.calls) == 1  # no retry
    assert artifact["outcome"] == "fallback"
    assert artifact["cost"]["status"] == status
    assert artifact["validation"]["violations"][0]["detail"] == error.code
    assert (ledger.summary().committed_usd > 0) is committed


def test_budget_exhaustion_blocks_the_call(report, package, tmp_path):
    ledger = SpendLedger(tmp_path / "small.jsonl", Decimal("0.10"))
    provider = live_mock(package)
    artifact = generate_insights(report, provider, ledger=ledger)
    assert provider.calls == []
    assert artifact["outcome"] == "fallback"
    assert artifact["cost"]["status"] == "not_attempted"
    assert artifact["validation"]["violations"][0] == {
        "check": "budget",
        "path": "$",
        "detail": "BudgetExceededError",
    }


def test_two_held_requests_exhaust_the_dollar_budget(report, package, ledger):
    for _ in range(2):
        generate_insights(
            report,
            live_mock(package, error=ProviderError("timeout", "unknown", "t")),
            ledger=ledger,
        )
    provider = live_mock(package)
    artifact = generate_insights(report, provider, ledger=ledger)
    assert provider.calls == [] and artifact["cost"]["status"] == "not_attempted"


# --- pricing ---------------------------------------------------------------------------


def test_maximum_cost_is_a_true_upper_bound(package):
    request = build_request(package, InsightConfig())
    max_input, max_cost = request_max_cost(request)
    assert max_input > len((request.system + request.user).encode())
    worst_actual = usage_cost(
        "claude-opus-5-5",
        TokenUsage(input_tokens=max_input, output_tokens=request.max_output_tokens),
    )
    assert max_cost > worst_actual
    assert max_cost < Decimal("0.50")
    assert max_request_cost("claude-opus-5-5", 1_000_000, 0) == Decimal("5.5")
    assert input_token_upper_bound("ab", "€") == 5 + 2048


# --- the Anthropic adapter, with a fake SDK client ------------------------------------


class FakeClient:
    def __init__(self, message=None, error=None):
        self.options: dict = {}
        self.calls: list[dict] = []
        self.message, self.error = message, error
        self.messages = self

    def factory(self, **options):
        self.options = options
        return self

    def stream(self, **params):
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        message = self.message

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return message

        return Stream()


def sdk_message(text: str, **usage) -> SimpleNamespace:
    return SimpleNamespace(
        id="msg_test",
        model="claude-opus-5-5",
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 9_000),
            output_tokens=usage.get("output_tokens", 3_000),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cache_creation=None,
            service_tier="standard",
            speed=None,
            inference_geo=usage.get("inference_geo", "global"),
        ),
    )


def test_adapter_disables_retries_and_requests_structured_output(report, package, ledger):
    fake = FakeClient(message=sdk_message(json.dumps(deterministic_response(package))))
    provider = AnthropicProvider(SecretStr(FAKE_KEY), client_factory=fake.factory)
    artifact = generate_insights(report, provider, InsightConfig(effort="medium"), ledger=ledger)
    assert artifact["outcome"] == "validated"
    assert fake.options["max_retries"] == 0
    assert fake.options["timeout"] == 300.0
    (params,) = fake.calls
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in params and "tool_choice" not in params
    assert artifact["provider"]["message_id"] == "msg_test"
    assert artifact["cost"]["status"] == "settled"


@pytest.mark.parametrize(
    ("error", "code", "billable"),
    [
        (anthropic.APITimeoutError(request=REQUEST), "timeout", "unknown"),
        (anthropic.APIConnectionError(request=REQUEST), "connection", "unknown"),
        (
            anthropic.RateLimitError(
                "slow down", response=httpx2.Response(429, request=REQUEST), body=None
            ),
            "rate_limited",
            "no",
        ),
        (
            anthropic.InternalServerError(
                "boom", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "server_error",
            "unknown",
        ),
        (
            anthropic.AuthenticationError(
                "bad key", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            "authentication",
            "no",
        ),
    ],
)
def test_adapter_maps_sdk_errors_once(error, code, billable, package):
    fake = FakeClient(error=error)
    provider = AnthropicProvider(SecretStr(FAKE_KEY), client_factory=fake.factory)
    with pytest.raises(ProviderError) as raised:
        provider.complete(build_request(package, InsightConfig()))
    assert (raised.value.code, raised.value.billable) == (code, billable)
    assert len(fake.calls) == 1


def test_a_regional_inference_response_is_held_not_priced(report, package, ledger):
    fake = FakeClient(
        message=sdk_message(json.dumps(deterministic_response(package)), inference_geo="us")
    )
    provider = AnthropicProvider(SecretStr(FAKE_KEY), client_factory=fake.factory)
    artifact = generate_insights(report, provider, ledger=ledger)
    assert artifact["cost"]["status"] == "held_unknown"


# --- credentials -------------------------------------------------------------------------


@pytest.mark.parametrize("key", [SecretStr(""), None, FAKE_KEY])
def test_a_missing_or_unwrapped_key_is_refused(key):
    with pytest.raises(ProviderError) as raised:
        AnthropicProvider(key)
    assert raised.value.code == "missing_key"


def test_the_key_never_appears_in_output_artifacts_ledger_or_logs(
    report, package, ledger, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    leaky = anthropic.AuthenticationError(
        f"invalid x-api-key {FAKE_KEY}", response=httpx2.Response(401, request=REQUEST), body=None
    )
    outputs = []
    for fake in (
        FakeClient(message=sdk_message(json.dumps(deterministic_response(package)))),
        FakeClient(error=leaky),
    ):
        provider = AnthropicProvider(SecretStr(FAKE_KEY), client_factory=fake.factory)
        assert FAKE_KEY not in repr(provider) and FAKE_KEY not in str(vars(provider))
        outputs.append(json.dumps(generate_insights(report, provider, ledger=ledger)))
        assert fake.options["api_key"] == FAKE_KEY  # handed to the SDK, and only there
    captured = capsys.readouterr()
    for text in (*outputs, ledger.path.read_text(), captured.out, captured.err, caplog.text):
        assert FAKE_KEY not in text
        assert "sk-ant-" not in text


# --- CLI gates -----------------------------------------------------------------------


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(_env_file=None, ai_ledger_path=tmp_path / "ledger.jsonl", **overrides)


def run_cli(tmp_path, *extra, factory=None, **overrides) -> tuple[int, list]:
    built = []

    def provider_factory(_settings):
        built.append(True)
        return factory() if factory else MockProvider(payload="never reached")

    out = tmp_path / "artifact.json"
    code = cli.main(
        ["generate", "--report", str(SAMPLE), "--out", str(out), *extra],
        settings=settings(tmp_path, **overrides),
        provider_factory=provider_factory,
    )
    return code, built


def test_cli_defaults_to_offline_mock(tmp_path, capsys):
    code, built = run_cli(tmp_path)
    assert code == cli.EXIT_VALIDATED and built == []
    assert "Mock mode: offline, no API call, no charge." in capsys.readouterr().out
    assert not (tmp_path / "ledger.jsonl").exists()


@pytest.mark.parametrize(
    ("extra", "overrides", "message"),
    [
        ((), {}, "live calls are disabled"),
        (("--confirm-max-cost", "1.00"), {}, "live calls are disabled"),
        (("--confirm-max-cost", "1.00"), {"ai_live_calls_enabled": True}, "ANTHROPIC_API_KEY"),
        (
            (),
            {"ai_live_calls_enabled": True, "anthropic_api_key": FAKE_KEY},
            "--confirm-max-cost",
        ),
        (
            ("--confirm-max-cost", "0.01"),
            {"ai_live_calls_enabled": True, "anthropic_api_key": FAKE_KEY},
            "below this request's maximum",
        ),
        (
            ("--confirm-max-cost", "1.00"),
            {"ai_live_calls_enabled": True, "anthropic_api_key": FAKE_KEY, "ai_budget_usd": "0.10"},
            "not enough budget",
        ),
    ],
)
def test_cli_live_gates_block_before_any_provider_exists(
    tmp_path, capsys, extra, overrides, message
):
    code, built = run_cli(tmp_path, "--live", *extra, **overrides)
    assert code == cli.EXIT_BLOCKED and built == []
    err = capsys.readouterr().err
    assert message in err and FAKE_KEY not in err
    assert not (tmp_path / "artifact.json").exists()


def test_cli_blocks_when_the_ledger_needs_review(tmp_path, capsys):
    ledger = SpendLedger(tmp_path / "ledger.jsonl")
    ledger.reserve("old", "claude-opus-5-5", Decimal("0.01"))
    ledger.settle("old", Decimal("0.02"), TokenUsage(1, 1))
    code, built = run_cli(
        tmp_path, "--live", "--confirm-max-cost", "1.00",
        ai_live_calls_enabled=True, anthropic_api_key=FAKE_KEY,
    )  # fmt: skip
    assert code == cli.EXIT_BLOCKED and built == []
    assert "needs review" in capsys.readouterr().err


def test_cli_live_run_with_every_gate_passed_uses_the_ledger(tmp_path, capsys, package):
    code, built = run_cli(
        tmp_path, "--live", "--confirm-max-cost", "0.50",
        factory=lambda: live_mock(package, usage=TokenUsage(9_000, 3_000)),
        ai_live_calls_enabled=True, anthropic_api_key=FAKE_KEY,
    )  # fmt: skip
    assert code == cli.EXIT_VALIDATED and built == [True]
    out = capsys.readouterr().out
    assert "Live mode: one request, no retries." in out
    assert "not the account balance" in out and FAKE_KEY not in out
    assert SpendLedger(tmp_path / "ledger.jsonl").summary().settled_usd == Decimal("0.096")


def test_settings_default_to_offline():
    defaults = Settings(_env_file=None)
    assert defaults.ai_live_calls_enabled is False
    assert defaults.anthropic_api_key is None
    assert defaults.anthropic_model == "claude-opus-5-5"


# --- dashboard ---------------------------------------------------------------------------


def test_dashboard_uses_the_fallback_without_an_artifact(report):
    block = dashboard_insights(report, None)
    provenance = block["provenance"]
    assert provenance["source"] == "deterministic_fallback"
    assert provenance["is_ai_output"] is False and provenance["is_live_inference"] is False
    assert provenance["fallback_reason"] == "no AI artifact has been generated for this report"


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (lambda a: a["model_output"]["observed_facts"][0].update(statement="ROAS hit 99.99."),
         "artifact failed re-validation"),
        (lambda a: a.update(package_id="0" * 64), "artifact was generated from different evidence"),
        (lambda a: a.update(report_id="1" * 64), "artifact is for a different report"),
        (lambda a: a.update(outcome="fallback"), "artifact holds no validated model output"),
        (lambda a: a.update(artifact_schema_version="0.1"), "unsupported artifact version"),
    ],
)  # fmt: skip
def test_a_tampered_or_stale_artifact_falls_back(report, tamper, reason):
    artifact = json.loads(ARTIFACT.read_text())
    tamper(artifact)
    block = dashboard_insights(report, artifact)
    assert block["provenance"]["source"] == "deterministic_fallback"
    assert block["provenance"]["fallback_reason"] == reason
    assert "99.99" not in json.dumps(block)


def test_dashboard_data_never_presents_static_content_as_live_ai():
    data = json.loads((PROJECT_ROOT / "frontend" / "data" / "dashboard_data.json").read_text())
    text = json.dumps(data)
    assert "sk-ant-" not in text and "api_key" not in text.lower()
    assert data["meta"]["ai"]["browser_calls_anthropic"] is False
    assert data["meta"]["ai"]["manual_balance_snapshot"]["as_of"] == "2026-11-12"
    sources = set()
    for client in data["clients"]:
        provenance = client["ai_insights"]["provenance"]
        sources.add(provenance["source"])
        assert provenance["is_live_inference"] is False
        assert client["ai_insights"]["illustrative"] is (not provenance["is_ai_output"])
        for experiment in client["ai_insights"]["experiments"]:
            assert experiment["requires_human_approval"] and not experiment["executed"]
    assert sources == {"mock", "deterministic_fallback"}
    assert "ai_insights" in data["meta"]["illustrative_sections"]


def test_the_dashboard_builder_falls_back_for_an_empty_artifact_dir(tmp_path, report):
    spec = importlib.util.spec_from_file_location(
        "build_dashboard_data", PROJECT_ROOT / "frontend" / "tools" / "build_dashboard_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    block = module.ai_insights(report, tmp_path)
    assert block["provenance"]["source"] == "deterministic_fallback" and block["illustrative"]
