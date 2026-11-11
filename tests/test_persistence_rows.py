import hashlib
import json
import re
from decimal import Decimal
from uuid import uuid4

import pytest

from factories import make_record, make_row
from growthpulse_ai.accounts.assignment import assign_findings
from growthpulse_ai.accounts.legacy import LEGACY_CLIENT_ID, LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.data_quality import validate_rows
from growthpulse_ai.persistence import store
from growthpulse_ai.persistence.rows import (
    finding_rows,
    quarantine_rows,
    record_row,
    report_row,
)
from growthpulse_ai.pipeline.sample_report import build_sample_report
from growthpulse_ai.schema import DataSource

RUN_ID = uuid4()
ACCOUNT_ID = uuid4()


def placeholders(query: str) -> set[str]:
    return set(re.findall(r"%\((\w+)\)s", query))


@pytest.fixture(scope="module")
def report() -> dict:
    return build_sample_report()


def scoped_findings(report: dict) -> list[dict]:
    return finding_rows(
        report,
        client_id=LEGACY_CLIENT_ID,
        finding_accounts=assign_findings(LEGACY_SYNTHETIC_CLIENT, report),
    )


def bad_rows() -> list[dict]:
    return [
        make_row(spend="-1.00"),
        make_row(campaign_id="", spend="2.00"),
        make_row(clicks=500, impressions=100),
    ]


# Every row function supplies exactly the parameters its SQL statement uses.


def test_record_row_matches_upsert_parameters():
    assert set(record_row(make_record(), RUN_ID, ad_account_id=ACCOUNT_ID)) == placeholders(
        store.UPSERT_RECORD
    )


def test_quarantine_row_matches_upsert_parameters():
    rows = quarantine_rows(
        validate_rows(bad_rows()).quarantined,
        data_source=DataSource.SYNTHETIC,
        connector="synthetic",
        run_id=RUN_ID,
    )
    assert rows
    assert all(set(row) == placeholders(store.UPSERT_QUARANTINE) for row in rows)


def test_report_row_matches_insert_parameters(report: dict):
    assert set(report_row(report, RUN_ID, client_id=LEGACY_CLIENT_ID)) == placeholders(
        store.INSERT_REPORT
    )


def test_finding_rows_match_upsert_parameters(report: dict):
    rows = scoped_findings(report)
    assert rows
    assert all(set(row) == placeholders(store.UPSERT_FINDING) for row in rows)


# Records


def test_record_row_keeps_decimal_precision_and_identity():
    record = make_record(spend="12.345678", conversions="1.5")
    row = record_row(record, RUN_ID, ad_account_id=ACCOUNT_ID)
    assert row["record_id"] == record.record_id
    assert row["spend"] == Decimal("12.345678")
    assert row["conversions"] == Decimal("1.5")
    assert row["run_id"] == RUN_ID


def test_record_without_attribution_has_no_attribution_columns():
    row = record_row(make_record(attribution=None), RUN_ID, ad_account_id=ACCOUNT_ID)
    assert [
        row["attribution_model"],
        row["click_window_days"],
        row["view_window_days"],
        row["report_time_basis"],
    ] == [None, None, None, None]


def test_record_with_attribution_fills_its_columns():
    row = record_row(make_record(), RUN_ID, ad_account_id=ACCOUNT_ID)
    assert row["attribution_model"] is not None
    assert row["click_window_days"] is not None
    assert row["report_time_basis"] is not None


# Quarantine


def quarantine(rows: list, data_source=DataSource.SYNTHETIC) -> list[dict]:
    return quarantine_rows(
        validate_rows(rows).quarantined,
        data_source=data_source,
        connector="synthetic",
        run_id=RUN_ID,
    )


def test_quarantine_ids_do_not_depend_on_row_order():
    forward = quarantine(bad_rows())
    backward = quarantine(list(reversed(bad_rows())))
    assert [row["quarantine_id"] for row in forward] == [row["quarantine_id"] for row in backward]


def test_repeated_payloads_share_one_entry_with_merged_issues():
    row = make_row(spend="-1.00")
    entries = quarantine([row, dict(row)])
    assert len(entries) == 1
    assert entries[0]["row_index"] == 0


