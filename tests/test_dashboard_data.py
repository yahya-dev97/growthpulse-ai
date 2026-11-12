"""The dashboard's data file: reproducible, consistent with the reports, honestly labelled.

The dashboard (frontend/) reads one generated file. These checks run offline and make sure
it is exactly what the real pipeline produces today, so no figure on screen is invented.
"""

import importlib.util
import json
from collections import defaultdict
from decimal import Decimal

import pytest

from growthpulse_ai.config import PROJECT_ROOT

GENERATOR = PROJECT_ROOT / "frontend" / "tools" / "build_dashboard_data.py"
DATA = PROJECT_ROOT / "frontend" / "data" / "dashboard_data.json"
M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"


def generator():
    spec = importlib.util.spec_from_file_location("build_dashboard_data", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(DATA.read_text())


def test_committed_file_is_exactly_what_the_pipeline_produces_now(data):
    regenerated = json.loads(json.dumps(generator().build(), sort_keys=True))
    assert data == regenerated, "Run: uv run python frontend/tools/build_dashboard_data.py"


def test_legacy_client_shows_the_milestone_4_report(data):
    legacy = data["clients"][0]
    assert legacy["key"] == "examplescan-uk"
    assert legacy["report"]["report_id"] == M4_REPORT_ID
    assert legacy["report"]["record_count"] == 300
    assert len(legacy["findings"]) + len(legacy["suppressed_findings"]) == 19


def test_everything_is_synthetic_and_illustrative_sections_are_flagged(data):
    assert data["meta"]["synthetic"] is True and data["meta"]["data_source"] == "synthetic"
    # ai_insights is illustrative until a validated Claude artifact exists for every client.
    assert set(data["meta"]["illustrative_sections"]) == {"sync", "experiments", "ai_insights"}
    for client in data["clients"]:
        assert client["name"].endswith("(fictional)")
        assert all(a["external_account_id"].startswith("synthetic-") for a in client["accounts"])
        assert client["sync"]["illustrative"] is True
        assert client["experiments"]["illustrative"] is True
        for draft in client["experiments"]["drafts"]:
            assert draft["illustrative"] and draft["requires_human_approval"]
            assert draft["status"] == "draft"
            assert "[SYNTHETIC]" not in draft["hypothesis"]


def test_daily_spend_adds_up_to_each_currency_portfolio(data):
    for client in data["clients"]:
        currency_of = {a["external_account_id"]: a["currency"] for a in client["accounts"]}
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for row in client["daily"]["delivery"]:
            totals[currency_of[row["account_id"]]] += Decimal(row["spend"])
        for portfolio in client["portfolios"]:
            assert totals[portfolio["currency"]] == Decimal(portfolio["delivery"]["spend"])


def test_daily_conversions_add_up_within_each_currency_and_basis(data):
    """Each portfolio's conversions, per basis, equal its accounts' daily rows. A basis
    shared by accounts in different currencies stays in separate portfolios."""
    for client in data["clients"]:
        currency_of = {a["external_account_id"]: a["currency"] for a in client["accounts"]}
        totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for row in client["daily"]["conversions"]:
            totals[(currency_of[row["account_id"]], row["basis_id"])] += Decimal(row["conversions"])
        reported = {
            (portfolio["currency"], basis["basis"]["basis_id"]): Decimal(
                basis["platform_attributed_conversions"]
            )
            for portfolio in client["portfolios"]
            for basis in portfolio["conversions_by_basis"]
        }
        assert totals == reported
        assert {basis for _, basis in totals} == set(client["daily"]["bases"])


def test_findings_and_breakdowns_stay_inside_their_client(data):
    for client in data["clients"]:
        own = {a["external_account_id"] for a in client["accounts"]}
        assert {f["account_id"] for f in client["findings"]} <= own
        assert {a["account_id"] for a in client["breakdown"]["accounts"]} == own
        draft_sources = {d["derived_from_finding"] for d in client["experiments"]["drafts"]}
        assert draft_sources <= {f["finding_id"] for f in client["findings"]}


def test_multi_currency_client_is_never_summed(data):
    europe = next(c for c in data["clients"] if c["key"] == "examplescan-eu")
    assert sorted(p["currency"] for p in europe["portfolios"]) == ["EUR", "GBP", "USD"]
    assert europe["breakdown"]["compatibility"]["mixed_currencies"] is True
