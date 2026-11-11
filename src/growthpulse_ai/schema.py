"""Normalized advertising performance schema, shared by connectors and analytics.

One record is one campaign, on one reporting date, for one conversion definition under
one attribution setting.

What the outcome fields mean:

- ``conversions`` are platform-attributed: the platform's own claim, under its own
  attribution rules. Google Ads and Meta can both claim the same customer.
- Actual bookings exist only in the business's booking system and are not in this schema.
- True customer acquisition cost is total acquisition cost divided by new customers, using
  business records. Spend divided by ``conversions`` is only a platform-reported proxy.

Conversions are comparable only between records sharing a ``conversion_basis``.
"""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self
from zoneinfo import ZoneInfo, available_timezones

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=255)]
# Google Ads reports cost in micros, so six decimal places are preserved.
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]

ISO_4217_CODES = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "ANG",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XCD",
        "XCG",
        "XOF",
        "XPF",
        "YER",
        "ZAR",
        "ZMW",
        "ZWG",
    ]
)
IANA_TIMEZONES = frozenset(available_timezones())
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class Platform(StrEnum):
    GOOGLE_ADS = "google_ads"
    META_ADS = "meta_ads"


class CampaignType(StrEnum):
    GOOGLE_SEARCH = "google_search"
    GOOGLE_PERFORMANCE_MAX = "google_performance_max"
    GOOGLE_OTHER = "google_other"
    META_LEADS = "meta_leads"
    META_SALES = "meta_sales"
    META_OTHER = "meta_other"


class DataSource(StrEnum):
    SYNTHETIC = "synthetic"
    LIVE = "live"


class ConversionStage(StrEnum):
    """How far through the funnel the tracked event is."""

    LEAD = "lead"
    BOOKING_REQUEST = "booking_request"
    CONFIRMED_BOOKING = "confirmed_booking"
    PURCHASE = "purchase"
    OTHER = "other"


class AttributionModel(StrEnum):
    DATA_DRIVEN = "data_driven"
    LAST_CLICK = "last_click"
    PLATFORM_WINDOWED = "platform_windowed"


class ReportTimeBasis(StrEnum):
    """Which date a conversion is reported against."""

    INTERACTION = "interaction"
    CONVERSION = "conversion"
    MIXED = "mixed"


PLATFORM_CAMPAIGN_TYPES = {
    Platform.GOOGLE_ADS: {
        CampaignType.GOOGLE_SEARCH,
        CampaignType.GOOGLE_PERFORMANCE_MAX,
        CampaignType.GOOGLE_OTHER,
    },
    Platform.META_ADS: {
        CampaignType.META_LEADS,
        CampaignType.META_SALES,
        CampaignType.META_OTHER,
    },
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        # Rejected values may be misplaced secrets, so keep them out of error messages.
        hide_input_in_errors=True,
    )


class ConversionDefinition(_StrictModel):
    """The event a platform counted as a conversion."""

    event_id: NonEmptyStr
    event_name: NonEmptyStr
    stage: ConversionStage


class AttributionMetadata(_StrictModel):
    """The rules a platform applied when crediting conversions."""

    attribution_model: AttributionModel
    click_window_days: int = Field(ge=1, le=90)
    view_window_days: int | None = Field(default=None, ge=1, le=30)
    report_time_basis: ReportTimeBasis


def _attribution_problem(platform: Platform, attribution: AttributionMetadata) -> str | None:
    """Return why this attribution setting is unsupported on the platform, if it is."""
    if platform is Platform.GOOGLE_ADS:
        if attribution.attribution_model not in {
            AttributionModel.DATA_DRIVEN,
            AttributionModel.LAST_CLICK,
        }:
            return "Google Ads attribution must be data_driven or last_click"
        if attribution.report_time_basis is ReportTimeBasis.MIXED:
            return "Google Ads does not report on a mixed time basis"
        return None
    if attribution.attribution_model is not AttributionModel.PLATFORM_WINDOWED:
        return "Meta Ads attribution must be platform_windowed"
    if attribution.click_window_days not in {1, 7, 28}:
        return "Meta Ads click window must be 1, 7, or 28 days"
    if attribution.view_window_days not in {None, 1}:
        return "Meta Ads view window must be blank or 1 day"
    return None


