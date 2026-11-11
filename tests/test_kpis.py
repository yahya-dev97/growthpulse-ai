from datetime import date
from decimal import Decimal, localcontext

import pytest

from factories import make_meta_record, make_record, make_row
from growthpulse_ai.analytics.errors import (
    AnalyticsError,
    ConflictingDeliveryError,
    DuplicateRecordError,
    MixedCurrencyError,
    MixedDataSourceError,
)
from growthpulse_ai.analytics.kpis import (
    calculate_kpis,
    compare_conversions,
    compare_delivery,
    compare_metric,
    safe_divide,
)


def second_event() -> dict:
    return {"event_id": "second", "event_name": "Second event", "stage": "lead"}


def only_conversions(kpis):
    assert len(kpis.conversions_by_basis) == 1
    return kpis.conversions_by_basis[0]


def test_delivery_kpis():
    delivery = calculate_kpis([make_record()]).delivery
    assert delivery.spend == Decimal("120.50")
    assert delivery.impressions == 1000
    assert delivery.clicks == 50
    assert delivery.ctr == Decimal("0.05")
    assert delivery.cpc == Decimal("2.41")


def test_conversion_kpis():
    kpis = only_conversions(calculate_kpis([make_record()]))
    assert kpis.platform_attributed_conversions == Decimal("3.25")
    assert kpis.platform_attributed_conversion_value == Decimal("900.00")
    assert kpis.platform_attributed_conversion_rate == Decimal("0.065")
    assert round(kpis.cost_per_platform_attributed_conversion, 8) == Decimal("37.07692308")
    assert round(kpis.platform_attributed_roas, 8) == Decimal("7.46887967")


def test_kpis_sum_across_days():
    rows = [
        make_record(reporting_date="2026-10-10"),
        make_record(reporting_date="2026-10-11", spend="79.50", clicks=150, impressions=1000),
    ]
    delivery = calculate_kpis(rows).delivery
    assert delivery.spend == Decimal("200.00")
    assert delivery.clicks == 200
    assert delivery.cpc == Decimal(1)


def test_money_is_decimal_exact():
    rows = [
        make_record(reporting_date="2026-10-10", spend="0.10"),
        make_record(reporting_date="2026-10-11", spend="0.20"),
    ]
    spend = calculate_kpis(rows).delivery.spend
    assert isinstance(spend, Decimal)
    assert spend == Decimal("0.30")


def test_fractional_conversions_are_preserved():
    rows = [
        make_record(reporting_date="2026-10-10", conversions="1.25"),
        make_record(reporting_date="2026-10-11", conversions="2.50"),
    ]
    assert only_conversions(calculate_kpis(rows)).platform_attributed_conversions == Decimal("3.75")


def test_results_ignore_ambient_decimal_precision():
    expected = calculate_kpis([make_record()])
    with localcontext() as context:
        context.prec = 3
        assert calculate_kpis([make_record()]) == expected


def test_safe_divide_returns_none_for_zero():
    assert safe_divide(Decimal(1), 0) is None
    assert safe_divide(Decimal(0), Decimal(0)) is None


def test_zero_impressions_make_ctr_undefined():
    row = make_record(impressions=0, clicks=0)
    assert calculate_kpis([row]).delivery.ctr is None


def test_zero_clicks_make_cpc_and_rate_undefined():
    kpis = calculate_kpis([make_record(clicks=0)])
    assert kpis.delivery.cpc is None
    assert only_conversions(kpis).platform_attributed_conversion_rate is None


def test_zero_conversions_make_cost_undefined_not_zero():
    kpis = only_conversions(calculate_kpis([make_record(conversions="0", conversion_value="0")]))
    assert kpis.cost_per_platform_attributed_conversion is None


def test_zero_conversions_with_clicks_is_a_real_zero_rate():
    kpis = only_conversions(calculate_kpis([make_record(conversions="0", conversion_value="0")]))
    assert kpis.platform_attributed_conversion_rate == Decimal(0)


def test_zero_spend_makes_roas_undefined():
    kpis = only_conversions(calculate_kpis([make_record(spend="0")]))
    assert kpis.platform_attributed_roas is None


def test_empty_input_is_rejected():
    with pytest.raises(AnalyticsError):
        calculate_kpis([])


def test_rejects_mixed_currencies():
    rows = [
        make_record(account_id="a", currency="GBP"),
        make_record(account_id="b", currency="EUR"),
    ]
    with pytest.raises(MixedCurrencyError):
        calculate_kpis(rows)


def test_rejects_mixed_data_sources():
    rows = [make_record(data_source="synthetic"), make_record(data_source="live")]
    with pytest.raises(MixedDataSourceError):
        calculate_kpis(rows)


def test_multi_event_rows_do_not_double_count_delivery():
    rows = [make_record(), make_record(conversion_definition=second_event())]
    delivery = calculate_kpis(rows).delivery
    assert delivery.spend == Decimal("120.50")
    assert delivery.impressions == 1000
    assert delivery.clicks == 50


