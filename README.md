# GrowthPulse AI

An AI-powered performance marketing analysis platform. GrowthPulse ingests Google Ads and Meta Ads performance data, calculates marketing KPIs, detects anomalies, uses the Anthropic API to generate evidence-backed insights and experiment recommendations, stores the results in Supabase PostgreSQL, and displays them in a Streamlit dashboard.

> **Status: Milestone 1 (project foundation).** No analysis features exist yet. See the [roadmap](#milestone-roadmap).

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

`tests/test_architecture.py` enforces these import boundaries, so a violation fails the test suite.

```text
growthpulse-ai/
├── .github/workflows/ci.yml    # lint, format check, and tests
├── src/growthpulse_ai/
│   ├── config.py               # settings from environment variables
│   ├── connectors/
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

## Milestone roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Project foundation: tooling, configuration, tests | In review |
| 2 | Normalized data schema and synthetic data connector | Planned |
| 3 | KPI calculation and anomaly detection | Planned |
| 4 | Supabase schema and persistence layer | Planned |
| 5 | AI insights and experiment recommendations | Planned |
| 6 | Streamlit dashboard | Planned |
| 7 | Scheduled pipeline on GitHub Actions | Planned |
| 8 | Read-only Google Ads and Meta connectors, deployment | Planned |
