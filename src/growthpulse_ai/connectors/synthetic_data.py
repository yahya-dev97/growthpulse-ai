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


CAMPAIGNS_BY_KEY = {spec.key: spec for spec in CAMPAIGNS}


@dataclass(frozen=True, slots=True)
class SyntheticAccountSpec:
    """One fictional advertising account and the campaigns it runs."""

    platform: Platform
    account_id: str
    currency: str
    timezone: str
    campaign_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyntheticClientSpec:
    """One fictional client: its accounts, seed and planted scenarios."""

    key: str
    name: str
    seed: int
    accounts: tuple[SyntheticAccountSpec, ...]
    scenarios: tuple[SyntheticScenario, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.endswith("(fictional)"):
            raise ValueError("Synthetic client names must be marked (fictional)")
        keys = [key for account in self.accounts for key in account.campaign_keys]
        if len(keys) != len(set(keys)):
            raise ValueError("A campaign may belong to only one account of a client")
        for account in self.accounts:
            if not account.account_id.startswith("synthetic-"):
                raise ValueError("Synthetic account IDs must start with synthetic-")
            for key in account.campaign_keys:
                if key not in CAMPAIGNS_BY_KEY:
                    raise ValueError("Unknown synthetic campaign")
                if CAMPAIGNS_BY_KEY[key].platform is not account.platform:
                    raise ValueError("A campaign must run on its account's platform")
        if len({(a.platform, a.account_id) for a in self.accounts}) != len(self.accounts):
            raise ValueError("Account IDs must be unique within a client")
        if any(scenario.campaign_key not in keys for scenario in self.scenarios):
            raise ValueError("A scenario must name one of the client's campaigns")

    def account_for_campaign(self, campaign_key: str) -> SyntheticAccountSpec | None:
        for account in self.accounts:
            if campaign_key in account.campaign_keys:
                return account
        return None


def _platform_keys(platform: Platform) -> tuple[str, ...]:
    return tuple(spec.key for spec in CAMPAIGNS if spec.platform is platform)


# Milestones 2-4 data, unchanged: same seed, accounts, campaigns and scenarios.
DEFAULT_CLIENT = SyntheticClientSpec(
    key="examplescan-uk",
    name=BUSINESS_NAME,
    seed=DEFAULT_SEED,
    accounts=tuple(
        SyntheticAccountSpec(platform, ACCOUNT_IDS[platform], CURRENCY, TIMEZONE, keys)
        for platform, keys in ((G, _platform_keys(G)), (M, _platform_keys(M)))
    ),
    scenarios=SCENARIOS,
)

# Further fictional clients, for multi-client and isolation tests. One reuses campaign IDs
# from the default client on different accounts; one mixes currencies and timezones.
SYNTHETIC_CLIENTS = (
    DEFAULT_CLIENT,
    SyntheticClientSpec(
        key="exampleclinic-ie",
        name="ExampleClinic Ireland (fictional)",
        seed=20261002,
        accounts=(
            SyntheticAccountSpec(
                G, "synthetic-google-account-002", "EUR", "Europe/Dublin",
                ("gs-ct-national", "gs-mri-brand", "pmax-mri-national"),
            ),
            SyntheticAccountSpec(
                M, "synthetic-meta-account-002", "EUR", "Europe/Dublin",
                ("meta-mri-prospecting", "meta-ultrasound-retargeting"),
            ),
        ),
        scenarios=(
            SyntheticScenario(
                "pmax-mri-national", ScenarioKind.CONVERSION_RATE_DECLINE, start_day=16,
                end_day=29, ramp_days=4, cvr_multiplier=0.45,
                description="Conversion rate falls while CPC and spend hold steady.",
            ),
        ),
    ),
    SyntheticClientSpec(
        key="examplescan-eu",
        name="ExampleScan Europe (fictional)",
        seed=20261003,
        accounts=(
            SyntheticAccountSpec(
                G, "synthetic-google-account-003", "GBP", "Europe/London",
                ("gs-mri-london", "gs-ultrasound-manchester"),
            ),
            SyntheticAccountSpec(
                G, "synthetic-google-account-004", "EUR", "Europe/Berlin",
                ("pmax-ct-national", "pmax-ultrasound-north"),
            ),
            SyntheticAccountSpec(
                M, "synthetic-meta-account-003", "USD", "America/New_York",
                ("meta-ct-prospecting",),
            ),
        ),
    ),
)  # fmt: skip


def _normal(rng: random.Random) -> float:
    """Approximate a standard normal draw, using only ``rng.random()``."""
    return sum(rng.random() for _ in range(12)) - 6.0


def _scenario_multipliers(
    campaign_key: str, day: int, scenarios: tuple[SyntheticScenario, ...] = SCENARIOS
) -> tuple[float, float]:
    cpc_multiplier = cvr_multiplier = 1.0
    for scenario in scenarios:
        if scenario.campaign_key == campaign_key and scenario.start_day <= day <= scenario.end_day:
            progress = min(1.0, (day - scenario.start_day + 1) / scenario.ramp_days)
            cpc_multiplier *= 1 + (scenario.cpc_multiplier - 1) * progress
            cvr_multiplier *= 1 + (scenario.cvr_multiplier - 1) * progress
    return cpc_multiplier, cvr_multiplier


def _campaign_rows(
    spec: CampaignSpec,
    seed: int,
    start_date: date,
    days: int,
    ingested_at: str,
    account: SyntheticAccountSpec,
    scenarios: tuple[SyntheticScenario, ...],
) -> list[dict[str, Any]]:
    # Seeding per campaign means adding a campaign leaves the others unchanged.
    rng = random.Random(f"{seed}:{spec.key}")  # noqa: S311 - simulation, not security
    cpc_drift = cvr_drift = 0.0
    rows = []
    for day in range(days):
        reporting_date = start_date + timedelta(days=day)
        cpc_drift = 0.7 * cpc_drift + 0.03 * _normal(rng)
        cvr_drift = 0.7 * cvr_drift + 0.04 * _normal(rng)
        cpc_multiplier, cvr_multiplier = _scenario_multipliers(spec.key, day, scenarios)

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
                "account_id": account.account_id,
                "campaign_id": spec.campaign_id,
                "campaign_name": f"[SYNTHETIC] {spec.label}",
                "campaign_type": spec.campaign_type.value,
                "impressions": impressions,
                "clicks": clicks,
                "spend": f"{spend:.2f}",
                "conversions": f"{conversions:.2f}",
                "conversion_value": f"{value:.2f}",
                "currency": account.currency,
                "account_timezone": account.timezone,
                "conversion_definition": dict(CONVERSION_DEFINITIONS[spec.conversion]),
                "attribution": dict(ATTRIBUTION[spec.platform]),
                "data_source": DataSource.SYNTHETIC.value,
                "ingested_at": ingested_at,
            }
        )
    return rows


def generate_synthetic_rows(
    seed: int | None = None,
    start_date: date = DEFAULT_START_DATE,
    days: int = DEFAULT_DAYS,
    client: SyntheticClientSpec = DEFAULT_CLIENT,
) -> list[dict[str, Any]]:
    """Return raw rows for every campaign and day of one fictional client.

    The same arguments give the same rows. The defaults reproduce the Milestone 2-4
    dataset exactly. ``seed`` defaults to the client's own seed.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    seed = client.seed if seed is None else seed
    # Derived from the dates, not the clock, so output never depends on when it runs.
    ingested_at = datetime.combine(
        start_date + timedelta(days=days), time(6), tzinfo=UTC
    ).isoformat()
    rows = []
    # Catalogue order, so the default client's rows come out exactly as before.
    for spec in CAMPAIGNS:
        account = client.account_for_campaign(spec.key)
        if account is not None:
            rows.extend(
                _campaign_rows(spec, seed, start_date, days, ingested_at, account, client.scenarios)
            )
    return rows
