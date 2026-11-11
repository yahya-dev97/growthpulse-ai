"""Multi-client compatibility: Milestone 4 behaviour is unchanged, and clients stay apart."""

import hashlib
import json
from collections import Counter

import pytest

from growthpulse_ai.accounts.assignment import assign_findings, assign_records, report_findings
from growthpulse_ai.accounts.legacy import (
    LEGACY_CLIENT_ID,
    LEGACY_EXTERNAL_ACCOUNT_IDS,
    LEGACY_GOOGLE_ACCOUNT_ID,
    LEGACY_META_ACCOUNT_ID,
    LEGACY_SYNTHETIC_CLIENT,
)
from growthpulse_ai.accounts.models import (
    AccountContext,
    AccountMismatchError,
    check_unique_assignment,
)
from growthpulse_ai.analytics.breakdown import build_account_breakdown
from growthpulse_ai.analytics.report import build_report, dumps_report
from growthpulse_ai.connectors import synthetic_data
from growthpulse_ai.connectors.factory import SyntheticConnectorFactory
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import (
    DEFAULT_CLIENT,
    SYNTHETIC_CLIENTS,
    SyntheticAccountSpec,
    SyntheticClientSpec,
    generate_synthetic_rows,
)
from growthpulse_ai.data_quality import IssueCode, combine_summaries, window_quality_summary
from growthpulse_ai.pipeline.client_analysis import analyse_client
from growthpulse_ai.pipeline.sample_report import SAMPLE_REPORT_PATH, build_sample_report
from growthpulse_ai.schema import DataSource, Platform
from multi_client import (
    CLIENTS,
    END,
    START,
    client_context,
    stable_uuid,
    sync_client,
    sync_summaries,
)

# Fingerprints of the Milestone 4 dataset and report, recorded before Milestone 5 changes.
M4_ROWS_SHA256 = "fcf86a71e6d1f9a1776863b8443a0e4bc809ef3dc5fdba1f423683a78fabf45f"
M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"


def combined():
    return SyntheticConnector().fetch(START, END)


# Milestone 4 compatibility


def test_default_synthetic_rows_are_byte_identical_to_milestone_4():
    rows = generate_synthetic_rows()
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    assert digest == M4_ROWS_SHA256


def test_sample_report_and_its_id_are_unchanged():
    report = build_sample_report()
    assert report["report_id"] == M4_REPORT_ID
    assert dumps_report(report) == SAMPLE_REPORT_PATH.read_text(encoding="utf-8")


def test_legacy_client_matches_the_synthetic_generator():
    assert LEGACY_EXTERNAL_ACCOUNT_IDS == synthetic_data.ACCOUNT_IDS
    for account in LEGACY_SYNTHETIC_CLIENT.accounts:
        assert (account.currency, account.timezone) == (
            synthetic_data.CURRENCY,
            synthetic_data.TIMEZONE,
        )
    assert LEGACY_SYNTHETIC_CLIENT.name == DEFAULT_CLIENT.name


def test_every_milestone_4_record_maps_to_exactly_one_legacy_account():
    records = combined().records
    assigned = assign_records(LEGACY_SYNTHETIC_CLIENT, records)
    assert {key: len(rows) for key, rows in assigned.items()} == {
        LEGACY_GOOGLE_ACCOUNT_ID: 210,
        LEGACY_META_ACCOUNT_ID: 90,
    }
    mapped = [record for rows in assigned.values() for record in rows]
    assert sorted(r.record_id for r in mapped) == sorted(r.record_id for r in records)
    # Each identity names one account only: the mapping cannot be ambiguous.
    identities = Counter(a.identity for a in LEGACY_SYNTHETIC_CLIENT.accounts)
    assert max(identities.values()) == 1


def test_every_milestone_4_finding_maps_to_one_legacy_account_without_changing_ids():
    report = build_sample_report()
    assigned = assign_findings(LEGACY_SYNTHETIC_CLIENT, report)
    assert set(assigned) == {finding["finding_id"] for finding in report_findings(report)}
    assert len(assigned) == 19
    assert set(assigned.values()) <= {LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID}
    # Assignment reads the report; it never rewrites it.
    assert report["report_id"] == M4_REPORT_ID


