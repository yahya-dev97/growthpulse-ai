"""Pure parts of the stored-record loader. The database round trip is in the integration tests."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from growthpulse_ai.persistence.load import canonical_amount, row_to_record


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("237.340000", "237.34"),
        ("100.000000", "100.00"),
        ("0.000000", "0.00"),
        ("4.500000", "4.50"),
        ("1.234500", "1.2345"),
        ("99999999999999.999999", "99999999999999.999999"),
        ("1000000.000000", "1000000.00"),
    ],
)
def test_canonical_amount_drops_only_padding(stored: str, expected: str):
    result = canonical_amount(Decimal(stored))
    assert str(result) == expected
    assert result == Decimal(stored)


def test_canonical_amount_refuses_non_finite_values():
    with pytest.raises(ValueError):
        canonical_amount(Decimal("NaN"))


def stored_row(**overrides):
    row = {
        "data_source": "synthetic",
        "reporting_date": date(2026, 10, 1),
        "platform": "meta_ads",
        "account_id": "synthetic-meta-account-001",
        "campaign_id": "synthetic-c",
        "campaign_name": "[SYNTHETIC] C",
        "campaign_type": "meta_leads",
        "impressions": 1000,
        "clicks": 10,
        "spend": Decimal("12.300000"),
        "conversions": Decimal("1.000000"),
        "conversion_value": Decimal("50.000000"),
        "currency": "GBP",
        "account_timezone": "Europe/London",
        "conversion_event_id": "e",
        "conversion_event_name": "Lead",
        "conversion_stage": "lead",
        "attribution_model": "platform_windowed",
        "click_window_days": 7,
        "view_window_days": 1,
        "report_time_basis": "mixed",
        "ingested_at": datetime(
            2026, 10, 31, 15, 45, tzinfo=timezone(timedelta(hours=13, minutes=45))
        ),
    }
    row.update(overrides)
    return row


def test_rows_become_records_with_utc_ingestion_times():
    record = row_to_record(stored_row())
    assert record.ingested_at == datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
    assert record.ingested_at.utcoffset() == timedelta(0)
    assert str(record.spend) == "12.30"
    assert record.attribution.view_window_days == 1


def test_rows_without_attribution_load_as_unattributed():
    record = row_to_record(
        stored_row(attribution_model=None, click_window_days=None, view_window_days=None,
                   report_time_basis=None)
    )  # fmt: skip
    assert record.attribution is None
