# GrowthPulse AI — technical reference

> Moved from the repository root on 2026-11-12. The portfolio overview is now [README.md](../README.md). This file keeps the detailed reference: data model, attribution rules, KPI formulas, thresholds, report structure and persistence. Its architecture section and roadmap table describe the original plan, which named a Streamlit dashboard. The implemented dashboard is plain HTML in [frontend/](../frontend/README.md), and the README's roadmap supersedes the one here.

GrowthPulse ingests Google Ads and Meta Ads performance data (synthetic only, today), calculates marketing KPIs, detects anomalies, and stores results in Supabase PostgreSQL. The Anthropic insight pipeline is built and tested offline. One approved live test was made, and its response failed validation. It's documented in [AI_INSIGHTS.md](AI_INSIGHTS.md). Planned, not yet built: live advertising connectors and a hosted dashboard. See the capability matrix in [README.md](../README.md).

> **Status: Milestone 3 (KPI analytics and anomaly detection).** AI analysis, persistence, and the dashboard do not exist yet. See the [roadmap](#milestone-roadmap).

## Goals

- Turn raw advertising data into KPIs, anomalies, and recommendations a growth team can act on.
- Tie every AI insight to specific computed metrics, so each claim can be checked.
- Run on synthetic data with no accounts, and support authorized live connections through the same interfaces.
- Keep scheduled processing separate from the dashboard.

## Architecture

There are two separate runtimes. They share one library and communicate only through the database.

```text
 SCHEDULED BACKEND (GitHub Actions)              DASHBOARD (Streamlit)
 ┌────────────────────────────────┐              ┌──────────────────┐
 │ pipeline/  (orchestrator)      │              │ dashboard/       │
 │  connectors → analytics → ai   │              │ read-only views  │
 └───────────────┬────────────────┘              └────────┬─────────┘
                 │ write                                  │ read
                 └──────► persistence/ ◄──────────────────┘
                          Supabase Postgres
```

| Package | Responsibility | Boundary |
|---|---|---|
| `connectors/` | Synthetic, Google Ads, and Meta Ads sources, normalized to one schema | Read-only |
| `analytics/` | KPI calculation and anomaly detection | Pure functions, no I/O |
| `ai_analysis/` | Insights and experiment recommendations via the Anthropic API, validated against the report's evidence, with a deterministic fallback and a spend ledger ([details](AI_INSIGHTS.md)) | Works only from computed metrics. Imports no connector, persistence, pipeline, dashboard or credentials code |
| `persistence/` | Supabase PostgreSQL access | The only package that runs SQL |
| `pipeline/` | Scheduled job | The only package that composes the backend layers |
| `dashboard/` | Streamlit UI | Never imports connectors, AI analysis, or the pipeline |

Two shared modules sit below every layer and import none of them. `schema.py` defines the normalized records, and `data_quality.py` holds the validation rules. Connectors and analytics meet only at the schema, so a new data source requires no analytics changes.

`tests/test_architecture.py` enforces these import boundaries, so a violation fails the test suite.

```text
growthpulse-ai/
├── .github/workflows/ci.yml    # lint, format check, and tests
├── src/growthpulse_ai/
│   ├── config.py               # settings from environment variables
│   ├── schema.py               # normalized record models and identity
│   ├── data_quality.py         # validation, quarantine, and warnings
│   ├── connectors/
│   │   ├── base.py             # interface every data source implements
│   │   ├── synthetic.py        # synthetic connector
│   │   └── synthetic_data.py   # deterministic dataset generator
│   ├── analytics/
│   │   ├── periods.py          # date ranges and comparisons
│   │   ├── kpis.py             # KPI formulas and aggregation guards
│   │   ├── noise.py            # conversion-count noise guard
│   │   ├── anomalies.py        # configurable rules and findings
│   │   ├── trends.py           # finding groups and persistence
│   │   └── report.py           # JSON-compatible report
│   ├── ai_analysis/
│   ├── persistence/
│   ├── pipeline/
│   │   └── sample_report.py    # local end-to-end flow
│   └── dashboard/
├── docs/samples/               # generated sample report
├── tests/
├── .env.example
├── pyproject.toml
└── uv.lock
```

## Safety guardrails

- **Read-only advertising access.** Integrations only read reporting data. GrowthPulse never changes campaigns, budgets, bids, or any other advertising object, and recommendations are advisory.
- **Least-privilege credentials.** Meta tokens use `ads_read` only. Google's Ads OAuth scope has no read-only variant, so the authorizing Google user should hold "Read only" access on the ads account.
- **No invented results.** Synthetic data is always labeled as synthetic and is never presented as real campaign performance.
- **No patient information.** The system works with aggregated advertising metrics. Do not load real patient or other personal health data.
- **No committed secrets.** Credentials come from environment variables only, and `.gitignore` excludes `.env` and common credential files.
- **Bounded AI spend.** Live Anthropic calls are off by default and need explicit opt-in. Each one reserves its maximum cost in a local ledger capped at $1.00, makes no retries, and fails closed whenever its cost can't be determined. See [AI_INSIGHTS.md](AI_INSIGHTS.md).

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and Git. uv provides Python 3.12 if it is not already installed.

```bash
uv sync                  # create .venv and install locked dependencies
cp .env.example .env     # optional: not needed for synthetic data
```

## Development

```bash
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
```

## Configuration

All settings are environment variables, documented in [.env.example](../.env.example). Real environment variables take precedence over `.env`. Setting `DATA_SOURCE=live` fails at startup unless the Google Ads and Meta credentials are present.

## Normalized data model

`AdPerformanceRecord` in [schema.py](../src/growthpulse_ai/schema.py) is immutable and rejects unknown fields. One record is **one campaign, on one reporting date, for one conversion definition under one attribution setting**.

| Field | Type | Notes |
|---|---|---|
| `reporting_date` | date | `YYYY-MM-DD`, in the account's reporting timezone |
| `platform` | enum | `google_ads` or `meta_ads` |
| `account_id`, `campaign_id`, `campaign_name` | string | Required, non-blank |
| `campaign_type` | enum | Must belong to the platform |
| `impressions`, `clicks` | integer | Non-negative |
| `spend` | Decimal | Up to six decimal places, to hold Google's micros |
| `conversions` | Decimal | Platform-attributed. Fractional under data-driven attribution |
| `conversion_value` | Decimal | Platform-reported, not realized revenue |
| `currency` | string | ISO 4217 |
| `account_timezone` | string | IANA name |
| `conversion_definition` | object | `event_id`, `event_name`, funnel `stage` |
| `attribution` | object or null | `attribution_model`, `click_window_days`, `view_window_days`, `report_time_basis` |
| `data_source` | enum | `synthetic` or `live` |
| `ingested_at` | datetime | Timezone-aware |

Three derived values govern how records are used:

- **`record_id`** is a SHA-256 of data source, date, platform, account, campaign, conversion event, and attribution settings. It is the upsert key. Metrics, names, and `ingested_at` are excluded so a restated row updates in place, and data source is included so synthetic rows can never overwrite live rows.
- **`delivery_key`** is the grain of impressions, clicks, and spend. Rows for several conversion events on one campaign-day repeat the same delivery metrics, so those metrics must be aggregated at this grain to avoid double counting.
- **`conversion_basis`** identifies what a conversion means. Conversions may only be compared or summed between records with the same basis.

## Attribution limitations

These three measures are different, and only the first exists in this schema:

| Measure | Source | What it means |
|---|---|---|
| Platform-attributed conversions | Google Ads, Meta | The platform's claim, under its own rules. Google and Meta can both claim the same customer, and the sum across platforms can exceed the real total |
| Actual bookings | The business's booking system | Bookings that really happened. Requires first-party data |
| True customer acquisition cost | Finance and booking records | Total acquisition cost divided by new customers, including costs outside ad platforms |

Spend divided by `conversions` is therefore **cost per platform-attributed conversion**, a proxy that should not be reported as CAC.

Google and Meta conversions are not directly comparable. They can differ in the event tracked, the funnel stage, the attribution model, the lookback windows, view-through credit, and the date a conversion is reported against.

The analytics engine enforces this by naming outcome metrics `platform_attributed_*`, reporting them per conversion basis, and setting `true_cac_available` to `false` in every report.

## Synthetic data assumptions

The generator in [synthetic_data.py](../src/growthpulse_ai/connectors/synthetic_data.py) models a fictional UK medical imaging marketplace, "ExampleScan UK". All values are invented and illustrate behavior only. They are not benchmarks.

- 10 campaigns across Google Search, Performance Max, and Meta, covering MRI, CT, and ultrasound, for 30 days from 2026-10-01: 300 records in GBP on `Europe/London`.
- Fixed seed `20261001`. Output depends only on the arguments, never on the clock.
- Weekday patterns, autocorrelated CPC and conversion-rate drift, and sampled daily conversions.
- Google uses a booking-request event with fractional data-driven credit. Meta uses whole conversions on a 7-day click and 1-day view window, mostly on a lead event, so the two platforms are deliberately not like-for-like.
- Records are labeled through `data_source`, `synthetic-` identifiers, and a `[SYNTHETIC]` campaign name prefix. They contain aggregated advertising metrics only, with no patient or personal data.
- Planted scenarios are listed in `SCENARIOS`, as ground truth for testing anomaly detection later.

## Connector interface

Every source subclasses `AdDataConnector` and implements one read method. Validation, quarantine, and logging are inherited.

```python
class AdDataConnector(ABC):
    name: ClassVar[str]
    data_source: ClassVar[DataSource]

    @abstractmethod
    def _fetch_raw(self, start_date: date, end_date: date) -> Iterable[Any]: ...

    def fetch(self, start_date: date, end_date: date) -> ValidationResult: ...
```

```python
result = SyntheticConnector().fetch(date(2026, 10, 1), date(2026, 10, 30))
result.records  # validated AdPerformanceRecord objects, for analytics
result.quarantined  # rejected rows, each with structured issues
result.warnings  # accepted-with-caveat issues
result.summary  # counts by outcome and issue code
```

A source that cannot be read raises `ConnectorError`, which names only the exception type because API errors can embed tokens in URLs. Connectors expose no write operations.

## Data quality behavior

Hard failures quarantine the row, so it never reaches analytics. Warnings keep the record and flag it. Issue messages and logs never contain field values.

| Hard failure | Code |
|---|---|
| Negative spend, impressions, clicks, conversions, or value | `negative_value` |
| Clicks exceed impressions | `clicks_exceed_impressions` |
| Missing or blank campaign ID or name | `missing_campaign_identifier` |
| Malformed, impossible, or future date, or timestamp without a timezone | `invalid_date` |
| Not an ISO 4217 code | `invalid_currency` |
| Not an IANA timezone | `invalid_timezone` |
| Missing or incomplete conversion definition | `missing_conversion_definition` |
| Invalid attribution, or a setting the platform does not support | `invalid_attribution` |
| Campaign type outside the platform; account with several currencies or timezones; campaign with several types; event ID with several definitions; one campaign-day with differing delivery metrics | `inconsistent_dimensions` |
| Exact repeat of a record identity (the first row is kept) | `duplicate_record` |
| Same identity with different values (every copy is rejected) | `conflicting_duplicate` |
| Row's data source differs from the connector's | `data_source_mismatch` |
| Any other bad field or a non-record row | `invalid_field`, `invalid_schema` |

When rows in a group disagree, no single row can be trusted, so the whole group is rejected.

| Warning | Code |
|---|---|
| Attribution metadata unavailable | `missing_attribution` |
| Spend with no conversions | `zero_conversions_with_spend` |
| Spend with no impressions | `spend_without_impressions` |
| Clicks with no spend | `clicks_without_spend` |
| Conversions exceed clicks, which view-through credit can explain | `conversions_exceed_clicks` |
| Conversion value with no conversions | `value_without_conversions` |
| Campaign renamed during the period | `campaign_renamed` |
| Attribution settings changed during the period | `attribution_changed` |

## Future Google and Meta API mapping

Planned for Milestone 6, and to be verified against the API versions current at that time.

| Field | Google Ads (GAQL) | Meta Marketing API (Insights) |
|---|---|---|
| `reporting_date` | `segments.date` | `date_start`, with `time_increment=1` |
| `account_id` | `customer.id` | `account_id` |
| `campaign_id`, `campaign_name` | `campaign.id`, `campaign.name` | `campaign_id`, `campaign_name`, with `level=campaign` |
| `campaign_type` | `campaign.advertising_channel_type` | `objective` |
| `impressions` | `metrics.impressions` | `impressions` |
| `clicks` | `metrics.clicks` | `inline_link_clicks` (`clicks` counts all clicks) |
| `spend` | `metrics.cost_micros` ÷ 1,000,000 | `spend` |
| `conversions` | `metrics.conversions` | `actions`, by action type |
| `conversion_value` | `metrics.conversions_value` | `action_values`, by action type |
| `currency` | `customer.currency_code` | `account_currency` |
| `account_timezone` | `customer.time_zone` | Ad account `timezone_name` |
| `conversion_definition` | `segments.conversion_action`, `conversion_action.category` | Action type, or custom conversion |
| `attribution` | `conversion_action.attribution_model_settings`, lookback window fields | `action_attribution_windows`, `action_report_time` |

Two points will shape those connectors. Google does not return impressions, clicks, or cost when a query is segmented by conversion action, so delivery and conversion queries must be joined. Meta restricts some lower-funnel event sharing for health-related advertisers, so the events available for a medical business must be confirmed first.

## KPI formulas

| KPI | Formula | Undefined (null) when |
|---|---|---|
| Spend, impressions, clicks | Sum, once per `delivery_key` | Never |
| CTR | clicks ÷ impressions | Impressions are zero |
| CPC | spend ÷ clicks | Clicks are zero |
| Platform-attributed conversions | Sum within one conversion basis | Never |
| Platform-attributed conversion rate | conversions ÷ clicks | Clicks are zero |
| Cost per platform-attributed conversion (**proxy, not true CAC**) | spend ÷ conversions | Conversions are zero |
| Platform-attributed conversion value | Sum within one conversion basis | Never |
| Platform-attributed ROAS | conversion value ÷ spend | Spend is zero |
| Absolute change | current − baseline | Either value is null |
| Percent change | (current − baseline) ÷ baseline × 100 | Baseline is zero or null |

Rates are fractions, so `0.05` means 5%. Percent changes are in percent, so `25` means +25%. Money uses `Decimal` throughout, and a zero conversion rate is a real `0`, distinct from an undefined `null`.

## Aggregation rules

- Delivery metrics are summed once per `delivery_key`, never once per conversion-event row.
- Conversions are never totalled across conversion bases. Results are always a list per basis, and no combined figure exists.
- Each basis calculates cost and ROAS only from the spend of the rows reporting that basis.
- An aggregate cannot span two currencies or mix synthetic and live data. Portfolios are per currency.
- Reporting dates stay in each account's timezone and are never converted.
- Conflicting delivery metrics or duplicate records stop the run with an error, rather than choosing a value.

Compared periods must be complete, non-overlapping, and equal in length. A day is complete only when data was ingested after that day ended in the account's timezone. The default report covers the full range, the latest 7 days against the previous 7, and the latest 10 days against the first 10.

## Anomaly thresholds

Rules compare one campaign on one conversion basis across two periods. All thresholds are fields of `AnomalyThresholds` and can be overridden. Findings state what changed and do not claim causes or statistical significance.

| Rule | Fires when | High severity at |
|---|---|---|
| `cost_per_conversion_increase` | Cost rises ≥ 25% and ≥ 5 in absolute terms, with ≥ 3 current conversions | ≥ 50% |
| `conversion_rate_decline` | Rate falls ≥ 20% | ≥ 40% |
| `spend_without_conversion_growth` | Spend rises ≥ 20% and conversion growth is under half of spend growth | Gap ≥ 40 points |
| `roas_deterioration` | ROAS falls ≥ 25%, with baseline value ≥ 500 | ≥ 50% |
| `spend_with_zero_conversions` | ≥ 3 consecutive days of spend with no conversions, run spend ≥ 200, and ≥ 3 conversions expected at baseline cost | ≥ 5 days |
| `strong_performer` | Cost ≤ 60% of peer median, ROAS ≥ 125% of peer median, ≥ 20 conversions, ≥ 3 same-basis peers, own cost not up more than 10% | Always `info` |

The first four rules, and peers for `strong_performer`, also need spend ≥ 100 and clicks ≥ 100 in both periods and ≥ 10 baseline conversions. Monetary thresholds are in the account's currency and are not converted.

Campaigns that cannot be compared are listed in `exclusions` with a reason: incomplete coverage, a conversion basis not present in both periods, unavailable attribution, or insufficient volume.

### Noise guard

The cost, conversion-rate, and ROAS rules also apply a guard, after their own thresholds and volume minimums. A change clears it when:

```text
|ln(current / baseline)|  >=  width × √(1/B + 1/C)
```

B and C are the platform-attributed conversion counts in the two periods. Default width is 1.5 (`noise_guard_width`). High severity needs a wider 2.0 (`noise_guard_high_severity_width`).

| Outcome | Meaning |
|---|---|
| `passed` | Clears the band and keeps its severity |
| `downgraded` | A high-severity change clears 1.5 but not 2.0, so it becomes medium. `pre_guard_severity` keeps the original |
| `suppressed` | Inside the band. Moved to `suppressed_findings` with all evidence intact |
| `not_evaluated` | A zero value or count leaves the log undefined, so the finding is left unchanged |
| `not_applicable` | The rule is not guarded |

Assumptions: conversion counts behave roughly like independent Poisson counts, spend and clicks are treated as fixed, and conversion-value variability is ignored, which makes the ROAS band optimistic. **This is a heuristic. It is not a statistical significance test or a confidence interval**, and it implies no p-value or coverage. Platform-attributed conversions are not verified customers or bookings. Setting both widths to 0 disables the guard.

### Comparison windows

Each comparison has a `purpose` and is reported separately. Metrics from different windows are never combined.

- `recent_change`: latest 7 days against the previous 7.
- `sustained_change`: latest 10 days against the first 10, with the window length configurable.

`persistence_assessments` connects them. Each sustained deterioration is checked against the latest window, because a change that levelled off produces no recent finding but has not gone away.

| Status | Meaning |
|---|---|
| `recent_change_continuing` | The recent comparison also flags it |
| `plateaued_still_elevated` | No recent finding, but the latest window is still beyond the threshold and noise guard against the longer baseline |
| `not_confirmed_elevated` | The latest window does not clear both checks |
| `not_assessable` | The latest window has gaps or an undefined metric |

`resolution_confirmed` is always `false`, because this data cannot show that an issue has been fixed.

### Related findings

`finding_groups` links active adverse findings in the same campaign, conversion basis, and window whose metrics share an input, such as spend or conversions. Each original finding stays in place with its evidence. Metrics that share inputs can move together arithmetically, so a group does not establish that one caused another.

## Analytics report

Run the local flow, from synthetic connector through to the report:

```bash
uv run python -m growthpulse_ai.pipeline.sample_report
```

This writes [docs/samples/synthetic_analytics_report.json](samples/synthetic_analytics_report.json). The report is deterministic, contains no clock reading, and holds unformatted values, with decimals as strings to preserve precision. `report_id` and each `finding_id` are stable for later upserts. An abridged finding:

```json
{
  "finding_id": "1438d8f36e01b8a95910befd948a875b51acf32b6a31bf4618b889e0d5cfe782",
  "rule_id": "cost_per_conversion_increase",
  "severity": "high",
  "platform": "google_ads",
  "campaign_id": "synthetic-gs-mri-london",
  "metric": "cost_per_platform_attributed_conversion",
  "metric_label": "Cost per platform-attributed conversion (proxy, not true CAC)",
  "baseline_value": "52.76971731",
  "current_value": "108.87786653",
  "absolute_change": "56.10814922",
  "percent_change": "106.32641613",
  "baseline_period": { "start": "2026-10-01", "end": "2026-10-10" },
  "current_period": { "start": "2026-10-21", "end": "2026-10-30" },
  "evidence": { "baseline_conversions": "77.47", "current_conversions": "38.81" },
  "pre_guard_severity": "high",
  "noise_guard": {
    "outcome": "passed",
    "log_ratio_magnitude": "0.72428927",
    "band": "0.29498856",
    "high_severity_band": "0.39331808"
  },
  "data_limitations": [
    "This is a threshold rule. No statistical significance test was performed.",
    "The data shows that this changed. It does not establish why."
  ]
}
```

## Persistence

Results are stored in Supabase PostgreSQL through the `persistence` package, which is the only code that runs SQL. The schema is defined by the migrations in [supabase/migrations](../supabase/migrations), which are applied to the hosted project. Synthetic pipeline runs persist there idempotently. [MILESTONE_4_REPORT.md](../MILESTONE_4_REPORT.md) has the verified results.

- **Private schemas.** Tables live in `growthpulse` and dashboard views in `growthpulse_read`. Neither is exposed through the Supabase Data API, and the Supabase API roles (`anon`, `authenticated`, `service_role`) have no access to either.
- **Two roles.** The pipeline logs in as `growthpulse_writer`, which can insert and update named columns but cannot delete, truncate, or change the schema. The dashboard logs in as `growthpulse_dashboard`, which can only `SELECT` from the read views. The application refuses a connection that logs in as any other role, including `postgres`.
- **Idempotent writes.** Records are upserted by `record_id` and change only when their values differ. A report's ID is a hash of its content, so an identical report is stored once. Reports, experiment proposals and status events are append-only, and a database trigger rejects any update.
- **Failure records.** A run row is committed before any data is read. All data from a run is written in one transaction, so a failure leaves nothing half-written, and the run is then marked failed with a fixed error code, never an error message.
- **Experiments are advisory.** The pipeline can propose an experiment but cannot approve, start or complete one. The database enforces this and the allowed status transitions.

Run a persisted pipeline (requires `DATABASE_URL`, see [.env.example](../.env.example)):

```bash
uv run python -m growthpulse_ai.pipeline.persisted_run
```

Integration tests apply the migrations to a throwaway PostgreSQL server and are skipped unless `GROWTHPULSE_TEST_DATABASE_URL` is set. They drop and recreate the GrowthPulse schemas and refuse Supabase hosts. CI runs them against a PostgreSQL 17 container.

## Known limitations

- Rules and the noise guard involve no statistical test, so ordinary sampling variation can still trigger them. Across 200 synthetic seeds, 6.9% to 7.2% of campaigns with no planted anomaly received at least one active adverse finding. These rates describe this generator and do not predict real-world accuracy.
- The guard reduces sensitivity at low volumes, most of all in 7-day windows.
- A comparison only sees change between its two windows. Persistence assessments cover plateaus only where a longer comparison exists.
- Recent days can be restated as late conversions arrive, so the latest period may be understated.
- `conversion_rate` divides by clicks, so view-through credit can inflate it on Meta.
- Live connectors must emit zero-conversion rows, or days without conversions will look like missing data.
- Findings describe platform-reported data and do not establish incrementality.

## Milestone roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Project foundation: tooling, configuration, tests | Complete |
| 2 | Normalized data schema and synthetic data connector | Complete |
| 3 | KPI calculation and anomaly detection | Complete |
| 4 | Supabase schema and persistence layer | Complete: migrations applied, synthetic runs persisted |
| 5 | Multi-client and multi-ad-account architecture ([design](../MILESTONE_5_DESIGN.md)) | In progress: Phases A and B committed; Phase C complete locally; not yet applied to Supabase |
| 6 | OAuth and read-only Google Ads and Meta integrations | Planned |
| 7 | AI insights and experiment recommendations | Planned |
| 8 | Streamlit dashboard | Planned |
| 9 | Scheduled pipeline on GitHub Actions, deployment | Planned |
