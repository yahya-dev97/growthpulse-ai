"""Builders for raw test rows."""

from datetime import date, timedelta
from typing import Any

from growthpulse_ai.schema import AdPerformanceRecord


def make_row(**overrides: Any) -> dict[str, Any]:
    """Return a valid raw Google Ads row, with any fields replaced."""
    row: dict[str, Any] = {
        "reporting_date": "2026-10-10",
        "platform": "google_ads",
        "account_id": "synthetic-account",
        "campaign_id": "synthetic-campaign",
        "campaign_name": "[SYNTHETIC] Test campaign",
        "campaign_type": "google_search",
        "impressions": 1000,
        "clicks": 50,
        "spend": "120.50",
        "conversions": "3.25",
        "conversion_value": "900.00",
        "currency": "GBP",
        "account_timezone": "Europe/London",
        "conversion_definition": {
            "event_id": "synthetic-booking",
            "event_name": "Booking request",
            "stage": "booking_request",
        },
        "attribution": {
            "attribution_model": "data_driven",
            "click_window_days": 30,
            "view_window_days": None,
            "report_time_basis": "interaction",
        },
        "data_source": "synthetic",
        "ingested_at": "2026-10-31T06:00:00+00:00",
    }
    row.update(overrides)
    return row


def make_meta_row(**overrides: Any) -> dict[str, Any]:
    """Return a valid raw Meta Ads row, with any fields replaced."""
    meta = {
        "platform": "meta_ads",
        "account_id": "synthetic-meta-account",
        "campaign_type": "meta_leads",
        "conversion_definition": {
            "event_id": "synthetic-lead",
            "event_name": "Enquiry form",
            "stage": "lead",
        },
        "attribution": {
            "attribution_model": "platform_windowed",
            "click_window_days": 7,
            "view_window_days": 1,
            "report_time_basis": "mixed",
        },
    }
    return make_row(**{**meta, **overrides})


def make_record(**overrides: Any) -> AdPerformanceRecord:
    return AdPerformanceRecord.model_validate(make_row(**overrides))


def make_meta_record(**overrides: Any) -> AdPerformanceRecord:
    return AdPerformanceRecord.model_validate(make_meta_row(**overrides))


def make_days(start: date, days: int, **overrides: Any) -> list[AdPerformanceRecord]:
    """Return one record per consecutive day, each with the same metrics."""
    return [
        make_record(reporting_date=(start + timedelta(days=offset)).isoformat(), **overrides)
        for offset in range(days)
    ]
