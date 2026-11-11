# Milestone 5, Phase C report: scoped pipeline and persistence

**Status: Phase C finalised with your 2026-11-12 decisions.** Phase D has not started. Its preparation is [PHASE_D_READINESS_CHECKLIST.md](PHASE_D_READINESS_CHECKLIST.md).

Everything was tested against synthetic data, test fakes and disposable local PostgreSQL 16.2 servers. CI results on PostgreSQL 17 are in section 7.

**What Phase C did not touch:**
- No Supabase connection was made, nothing was written there, and no migration was applied there.
- No Meta or Google connection was made.
- No real OAuth credential or token exists.
- No login was enabled for the new roles.
- Nothing is committed.

## 1. Results at a glance

| Check | Result |
|---|---|
| Offline tests | **829 passed** (798 at `e09f16c`) |
| PostgreSQL integration tests, disposable PostgreSQL 16.2 | **278 passed** (224 at `e09f16c`), 54 of them in the Phase C module |
| Total | **1,107 tests** |
| `ruff check` (with the bandit `S` rules), `ruff format --check`, `uv lock --check` | Passed |
| Migrations, analytics engine, schema and sample report since `e09f16c` | Unchanged |
| Record, finding and report ID formulas | Unchanged |
| Milestone 4 data upgraded, then synced and analysed with the new code | Report `feb1e176…` reproduced byte for byte. 0 records, 0 reports and 0 findings rewritten. 1 breakdown added |
| Secret scan of new and changed files | Nothing found |

## 2. What was built

### Explicit context

The application loads its context from the database and passes it explicitly. No write depends on a database default.

- **`persistence/tenancy.py`** (new). It loads, read-only and as the writer:
  - a client with all its accounts;
  - each account's sync inputs and watermarks;
  - each account's latest successful sync summary;
  - the accounts that are due.
- **`persistence/rows.py`.** Record rows need an `ad_account_id`, report rows a `client_id`, and finding rows a `client_id` plus an account for every finding; a finding without one raises an error. Quarantined rows from an account sync carry the account, and it joins their ID. Combined runs keep the Milestone 4 quarantine ID.
- **`persistence/store.py`.** Every insert names its scope columns: runs, records, quarantined rows, reports, findings, breakdowns and proposals. `ExperimentProposal` now needs a `client_id`.

### Independent account syncs

`pipeline/account_sync.py` (new), plus the store functions it calls. Each step is its own transaction:

1. **Claim.** Locks the account's sync state, refuses when appropriate, takes the lease, and commits a running `account_sync` run with its window, attempt number and the failed run it retries.
   - It refuses: unknown or paused accounts (row-level security already hides them from the pipeline), archived accounts, archived clients, disabled syncs, accounts stopped for attention, and accounts whose lease another run holds.
   - It takes over an expired lease and marks the stale run `abandoned`.
2. **Fetch.** Uses the account's own connector, whose account guard quarantines rows from any other account.
3. **Save, all or nothing.**
   - It checks it still holds the lease.
   - It refuses any record of another account, so nothing is ever re-filed under a different account.
   - It writes records and quarantined rows with this account's ID, and finishes the run with its counts and validation summary.
   - It advances the watermark, which never moves backwards, resets failures, sets the next due time and releases the lease.
4. **On failure.** Marks the run failed with a fixed code, then either schedules a retry with backoff or stops the account for attention. The watermark is never touched.

Also new:
- **Due accounts.** `due_accounts` lists accounts that are enabled, active, not paused or running, past their due time, and for live accounts, whose connection is active.
- **Default window.** When none is given, a sync fetches the backfill on its first run, and afterwards re-fetches its lookback period, ending yesterday in the account's timezone.

### Client analysis

`pipeline/client_analysis.py`, `run_client_analysis`.