def test_independent_account_syncs_add_up_to_the_combined_fetch():
    whole = combined()
    records, evidence = sync_client(LEGACY_SYNTHETIC_CLIENT)
    assert sorted(r.record_id for r in records) == sorted(r.record_id for r in whole.records)
    assert combine_summaries(sync_summaries(LEGACY_SYNTHETIC_CLIENT).values()) == whole.summary
    # Recomputed from the records and evidence alone, quality matches the combined fetch.
    assert window_quality_summary(records, evidence) == whole.summary


def test_client_analysis_from_account_syncs_reproduces_the_milestone_4_report():
    records, summaries = sync_client(LEGACY_SYNTHETIC_CLIENT)
    records.reverse()
    analysis = analyse_client(LEGACY_SYNTHETIC_CLIENT, records, summaries)
    assert analysis.report == build_sample_report()
    assert analysis.report["report_id"] == M4_REPORT_ID
    assert analysis.client_id == LEGACY_CLIENT_ID
    assert set(analysis.ad_account_ids) == {LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID}
    assert len(analysis.finding_accounts) == 19


# Account-level breakdown


def test_breakdown_reports_each_account_separately_and_leaves_the_report_alone():
    records, summaries = sync_client(LEGACY_SYNTHETIC_CLIENT)
    analysis = analyse_client(LEGACY_SYNTHETIC_CLIENT, records, summaries)
    breakdown = analysis.breakdown
    assert breakdown["report_id"] == M4_REPORT_ID
    assert [(a["platform"], a["record_count"]) for a in breakdown["accounts"]] == [
        ("google_ads", 210),
        ("meta_ads", 90),
    ]
    compatibility = breakdown["compatibility"]
    assert compatibility["currencies"] == ["GBP"]
    assert not compatibility["mixed_currencies"]
    assert compatibility["multiple_conversion_bases"]
    assert compatibility["mixed_attribution_settings"]
    assert "breakdown" not in analysis.report


def test_breakdown_is_deterministic_and_order_independent():
    records = list(combined().records)
    first = build_account_breakdown(records, report_id=M4_REPORT_ID)
    records.reverse()
    second = build_account_breakdown(records, report_id=M4_REPORT_ID)
    assert first == second
    assert len(first["breakdown_id"]) == 64


def test_breakdown_never_totals_conversions_across_bases():
    records, _ = sync_client(LEGACY_SYNTHETIC_CLIENT)
    breakdown = build_account_breakdown(records, report_id=M4_REPORT_ID)
    meta = next(a for a in breakdown["accounts"] if a["platform"] == "meta_ads")
    bases = meta["kpis"]["conversions_by_basis"]
    # Meta's lead and booking-request events stay separate rather than being summed.
    assert {b["basis"]["event_id"] for b in bases} == {
        "synthetic-meta-enquiry-lead",
        "synthetic-meta-booking-request",
    }
    assert "platform_attributed_conversions" not in meta["kpis"]["delivery"]


# Several clients


def test_synthetic_clients_never_share_an_account():
    check_unique_assignment(CLIENTS)
    identities = [a.identity for client in CLIENTS for a in client.accounts]
    assert len(identities) == len(set(identities))


def test_clients_produce_disjoint_records_findings_and_reports():
    record_ids, finding_ids, report_ids = [], [], []
    for client in CLIENTS:
        records, summaries = sync_client(client)
        analysis = analyse_client(client, records, summaries)
        record_ids.append({r.record_id for r in records})
        finding_ids.append(set(analysis.finding_accounts))
        report_ids.append(analysis.report["report_id"])
    for i in range(len(CLIENTS)):
        for j in range(i + 1, len(CLIENTS)):
            assert not record_ids[i] & record_ids[j]
            assert not finding_ids[i] & finding_ids[j]
    assert len(set(report_ids)) == len(CLIENTS)


def test_a_reused_campaign_id_stays_apart_across_clients():
    uk, ireland = CLIENTS[0], CLIENTS[1]
    uk_records, _ = sync_client(uk)
    ie_records, _ = sync_client(ireland)
    shared = {r.campaign_id for r in uk_records} & {r.campaign_id for r in ie_records}
    assert "synthetic-gs-ct-national" in shared
    assert not {r.record_id for r in uk_records} & {r.record_id for r in ie_records}


