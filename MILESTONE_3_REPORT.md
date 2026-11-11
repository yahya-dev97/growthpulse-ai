# Milestone 3 Report: KPI Analytics and Anomaly Detection

**Status:** complete, including the Milestone 3.1 refinements, and committed locally. Milestone 4 has not started.

## Result

Validated records now flow through a deterministic KPI engine and anomaly detector into a JSON-compatible report. All four planted scenarios are detected in the sustained comparison on the default dataset, and all 448 tests pass.

Milestone 3.1 added a noise guard, comparison purposes, related-finding groups, and persistence assessments. On synthetic data, the guard cut adverse findings on campaigns with no planted anomaly from 15.6%–19.9% to 6.9%–7.2%. **Synthetic results describe this generator and do not predict real-world accuracy.** No AI API, Supabase, or advertising account was used, no dependencies were added, and nothing was pushed.

## Git history

| Commit | Hash | Author and committer email |
|---|---|---|
| Milestone 1, rewritten from `f9e3950` | `2559893` | GitHub noreply address |
| Milestone 2 | `5d3504c` | GitHub noreply address |

The noreply address is set for this repository only. The rewrite kept the Milestone 1 file tree and commit dates identical, and the Milestone 2 files were preserved. The old commit object was purged. A scan of every Git object found no personal email, and a scan of the full history found no credentials.

## Files

| Status | Path | Purpose |
|---|---|---|
| New | `src/growthpulse_ai/analytics/errors.py` | Analytics error types |
| New | `src/growthpulse_ai/analytics/periods.py` | Periods, comparisons, completeness |
| New | `src/growthpulse_ai/analytics/kpis.py` | KPI formulas and aggregation guards |
| New | `src/growthpulse_ai/analytics/noise.py` | Conversion-count noise guard |
| New | `src/growthpulse_ai/analytics/anomalies.py` | Rules, thresholds, findings, exclusions |
| New | `src/growthpulse_ai/analytics/trends.py` | Finding groups and persistence assessments |
| New | `src/growthpulse_ai/analytics/report.py` | Deterministic report |
| New | `src/growthpulse_ai/pipeline/sample_report.py` | Local end-to-end flow |
| New | `docs/samples/synthetic_analytics_report.json` | Generated sample report |
| New | `tests/test_periods.py`, `tests/test_kpis.py`, `tests/test_anomalies.py`, `tests/test_noise_guard.py`, `tests/test_trends.py`, `tests/test_report.py` | Test suite |
| Changed | `tests/factories.py`, `tests/test_smoke.py`, `tests/test_architecture.py` | Record builders, new modules, analytics dependency check |
| Changed | `README.md` | Formulas, aggregation, thresholds, example, limitations |
| New | `MILESTONE_3_REPORT.md` | This report |

## Architectural decisions

| Decision | Reason |
|---|---|
| Analytics imports only the schema, enforced by a test | New data sources need no analytics changes |
| Results are per conversion basis, with no total field | A cross-platform conversion total cannot be produced by accident |
| Each basis uses only the spend of rows reporting it | Meta spend cannot dilute a Google cost per conversion |
| Conflicts and duplicates raise errors | The connector should already have rejected them, so reaching analytics means something is wrong |
| Non-comparable campaigns become exclusions | A skipped comparison stays visible |
| Zero-conversion rule looks for consecutive-day runs | An outage can straddle windows and never fill a whole period |
| Strong performer needs low cost and high ROAS | Cheap conversions can simply be low-value ones, as the ultrasound campaigns show |
| Metrics are named `platform_attributed_*` | The name carries the caveat wherever the number travels |
| Findings exclude causes, and their IDs exclude values | Avoids overclaiming, and a restated finding updates in place |
| Suppressed findings are kept in a separate list | Evidence stays available without raising an alert, and the finding keeps its ID |
| High severity needs a wider band than medium | A borderline change is reported, but not as high severity |
| Groups require shared metric inputs | A link is justified by arithmetic, and no causal claim is made |
| `resolution_confirmed` is always `false` | Advertising data cannot show that an issue has been fixed |

