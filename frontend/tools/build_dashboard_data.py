"""Build the dashboard's data file from the real synthetic pipeline. No database needed.

Usage: uv run python frontend/tools/build_dashboard_data.py

Every figure in frontend/data/dashboard_data.json is derived here, deterministically, from
the same code paths the backend uses: the synthetic connectors fetch each account, and
``analyse_client`` builds each client's report and account breakdown. The legacy client's
report must come out as the committed sample report (feb1e176...), which this script
asserts. Daily series are computed from the records with the same rules as analytics:
delivery metrics are deduplicated per campaign-day, and conversions are only ever grouped
within one conversion basis.

Two sections cannot be derived without a database and are flagged ``"illustrative": true``
in the output: live sync state (shape shown from the data's own watermarks) and the
experiment workflow (deterministic drafts derived from findings; real AI proposals and
persistence arrive in Milestone 7). The frontend labels both.

AI insights come from a pre-generated artifact in docs/samples/ai_insights/ when one
exists for the report, re-validated here against a freshly built evidence package; else
from the deterministic fallback. Both mock and fallback output are flagged illustrative.
Nothing here calls the Anthropic API.

This script lives outside src/growthpulse_ai on purpose: it may import connectors and
pipeline code freely, which the dashboard package itself must never do.
"""

import hashlib
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from growthpulse_ai.accounts.legacy import LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.accounts.models import AccountContext, ClientContext
from growthpulse_ai.ai_analysis.pricing import PRICING_SOURCE, PRICING_VERSION
from growthpulse_ai.ai_analysis.service import dashboard_insights
from growthpulse_ai.analytics.kpis import ConversionBasis
from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.connectors.factory import SyntheticConnectorFactory
from growthpulse_ai.connectors.synthetic_data import (
    DEFAULT_CLIENT,
    DEFAULT_DAYS,
    DEFAULT_START_DATE,
    SYNTHETIC_CLIENTS,
)
from growthpulse_ai.pipeline.client_analysis import analyse_client
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

START = DEFAULT_START_DATE
END = DEFAULT_START_DATE + timedelta(days=DEFAULT_DAYS - 1)
M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"
OUTPUT = PROJECT_ROOT / "frontend" / "data" / "dashboard_data.json"
# Pre-generated insights artifacts, one per report ID. Each is re-validated on load.
AI_ARTIFACTS = PROJECT_ROOT / "docs" / "samples" / "ai_insights"


CENTS = Decimal("0.01")
PLATFORM_LABELS = {"google_ads": "Google Ads", "meta_ads": "Meta Ads"}
PLATFORM_SHORT = {"google_ads": "Google", "meta_ads": "Meta"}
STAGE_LABELS = {
    "lead": "Leads",
    "booking_request": "Booking requests",
    "confirmed_booking": "Confirmed bookings",
    "purchase": "Purchases",
    "other": "Conversions",
}

# Deterministic, advisory-only draft templates keyed by the finding rule that produced
# them. These are fixtures for the demonstration workflow, not AI output.
EXPERIMENT_TEMPLATES = {
    "cost_per_conversion_increase": (
        "Bid-strategy split test",
        "Rising cost per platform-attributed conversion on {campaign} may be auction "
        "pressure; a controlled bid-strategy test isolates whether a target-based "
        "strategy recovers efficiency.",
        "Run an A/B bid-strategy experiment on {campaign} for 14 days. A person reviews "
        "and applies any change manually.",
    ),
    "conversion_rate_decline": (
        "Landing-page variant test",
        "Conversion rate on {campaign} declined while delivery held steady, which points "
        "at the post-click experience rather than the auction.",
        "Test the current landing page against one variant for {campaign} traffic. A "
        "person reviews and applies any change manually.",
    ),
    "spend_without_conversion_growth": (
        "Budget reallocation test",
        "Spend on {campaign} grew without matching conversion growth; shifting a share of "
        "budget to stronger campaigns may hold volume at lower cost.",
        "Propose shifting 20% of {campaign} budget to the best performer for 14 days. A "
        "person reviews and applies any change manually.",
    ),
    "spend_with_zero_conversions": (
        "Tracking audit and pause test",
        "{campaign} spent with zero platform-attributed conversions, which is as likely a "
        "tracking outage as true non-performance.",
        "Audit conversion tracking for {campaign}; if intact, test a 7-day pause. A "
        "person reviews and applies any change manually.",
    ),
    "roas_deterioration": (
        "Creative refresh test",
        "ROAS on {campaign} deteriorated against its baseline; creative fatigue is the "
        "usual first suspect when delivery is stable.",
        "Test two fresh creatives against the incumbent on {campaign}. A person reviews "
        "and applies any change manually.",
    ),
    "strong_performer": (
        "Budget scaling test",
        "{campaign} sustains a low cost per platform-attributed conversion; a measured "
        "budget increase tests whether efficiency holds at higher volume.",
        "Increase {campaign} budget by 15% for 14 days and watch cost per conversion. A "
        "person reviews and applies any change manually.",
    ),
}