def test_analysis_refuses_another_clients_records():
    uk_records, _ = sync_client(CLIENTS[0])
    ie_records, ie_summaries = sync_client(CLIENTS[1])
    with pytest.raises(AccountMismatchError):
        analyse_client(CLIENTS[1], [*ie_records, uk_records[0]], ie_summaries)


def test_analysis_refuses_evidence_from_another_client():
    records, evidence = sync_client(CLIENTS[1])
    foreign = [{"quarantine_id": "0" * 64, "ad_account_id": str(LEGACY_GOOGLE_ACCOUNT_ID),
                "reporting_date": "2026-10-05", "rows": 1, "errors_by_code": {"x": 1}}]  # fmt: skip
    with pytest.raises(AccountMismatchError):
        analyse_client(CLIENTS[1], records, [*evidence, *foreign])


def test_report_quality_comes_from_the_analysed_records_not_how_they_were_synced():
    records, _ = sync_client(LEGACY_SYNTHETIC_CLIENT)
    first_half = [r for r in records if r.reporting_date.day <= 15]
    second_half = [r for r in records if r.reporting_date.day > 15]
    # The same records in any grouping or order give the same report.
    one = analyse_client(LEGACY_SYNTHETIC_CLIENT, [*second_half, *first_half])
    two = analyse_client(LEGACY_SYNTHETIC_CLIENT, [*first_half, *second_half])
    assert one.report == two.report == build_sample_report()


def test_mixed_currency_client_keeps_every_total_in_one_currency():
    europe = CLIENTS[2]
    records, summaries = sync_client(europe)
    analysis = analyse_client(europe, records, summaries)
    portfolios = analysis.report["full_period"]["portfolios"]
    assert sorted(p["currency"] for p in portfolios) == ["EUR", "GBP", "USD"]
    compatibility = analysis.breakdown["compatibility"]
    assert compatibility["mixed_currencies"] and compatibility["mixed_timezones"]
    assert compatibility["account_timezones"] == [
        "America/New_York",
        "Europe/Berlin",
        "Europe/London",
    ]
    for account in analysis.breakdown["accounts"]:
        assert account["kpis"]["currency"] == account["currency"]


def test_two_accounts_on_one_platform_are_reported_separately():
    europe = CLIENTS[2]
    records, _ = sync_client(europe)
    breakdown = build_account_breakdown(records, report_id="0" * 64)
    google = [a for a in breakdown["accounts"] if a["platform"] == "google_ads"]
    assert [(a["account_id"], a["currency"]) for a in google] == [
        ("synthetic-google-account-003", "GBP"),
        ("synthetic-google-account-004", "EUR"),
    ]


def test_every_synthetic_client_is_analysable():
    for client in CLIENTS:
        records, evidence = sync_client(client)
        report = analyse_client(client, records, evidence).report
        summaries = sync_summaries(client).values()
        assert report == build_report(records, data_quality=combine_summaries(summaries))


# Account guard and factory


def test_account_guard_quarantines_rows_from_any_other_account():
    target = CLIENTS[1].accounts[0]
    rows = [
        *generate_synthetic_rows(client=SYNTHETIC_CLIENTS[1]),
        *generate_synthetic_rows(),
    ]
    result = SyntheticConnector(rows=rows, account=target).fetch(START, END)
    assert {(r.platform, r.account_id) for r in result.records} == {
        (target.platform, target.external_account_id)
    }
    foreign = len(rows) - len(result.records)
    assert result.summary["errors_by_code"] == {IssueCode.ACCOUNT_MISMATCH.value: foreign}
    assert all(
        issue.code is IssueCode.ACCOUNT_MISMATCH
        for row in result.quarantined
        for issue in row.issues
    )


def test_connector_without_an_account_keeps_milestone_4_behaviour():
    result = SyntheticConnector().fetch(START, END)
    assert len(result.records) == 300
    assert result.quarantined == ()


def test_factory_returns_a_connector_bound_to_the_account():
    account = CLIENTS[2].accounts[1]
    connector = SyntheticConnectorFactory().for_account(account)
    assert connector.account == account
    records = connector.fetch(START, END).records
    assert records and {r.account_id for r in records} == {account.external_account_id}