def test_multi_event_rows_keep_conversions_separate():
    rows = [
        make_record(conversions="3"),
        make_record(conversion_definition=second_event(), conversions="7"),
    ]
    kpis = calculate_kpis(rows)
    assert sorted(c.platform_attributed_conversions for c in kpis.conversions_by_basis) == [3, 7]


def test_each_event_uses_the_full_unrepeated_spend():
    rows = [make_record(), make_record(conversion_definition=second_event())]
    assert [c.spend for c in calculate_kpis(rows).conversions_by_basis] == [Decimal("120.50")] * 2


def test_conflicting_delivery_is_detected():
    rows = [make_record(), make_record(conversion_definition=second_event(), spend="1.00")]
    with pytest.raises(ConflictingDeliveryError):
        calculate_kpis(rows)


@pytest.mark.parametrize("field,value", [("impressions", 2000), ("clicks", 49)])
def test_conflicting_counts_are_detected(field: str, value: int):
    rows = [make_record(), make_record(conversion_definition=second_event(), **{field: value})]
    with pytest.raises(ConflictingDeliveryError):
        calculate_kpis(rows)


def test_duplicate_records_are_rejected():
    with pytest.raises(DuplicateRecordError):
        calculate_kpis([make_record(), make_record()])


def test_platforms_keep_separate_conversion_results():
    kpis = calculate_kpis([make_record(), make_meta_record()])
    platforms = {item.basis.platform for item in kpis.conversions_by_basis}
    assert platforms == {"google_ads", "meta_ads"}
    assert len(kpis.conversions_by_basis) == 2


def test_no_cross_basis_conversion_total_exists():
    kpis = calculate_kpis([make_record(), make_meta_record()])
    assert not hasattr(kpis, "platform_attributed_conversions")
    assert not hasattr(kpis.delivery, "platform_attributed_conversions")


def test_basis_cost_uses_only_its_own_spend():
    google = make_record(spend="100.00", conversions="10")
    meta = make_meta_record(spend="900.00", conversions="10")
    kpis = calculate_kpis([google, meta]).by_basis
    costs = {item.basis.platform: item for item in kpis.values()}
    assert costs["google_ads"].cost_per_platform_attributed_conversion == Decimal(10)
    assert costs["meta_ads"].cost_per_platform_attributed_conversion == Decimal(90)


def test_attribution_windows_split_bases():
    row = make_row()
    changed = {**row["attribution"], "click_window_days": 7}
    rows = [make_record(), make_record(reporting_date="2026-10-11", attribution=changed)]
    assert len(calculate_kpis(rows).conversions_by_basis) == 2


def test_same_basis_sums_across_campaigns():
    rows = [make_record(campaign_id="a"), make_record(campaign_id="b")]
    kpis = only_conversions(calculate_kpis(rows))
    assert kpis.platform_attributed_conversions == Decimal("6.50")
    assert kpis.spend == Decimal("241.00")


def test_timezones_are_preserved():
    rows = [
        make_record(account_id="a", account_timezone="Europe/London"),
        make_record(account_id="b", account_timezone="America/New_York"),
    ]
    assert calculate_kpis(rows).account_timezones == ("America/New_York", "Europe/London")


def test_reporting_date_is_not_shifted_by_timezone():
    row = make_record(reporting_date="2026-10-10", account_timezone="Pacific/Auckland")
    assert row.reporting_date == date(2026, 10, 10)


def test_compare_metric_changes():
    change = compare_metric("x", Decimal(100), Decimal(125))
    assert change.absolute_change == Decimal(25)
    assert change.percent_change == Decimal(25)


def test_compare_metric_handles_decline():
    change = compare_metric("x", Decimal(100), Decimal(60))
    assert change.absolute_change == Decimal(-40)
    assert change.percent_change == Decimal(-40)


def test_zero_baseline_has_absolute_but_no_percent_change():
    change = compare_metric("x", Decimal(0), Decimal(5))
    assert change.absolute_change == Decimal(5)
    assert change.percent_change is None


@pytest.mark.parametrize(("baseline", "current"), [(None, Decimal(1)), (Decimal(1), None)])
def test_undefined_values_have_no_change(baseline, current):
    change = compare_metric("x", baseline, current)
    assert change.absolute_change is None
    assert change.percent_change is None


def test_compare_delivery_covers_every_metric():
    kpis = calculate_kpis([make_record()]).delivery
    names = [change.metric for change in compare_delivery(kpis, kpis)]
    assert names == ["spend", "impressions", "clicks", "ctr", "cpc"]


def test_comparing_different_bases_is_refused():
    google = calculate_kpis([make_record()]).conversions_by_basis[0]
    meta = calculate_kpis([make_meta_record()]).conversions_by_basis[0]
    with pytest.raises(AnalyticsError):
        compare_conversions(google, meta)