## Actual calculated synthetic KPIs

All figures are synthetic, for 2026-10-01 to 2026-10-30.

**Portfolio delivery (GBP):** spend £62,289.26, impressions 2,038,239, clicks 40,669, CTR 2.00%, CPC £1.53.

**By conversion basis, never combined:**

| Basis | Stage | Spend | Clicks | Conversions | Value | Rate | Cost per conversion (proxy) | ROAS |
|---|---|---|---|---|---|---|---|---|
| Google booking request | booking_request | £48,431.32 | 26,082 | 1,152.28 | £381,536.64 | 4.42% | £42.03 | 7.88 |
| Meta enquiry lead | lead | £11,740.49 | 12,817 | 256 | £91,534.54 | 2.00% | £45.86 | 7.80 |
| Meta booking request | booking_request | £2,117.45 | 1,770 | 92 | £13,856.13 | 5.20% | £23.02 | 6.54 |

**By campaign:**

| Campaign | Spend | Clicks | CTR | CPC | Conversions | Rate | Cost (proxy) | ROAS |
|---|---|---|---|---|---|---|---|---|
| `gs-mri-london` | £12,445.48 | 2,948 | 6.18% | £4.22 | 166.45 | 5.65% | £74.77 | 4.81 |
| `gs-ct-national` | £6,953.49 | 2,380 | 5.51% | £2.92 | 108.45 | 4.56% | £64.12 | 6.39 |
| `gs-ultrasound-manchester` | £3,690.15 | 1,987 | 7.13% | £1.86 | 108.77 | 5.47% | £33.93 | 4.26 |
| `gs-mri-brand` | £2,541.60 | 2,695 | 20.52% | £0.94 | 343.14 | 12.73% | £7.41 | 51.55 |
| `pmax-mri-national` | £11,135.24 | 7,232 | 2.17% | £1.54 | 194.90 | 2.69% | £57.13 | 6.25 |
| `pmax-ct-national` | £7,567.98 | 5,125 | 1.88% | £1.48 | 106.86 | 2.09% | £70.82 | 5.79 |
| `pmax-ultrasound-north` | £4,097.38 | 3,715 | 2.63% | £1.10 | 123.71 | 3.33% | £33.12 | 4.16 |
| `meta-mri-prospecting` | £7,270.01 | 7,600 | 1.29% | £0.96 | 165 | 2.17% | £44.06 | 7.65 |
| `meta-ct-prospecting` | £4,470.48 | 5,217 | 1.11% | £0.86 | 91 | 1.74% | £49.13 | 8.03 |
| `meta-ultrasound-retargeting` | £2,117.45 | 1,770 | 1.79% | £1.20 | 92 | 5.20% | £23.02 | 6.54 |

## Detected anomalies

Results on the default seed with the noise guard on. All figures are synthetic.

**Sustained comparison, latest 10 days (Oct 21–30) against first 10 days (Oct 1–10): 10 active, 4 suppressed, 3 groups.**

| Severity | Rule | Campaign | Baseline → current | Change | Guard | Classification |
|---|---|---|---|---|---|---|
| High | Cost per conversion | `gs-mri-london` | £52.77 → £108.88 | +106.3% | Passed | **Planted** |
| High | Conversion rate | `pmax-ct-national` | 2.91% → 1.30% | −55.4% | Passed | **Planted** |
| High | Zero conversions | `meta-ct-prospecting` | 22.74 expected → 0, Oct 21–26 | −100% | Not guarded | **Planted** |
| Info | Strong performer | `gs-mri-brand` | Peer median £57.09 → £6.88 | −88.0% | Not guarded | **Planted** |
| High | ROAS | `gs-mri-london` | 6.83 → 3.31 | −51.6% | Passed | Linked to planted change |
| High | Cost per conversion | `pmax-ct-national` | £50.29 → £113.58 | +125.8% | Passed | Linked to planted change |
| High | ROAS | `pmax-ct-national` | 8.09 → 3.69 | −54.4% | Passed | Linked to planted change |
| High | Cost per conversion | `meta-ct-prospecting` | £40.06 → £93.62 | +133.7% | Passed | Linked to planted change |
| High | Conversion rate | `meta-ct-prospecting` | 2.11% → 0.94% | −55.5% | Passed | Linked to planted change |
| High | ROAS | `meta-ct-prospecting` | 9.66 → 4.35 | −54.9% | Passed | Linked to planted change |

