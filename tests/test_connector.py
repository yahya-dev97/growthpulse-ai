import logging
from datetime import date

import pytest

from factories import make_row
from growthpulse_ai.connectors.base import AdDataConnector, ConnectorError
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import DEFAULT_START_DATE
from growthpulse_ai.data_quality import IssueCode
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

START = DEFAULT_START_DATE
END = date(2026, 10, 30)
FAKE_SECRET = "fake-secret-value-for-tests"


def test_returns_validated_records():
    result = SyntheticConnector().fetch(START, END)
    assert len(result.records) == 300
    assert all(isinstance(record, AdPerformanceRecord) for record in result.records)
    assert result.quarantined == ()


def test_all_records_are_labeled_synthetic():
    records = SyntheticConnector().fetch(START, END).records
    assert {record.data_source for record in records} == {DataSource.SYNTHETIC}


def test_generated_data_only_warns_about_zero_conversions():
    warnings = SyntheticConnector().fetch(START, END).warnings
    assert {issue.code for issue in warnings} == {IssueCode.ZERO_CONVERSIONS_WITH_SPEND}


def test_fetch_is_repeatable():
    first = SyntheticConnector().fetch(START, END).records
    assert SyntheticConnector().fetch(START, END).records == first


def test_filters_to_inclusive_date_range():
    day = date(2026, 10, 5)
    records = SyntheticConnector().fetch(day, date(2026, 10, 6)).records
    assert {record.reporting_date for record in records} == {day, date(2026, 10, 6)}
    assert len(records) == 20


def test_range_outside_dataset_is_empty():
    result = SyntheticConnector().fetch(date(2025, 1, 1), date(2025, 1, 31))
    assert result.records == ()


def test_rejects_reversed_range():
    with pytest.raises(ValueError, match="start_date"):
        SyntheticConnector().fetch(END, START)


def test_quarantines_malformed_rows_and_keeps_valid_ones():
    rows = [make_row(), make_row(reporting_date="2026-10-11", spend="-1")]
    result = SyntheticConnector(rows=rows).fetch(START, END)
    assert len(result.records) == 1
    assert result.quarantined[0].issues[0].code is IssueCode.NEGATIVE_VALUE


@pytest.mark.parametrize("row", ["text", None, 42, [], {}, make_row(reporting_date="nonsense")])
def test_rows_without_usable_dates_are_quarantined_not_dropped(row):
    result = SyntheticConnector(rows=[row]).fetch(START, END)
    assert result.records == ()
    assert len(result.quarantined) == 1


def test_detects_duplicates():
    result = SyntheticConnector(rows=[make_row(), make_row()]).fetch(START, END)
    assert len(result.records) == 1
    assert result.quarantined[0].issues[0].code is IssueCode.DUPLICATE_RECORD


def test_rejects_live_rows():
    result = SyntheticConnector(rows=[make_row(data_source="live")]).fetch(START, END)
    assert result.records == ()
    assert result.quarantined[0].issues[0].code is IssueCode.DATA_SOURCE_MISMATCH


def test_zero_conversion_records_reach_downstream():
    row = make_row(conversions="0", conversion_value="0")
    result = SyntheticConnector(rows=[row]).fetch(START, END)
    assert len(result.records) == 1
    assert result.warnings[0].code is IssueCode.ZERO_CONVERSIONS_WITH_SPEND


def test_logs_structured_counts(caplog: pytest.LogCaptureFixture):
    rows = [make_row(), make_row(reporting_date="2026-10-11", currency="XX1")]
    with caplog.at_level(logging.INFO, logger="growthpulse_ai.connectors.base"):
        SyntheticConnector(rows=rows).fetch(START, END)
    assert "total_rows=2 valid_records=1 quarantined_rows=1" in caplog.text
    assert "code=invalid_currency count=1" in caplog.text


def test_logs_never_contain_rejected_values(caplog: pytest.LogCaptureFixture):
    rows = [make_row(currency=FAKE_SECRET)]
    with caplog.at_level(logging.DEBUG):
        SyntheticConnector(rows=rows).fetch(START, END)
    assert FAKE_SECRET not in caplog.text


class ExplodingConnector(AdDataConnector):
    name = "exploding"
    data_source = DataSource.LIVE

    def _fetch_raw(self, start_date: date, end_date: date):
        raise RuntimeError(f"https://example.test/?access_token={FAKE_SECRET}")


def test_source_failure_raises_structured_error():
    with pytest.raises(ConnectorError) as error:
        ExplodingConnector().fetch(START, END)
    assert error.value.connector == "exploding"
    assert "RuntimeError" in error.value.message


def test_source_failure_does_not_expose_error_details():
    with pytest.raises(ConnectorError) as error:
        ExplodingConnector().fetch(START, END)
    assert FAKE_SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


class StubLiveConnector(AdDataConnector):
    """Stands in for a future Google or Meta connector."""

    name = "stub_live"
    data_source = DataSource.LIVE

    def _fetch_raw(self, start_date: date, end_date: date):
        return [make_row(data_source="live")]


def test_a_new_source_only_implements_fetch_raw():
    result = StubLiveConnector().fetch(START, END)
    assert [record.data_source for record in result.records] == [DataSource.LIVE]


def test_live_connector_rejects_synthetic_rows():
    class Mislabeled(StubLiveConnector):
        def _fetch_raw(self, start_date: date, end_date: date):
            return [make_row(data_source="synthetic")]

    result = Mislabeled().fetch(START, END)
    assert result.quarantined[0].issues[0].code is IssueCode.DATA_SOURCE_MISMATCH