- **Reads only stored records** of the client's **active** accounts, plus the quality evidence of every successful sync covering the window. It never calls a connector, and a test makes any connector fetch fail during analysis.
- **Computes data quality deterministically** (decision 1). It doesn't use the latest sync's summary.
  - **Valid records and warnings** are recomputed from the stored records in the window. Those are already deduplicated by record ID, so re-fetched days never count twice.
  - **Quarantined rows** come from evidence each sync stores on its run. The evidence is keyed by the account-scoped quarantine ID and dated by the row's own reporting date, and holds no payload values. Overlapping syncs are merged by ID: the largest copy count is kept, and ties are broken by content. The result never depends on the order syncs ran in. Undated bad rows count when their sync overlaps the window, so problems are surfaced rather than hidden.
  - **Missing evidence refuses the analysis.** If an active account's successful syncs don't cover every day of the window, the analysis fails with `quality_evidence_incomplete` and writes nothing. That includes the upgraded Milestone 4 rows, which came from combined runs, until their accounts have synced.
- **Refuses an incomplete window.** If any active account hasn't synced through the window's end, it stops before starting a run, so it never reports on incomplete data.
- **Refuses mismatched records.** Any record from outside the client, or whose currency or timezone differs from its account, fails the analysis (`account_mismatch`).
- **Flags legitimate differences.** Currencies, conversion bases and attribution settings that differ between accounts stay separate in the report and are flagged in the breakdown.
- **Stores everything in one transaction:** report, findings and breakdown. Each check fails the analysis with `run_state_conflict` and writes nothing:
  - a report ID already held by another client;
  - a finding ID held by another client;
  - a different breakdown already stored for the same report.

### Commands
- **`run_client`.** Syncs each of a client's syncable accounts, then analyses the client. If any sync fails, the analysis doesn't run.
- **The synthetic command (`persisted_run`)** now does this for an explicit `--client-id`. The default is the fictional legacy client, named explicitly rather than taken from a database default, with the fixed synthetic window, 1–30 October.
- **`run_and_persist`,** the single-transaction combined run, is kept but now requires an explicit `ClientContext`. Rows of an account outside that client fail the run with `account_mismatch`.
- **New fixed error codes:** `account_mismatch`, `account_busy`, `account_not_syncable`, `client_not_found`, `quality_evidence_incomplete` and `incomplete_response`. Classification moved to `pipeline/failures.py`.

### Complete responses only (decision 3)
- Connectors now report whether their last fetch is known to be the complete answer for its window. The base connector says no unless a connector proves it. Synthetic connectors always do. A live connector (Milestone 6) must confirm a successful, fully paginated response.
- An account sync whose response isn't confirmed complete fails with `incomplete_response`, which is retryable, and its watermark doesn't move, whether the response was empty or not. Partial pages, authorisation failures and API errors are never treated as an empty dataset.

### Onboarding and activation (decision 2)
- V1 keeps manual activation. Onboarding accounts sync, but aren't analysed until the owner activates them.
- The pipeline never activates an account. The writer has no grant to do so, and can't run `set_account_status`. Tests cover all of this, running the owner function as the migration owner without changing any role's login.

### Proposed migration 9, not applied
- **File:** `supabase/proposed/20261111200500_remove_scope_fallbacks.sql`. It sits outside `supabase/migrations`, so `db push` can't run it.
- **What it does:** removes the Phase B fallbacks, meaning the legacy-client default on runs and the four scope-derivation triggers. It keeps the run-scope check, every composite foreign key and every constraint, uses restrict-only drops, and changes no data.
- **Tested:**
  - the new code works with it and without it;
  - the frozen Milestone 4 store fails with it, which shows the fallbacks are no longer needed by the application;
  - a writer that omits scope is refused;
  - the Milestone 5 revert still restores the exact Milestone 4 schema afterwards.

## 3. Backward compatibility

| Check | Result |
|---|---|
| Upgrade from the exact Milestone 4 state | Built by a frozen, hash-pinned copy of the Milestone 4 store, run twice as on Supabase, then the five Phase B migrations. Still 300 records, 19 findings and report `feb1e176…` |
| New flow on that upgraded data | Syncs receive 210 and 90 records and write 0. The analysis reproduces `feb1e176…` with its stored bytes identical, writes 0 reports and 0 findings, and adds 1 breakdown. Records, reports, findings and quarantine are row-for-row unchanged. A second run writes nothing at all |
| Fresh install | Writes 210 + 90 records, 1 report, 19 findings and 1 breakdown, with report `feb1e176…`. A second run writes nothing |
| With the fallbacks removed | The same results |
| Milestone 4 combined run | Still available, scoped explicitly |

