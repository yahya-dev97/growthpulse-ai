"""Deterministic synthetic data for a fictional UK medical imaging marketplace.

Every value is invented. The business, accounts, and campaigns do not exist, and rows hold
only aggregated advertising metrics, with no patient or personal data. Each row is labeled
through data_source, "synthetic-" identifiers, and a "[SYNTHETIC]" campaign name prefix.

Randomness uses only ``random.Random.random()``, the one method Python guarantees to stay
stable across versions, so a seed always reproduces the same dataset.
"""

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from growthpulse_ai.schema import (
    AttributionModel,
    CampaignType,
    ConversionStage,
    DataSource,
    Platform,
    ReportTimeBasis,
)

DEFAULT_SEED = 20261001
DEFAULT_START_DATE = date(2026, 10, 1)
DEFAULT_DAYS = 30

BUSINESS_NAME = "ExampleScan UK (fictional)"
CURRENCY = "GBP"
TIMEZONE = "Europe/London"
ACCOUNT_IDS = {
    Platform.GOOGLE_ADS: "synthetic-google-account-001",
    Platform.META_ADS: "synthetic-meta-account-001",
}

# Monday to Sunday spend multipliers.
WEEKDAY_FACTORS = {
    Platform.GOOGLE_ADS: (1.06, 1.08, 1.06, 1.03, 0.96, 0.80, 0.84),
    Platform.META_ADS: (0.98, 0.99, 1.00, 1.00, 1.02, 1.05, 1.06),
}

CONVERSION_DEFINITIONS = {
    "google_booking_request": {
        "event_id": "synthetic-google-booking-request",
        "event_name": "Scan booking request submitted",
        "stage": ConversionStage.BOOKING_REQUEST.value,
    },
    "meta_lead": {
        "event_id": "synthetic-meta-enquiry-lead",
        "event_name": "Scan enquiry form submitted",
        "stage": ConversionStage.LEAD.value,
    },
    "meta_booking_request": {
        "event_id": "synthetic-meta-booking-request",
        "event_name": "Scan booking request submitted",
        "stage": ConversionStage.BOOKING_REQUEST.value,
    },
}

ATTRIBUTION = {
    Platform.GOOGLE_ADS: {
        "attribution_model": AttributionModel.DATA_DRIVEN.value,
        "click_window_days": 30,
        "view_window_days": None,
        "report_time_basis": ReportTimeBasis.INTERACTION.value,
    },
    Platform.META_ADS: {
        "attribution_model": AttributionModel.PLATFORM_WINDOWED.value,
        "click_window_days": 7,
        "view_window_days": 1,
        "report_time_basis": ReportTimeBasis.MIXED.value,
    },
}


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    key: str
    platform: Platform
    campaign_type: CampaignType
    label: str
    conversion: str
    daily_spend: float
    cpc: float
    ctr: float
    cvr: float
    value_per_conversion: float

    @property
    def campaign_id(self) -> str:
        return f"synthetic-{self.key}"


G, M = Platform.GOOGLE_ADS, Platform.META_ADS
SEARCH, PMAX = CampaignType.GOOGLE_SEARCH, CampaignType.GOOGLE_PERFORMANCE_MAX
LEADS, SALES = CampaignType.META_LEADS, CampaignType.META_SALES
GOOGLE_EVENT = "google_booking_request"

CAMPAIGNS = (
    CampaignSpec(
        "gs-mri-london", G, SEARCH, "Search | MRI | London", GOOGLE_EVENT,
        420, 3.40, 0.062, 0.058, 365,
    ),
    CampaignSpec(
        "gs-ct-national", G, SEARCH, "Search | CT | National", GOOGLE_EVENT,
        230, 2.90, 0.054, 0.047, 410,
    ),
    CampaignSpec(
        "gs-ultrasound-manchester", G, SEARCH, "Search | Ultrasound | Manchester", GOOGLE_EVENT,
        125, 1.85, 0.071, 0.069, 145,
    ),
    CampaignSpec(
        "gs-mri-brand", G, SEARCH, "Search | MRI | Brand", GOOGLE_EVENT,
        85, 0.95, 0.210, 0.135, 380,
    ),
    CampaignSpec(
        "pmax-mri-national", G, PMAX, "PMax | MRI | National", GOOGLE_EVENT,
        380, 1.60, 0.022, 0.031, 355,
    ),
    CampaignSpec(
        "pmax-ct-national", G, PMAX, "PMax | CT | National", GOOGLE_EVENT,
        260, 1.45, 0.019, 0.034, 405,
    ),
    CampaignSpec(
        "pmax-ultrasound-north", G, PMAX, "PMax | Ultrasound | North", GOOGLE_EVENT,
        140, 1.10, 0.026, 0.038, 140,
    ),
    CampaignSpec(
        "meta-mri-prospecting", M, LEADS, "Meta | MRI | Prospecting", "meta_lead",
        240, 0.95, 0.013, 0.021, 340,
    ),
    CampaignSpec(
        "meta-ct-prospecting", M, LEADS, "Meta | CT | Prospecting", "meta_lead",
        150, 0.85, 0.011, 0.024, 390,
    ),
    CampaignSpec(
        "meta-ultrasound-retargeting", M, SALES, "Meta | Ultrasound | Retargeting",
        "meta_booking_request", 70, 1.15, 0.018, 0.052, 150,
    ),
)  # fmt: skip


class ScenarioKind(StrEnum):
    SUSTAINED_COST_PER_CONVERSION_INCREASE = "sustained_cost_per_conversion_increase"
    CONVERSION_RATE_DECLINE = "conversion_rate_decline"
    STRONG_PERFORMER = "strong_performer"
    ZERO_CONVERSIONS = "zero_conversions"


