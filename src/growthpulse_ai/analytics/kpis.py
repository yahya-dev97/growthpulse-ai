"""KPI calculations over validated records.

Aggregation rules enforced here:

- Delivery metrics are summed once per ``delivery_key``, never per conversion-event row.
- Conversions are only ever totalled within one ``conversion_basis``.
- An aggregate never spans more than one currency or data source.

Undefined ratios are None, not zero. Rates are fractions, so 0.05 means 5%.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from functools import reduce

from growthpulse_ai.analytics.errors import (
    AnalyticsError,
    ConflictingDeliveryError,
    DuplicateRecordError,
    MixedCurrencyError,
    MixedDataSourceError,
)
from growthpulse_ai.schema import AdPerformanceRecord

# An explicit context keeps results independent of any ambient decimal settings.
_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_ZERO = Decimal(0)

type Number = Decimal | int


def safe_divide(numerator: Number, denominator: Number) -> Decimal | None:
    """Return the quotient, or None when the denominator is zero."""
    if denominator == 0:
        return None
    return _CONTEXT.divide(Decimal(numerator), Decimal(denominator))


@dataclass(frozen=True, slots=True)
class MetricChange:
    metric: str
    baseline: Number | None
    current: Number | None
    absolute_change: Decimal | None
    percent_change: Decimal | None
    """In percent, so 25 means +25%. None when the baseline is zero or undefined."""


def compare_metric(metric: str, baseline: Number | None, current: Number | None) -> MetricChange:
    if baseline is None or current is None:
        return MetricChange(metric, baseline, current, None, None)
    absolute = _CONTEXT.subtract(Decimal(current), Decimal(baseline))
    ratio = safe_divide(absolute, baseline)
    percent = None if ratio is None else _CONTEXT.multiply(ratio, Decimal(100))
    return MetricChange(metric, baseline, current, absolute, percent)


@dataclass(frozen=True, slots=True)
class ConversionBasis:
    """What a conversion means. Conversions are comparable only within one basis."""

    basis_id: str
    platform: str
    event_id: str
    event_name: str
    stage: str
    attribution_model: str | None
    click_window_days: int | None
    view_window_days: int | None
    report_time_basis: str | None

    @classmethod
    def from_record(cls, record: AdPerformanceRecord) -> "ConversionBasis":
        attribution = record.attribution
        canonical = json.dumps(record.conversion_basis, separators=(",", ":"))
        return cls(
            basis_id=hashlib.sha256(canonical.encode()).hexdigest()[:16],
            platform=record.platform.value,
            event_id=record.conversion_definition.event_id,
            event_name=record.conversion_definition.event_name,
            stage=record.conversion_definition.stage.value,
            attribution_model=attribution.attribution_model.value if attribution else None,
            click_window_days=attribution.click_window_days if attribution else None,
            view_window_days=attribution.view_window_days if attribution else None,
            report_time_basis=attribution.report_time_basis.value if attribution else None,
        )

    @property
    def has_attribution(self) -> bool:
        return self.attribution_model is not None


@dataclass(frozen=True, slots=True)
class DeliveryKpis:
    spend: Decimal
    impressions: int
    clicks: int
    ctr: Decimal | None
    cpc: Decimal | None


@dataclass(frozen=True, slots=True)
class ConversionKpis:
    """KPIs for one basis, using only the spend and clicks of rows reporting that basis."""

    basis: ConversionBasis
    spend: Decimal
    clicks: int
    platform_attributed_conversions: Decimal
    platform_attributed_conversion_value: Decimal
    platform_attributed_conversion_rate: Decimal | None
    cost_per_platform_attributed_conversion: Decimal | None
    """A platform-reported proxy. This is not true customer acquisition cost."""
    platform_attributed_roas: Decimal | None


@dataclass(frozen=True, slots=True)
class KpiSet:
    data_source: str
    currency: str
    account_timezones: tuple[str, ...]
    delivery: DeliveryKpis
    conversions_by_basis: tuple[ConversionKpis, ...]

    @property
    def by_basis(self) -> dict[str, ConversionKpis]:
        return {item.basis.basis_id: item for item in self.conversions_by_basis}


def validate_records(records: Sequence[AdPerformanceRecord]) -> None:
    """Reject input that would silently double count or mix incompatible data."""
    if len({record.data_source for record in records}) > 1:
        raise MixedDataSourceError("Records mix synthetic and live data")
    if len({record.record_id for record in records}) != len(records):
        raise DuplicateRecordError("The same record identity appears more than once")
    delivery_values: defaultdict[tuple[str, ...], set] = defaultdict(set)
    for record in records:
        delivery_values[record.delivery_key].add(
            (record.impressions, record.clicks, record.spend, record.currency)
        )
    conflicts = sorted(key for key, values in delivery_values.items() if len(values) > 1)
    if conflicts:
        raise ConflictingDeliveryError(
            f"{len(conflicts)} delivery_key value(s) report conflicting metrics, "
            f"first on {conflicts[0][1]} for campaign {conflicts[0][4]}"
        )


def _total(values: Iterable[Decimal]) -> Decimal:
    return reduce(_CONTEXT.add, values, _ZERO)


def _delivery_totals(records: Iterable[AdPerformanceRecord]) -> tuple[Decimal, int, int]:
    """Sum delivery metrics once per delivery_key."""
    unique = {record.delivery_key: record for record in records}.values()
    return (
        _total(record.spend for record in unique),
        sum(record.impressions for record in unique),
        sum(record.clicks for record in unique),
    )


def _conversion_kpis(records: Sequence[AdPerformanceRecord]) -> ConversionKpis:
    spend, _, clicks = _delivery_totals(records)
    conversions = _total(record.conversions for record in records)
    value = _total(record.conversion_value for record in records)
    return ConversionKpis(
        basis=ConversionBasis.from_record(records[0]),
        spend=spend,
        clicks=clicks,
        platform_attributed_conversions=conversions,
        platform_attributed_conversion_value=value,
        platform_attributed_conversion_rate=safe_divide(conversions, clicks),
        cost_per_platform_attributed_conversion=safe_divide(spend, conversions),
        platform_attributed_roas=safe_divide(value, spend),
    )


def calculate_kpis(records: Iterable[AdPerformanceRecord]) -> KpiSet:
    """Calculate KPIs for one group of records, which must share a single currency."""
    rows = list(records)
    if not rows:
        raise AnalyticsError("Cannot calculate KPIs for no records")
    if len({row.currency for row in rows}) > 1:
        raise MixedCurrencyError("Records mix currencies. Aggregate each currency separately")
    validate_records(rows)

    spend, impressions, clicks = _delivery_totals(rows)
    by_basis: defaultdict[tuple, list[AdPerformanceRecord]] = defaultdict(list)
    for row in rows:
        by_basis[row.conversion_basis].append(row)
    conversions = sorted(
        (_conversion_kpis(group) for group in by_basis.values()),
        key=lambda item: item.basis.basis_id,
    )
    return KpiSet(
        data_source=rows[0].data_source.value,
        currency=rows[0].currency,
        account_timezones=tuple(sorted({row.account_timezone for row in rows})),
        delivery=DeliveryKpis(
            spend=spend,
            impressions=impressions,
            clicks=clicks,
            ctr=safe_divide(clicks, impressions),
            cpc=safe_divide(spend, clicks),
        ),
        conversions_by_basis=tuple(conversions),
    )


DELIVERY_METRICS = ("spend", "impressions", "clicks", "ctr", "cpc")
CONVERSION_METRICS = (
    "platform_attributed_conversions",
    "platform_attributed_conversion_value",
    "platform_attributed_conversion_rate",
    "cost_per_platform_attributed_conversion",
    "platform_attributed_roas",
)


def compare_delivery(baseline: DeliveryKpis, current: DeliveryKpis) -> tuple[MetricChange, ...]:
    return tuple(
        compare_metric(name, getattr(baseline, name), getattr(current, name))
        for name in DELIVERY_METRICS
    )


def compare_conversions(
    baseline: ConversionKpis, current: ConversionKpis
) -> tuple[MetricChange, ...]:
    """Compare two results for the same basis. Different bases are never comparable."""
    if baseline.basis.basis_id != current.basis.basis_id:
        raise AnalyticsError("Cannot compare conversions from different conversion bases")
    return tuple(
        compare_metric(name, getattr(baseline, name), getattr(current, name))
        for name in CONVERSION_METRICS
    )