def stable_uuid(*parts: str):
    return uuid5(NAMESPACE_URL, "growthpulse-dashboard:" + ":".join(parts))


def client_context(spec) -> ClientContext:
    """The legacy client keeps its fixed IDs; other fictional clients get stable ones."""
    if spec is DEFAULT_CLIENT:
        return LEGACY_SYNTHETIC_CLIENT
    client_id = stable_uuid("client", spec.key)
    return ClientContext(
        client_id=client_id,
        name=spec.name,
        data_source=DataSource.SYNTHETIC,
        accounts=tuple(
            AccountContext(
                ad_account_id=stable_uuid("account", account.platform.value, account.account_id),
                client_id=client_id,
                data_source=DataSource.SYNTHETIC,
                platform=account.platform,
                external_account_id=account.account_id,
                currency=account.currency,
                timezone=account.timezone,
                display_name=f"{spec.name} {PLATFORM_LABELS[account.platform.value]}",
            )
            for account in spec.accounts
        ),
    )


def account_name(account: AccountContext) -> str:
    return f"{PLATFORM_LABELS[account.platform.value]} · {account.external_account_id}"


def money(value: Decimal) -> str:
    return str(value.quantize(CENTS))


def basis_label(basis: ConversionBasis) -> str:
    return f"{STAGE_LABELS[basis.stage]} · {PLATFORM_SHORT[basis.platform]}"


def daily_series(records: list[AdPerformanceRecord]) -> dict:
    """Per-day delivery (deduplicated per campaign-day) and per-basis conversions."""
    delivery: dict[tuple, dict[str, Decimal | int]] = defaultdict(
        lambda: {"spend": Decimal(0), "clicks": 0, "impressions": 0}
    )
    conversions: dict[tuple, Decimal] = defaultdict(Decimal)
    bases: dict[str, ConversionBasis] = {}
    for record in {r.delivery_key: r for r in records}.values():
        key = (record.account_id, record.platform.value, record.reporting_date)
        delivery[key]["spend"] += record.spend
        delivery[key]["clicks"] += record.clicks
        delivery[key]["impressions"] += record.impressions
    for record in records:
        basis = ConversionBasis.from_record(record)
        bases[basis.basis_id] = basis
        conversions[(record.account_id, basis.basis_id, record.reporting_date)] += (
            record.conversions
        )
    return {
        "delivery": [
            {
                "account_id": account,
                "platform": platform,
                "date": day.isoformat(),
                "spend": money(values["spend"]),
                "clicks": values["clicks"],
                "impressions": values["impressions"],
            }
            for (account, platform, day), values in sorted(delivery.items())
        ],
        "conversions": [
            {
                "account_id": account,
                "basis_id": basis_id,
                "date": day.isoformat(),
                "conversions": str(value.quantize(CENTS)),
            }
            for (account, basis_id, day), value in sorted(conversions.items())
        ],
        "bases": {
            basis_id: {
                "label": basis_label(basis),
                "platform": basis.platform,
                "stage": basis.stage,
                "event_name": basis.event_name,
                "attribution_model": basis.attribution_model,
            }
            for basis_id, basis in sorted(bases.items())
        },
    }


def metric_changes(change_list: list[dict]) -> dict:
    return {
        change["metric"]: {
            "baseline": change["baseline"],
            "current": change["current"],
            "percent_change": change["percent_change"],
        }
        for change in change_list
    }