def test_quarantine_issues_keep_codes_but_never_messages():
    entries = quarantine(bad_rows())
    for entry in entries:
        assert entry["issue_codes"]
        for issue in entry["issues"].obj:
            assert set(issue) == {"code", "severity", "field"}


def test_raw_payload_is_kept_for_synthetic_data_only():
    assert all(entry["raw_payload"] is not None for entry in quarantine(bad_rows()))
    live = quarantine(bad_rows(), data_source=DataSource.LIVE)
    assert all(entry["raw_payload"] is None for entry in live)
    assert all(re.fullmatch(r"[0-9a-f]{64}", entry["payload_sha256"]) for entry in live)


def test_quarantine_id_depends_on_data_source_and_connector():
    synthetic = {row["quarantine_id"] for row in quarantine(bad_rows())}
    live = {row["quarantine_id"] for row in quarantine(bad_rows(), DataSource.LIVE)}
    assert synthetic.isdisjoint(live)


def test_unserializable_payload_is_hashed_not_rejected():
    entries = quarantine([object()])
    assert len(entries) == 1
    assert entries[0]["issue_codes"] == ["invalid_schema"]


# Reports and findings


def test_report_row_reads_the_report_header(report: dict):
    row = report_row(report, RUN_ID, client_id=LEGACY_CLIENT_ID)
    assert row["report_id"] == report["report_id"]
    assert row["data_source"] == "synthetic"
    assert row["record_count"] == report["input"]["record_count"]
    assert row["period_start"].isoformat() == report["full_period"]["period"]["start"]
    assert row["report"].obj is report


def test_finding_rows_include_active_and_suppressed_findings(report: dict):
    rows = scoped_findings(report)
    expected = sum(
        len(c["findings"]) + len(c["suppressed_findings"]) for c in report["comparisons"]
    )
    assert len(rows) == expected
    suppressed = [row for row in rows if row["is_suppressed"]]
    assert suppressed
    assert all(row["guard_outcome"] == "suppressed" for row in suppressed)
    assert all(row["severity"] for row in rows)


def test_finding_rows_record_their_comparison(report: dict):
    purposes = {row["comparison_purpose"] for row in scoped_findings(report)}
    assert purposes == {"recent_change", "sustained_change"}


def test_finding_ids_are_unique_across_comparisons(report: dict):
    ids = [row["finding_id"] for row in scoped_findings(report)]
    assert len(ids) == len(set(ids))


def test_rows_carry_their_scope_explicitly(report: dict):
    assert (
        record_row(make_record(), RUN_ID, ad_account_id=ACCOUNT_ID)["ad_account_id"] == ACCOUNT_ID
    )
    assert report_row(report, RUN_ID, client_id=LEGACY_CLIENT_ID)["client_id"] == LEGACY_CLIENT_ID
    accounts = assign_findings(LEGACY_SYNTHETIC_CLIENT, report)
    for row in scoped_findings(report):
        assert row["client_id"] == LEGACY_CLIENT_ID
        assert row["ad_account_id"] == accounts[row["finding_id"]]


def test_a_finding_without_an_account_is_an_error(report: dict):
    with pytest.raises(ValueError, match="no account"):
        finding_rows(report, client_id=LEGACY_CLIENT_ID, finding_accounts={})


def test_account_scoped_quarantine_ids_differ_per_account_and_keep_the_combined_formula():
    quarantined = validate_rows([make_row(spend="-1.00")]).quarantined
    common = {"data_source": DataSource.SYNTHETIC, "connector": "synthetic", "run_id": RUN_ID}
    combined = quarantine_rows(quarantined, **common)
    first = quarantine_rows(quarantined, **common, ad_account_id=uuid4())
    second = quarantine_rows(quarantined, **common, ad_account_id=uuid4())
    assert combined[0]["ad_account_id"] is None
    assert len({combined[0]["quarantine_id"], first[0]["quarantine_id"],
                second[0]["quarantine_id"]}) == 3  # fmt: skip
    # Milestone 4's formula, unchanged for combined runs.
    payload = combined[0]["payload_sha256"]
    expected = hashlib.sha256(json.dumps(["synthetic", "synthetic", payload]).encode()).hexdigest()
    assert combined[0]["quarantine_id"] == expected
