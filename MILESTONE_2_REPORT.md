# Milestone 2 Report: Normalized Data and Synthetic Connector

**Status:** complete and approved.

## Result

Advertising data now flows through a typed schema, shared quality rules, and a replaceable connector. A deterministic 300-record synthetic dataset passes validation with no rejected rows, and all 214 tests pass. No dependencies were added, and nothing connected to Supabase, an advertising account, or GitHub.

The Milestone 1 foundation was committed locally as `2559893` after the staged diff was checked.

## Files

| Status | Path | Purpose |
|---|---|---|
| New | `src/growthpulse_ai/schema.py` | Normalized models, record identity, comparability keys |
| New | `src/growthpulse_ai/data_quality.py` | Validation, quarantine, errors and warnings |
| New | `src/growthpulse_ai/connectors/base.py` | Connector interface and `ConnectorError` |
| New | `src/growthpulse_ai/connectors/synthetic.py` | Synthetic connector |
| New | `src/growthpulse_ai/connectors/synthetic_data.py` | Deterministic generator and planted scenarios |
| New | `tests/factories.py`, `tests/test_schema.py`, `tests/test_data_quality.py`, `tests/test_connector.py`, `tests/test_synthetic_data.py` | Test suite |
| Changed | `tests/test_architecture.py` | Shared modules added to the import boundaries |
| Changed | `tests/test_smoke.py` | New modules added to the import check |
| Changed | `README.md` | Data model, assumptions, connector, quality, mapping, attribution |
| New | `MILESTONE_2_REPORT.md` | This report |

## Schema decisions

| Decision | Reason |
|---|---|
| One row per campaign, date, conversion definition, and attribution setting | Conversions mean nothing without their definition and rules |
| `conversions` is `Decimal` | Google's data-driven attribution reports fractional conversions |
| Money keeps six decimal places | Google reports cost in micros |
| `record_id` is a hash of identity dimensions only | A restated row updates in place on upsert |
| `data_source` is part of the identity | Synthetic rows can never overwrite live rows |
| `delivery_key` and a consistency rule | Several conversion events on a campaign-day repeat the same spend, which would otherwise be double counted |
| `conversion_basis` | Makes "are these comparable?" an explicit check |
| Schema and quality rules sit outside the layers | Connectors and analytics may not import each other |
| Rejected values never appear in issues, logs, or error text | A misplaced secret cannot leak |

Platform-attributed conversions, actual bookings, and true CAC are defined in the README under "Attribution limitations".

## Dataset size and coverage

| Measure | Value |
|---|---|
| Records | 300 (10 campaigns × 30 days), all unique identities |
| Dates | 2026-10-01 to 2026-10-30 |
| Google Search | 4 campaigns, 120 records |
| Google Performance Max | 3 campaigns, 90 records |
| Meta Ads | 3 campaigns, 90 records (60 leads, 30 sales) |
| Modalities | MRI, CT, and ultrasound on each channel |
| Total synthetic spend | £62,289.26 |
| Distinct conversion bases | 3 |
| Validation outcome | 300 valid, 0 quarantined, 12 warnings (all zero conversions with spend) |
| Seed | `20261001` |

## Synthetic anomaly scenarios

Measured from the generated data, comparing the first 10 days with the last 10 days. All figures are synthetic.

| Scenario | Campaign | Active | Measured effect |
|---|---|---|---|
| Sustained cost-per-conversion increase | `gs-mri-london` | Days 13–30 | Cost per conversion £52.77 → £108.88 |
| Conversion-rate decline | `pmax-ct-national` | Days 16–30 | Conversion rate 2.91% → 1.30% |
| Strong performer | `gs-mri-brand` | All 30 days | £7.41 cost per conversion, lowest of all campaigns |
| Zero conversions | `meta-ct-prospecting` | Days 20–26 | 7 days of spend with no conversions |

The requested "sustained CAC increase" is implemented as a rise in cost per platform-attributed conversion, because true CAC cannot be derived from advertising data. The other 5 zero-conversion days occur naturally on low-volume campaigns.

## Test results

Final run, executed in the project directory:

| Command | Result |
|---|---|
| `uv lock --check` | Lockfile up to date |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 25 files already formatted |
| `uv run pytest` | **214 passed, 0 failed** |

| Test file | Tests |
|---|---|
| `test_schema.py` | 74 |
| `test_data_quality.py` | 63 |
| `test_synthetic_data.py` | 24 |
| `test_connector.py` | 23 |
| `test_smoke.py` | 14 |
| `test_config.py` | 13 |
| `test_architecture.py` | 3 |

Additional verification:

| Check | Result |
|---|---|
| Fresh install of only the files Git would commit, in a scratch directory | Lint, format, and 214 tests passed |
| 25 rules and behaviors removed one at a time in a scratch copy | All 25 caused a test failure |
| Dataset reproducibility | Pinned by a SHA-256 digest of the full default dataset |
| Validation across 20 other seeds | No rejected rows |

Problems found and fixed during development:

- One test wrongly assumed London was on UTC in October.
- The first removal checks showed two untested protections: strict date parsing and hiding rejected values in raw validation errors. Tests were added for both.

## Known limitations

- Only one conversion event per campaign is generated. Multi-event rows are covered by tests, not by the dataset.
- Only the first cross-field problem on a row is reported, though all field-level problems are.
- Quarantined rows are held in memory. Durable storage belongs with persistence in Milestone 4.
- The Google and Meta field mapping was written from existing knowledge and has not been tested against a live API.
- Reproducibility was run on Python 3.12.11 only.
- The GitHub Actions workflow has still not been executed.
- Synthetic values illustrate behavior and are not industry benchmarks.

## Next milestone dependencies

Milestone 3 (KPI calculation and anomaly detection) needs no accounts or credentials. It will build on:

- `result.records` as its only input.
- `delivery_key`, to aggregate spend without double counting.
- `conversion_basis`, to avoid combining unlike conversions.
- `SCENARIOS`, as ground truth for testing detection.

Supabase (Milestone 4) and the Anthropic API key (Milestone 5) remain subject to your approval.