@dataclass(frozen=True, slots=True)
class SyntheticScenario:
    """A planted pattern, kept as ground truth for testing anomaly detection later."""

    campaign_key: str
    kind: ScenarioKind
    start_day: int
    end_day: int
    description: str
    ramp_days: int = 1
    cpc_multiplier: float = 1.0
    cvr_multiplier: float = 1.0


SCENARIOS = (
    SyntheticScenario(
        "gs-mri-london",
        ScenarioKind.SUSTAINED_COST_PER_CONVERSION_INCREASE,
        start_day=12,
        end_day=29,
        ramp_days=8,
        cpc_multiplier=1.55,
        cvr_multiplier=0.85,
        description="Auction pressure raises CPC while conversion rate softens. "
        "A proxy for rising CAC, based on platform-attributed conversions.",
    ),
    SyntheticScenario(
        "pmax-ct-national",
        ScenarioKind.CONVERSION_RATE_DECLINE,
        start_day=15,
        end_day=29,
        ramp_days=5,
        cvr_multiplier=0.40,
        description="Conversion rate falls while CPC and spend hold steady.",
    ),
    SyntheticScenario(
        "gs-mri-brand",
        ScenarioKind.STRONG_PERFORMER,
        start_day=0,
        end_day=29,
        description="Brand search with low cost per conversion throughout.",
    ),
    SyntheticScenario(
        "meta-ct-prospecting",
        ScenarioKind.ZERO_CONVERSIONS,
        start_day=19,
        end_day=25,
        cvr_multiplier=0.0,
        description="Seven days of spend with no conversions, as a tracking outage would show.",
    ),
)


def _normal(rng: random.Random) -> float:
    """Approximate a standard normal draw, using only ``rng.random()``."""
    return sum(rng.random() for _ in range(12)) - 6.0


def _scenario_multipliers(campaign_key: str, day: int) -> tuple[float, float]:
    cpc_multiplier = cvr_multiplier = 1.0
    for scenario in SCENARIOS:
        if scenario.campaign_key == campaign_key and scenario.start_day <= day <= scenario.end_day:
            progress = min(1.0, (day - scenario.start_day + 1) / scenario.ramp_days)
            cpc_multiplier *= 1 + (scenario.cpc_multiplier - 1) * progress
            cvr_multiplier *= 1 + (scenario.cvr_multiplier - 1) * progress
    return cpc_multiplier, cvr_multiplier


def _campaign_rows(
    spec: CampaignSpec, seed: int, start_date: date, days: int, ingested_at: str
) -> list[dict[str, Any]]:
    # Seeding per campaign means adding a campaign leaves the others unchanged.
    rng = random.Random(f"{seed}:{spec.key}")  # noqa: S311 - simulation, not security
    cpc_drift = cvr_drift = 0.0
    rows = []
    for day in range(days):
        reporting_date = start_date + timedelta(days=day)
        cpc_drift = 0.7 * cpc_drift + 0.03 * _normal(rng)
        cvr_drift = 0.7 * cvr_drift + 0.04 * _normal(rng)
        cpc_multiplier, cvr_multiplier = _scenario_multipliers(spec.key, day)

        weekday_factor = WEEKDAY_FACTORS[spec.platform][reporting_date.weekday()]
        spend = round(spec.daily_spend * weekday_factor * (1 + 0.07 * _normal(rng)), 2)
        cpc = spec.cpc * (1 + cpc_drift) * cpc_multiplier
        clicks = max(1, round(spend / cpc))
        impressions = max(clicks, round(clicks / (spec.ctr * (1 + 0.06 * _normal(rng)))))

        cvr = spec.cvr * (1 + cvr_drift) * cvr_multiplier
        conversions = float(sum(rng.random() < cvr for _ in range(clicks)))
        if spec.platform is Platform.GOOGLE_ADS:
            # Data-driven attribution credits conversions fractionally.
            conversions = round(conversions * (0.92 + 0.08 * rng.random()), 2)
        value = round(conversions * spec.value_per_conversion * (1 + 0.05 * _normal(rng)), 2)

        rows.append(
            {
                "reporting_date": reporting_date.isoformat(),
                "platform": spec.platform.value,
                "account_id": ACCOUNT_IDS[spec.platform],
                "campaign_id": spec.campaign_id,
                "campaign_name": f"[SYNTHETIC] {spec.label}",
                "campaign_type": spec.campaign_type.value,
                "impressions": impressions,
                "clicks": clicks,
                "spend": f"{spend:.2f}",
                "conversions": f"{conversions:.2f}",
                "conversion_value": f"{value:.2f}",
                "currency": CURRENCY,
                "account_timezone": TIMEZONE,
                "conversion_definition": dict(CONVERSION_DEFINITIONS[spec.conversion]),
                "attribution": dict(ATTRIBUTION[spec.platform]),
                "data_source": DataSource.SYNTHETIC.value,
                "ingested_at": ingested_at,
            }
        )
    return rows


def generate_synthetic_rows(
    seed: int = DEFAULT_SEED,
    start_date: date = DEFAULT_START_DATE,
    days: int = DEFAULT_DAYS,
) -> list[dict[str, Any]]:
    """Return raw rows for every campaign and day. The same arguments give the same rows."""
    if days < 1:
        raise ValueError("days must be at least 1")
    # Derived from the dates, not the clock, so output never depends on when it runs.
    ingested_at = datetime.combine(
        start_date + timedelta(days=days), time(6), tzinfo=UTC
    ).isoformat()
    return [
        row
        for spec in CAMPAIGNS
        for row in _campaign_rows(spec, seed, start_date, days, ingested_at)
    ]