class AdPerformanceRecord(_StrictModel):
    reporting_date: date
    platform: Platform
    account_id: NonEmptyStr
    campaign_id: NonEmptyStr
    campaign_name: NonEmptyStr
    campaign_type: CampaignType
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    spend: NonNegativeDecimal
    conversions: NonNegativeDecimal = Field(
        description="Platform-attributed and possibly fractional. Not actual bookings."
    )
    conversion_value: NonNegativeDecimal = Field(
        description="Platform-reported value of attributed conversions. Not realized revenue."
    )
    currency: str
    account_timezone: str
    conversion_definition: ConversionDefinition
    attribution: AttributionMetadata | None = None
    data_source: DataSource
    ingested_at: AwareDatetime

    @field_validator("reporting_date", mode="before")
    @classmethod
    def _require_calendar_date(cls, value: Any) -> Any:
        # Pydantic would otherwise coerce integers and datetimes into dates.
        if type(value) is date:
            return value
        if isinstance(value, str) and ISO_DATE_PATTERN.fullmatch(value.strip()):
            return value.strip()
        raise PydanticCustomError("invalid_date", "reporting_date must be a YYYY-MM-DD date")

    @field_validator(
        "impressions", "clicks", "spend", "conversions", "conversion_value", mode="before"
    )
    @classmethod
    def _reject_booleans(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise PydanticCustomError("invalid_number", "Value must be a number, not a boolean")
        return value

    @field_validator("currency")
    @classmethod
    def _require_iso_currency(cls, value: str) -> str:
        if value not in ISO_4217_CODES:
            raise PydanticCustomError("invalid_currency", "currency must be an ISO 4217 code")
        return value

    @field_validator("account_timezone")
    @classmethod
    def _require_iana_timezone(cls, value: str) -> str:
        if value not in IANA_TIMEZONES:
            raise PydanticCustomError(
                "invalid_timezone", "account_timezone must be an IANA timezone name"
            )
        return value

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> Self:
        if self.clicks > self.impressions:
            raise PydanticCustomError(
                "clicks_exceed_impressions", "clicks cannot exceed impressions"
            )
        ingestion_day = self.ingested_at.astimezone(ZoneInfo(self.account_timezone)).date()
        if self.reporting_date > ingestion_day:
            raise PydanticCustomError(
                "invalid_date", "reporting_date cannot be later than the ingestion date"
            )
        if self.campaign_type not in PLATFORM_CAMPAIGN_TYPES[self.platform]:
            raise PydanticCustomError(
                "inconsistent_dimensions", "campaign_type does not belong to this platform"
            )
        if self.attribution is not None:
            problem = _attribution_problem(self.platform, self.attribution)
            if problem:
                raise PydanticCustomError("invalid_attribution", problem)
        return self

    @computed_field
    @property
    def record_id(self) -> str:
        """Stable identity for idempotent upserts.

        Excludes metrics, names, and ingested_at so a restated row updates in place.
        Includes data_source so synthetic rows can never overwrite live rows.
        """
        identity = [
            self.data_source.value,
            self.reporting_date.isoformat(),
            self.platform.value,
            self.account_id,
            self.campaign_id,
            self.conversion_definition.event_id,
            *self._attribution_identity,
        ]
        canonical = json.dumps(identity, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def _attribution_identity(self) -> tuple[str | int | None, ...]:
        if self.attribution is None:
            return (None, None, None, None)
        return (
            self.attribution.attribution_model.value,
            self.attribution.click_window_days,
            self.attribution.view_window_days,
            self.attribution.report_time_basis.value,
        )

    @property
    def delivery_key(self) -> tuple[str, ...]:
        """The grain of delivery metrics.

        Rows for several conversion events on one campaign-day repeat the same impressions,
        clicks, and spend. Aggregate those metrics at this grain to avoid double counting.
        """
        return (
            self.data_source.value,
            self.reporting_date.isoformat(),
            self.platform.value,
            self.account_id,
            self.campaign_id,
        )

    @property
    def conversion_basis(self) -> tuple[str | int | None, ...]:
        """Records are comparable on conversions only when this value matches."""
        return (
            self.platform.value,
            self.conversion_definition.event_id,
            self.conversion_definition.stage.value,
            *self._attribution_identity,
        )
