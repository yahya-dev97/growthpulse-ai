from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from factories import make_meta_row, make_row
from growthpulse_ai.schema import AdPerformanceRecord, DataSource, Platform


def build(**overrides) -> AdPerformanceRecord:
    return AdPerformanceRecord.model_validate(make_row(**overrides))


def attribution(**overrides) -> dict:
    return {**make_row()["attribution"], **overrides}


def conversion(**overrides) -> dict:
    return {**make_row()["conversion_definition"], **overrides}


def test_parses_valid_row():
    record = build()
    assert record.reporting_date == date(2026, 10, 10)
    assert record.platform is Platform.GOOGLE_ADS
    assert record.data_source is DataSource.SYNTHETIC


def test_money_and_conversions_are_exact_decimals():
    record = build(spend="0.10", conversions="1.25", conversion_value="0.20")
    assert record.spend == Decimal("0.10")
    assert record.conversions == Decimal("1.25")
    assert record.spend + record.conversion_value == Decimal("0.30")


def test_keeps_google_micros_precision():
    assert build(spend="1.234567").spend == Decimal("1.234567")


def test_records_are_immutable():
    record = build()
    with pytest.raises(ValidationError):
        record.spend = Decimal("1")


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        build(surprise="value")


def test_rejects_caller_supplied_record_id():
    with pytest.raises(ValidationError):
        build(record_id="forged")


@pytest.mark.parametrize("field", ["impressions", "clicks", "spend", "conversions"])
def test_rejects_booleans_as_numbers(field: str):
    with pytest.raises(ValidationError):
        build(**{field: True})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_money(value: str):
    with pytest.raises(ValidationError):
        build(spend=value)


def test_rejects_fractional_impressions():
    with pytest.raises(ValidationError):
        build(impressions=10.5)


@pytest.mark.parametrize(
    "value",
    [
        "2026-13-01",
        "2026-02-30",
        "10/10/2026",
        "20261010",
        "2026-W41-6",
        1,
        None,
        # Pydantic would coerce these three into valid dates without the format check.
        86400,
        "86400",
        datetime(2026, 10, 10),
    ],
)
def test_rejects_invalid_dates(value):
    with pytest.raises(ValidationError):
        build(reporting_date=value)


def test_validation_error_text_hides_rejected_values():
    with pytest.raises(ValidationError) as error:
        build(currency="fake-secret-value")
    assert "fake-secret-value" not in str(error.value)


def test_accepts_date_object():
    assert build(reporting_date=date(2026, 10, 1)).reporting_date == date(2026, 10, 1)


def test_rejects_naive_ingestion_timestamp():
    with pytest.raises(ValidationError):
        build(ingested_at="2026-10-31T06:00:00")


def test_future_date_check_uses_account_timezone():
    # 23:30 UTC on the 9th is already the 10th in Auckland, but still the 9th in New York.
    ingested_at = "2026-10-09T23:30:00+00:00"
    assert build(account_timezone="Pacific/Auckland", ingested_at=ingested_at)
    with pytest.raises(ValidationError):
        build(account_timezone="America/New_York", ingested_at=ingested_at)


@pytest.mark.parametrize("value", ["gbp", "GB", "POUND", "ZZZ", ""])
def test_rejects_invalid_currency(value: str):
    with pytest.raises(ValidationError):
        build(currency=value)


@pytest.mark.parametrize("value", ["BST", "Europe/Nowhere", "Europe", ""])
def test_rejects_invalid_timezone(value: str):
    with pytest.raises(ValidationError):
        build(account_timezone=value)


def test_attribution_is_optional():
    assert build(attribution=None).attribution is None


def test_conversion_definition_is_required():
    row = make_row()
    del row["conversion_definition"]
    with pytest.raises(ValidationError):
        AdPerformanceRecord.model_validate(row)


def test_rejects_google_campaign_type_on_meta():
    with pytest.raises(ValidationError):
        AdPerformanceRecord.model_validate(make_meta_row(campaign_type="google_search"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"attribution_model": "platform_windowed"},
        {"report_time_basis": "mixed"},
        {"click_window_days": 0},
        {"click_window_days": 91},
        {"view_window_days": 31},
        {"attribution_model": "first_touch_magic"},
    ],
)
def test_rejects_unsupported_google_attribution(overrides: dict):
    with pytest.raises(ValidationError):
        build(attribution=attribution(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"attribution_model": "data_driven"},
        {"click_window_days": 30},
        {"view_window_days": 7},
    ],
)
def test_rejects_unsupported_meta_attribution(overrides: dict):
    row = make_meta_row()
    row["attribution"] = {**row["attribution"], **overrides}
    with pytest.raises(ValidationError):
        AdPerformanceRecord.model_validate(row)


def test_record_id_is_stable_sha256():
    assert build().record_id == build().record_id
    assert len(build().record_id) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"impressions": 2000},
        {"clicks": 60},
        {"spend": "999.00"},
        {"conversions": "9"},
        {"conversion_value": "1.00"},
        {"campaign_name": "[SYNTHETIC] Renamed"},
        {"ingested_at": "2026-11-01T06:00:00+00:00"},
    ],
)
def test_restated_values_keep_the_same_identity(overrides: dict):
    assert build(**overrides).record_id == build().record_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"reporting_date": "2026-10-11"},
        {"account_id": "other"},
        {"campaign_id": "other"},
        {"data_source": "live"},
        {"conversion_definition": conversion(event_id="other")},
        {"attribution": attribution(attribution_model="last_click")},
        {"attribution": attribution(click_window_days=7)},
        {"attribution": attribution(view_window_days=1)},
        {"attribution": attribution(report_time_basis="conversion")},
        {"attribution": None},
    ],
)
def test_identity_dimensions_change_the_record_id(overrides: dict):
    assert build(**overrides).record_id != build().record_id


def test_platform_is_part_of_identity():
    shared = {"account_id": "same", "campaign_id": "same", "attribution": None}
    google = build(**shared)
    meta = AdPerformanceRecord.model_validate(
        make_meta_row(conversion_definition=make_row()["conversion_definition"], **shared)
    )
    assert google.record_id != meta.record_id


def test_identity_cannot_collide_through_delimiters():
    first = build(account_id="a|b", campaign_id="c")
    second = build(account_id="a", campaign_id="b|c")
    assert first.record_id != second.record_id


def test_delivery_key_ignores_conversion_dimensions():
    other_event = build(conversion_definition=conversion(event_id="other"))
    assert other_event.delivery_key == build().delivery_key
    assert other_event.record_id != build().record_id


def test_platforms_have_different_conversion_basis():
    meta = AdPerformanceRecord.model_validate(make_meta_row())
    assert meta.conversion_basis != build().conversion_basis


def test_attribution_window_changes_conversion_basis():
    assert (
        build(attribution=attribution(click_window_days=7)).conversion_basis
        != build().conversion_basis
    )


def test_json_round_trip_preserves_record():
    record = build()
    payload = record.model_dump(mode="json", exclude={"record_id"})
    assert AdPerformanceRecord.model_validate(payload) == record


def test_dump_includes_record_id_for_upserts():
    record = build()
    assert record.model_dump()["record_id"] == record.record_id
