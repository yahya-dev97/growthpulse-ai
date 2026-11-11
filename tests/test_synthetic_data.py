import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import pytest

from growthpulse_ai.connectors.synthetic_data import (
    CAMPAIGNS,
    DEFAULT_DAYS,
    DEFAULT_START_DATE,
    SCENARIOS,
    ScenarioKind,
    generate_synthetic_rows,
)
from growthpulse_ai.data_quality import validate_rows
from growthpulse_ai.schema import CampaignType, Platform

GOLDEN_DIGEST = "fcf86a71e6d1f9a1776863b8443a0e4bc809ef3dc5fdba1f423683a78fabf45f"


def digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def rows_by_campaign() -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for row in generate_synthetic_rows():
        grouped[row["campaign_id"]].append(row)
    return grouped


def total(rows: list[dict], field: str) -> Decimal:
    return sum((Decimal(str(row[field])) for row in rows), Decimal(0))


def cost_per_conversion(rows: list[dict]) -> Decimal:
    return total(rows, "spend") / total(rows, "conversions")


def conversion_rate(rows: list[dict]) -> Decimal:
    return total(rows, "conversions") / total(rows, "clicks")


def scenario_campaign_id(kind: ScenarioKind) -> str:
    return next(f"synthetic-{s.campaign_key}" for s in SCENARIOS if s.kind is kind)


def test_same_seed_reproduces_rows():
    assert generate_synthetic_rows(seed=7) == generate_synthetic_rows(seed=7)


def test_different_seed_changes_rows():
    assert digest(generate_synthetic_rows(seed=7)) != digest(generate_synthetic_rows(seed=8))


def test_default_dataset_matches_golden_digest():
    assert digest(generate_synthetic_rows()) == GOLDEN_DIGEST


def test_output_does_not_depend_on_the_clock():
    rows = generate_synthetic_rows()
    assert {row["ingested_at"] for row in rows} == {"2026-10-31T06:00:00+00:00"}


def test_covers_thirty_days_for_every_campaign():
    expected = {(DEFAULT_START_DATE + timedelta(days=i)).isoformat() for i in range(DEFAULT_DAYS)}
    grouped = rows_by_campaign()
    assert len(grouped) == len(CAMPAIGNS)
    for rows in grouped.values():
        assert len(rows) == 30
        assert {row["reporting_date"] for row in rows} == expected


def test_covers_required_channels_and_modalities():
    types = {spec.campaign_type for spec in CAMPAIGNS}
    assert {CampaignType.GOOGLE_SEARCH, CampaignType.GOOGLE_PERFORMANCE_MAX} <= types
    assert any(spec.platform is Platform.META_ADS for spec in CAMPAIGNS)
    for modality in ("MRI", "CT", "Ultrasound"):
        for platform_label in ("Search", "PMax", "Meta"):
            assert any(
                modality in spec.label and spec.label.startswith(platform_label)
                for spec in CAMPAIGNS
            )


def test_campaigns_have_different_spend_levels():
    spends = {campaign: total(rows, "spend") for campaign, rows in rows_by_campaign().items()}
    assert max(spends.values()) > 4 * min(spends.values())


def test_days_fluctuate():
    for rows in rows_by_campaign().values():
        assert len({row["spend"] for row in rows}) > 20


def test_every_generated_row_is_valid():
    result = validate_rows(generate_synthetic_rows())
    assert len(result.records) == 300
    assert result.quarantined == ()


def test_rows_are_clearly_labeled():
    for row in generate_synthetic_rows():
        assert row["data_source"] == "synthetic"
        assert row["campaign_name"].startswith("[SYNTHETIC] ")
        assert row["campaign_id"].startswith("synthetic-")
        assert row["account_id"].startswith("synthetic-")
        assert row["conversion_definition"]["event_id"].startswith("synthetic-")


def test_no_personal_or_credential_fields():
    text = json.dumps(generate_synthetic_rows()).lower()
    for term in ("patient", "email", "phone", "nhs", "postcode", "token", "password", "@"):
        assert term not in text