Suppressed and retained with evidence: the three `gs-ultrasound-manchester` false alarms (cost +38.3%, rate −24.0%, ROAS −28.9%) and the `gs-mri-london` conversion-rate change (−24.2%).

**Recent comparison, latest 7 days (Oct 24–30) against previous 7 days (Oct 17–23): 4 active, 1 suppressed, 1 group.**

| Severity | Rule | Campaign | Baseline → current | Change | Guard | Classification |
|---|---|---|---|---|---|---|
| Medium | Zero conversions | `meta-ct-prospecting` | 3.69 expected → 0, Oct 24–26 | −100% | Not guarded | **Planted**, partly in window |
| Info | Strong performer | `gs-mri-brand` | Peer median £59.94 → £6.10 | −89.8% | Not guarded | **Planted** |
| Medium | Cost per conversion | `gs-ultrasound-manchester` | £28.13 → £44.42 | +57.9% | Downgraded from high | **False alarm** |
| Medium | ROAS | `gs-ultrasound-manchester` | 5.18 → 3.19 | −38.5% | Passed | **False alarm** |

Suppressed and retained: the `gs-ultrasound-manchester` conversion-rate change (−34.6%). One campaign was excluded for insufficient volume.

**Persistence assessments: 6 plateaued and still elevated, 2 not confirmed elevated, none reported as resolved.**

| Campaign | Metric | Long baseline → latest 7 days | Status |
|---|---|---|---|
| `gs-mri-london` | Cost per conversion | £52.77 → £105.34 | Plateaued, still elevated |
| `gs-mri-london` | ROAS | 6.83 → 3.44 | Plateaued, still elevated |
| `pmax-ct-national` | Cost per conversion | £50.29 → £105.20 | Plateaued, still elevated |
| `pmax-ct-national` | Conversion rate | 2.91% → 1.42% | Plateaued, still elevated |
| `pmax-ct-national` | ROAS | 8.09 → 3.98 | Plateaued, still elevated |
| `meta-ct-prospecting` | Cost per conversion | £40.06 → £64.73 | Plateaued, still elevated |
| `meta-ct-prospecting` | Conversion rate | 2.11% → 1.36% | Not confirmed elevated |
| `meta-ct-prospecting` | ROAS | 9.66 → 6.30 | Not confirmed elevated |

### Measured detection across 200 seeds

Run with a temporary script outside the repository. "Original" is the same detector with both guard widths set to 0, which reproduces the first Milestone 3 results.

| Measure | Sustained: original | Sustained: guarded | Recent: original | Recent: guarded |
|---|---|---|---|---|
| Campaigns with no planted anomaly receiving any active adverse finding | 15.6% | **6.9%** | 19.9% | **7.2%** |
| Same, at high severity | 3.0% | **1.7%** | 5.7% | **2.4%** |
| Cost-per-conversion increase detected | 95.0% | 92.5% | 25.0% | 9.0% |
| Conversion-rate decline detected | 100.0% | 98.5% | 48.0% | 18.5% |
| Zero conversions detected | 100.0% | 100.0% | 96.5% | 96.5% |
| Strong performer detected | 76.5% | 76.5% | 76.0% | 76.0% |

Detected in either window with the guard on: cost increase 92.5%, conversion-rate decline 99.0%, zero conversions 100.0%, strong performer 90.0%.

**Planted plateaus across 200 seeds:**

| Scenario | Plateaued, still elevated | Recent change continuing | Not confirmed elevated | No sustained finding |
|---|---|---|---|---|
| Cost-per-conversion increase | 156 | 18 | 11 | 15 |
| Conversion-rate decline | 160 | 36 | 1 | 3 |

