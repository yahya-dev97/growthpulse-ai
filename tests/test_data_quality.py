from decimal import Decimal

import pytest

from factories import make_meta_row, make_row
from growthpulse_ai.data_quality import IssueCode, Severity, validate_rows
from growthpulse_ai.schema import DataSource

FAKE_SECRET = "fake-secret-value-for-tests"


def error_codes(rows: list) -> set[IssueCode]:
    result = validate_rows(rows)
    return {issue.code for row in result.quarantined for issue in row.issues}


def warning_codes(rows: list) -> set[IssueCode]:
    return {issue.code for issue in validate_rows(rows).warnings}


def without(field: str) -> dict:
    row = make_row()
    del row[field]
    return row


def test_clean_row_has_no_issues():
    result = validate_rows([make_row()])
    assert len(result.records) == 1
    assert result.quarantined == ()
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (make_row(spend="-0.01"), IssueCode.NEGATIVE_VALUE),
        (make_row(impressions=-1), IssueCode.NEGATIVE_VALUE),
        (make_row(clicks=-1), IssueCode.NEGATIVE_VALUE),
        (make_row(conversions="-1"), IssueCode.NEGATIVE_VALUE),
        (make_row(conversion_value="-1"), IssueCode.NEGATIVE_VALUE),
        (make_row(clicks=1001), IssueCode.CLICKS_EXCEED_IMPRESSIONS),
        (without("campaign_id"), IssueCode.MISSING_CAMPAIGN_IDENTIFIER),
        (make_row(campaign_id=""), IssueCode.MISSING_CAMPAIGN_IDENTIFIER),
        (make_row(campaign_id="   "), IssueCode.MISSING_CAMPAIGN_IDENTIFIER),
        (make_row(campaign_id=None), IssueCode.MISSING_CAMPAIGN_IDENTIFIER),
        (without("campaign_name"), IssueCode.MISSING_CAMPAIGN_IDENTIFIER),
        (make_row(reporting_date="2026-02-30"), IssueCode.INVALID_DATE),
        (make_row(reporting_date="not-a-date"), IssueCode.INVALID_DATE),
        (without("reporting_date"), IssueCode.INVALID_DATE),
        (make_row(reporting_date="2026-11-01"), IssueCode.INVALID_DATE),
        (make_row(ingested_at="2026-10-31T06:00:00"), IssueCode.INVALID_DATE),
        (make_row(currency="XX1"), IssueCode.INVALID_CURRENCY),
        (without("currency"), IssueCode.INVALID_CURRENCY),
        (make_row(account_timezone="Mars/Base"), IssueCode.INVALID_TIMEZONE),
        (without("conversion_definition"), IssueCode.MISSING_CONVERSION_DEFINITION),
        (make_row(conversion_definition=None), IssueCode.MISSING_CONVERSION_DEFINITION),
        (
            make_row(conversion_definition={"event_id": "x"}),
            IssueCode.MISSING_CONVERSION_DEFINITION,
        ),
        (make_row(attribution={"attribution_model": "magic"}), IssueCode.INVALID_ATTRIBUTION),
        (
            make_meta_row(attribution=make_row()["attribution"]),
            IssueCode.INVALID_ATTRIBUTION,
        ),
        (make_row(campaign_type="meta_leads"), IssueCode.INCONSISTENT_DIMENSIONS),
        (make_row(spend="abc"), IssueCode.INVALID_FIELD),
        (make_row(platform="tiktok"), IssueCode.INVALID_FIELD),
        ("not a row", IssueCode.INVALID_SCHEMA),
        (None, IssueCode.INVALID_SCHEMA),
    ],
)
def test_hard_failures_are_quarantined(row, code: IssueCode):
    result = validate_rows([row])
    assert result.records == ()
    assert len(result.quarantined) == 1
    issues = result.quarantined[0].issues
    assert code in {issue.code for issue in issues}
    assert all(issue.severity is Severity.ERROR for issue in issues)


def test_reports_every_field_error_on_a_row():
    codes = error_codes([make_row(spend="-1", currency="XX1", campaign_id="")])
    assert codes == {
        IssueCode.NEGATIVE_VALUE,
        IssueCode.INVALID_CURRENCY,
        IssueCode.MISSING_CAMPAIGN_IDENTIFIER,
    }


def test_issue_names_the_field_and_row():
    result = validate_rows([make_row(), make_row(spend="-1", reporting_date="2026-10-11")])
    issue = result.quarantined[0].issues[0]
    assert issue.field == "spend"
    assert issue.row_index == 1


def test_bad_rows_do_not_affect_good_rows():
    result = validate_rows([make_row(), make_row(spend="-1", reporting_date="2026-10-11")])
    assert len(result.records) == 1
    assert len(result.quarantined) == 1


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (make_row(attribution=None), IssueCode.MISSING_ATTRIBUTION),
        (
            make_row(conversions="0", conversion_value="0"),
            IssueCode.ZERO_CONVERSIONS_WITH_SPEND,
        ),
        (make_row(impressions=0, clicks=0), IssueCode.SPEND_WITHOUT_IMPRESSIONS),
        (make_row(spend="0"), IssueCode.CLICKS_WITHOUT_SPEND),
        (make_row(clicks=2, conversions="3"), IssueCode.CONVERSIONS_EXCEED_CLICKS),
        (make_row(conversions="0"), IssueCode.VALUE_WITHOUT_CONVERSIONS),
    ],
)
def test_warnings_keep_the_record(row: dict, code: IssueCode):
    result = validate_rows([row])
    assert len(result.records) == 1
    assert result.quarantined == ()
    assert code in {issue.code for issue in result.warnings}
    assert all(issue.severity is Severity.WARNING for issue in result.warnings)