def test_generated_rows_cannot_be_mutated_through_shared_state():
    first = generate_synthetic_rows()
    first[0]["conversion_definition"]["event_id"] = "changed"
    first[0]["attribution"]["click_window_days"] = 99
    assert digest(generate_synthetic_rows()) != digest(first)


def test_google_conversions_are_fractional_and_meta_are_whole():
    rows = generate_synthetic_rows()
    google = [Decimal(r["conversions"]) for r in rows if r["platform"] == "google_ads"]
    meta = [Decimal(r["conversions"]) for r in rows if r["platform"] == "meta_ads"]
    assert any(value != value.to_integral_value() for value in google)
    assert all(value == value.to_integral_value() for value in meta)


def test_platforms_use_different_conversion_bases():
    records = validate_rows(generate_synthetic_rows()).records
    google = {r.conversion_basis for r in records if r.platform is Platform.GOOGLE_ADS}
    meta = {r.conversion_basis for r in records if r.platform is Platform.META_ADS}
    assert google.isdisjoint(meta)


def test_clicks_never_exceed_impressions_across_seeds():
    for seed in range(20):
        for row in generate_synthetic_rows(seed=seed):
            assert row["clicks"] <= row["impressions"]


def test_other_seeds_also_validate():
    for seed in range(20):
        assert validate_rows(generate_synthetic_rows(seed=seed)).quarantined == ()


def test_sustained_cost_per_conversion_increase():
    campaign = scenario_campaign_id(ScenarioKind.SUSTAINED_COST_PER_CONVERSION_INCREASE)
    rows = rows_by_campaign()[campaign]
    early, middle, late = rows[:10], rows[20:25], rows[25:]
    assert cost_per_conversion(late) > Decimal("1.4") * cost_per_conversion(early)
    assert cost_per_conversion(middle) > Decimal("1.4") * cost_per_conversion(early)


def test_conversion_rate_decline():
    campaign = scenario_campaign_id(ScenarioKind.CONVERSION_RATE_DECLINE)
    rows = rows_by_campaign()[campaign]
    early, late = rows[:12], rows[20:]
    assert conversion_rate(late) < Decimal("0.7") * conversion_rate(early)


def test_conversion_rate_decline_is_not_a_spend_change():
    campaign = scenario_campaign_id(ScenarioKind.CONVERSION_RATE_DECLINE)
    rows = rows_by_campaign()[campaign]
    early, late = total(rows[:10], "spend"), total(rows[20:], "spend")
    assert abs(late - early) / early < Decimal("0.2")


def test_strong_performer_has_lowest_cost_per_conversion():
    campaign = scenario_campaign_id(ScenarioKind.STRONG_PERFORMER)
    costs = {
        name: cost_per_conversion(rows)
        for name, rows in rows_by_campaign().items()
        if total(rows, "conversions") > 0
    }
    assert min(costs, key=costs.get) == campaign


def test_zero_conversion_window_has_spend_and_no_conversions():
    scenario = next(s for s in SCENARIOS if s.kind is ScenarioKind.ZERO_CONVERSIONS)
    rows = rows_by_campaign()[f"synthetic-{scenario.campaign_key}"]
    window = rows[scenario.start_day : scenario.end_day + 1]
    assert len(window) == 7
    assert all(Decimal(row["conversions"]) == 0 for row in window)
    assert all(Decimal(row["conversion_value"]) == 0 for row in window)
    assert all(Decimal(row["spend"]) > 0 for row in window)
    assert total(rows[: scenario.start_day], "conversions") > 0


def test_scenarios_reference_real_campaigns():
    keys = {spec.key for spec in CAMPAIGNS}
    assert {scenario.campaign_key for scenario in SCENARIOS} <= keys


def test_custom_range_is_supported():
    rows = generate_synthetic_rows(start_date=date(2026, 1, 1), days=3)
    assert len(rows) == 30


def test_rejects_non_positive_days():
    with pytest.raises(ValueError, match="days"):
        generate_synthetic_rows(days=0)