**Differences from the original detector:**

- False alarms fell by more than half in both windows.
- Guard outcomes on the sustained comparison were 1,783 passed, 32 downgraded, 285 suppressed. On the recent comparison they were 320 passed, 88 downgraded, 637 suppressed.
- Zero-conversion and strong-performer detection are unchanged, because those rules are not guarded.

**Regressions:**

- Sustained detection lost 5 seeds for cost and 3 for conversion rate. All 8 findings were retained as suppressed.
- Recent detection fell from 25.0% to 9.0% for cost and from 48.0% to 18.5% for conversion rate. All 91 lost findings were retained as suppressed. Both planted ramps finish on Oct 20, early in the recent baseline window (Oct 17–23), so little further change remains to detect. The persistence assessment covers that case.

**Remaining limitations of the evaluation:**

- About 7% of campaigns with no planted anomaly still receive an adverse finding, including on the default seed.
- Those false alarms also carry into persistence assessments, where 122 of 208 assessments on non-planted campaigns read as plateaued.
- Strong-performer detection of about 76% is limited by its peer and ratio gates.
- The noise in this generator was designed by the same author as the detector, so the evaluation is not independent, and these rates do not predict real-world accuracy.

## Test results

Final run, executed in the project directory:

| Command | Result |
|---|---|
| `uv lock --check` | Lockfile up to date |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run pytest` | **448 passed, 0 failed** |

| Test file | Tests |
|---|---|
| `test_schema.py` | 74 |
| `test_data_quality.py` | 63 |
| `test_anomalies.py` | 58 |
| `test_report.py` | 40 |
| `test_noise_guard.py` | 39 |
| `test_kpis.py` | 36 |
| `test_trends.py` | 31 |
| `test_synthetic_data.py` | 24 |
| `test_connector.py` | 23 |
| `test_smoke.py` | 22 |
| `test_periods.py` | 21 |
| `test_config.py` | 13 |
| `test_architecture.py` | 4 |

Additional verification:

| Check | Result |
|---|---|
| End-to-end flow: synthetic connector → validation → KPIs → anomalies → report | Ran, 300 records in, report written |
| Committed sample compared with a fresh run | Byte-identical |
| 76 rules and guards removed one at a time in a scratch copy | All 76 caused a test failure |

Problems found and fixed during development:

- A report key named `conversions` looked like a total, so it was renamed `conversions_by_basis`.
- The removal checks showed five weak tests across both rounds, where another safeguard or test setup masked the rule under test. All five were strengthened.
- A persistence status named `no_longer_above_threshold` appeared on a metric still down 34.8%, because it was inside the noise band. It was renamed `not_confirmed_elevated`.
- One test compared decimals at the wrong precision.

## Sample report location

`docs/samples/synthetic_analytics_report.json`. Regenerate with `uv run python -m growthpulse_ai.pipeline.sample_report`.

## Limitations

- The noise guard is a heuristic, not a significance test or confidence interval. It assumes roughly independent Poisson conversion counts.
- The guard ignores conversion-value variability, so the ROAS band is optimistic.
- The guard lowers sensitivity at low volumes, most of all in 7-day windows.
- False alarms remain, as the measured rates show.
- Recent days can be understated until late conversions are reported.
- Conversion rate divides by clicks, so view-through credit can inflate it on Meta.
- Spend is not allocated between conversion events, so each event's cost uses the campaign's full spend.
- Monetary thresholds are fixed amounts and do not adjust for currency.
- Only single-event campaigns appear in the synthetic dataset. Multi-event behavior is covered by tests.
- Nothing has been tested against live data. The GitHub Actions workflow has still not been executed.

## Dependencies for Milestone 4

Milestone 4 needs your approval to create a Supabase project, plus:

- A database connection for the backend, and a separate `SELECT`-only role for the dashboard.
- A choice of Postgres driver, which will be a new dependency.
- Tables keyed on the stable identifiers that already exist: `record_id`, `finding_id`, and `report_id`.
- Durable storage for quarantined rows, which are currently held in memory.