def comparison_summary(comparison: dict) -> dict:
    return {
        "label": comparison["label"],
        "purpose": comparison["purpose"],
        "baseline_period": comparison["baseline_period"],
        "current_period": comparison["current_period"],
        "portfolios": [
            {
                "currency": portfolio["currency"],
                "delivery": metric_changes(portfolio["changes"]["delivery"]),
                "bases": {
                    basis_id: metric_changes(changes)
                    for basis_id, changes in portfolio["changes"]["conversions_by_basis"].items()
                },
            }
            for portfolio in comparison["portfolios"]
        ],
    }


def finding_summary(finding: dict, comparison: dict) -> dict:
    keep = (
        "finding_id", "rule_id", "severity", "pre_guard_severity", "direction", "metric",
        "metric_label", "percent_change", "absolute_change", "baseline_value", "current_value",
        "baseline_period", "current_period", "platform", "account_id", "campaign_id",
        "campaign_name", "currency", "observation",
    )  # fmt: skip
    summary = {key: finding[key] for key in keep}
    summary["comparison_label"] = comparison["label"]
    summary["comparison_purpose"] = comparison["purpose"]
    summary["noise_guard_outcome"] = finding["noise_guard"]["outcome"]
    # The numbers behind the change, exactly as the report states them.
    summary["evidence"] = finding["evidence"]
    summary["conversion_basis_id"] = finding["conversion_basis"]["basis_id"]
    return summary


def experiment_drafts(findings: list[dict]) -> list[dict]:
    """Deterministic demonstration drafts derived from the strongest findings."""
    ranked = sorted(
        findings,
        key=lambda f: ({"high": 0, "medium": 1, "info": 2}[f["severity"]], f["finding_id"]),
    )
    drafts = []
    for finding in ranked:
        template = EXPERIMENT_TEMPLATES.get(finding["rule_id"])
        if template is None:
            continue
        title, hypothesis, change = template
        campaign = finding["campaign_name"].removeprefix("[SYNTHETIC] ")
        drafts.append(
            {
                "draft_id": hashlib.sha256(
                    ("dashboard-draft:" + finding["finding_id"]).encode()
                ).hexdigest()[:16],
                "title": f"{title} — {campaign}",
                "hypothesis": hypothesis.format(campaign=campaign),
                "proposed_change": change.format(campaign=campaign),
                "primary_metric": finding["metric"],
                "severity": finding["severity"],
                "direction": finding["direction"],
                "derived_from_finding": finding["finding_id"],
                "campaign_name": finding["campaign_name"],
                "platform": finding["platform"],
                "status": "draft",
                "requires_human_approval": True,
                "illustrative": True,
            }
        )
    return drafts[:6]


def sync_panel(client: ClientContext, records: list[AdPerformanceRecord]) -> dict:
    """The shape of live sync state, derived from the data itself. Flagged illustrative."""
    by_account: dict[str, list[date]] = defaultdict(list)
    for record in records:
        by_account[record.account_id].append(record.reporting_date)
    return {
        "illustrative": True,
        "note": (
            "No database is connected in this demo build. Watermarks are derived from the "
            "synthetic dataset; statuses show the shape of live sync state."
        ),
        "accounts": [
            {
                "external_account_id": account.external_account_id,
                "platform": account.platform.value,
                "display_name": account_name(account),
                "currency": account.currency,
                "timezone": account.timezone,
                "account_status": "active",
                "sync_status": "idle",
                "frequency": "manual",
                "backfill_start": min(by_account[account.external_account_id]).isoformat(),
                "synced_through": max(by_account[account.external_account_id]).isoformat(),
                "consecutive_failures": 0,
            }
            for account in client.accounts
        ],
    }


def ai_insights(report: dict, artifact_dir=AI_ARTIFACTS) -> dict:
    """Validated stored insights for this report if any exist, else the fallback."""
    path = artifact_dir / f"{report['report_id']}.json"
    artifact = json.loads(path.read_text()) if path.exists() else None
    block = dashboard_insights(report, artifact)
    block["illustrative"] = not block["provenance"]["is_ai_output"]
    return block


