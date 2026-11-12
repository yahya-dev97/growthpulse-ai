"""Generate AI insights artifacts from an analytics report, offline by default.

    uv run python -m growthpulse_ai.ai_analysis estimate --report REPORT.json
    uv run python -m growthpulse_ai.ai_analysis generate --report REPORT.json --out OUT.json
    uv run python -m growthpulse_ai.ai_analysis generate --report REPORT.json --out OUT.json \\
        --live --confirm-max-cost 0.43
    uv run python -m growthpulse_ai.ai_analysis ledger

Without --live, the offline mock provider is used and nothing leaves the machine. A live
call needs every gate: --live, AI_LIVE_CALLS_ENABLED=true, ANTHROPIC_API_KEY set in the
environment, --confirm-max-cost at least the computed maximum, and a readable ledger with
room for the reservation. One request per run, never retried.
"""

import argparse
import json
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..config import Settings, get_settings
from .evidence import EvidenceError, build_evidence_package
from .fallback import deterministic_response
from .ledger import LedgerError, SpendLedger
from .providers import AnthropicProvider, InsightProvider, MockProvider, ProviderError
from .service import InsightConfig, build_request, generate_insights, request_max_cost

EXIT_VALIDATED, EXIT_BLOCKED, EXIT_FALLBACK = 0, 2, 3


class GateError(RuntimeError):
    """A live-call precondition failed. Nothing was sent."""


def _load_report(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read the report ({type(error).__name__})") from None


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a dollar amount such as 0.45") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("must be a positive dollar amount")
    return amount


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m growthpulse_ai.ai_analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    estimate = commands.add_parser("estimate", help="show the maximum cost of one request")
    estimate.add_argument("--report", type=Path, required=True)
    generate = commands.add_parser("generate", help="write an insights artifact")
    generate.add_argument("--report", type=Path, required=True)
    generate.add_argument("--out", type=Path, required=True)
    generate.add_argument("--live", action="store_true", help="make one paid Anthropic call")
    generate.add_argument("--confirm-max-cost", type=_money, metavar="USD")
    commands.add_parser("ledger", help="show the local spend ledger")
    return parser


def live_provider(
    settings: Settings,
    ledger: SpendLedger,
    max_cost: Decimal,
    confirmed: Decimal | None,
    factory: Callable[[Settings], InsightProvider] | None = None,
) -> InsightProvider:
    """Check every live-call gate, in order, then build the provider."""
    if not settings.ai_live_calls_enabled:
        raise GateError("live calls are disabled: set AI_LIVE_CALLS_ENABLED=true to opt in")
    if settings.anthropic_api_key is None:
        raise GateError("ANTHROPIC_API_KEY is not set in the environment")
    if confirmed is None:
        raise GateError(f"pass --confirm-max-cost {max_cost} to accept the maximum cost")
    if confirmed < max_cost:
        raise GateError(f"--confirm-max-cost is below this request's maximum (${max_cost})")
    try:
        summary = ledger.summary()
    except LedgerError as error:
        raise GateError(f"the spend ledger cannot be used ({type(error).__name__})") from None
    if summary.needs_review:
        raise GateError("the spend ledger needs review before any further live call")
    if summary.committed_usd + max_cost > summary.budget_usd:
        raise GateError(
            f"not enough budget: ${summary.remaining_usd} left, this request may cost ${max_cost}"
        )
    if factory is not None:
        return factory(settings)
    try:
        return AnthropicProvider(settings.anthropic_api_key)
    except ProviderError as error:
        raise GateError(str(error)) from None


def main(
    argv: list[str] | None = None,
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], InsightProvider] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    settings = settings or get_settings()
    ledger = SpendLedger(settings.ai_ledger_path, settings.ai_budget_usd)
    try:
        if args.command == "ledger":
            print(json.dumps(ledger.summary().as_dict(), indent=2))
            return EXIT_VALIDATED
        report = _load_report(args.report)
        package = build_evidence_package(report)
        config = InsightConfig.from_settings(settings)
        max_input, max_cost = request_max_cost(build_request(package, config))
        print(
            f"Report {package.report_id[:12]}…  evidence {package.package_id[:12]}…  "
            f"{len(package.content['findings'])} findings\n"
            f"Model {config.model}, effort {config.effort}. Maximum for one request: "
            f"{max_input:,} input + {config.max_output_tokens:,} output tokens = ${max_cost}"
        )
        if args.command == "estimate":
            return EXIT_VALIDATED

        if args.live:
            provider = live_provider(
                settings, ledger, max_cost, args.confirm_max_cost, provider_factory
            )
            print("Live mode: one request, no retries.")
        else:
            provider = MockProvider(payload=lambda _request: deterministic_response(package))
            print("Mock mode: offline, no API call, no charge.")
        artifact = generate_insights(
            report, provider, config, ledger=ledger if provider.live else None
        )
    except (GateError, EvidenceError, LedgerError) as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return EXIT_BLOCKED

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cost = artifact["cost"]
    estimated = cost["estimated_usd"]
    print(f"Outcome: {artifact['outcome']}. Cost: {cost['status']}, estimated ${estimated}")
    for violation in artifact["validation"]["violations"][:10]:
        print(f"  rejected: {violation['check']} at {violation['path']}: {violation['detail']}")
    if cost["ledger_after"]:
        ledger_view = cost["ledger_after"]
        print(
            f"Ledger: ${ledger_view['committed_usd']} committed of ${ledger_view['budget_usd']}, "
            f"${ledger_view['remaining_usd']} left (local estimate, not the account balance)"
        )
    print(f"Wrote {args.out}")
    return EXIT_VALIDATED if artifact["outcome"] == "validated" else EXIT_FALLBACK


if __name__ == "__main__":
    sys.exit(main())