def test_zero_conversion_record_is_valid():
    result = validate_rows([make_row(conversions="0", conversion_value="0")])
    assert result.records[0].conversions == Decimal("0")


def test_zero_spend_and_zero_conversions_is_not_flagged():
    row = make_row(impressions=0, clicks=0, spend="0", conversions="0", conversion_value="0")
    assert warning_codes([row]) == set()


def test_exact_duplicate_keeps_first_row():
    result = validate_rows([make_row(), make_row()])
    assert len(result.records) == 1
    assert result.quarantined[0].row_index == 1
    assert result.quarantined[0].issues[0].code is IssueCode.DUPLICATE_RECORD


def test_repeat_with_only_new_ingestion_time_is_an_exact_duplicate():
    later = make_row(ingested_at="2026-11-01T06:00:00+00:00")
    assert error_codes([make_row(), later]) == {IssueCode.DUPLICATE_RECORD}


def test_equal_decimals_with_different_scale_are_exact_duplicates():
    assert error_codes([make_row(spend="120.5"), make_row(spend="120.50")]) == {
        IssueCode.DUPLICATE_RECORD
    }


def test_conflicting_duplicates_are_all_rejected():
    result = validate_rows([make_row(), make_row(spend="999.00")])
    assert result.records == ()
    assert len(result.quarantined) == 2
    assert {row.issues[0].code for row in result.quarantined} == {IssueCode.CONFLICTING_DUPLICATE}


def test_duplicate_issue_carries_record_id():
    result = validate_rows([make_row(), make_row()])
    assert result.quarantined[0].issues[0].record_id == result.records[0].record_id


@pytest.mark.parametrize(
    "conflict",
    [
        {"currency": "EUR"},
        {"account_timezone": "Europe/Paris"},
        {"campaign_type": "google_performance_max"},
    ],
)
def test_inconsistent_dimensions_reject_the_group(conflict: dict):
    rows = [make_row(), make_row(reporting_date="2026-10-11", **conflict)]
    result = validate_rows(rows)
    assert result.records == ()
    assert {
        issue.code for row in result.quarantined for issue in row.issues
    } == {IssueCode.INCONSISTENT_DIMENSIONS}  # fmt: skip


def test_inconsistency_only_rejects_the_affected_account():
    rows = [
        make_row(),
        make_row(reporting_date="2026-10-11", currency="EUR"),
        make_row(account_id="other-account", campaign_id="other"),
    ]
    result = validate_rows(rows)
    assert [record.account_id for record in result.records] == ["other-account"]


def test_conflicting_conversion_definition_is_rejected():
    other = {**make_row()["conversion_definition"], "stage": "lead"}
    rows = [make_row(), make_row(reporting_date="2026-10-11", conversion_definition=other)]
    assert error_codes(rows) == {IssueCode.INCONSISTENT_DIMENSIONS}


def second_event() -> dict:
    return {"event_id": "second", "event_name": "Second event", "stage": "lead"}


def test_multiple_events_with_matching_delivery_are_valid():
    rows = [make_row(), make_row(conversion_definition=second_event())]
    assert len(validate_rows(rows).records) == 2


def test_multiple_events_with_different_delivery_are_rejected():
    rows = [make_row(), make_row(conversion_definition=second_event(), spend="1.00")]
    assert error_codes(rows) == {IssueCode.INCONSISTENT_DIMENSIONS}


def test_campaign_rename_is_a_warning():
    rows = [
        make_row(),
        make_row(reporting_date="2026-10-11", campaign_name="[SYNTHETIC] New name"),
    ]
    result = validate_rows(rows)
    assert len(result.records) == 2
    assert {issue.code for issue in result.warnings} == {IssueCode.CAMPAIGN_RENAMED}


def test_attribution_change_is_a_warning():
    changed = {**make_row()["attribution"], "click_window_days": 7}
    rows = [make_row(), make_row(reporting_date="2026-10-11", attribution=changed)]
    result = validate_rows(rows)
    assert len(result.records) == 2
    assert {issue.code for issue in result.warnings} == {IssueCode.ATTRIBUTION_CHANGED}


def test_expected_source_rejects_other_sources():
    result = validate_rows([make_row(data_source="live")], expected_source=DataSource.SYNTHETIC)
    assert result.records == ()
    assert result.quarantined[0].issues[0].code is IssueCode.DATA_SOURCE_MISMATCH


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": FAKE_SECRET},
        {"spend": FAKE_SECRET},
        {"platform": FAKE_SECRET},
        {"reporting_date": FAKE_SECRET},
        {"account_timezone": FAKE_SECRET},
    ],
)
def test_issues_never_contain_rejected_values(overrides: dict):
    result = validate_rows([make_row(**overrides)])
    assert FAKE_SECRET not in repr(result.quarantined[0].issues)
    assert FAKE_SECRET not in repr(result.quarantined[0])


def test_quarantine_keeps_raw_row_for_inspection():
    row = make_row(spend="-1")
    assert validate_rows([row]).quarantined[0].raw is row


def test_summary_counts_issues():
    rows = [make_row(), make_row(), make_row(reporting_date="bad")]
    assert validate_rows(rows).summary == {
        "total_rows": 3,
        "valid_records": 1,
        "quarantined_rows": 2,
        "warnings": 0,
        "errors_by_code": {"duplicate_record": 1, "invalid_date": 1},
        "warnings_by_code": {},
    }