def test_factory_refuses_live_and_unknown_accounts():
    base = CLIENTS[1].accounts[0]
    factory = SyntheticConnectorFactory()
    live = AccountContext(
        ad_account_id=base.ad_account_id,
        client_id=base.client_id,
        data_source=DataSource.LIVE,
        platform=base.platform,
        external_account_id="1234567890",
        currency="EUR",
        timezone="Europe/Dublin",
        connection_id=stable_uuid("connection"),
    )
    with pytest.raises(ValueError, match="live connector"):
        factory.for_account(live)
    unknown = AccountContext(
        ad_account_id=base.ad_account_id,
        client_id=base.client_id,
        data_source=DataSource.SYNTHETIC,
        platform=base.platform,
        external_account_id="synthetic-google-account-999",
        currency="EUR",
        timezone="Europe/Dublin",
    )
    with pytest.raises(ValueError, match="No synthetic data"):
        factory.for_account(unknown)


def test_factory_refuses_an_account_whose_currency_differs_from_its_data():
    base = CLIENTS[1].accounts[0]
    drifted = AccountContext(
        ad_account_id=base.ad_account_id,
        client_id=base.client_id,
        data_source=DataSource.SYNTHETIC,
        platform=base.platform,
        external_account_id=base.external_account_id,
        currency="GBP",
        timezone=base.timezone,
    )
    with pytest.raises(ValueError, match="currency or timezone"):
        SyntheticConnectorFactory().for_account(drifted)


def test_factory_refuses_an_account_shared_by_two_synthetic_clients():
    with pytest.raises(ValueError, match="two synthetic clients"):
        SyntheticConnectorFactory([DEFAULT_CLIENT, DEFAULT_CLIENT])


def test_connector_refuses_an_account_from_another_data_source():
    live = AccountContext(
        ad_account_id=stable_uuid("a"),
        client_id=stable_uuid("c"),
        data_source=DataSource.LIVE,
        platform=Platform.META_ADS,
        external_account_id="act_1",
        currency="GBP",
        timezone="Europe/London",
        connection_id=stable_uuid("conn"),
    )
    with pytest.raises(ValueError, match="data source"):
        SyntheticConnector(account=live)


# Synthetic client specs


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "Real Clinic Ltd"}, "fictional"),
        (
            {
                "accounts": (
                    SyntheticAccountSpec(
                        Platform.GOOGLE_ADS, "acct-1", "GBP", "Europe/London", ("gs-ct-national",)
                    ),
                )
            },
            "synthetic-",
        ),
        (
            {
                "accounts": (
                    SyntheticAccountSpec(
                        Platform.META_ADS,
                        "synthetic-x",
                        "GBP",
                        "Europe/London",
                        ("gs-ct-national",),
                    ),
                )
            },
            "platform",
        ),
        (
            {
                "accounts": (
                    SyntheticAccountSpec(
                        Platform.GOOGLE_ADS,
                        "synthetic-x",
                        "GBP",
                        "Europe/London",
                        ("gs-ct-national", "gs-ct-national"),
                    ),
                )
            },
            "only one account",
        ),
        (
            {
                "accounts": (
                    SyntheticAccountSpec(
                        Platform.GOOGLE_ADS, "synthetic-x", "GBP", "Europe/London", ("nope",)
                    ),
                )
            },
            "Unknown",
        ),
        ({"scenarios": synthetic_data.SCENARIOS}, "scenario"),
    ],
)
def test_synthetic_client_specs_are_validated(change: dict, message: str):
    fields = {
        "key": "test",
        "name": "Test (fictional)",
        "seed": 1,
        "accounts": (
            SyntheticAccountSpec(
                Platform.GOOGLE_ADS,
                "synthetic-google-account-900",
                "GBP",
                "Europe/London",
                ("gs-ct-national",),
            ),
        ),
    }
    with pytest.raises(ValueError, match=message):
        SyntheticClientSpec(**{**fields, **change})


def test_report_findings_lists_active_and_suppressed_findings():
    report = build_sample_report()
    findings = report_findings(report)
    assert len(findings) == 19
    assert sum(f["noise_guard"]["outcome"] == "suppressed" for f in findings) == 5


def test_client_context_helper_uses_the_legacy_client_for_the_default_spec():
    assert client_context(DEFAULT_CLIENT) is LEGACY_SYNTHETIC_CLIENT