def build_client(spec, artifact_dir=AI_ARTIFACTS) -> dict:
    client = client_context(spec)
    factory = SyntheticConnectorFactory()
    records: list[AdPerformanceRecord] = []
    for account in client.accounts:
        records.extend(factory.for_account(account).fetch(START, END).records)
    analysis = analyse_client(client, records)
    report, breakdown = analysis.report, analysis.breakdown

    active = [
        finding_summary(finding, comparison)
        for comparison in report["comparisons"]
        for finding in comparison["findings"]
    ]
    suppressed = [
        {
            "metric_label": finding["metric_label"],
            "campaign_name": finding["campaign_name"],
            "pre_guard_severity": finding["pre_guard_severity"],
            "noise_guard_outcome": finding["noise_guard"]["outcome"],
            "comparison_label": comparison["label"],
        }
        for comparison in report["comparisons"]
        for finding in comparison["suppressed_findings"]
    ]

    return {
        "id": str(client.client_id),
        "key": spec.key,
        "name": client.name,
        "accounts": [
            {
                "external_account_id": account.external_account_id,
                "platform": account.platform.value,
                "platform_label": PLATFORM_LABELS[account.platform.value],
                "display_name": account_name(account),
                "currency": account.currency,
                "timezone": account.timezone,
                "status": "active",
            }
            for account in client.accounts
        ],
        "report": {
            "report_id": report["report_id"],
            "schema_version": report["report_schema_version"],
            "data_as_of": report["data_as_of"],
            "period": report["full_period"]["period"],
            "record_count": report["input"]["record_count"],
            "data_quality": report["data_quality"],
            "limitations": report["limitations"],
        },
        "portfolios": report["full_period"]["portfolios"],
        "campaigns": report["full_period"]["campaigns"],
        "comparisons": [comparison_summary(c) for c in report["comparisons"]],
        "findings": active,
        "suppressed_findings": suppressed,
        "breakdown": {
            "breakdown_id": breakdown["breakdown_id"],
            "accounts": breakdown["accounts"],
            "compatibility": breakdown["compatibility"],
            "notes": breakdown["notes"],
        },
        "daily": daily_series(records),
        "sync": sync_panel(client, records),
        "history": [
            {
                "report_id": report["report_id"],
                "data_as_of": report["data_as_of"],
                "period": report["full_period"]["period"],
                "record_count": report["input"]["record_count"],
                "schema_version": report["report_schema_version"],
                "findings_active": len(active),
                "findings_suppressed": len(suppressed),
                "breakdown_id": breakdown["breakdown_id"],
            }
        ],
        "experiments": {
            "illustrative": True,
            "note": (
                "Drafts are deterministic templates derived from the findings, not AI "
                "output. AI-generated proposals and persisted approvals arrive with "
                "Milestone 7."
            ),
            "drafts": experiment_drafts(active),
        },
        "ai_insights": ai_insights(report, artifact_dir),
    }


def build(artifact_dir=AI_ARTIFACTS) -> dict:
    clients = [build_client(spec, artifact_dir) for spec in SYNTHETIC_CLIENTS]
    legacy = clients[0]
    if legacy["report"]["report_id"] != M4_REPORT_ID:
        raise RuntimeError("The legacy client's report no longer matches the sample report")
    return {
        "meta": {
            "product": "GrowthPulse AI",
            "generator": "frontend/tools/build_dashboard_data.py",
            "data_source": "synthetic",
            "synthetic": True,
            "period": {"start": START.isoformat(), "end": END.isoformat()},
            "note": (
                "Every figure derives from the deterministic synthetic dataset through the "
                "real analytics pipeline. Sections flagged illustrative are demonstration "
                "fixtures, not computed results."
            ),
            "illustrative_sections": ["sync", "experiments"]
            + (["ai_insights"] if any(c["ai_insights"]["illustrative"] for c in clients) else []),
            "report_ids": {client["key"]: client["report"]["report_id"] for client in clients},
            "ai": {
                "budget_usd": "1.00",
                "budget_note": (
                    "Development spending is capped at $1.00 and checked before every live "
                    "call. Generation happens offline, never in the browser."
                ),
                "pricing_version": PRICING_VERSION,
                "pricing_source": PRICING_SOURCE,
                "browser_calls_anthropic": False,
            },
        },
        "clients": clients,
    }


def main() -> int:
    payload = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"Wrote {OUTPUT.relative_to(PROJECT_ROOT)} ({OUTPUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