**Phase D expectation change.** After the Phase B migrations are applied on Supabase, the synthetic command will create:
- two account-sync runs, each writing 0 records;
- one analysis run, writing 0 reports, 0 findings and 1 breakdown;
- the same report ID.

## 4. Decisions applied, and remaining notes

| Item | Status |
|---|---|
| 1. Data quality from sync evidence covering the window, deduplicated and order-independent | **Implemented** (section 2). Report `feb1e176…` is reproduced through full, lookback, split and shuffled sync orders |
| 2. Manual activation of new accounts in V1 | **Kept**, with tested boundaries |
| 3. Watermarks advance only on confirmed-complete responses | **Implemented** in the connector interface and sync. Live connectors must implement the confirmation in Milestone 6 |
| 4. Don't apply the fallback-removal migration | **Kept** in `supabase/proposed/`, tested, not applied |
| Authentication failures | `auth_failed` stops the account. Marking the connection `needs_reauth` needs the token manager role, which is Milestone 6 work |
| Abandoned leases | Marked `abandoned` when taken over, but not counted as a consecutive failure |
| Undated quarantined rows | Counted for any analysis window their sync overlaps, so problems are surfaced. If a long sync contained one, it can appear in several analyses |
| Test logins | Phase C tests enable login only for the writer and dashboard roles, on throwaway servers. Phase B's committed tests still enable test logins for the new roles on throwaway servers. Nowhere else |

## 5. Tests added or changed

| File | Tests | Covers |
|---|---|---|
| `tests/test_quality_evidence.py` (new) | 21 | Evidence IDs equal the stored quarantine IDs; copies, codes and dates, without values; overlapping re-fetches counted once; order independence across all 6 orders; the Milestone 4 summary reproduced from records; dataset warnings; window coverage (6 cases); evidence selection and refusals; complete-response rules |
| `tests/test_postgres_phase_c.py` (new) | 54 | Backward compatibility, both with and without the fallbacks. Fresh install. Sync history, watermark, summary and schedule. Lookback and empty windows. Retries with backoff. Mid-save rollback. Stopping for attention. Lease refusal, takeover and loss. Paused, archived and disabled accounts. Due accounts. Foreign rows quarantined, never remapped. A guard bypass caught on save. Combined-run refusal. Three clients stored side by side. One account's failure isolated. Analysis restricted to active accounts without connectors. Incomplete windows. Currency mismatch. An empty analysis. Breakdown and report-ID conflicts. The proposed migration and the revert. **Since the decisions:** report identity through 4 sync orders; a re-fetched bad row counted once; evidence outside the window excluded; the same evidence in any order gives the same report; uncovered windows refused; upgraded data refused until synced; unconfirmed responses (with and without rows) never advance the watermark; onboarding accounts never analysed or auto-activated; only the owner action activates; fingerprints stable across upgrade, synthetic run and revert |
| `tests/test_migrations.py` (fingerprints) | 1 new | The fingerprint script is read-only and covers every Milestone 4 table, over Milestone 4 columns only |
| `tests/test_persistence_rows.py`, `test_persistence_store.py`, `test_persisted_run.py` | Updated, plus 3 new | Explicit scope on every row; the quarantine ID kept for combined runs and account-scoped for syncs; the store's two new checks against other clients' data |
| `tests/test_migrations.py` | 3 new | The proposed migration stays outside the migrations folder, is restrict-only, and removes exactly the fallbacks |
| `tests/test_m4_frozen.py` (new) | 3 | The frozen Milestone 4 store is hash-pinned and writes no scope columns |
| Phase A and B integration modules | Updated | They pass explicit clients. The Milestone 4 state is built with the frozen store |

## 6. Readiness for Phase D

The code is ready. The deployment steps, stop conditions and recovery options are in [PHASE_D_READINESS_CHECKLIST.md](PHASE_D_READINESS_CHECKLIST.md). A new read-only script, `supabase/verification/milestone_4_fingerprints.sql`, gives values that must stay identical before the migrations, after them, after the synthetic run (apart from the run count) and after any recovery. That's tested.

TLS `verify-full` remains a prerequisite before any real client data.

## 7. CI on PostgreSQL 17

Pending the push.
