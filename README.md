# GrowthPulse AI

An AI-powered performance marketing analysis platform. GrowthPulse ingests Google Ads and Meta Ads performance data, calculates marketing KPIs, detects anomalies, uses the Anthropic API to generate evidence-backed insights and experiment recommendations, stores the results in Supabase PostgreSQL, and displays them in a Streamlit dashboard.

> **Status: Milestone 2 (normalized data and synthetic connector).** KPI calculation and AI analysis do not exist yet. See the [roadmap](#milestone-roadmap).

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
| `ai_analysis/` | Insights and experiment recommendations via the Anthropic API | Works only from computed metrics |
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
│   ├── ai_analysis/
│   ├── persistence/
│   ├── pipeline/
│   └── dashboard/
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

All settings are environment variables, documented in [.env.example](.env.example). Real environment variables take precedence over `.env`. Setting `DATA_SOURCE=live` fails at startup unless the Google Ads and Meta credentials are present.

## Normalized data model

`AdPerformanceRecord` in [schema.py](src/growthpulse_ai/schema.py) is immutable and rejects unknown fields. One record is **one campaign, on one reporting date, for one conversion definition under one attribution setting**.

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

## Synthetic data assumptions

The generator in [synthetic_data.py](src/growthpulse_ai/connectors/synthetic_data.py) models a fictional UK medical imaging marketplace, "ExampleScan UK". All values are invented and illustrate behavior only. They are not benchmarks.

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

Planned for Milestone 8, and to be verified against the API versions current at that time.

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

## Milestone roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Project foundation: tooling, configuration, tests | Complete |
| 2 | Normalized data schema and synthetic data connector | Complete |
| 3 | KPI calculation and anomaly detection | Planned |
| 4 | Supabase schema and persistence layer | Planned |
| 5 | AI insights and experiment recommendations | Planned |
| 6 | Streamlit dashboard | Planned |
| 7 | Scheduled pipeline on GitHub Actions | Planned |
| 8 | Read-only Google Ads and Meta connectors, deployment | Planned |
