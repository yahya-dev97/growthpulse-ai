# Milestone 5, Phase A report: domain models, interfaces and compatibility

**Status: Phase A complete, awaiting review.** Phase B (migrations) has not started.

Everything was tested locally on 2026-11-11, against synthetic data, test fakes and disposable local PostgreSQL servers.

**What Phase A did not touch:**
- No Supabase connection was made.
- No migration was written or applied.
- No database role was created.
- No advertising API was called, and no OAuth flow ran.
- No real token or encryption key exists. Tests generate throwaway keys at runtime.
- Nothing is committed.

## 1. Results at a glance

| Check | Result |
|---|---|
| Offline test suite | **775 passed** (619 before Phase A, so 156 new), 94 PostgreSQL tests skipped |
| PostgreSQL integration tests, disposable local PostgreSQL 16.2 | **94 passed** (88 before, so 6 new) |
| Total | **869 tests** |
| `ruff check` (including the bandit `S` security rules) and `ruff format --check` | Passed |
| `uv lock --check` | Consistent |
| Default synthetic dataset | Byte-identical to Milestones 2–4 (300 rows, SHA-256 `fcf86a71…`) |
| Sample report | Byte-identical, with report ID `feb1e176…` |
| Report built from independent account syncs | Reproduces `feb1e176…` exactly |
| Report built from records stored and reloaded through PostgreSQL | Reproduces `feb1e176…` exactly, under 3 session timezones |
| Secret scan of new and changed files | No credentials, keys or `.env` values. The only match is the existing `PASSWORD` placeholder in `.env.example` |

PostgreSQL 17 integration tests will run in CI once this work is pushed. That needs your approval.

## 2. What was built

### Shared account layer (`src/growthpulse_ai/accounts/`)
- **`models.py`**
  - **Account and client contexts.** `AccountContext` and `ClientContext` validate currency and timezone. A live account must have a connection and a synthetic one must not, and every account must belong to its client.
  - **Mapping data to accounts.** `ClientContext.account_for` maps a record's or finding's `(platform, account_id)` to exactly one account, or refuses.
  - **Lifecycles.** Account and connection status values, with their allowed transitions. Archived and revoked are final.
  - **Discovery.** `DiscoveredAccount` represents an account a connection can see. `plan_account_additions` turns the owner's selection from one connection into new accounts, and one connection can expose many accounts. It refuses manager accounts, inaccessible accounts, accounts already assigned to any client, archived clients and synthetic clients.
  - **No reassignment.** `check_unique_assignment` refuses any account that belongs to two clients.
- **`assignment.py`**: maps records and findings to client accounts. It raises on anything from outside the client and never guesses.
- **`legacy.py`**: the fictional client "ExampleScan UK (fictional)", with fixed workspace, client and account UUIDs that the Phase B backfill will seed.
- **`scheduling.py`**: pure functions for scheduling, with the clock and randomness passed in.
  - the next due time in the account's timezone, including daylight-saving changes;
  - the sync window, with backfill and restatement lookback, and backfill chunking;
  - failure classification and backoff: 15 minutes doubling to a 6-hour cap, with ±20% jitter;
  - the state after a failure;
  - the date every account has synced through, which is where an analysis ends.

### Connectors
- **Account guard.** `validate_rows(..., expected_account=...)` quarantines any row from another account with the new issue code `account_mismatch`. A connector bound to an account always applies it, and a connector with no account behaves exactly as before.
- **Multi-client synthetic data.** `synthetic_data.py` now has `SyntheticClientSpec` and `SyntheticAccountSpec`, plus three fictional clients:
  - the default client, unchanged;
  - an Irish client that reuses campaign IDs on different accounts;
  - a European client with two Google accounts (GBP and EUR) and a Meta account (USD), across three timezones.
- **Connector factory.** `factory.py` defines `ConnectorFactory` and `SyntheticConnectorFactory`. The factory refuses live accounts, unknown accounts, and accounts whose currency or timezone differs from their data.

### Analytics
- **Account breakdown.** `analytics/breakdown.py` builds a deterministic per-account document with its own hash, linked to a report by `report_id`. It isn't part of the report body, so report IDs don't change.
  - It reports each account in its own currency and timezone, with KPIs per conversion basis.
  - Its compatibility section states the currencies, timezones, conversion bases and attribution settings involved, and flags when they're mixed. It never gives a cross-currency total.
- **Unchanged.** The report engine wasn't modified.

### Persistence and pipeline
- **Loader.** `persistence/load.py` rebuilds records for chosen accounts and dates from stored rows, read-only. It converts `ingested_at` to UTC and removes database padding from amounts.
- **Client analysis.** `pipeline/client_analysis.py` provides `analyse_client`. It takes stored records and each account's latest validation summary, never a connector, and refuses another client's records or summaries. It returns the report, the breakdown, and the account for each finding.
- **Summaries.** `data_quality.combine_summaries` adds per-account validation summaries together.

### Credentials (`src/growthpulse_ai/credentials/`), with fakes only
- **`crypto.py`.** AES-256-GCM through the `cryptography` package, version 50.0.2, a new dependency approved in decision 4.
  - The key ring is versioned, its repr shows versions only, and key-ring errors never echo key material.
  - Each encryption uses a random 96-bit nonce.
  - The authenticated context binds each ciphertext to its connection ID, token type and key version.
  - Rotation is supported, and decryption fails with a generic error.
- **`oauth.py`.**
  - `TokenSet` holds every token as a `SecretStr`.
  - Protocols for `OAuthProvider`, `AccountDiscovery`, `TokenStore` and `CredentialProvider`.
  - `EncryptingTokenStore` stores only ciphertext. Only refresh tokens and Meta's long-lived tokens are stored, never short-lived access tokens.
  - `RefreshingCredentialProvider` refreshes before expiry, saves a rotated refresh token, keeps access tokens in memory, and raises `ReauthorizationRequired` when no credential is usable.
  - PKCE and `state` helpers, checked against the RFC 7636 test vector.

### Configuration and boundaries
- **Settings.** `TOKEN_ENCRYPTION_KEYS` and `TOKEN_ENCRYPTION_ACTIVE_VERSION` are new, and must be set together. The five single-account settings are marked deprecated but kept, as decision 8 requires, and `legacy_account_settings_in_use` reports any that are set. `.env.example` is updated, with all values blank.
- **Package boundaries.** The architecture test now enforces:
  - `accounts` sits below every layer;
  - `credentials` imports no layer;
  - analytics, AI analysis and the dashboard can never import `credentials`.
- **README roadmap.** Renumbered as approved: Milestone 6 is OAuth and live integrations, 7 is AI insights, 8 is the dashboard, and 9 is scheduled automation and deployment.

## 3. Compatibility evidence

These are the guarantees you asked for, and the test that proves each one.

| Requirement | Evidence (test) |
|---|---|
| Existing record IDs are preserved | The identity formulas are unchanged, and the default dataset is byte-identical (`test_default_synthetic_rows_are_byte_identical_to_milestone_4`) |
| Existing report IDs and contents are preserved | `test_sample_report_and_its_id_are_unchanged` matches the committed sample file byte for byte |
| Existing finding IDs are preserved | All 19 findings map to accounts without being rewritten (`test_every_milestone_4_finding_maps_to_one_legacy_account_without_changing_ids`) |
| The backfill maps every record unambiguously | All 300 records map to exactly one legacy account, 210 Google and 90 Meta, and each identity names one account only (`test_every_milestone_4_record_maps_to_exactly_one_legacy_account`). The 25 of 25 live check in Milestone 4 already showed the stored record IDs equal these source IDs |
| Sync and analysis are independent | Per-account fetches add up to the combined fetch, records and summary alike. The client analysis built from them equals the Milestone 4 report (`test_client_analysis_from_account_syncs_reproduces_the_milestone_4_report`) |
| Stored data rebuilds the same report | Integration test `test_stored_milestone_4_records_rebuild_the_same_report`: data is stored through the unchanged Milestone 4 run, reloaded as the writer under UTC, +13:45 and −3:30 session timezones, matches the records byte for byte, and gives the same report ID |
| Clients stay isolated | Records, findings and reports don't overlap across the three clients, even where campaign IDs are reused. Analysis refuses another client's records or summaries. The guard quarantines foreign rows |
| Incompatible metrics are never aggregated | Each currency keeps its own totals, and the breakdown is per account and per conversion basis. Meta's two conversion events stay separate. Mixed currencies, timezones and attribution settings are flagged |
| Milestone 4 persistence is unchanged | All 88 existing integration tests pass unmodified. `store.py`, `rows.py`, `persisted_run.py` and the migrations are untouched |

## 4. Findings and design clarifications

These are recorded in section 13 of [MILESTONE_5_DESIGN.md](MILESTONE_5_DESIGN.md).

1. **Account breakdowns sit beside the report, not inside it.** Putting them in the body would change every report ID. Phase B adds an append-only `analytics_report_breakdowns` table for them.
2. **The decimal-scale risk is closed.**
   - PostgreSQL returns `237.340000` for a stored `237.34`. The report was already unaffected, because it serialises decimals by value; I checked this separately.
   - Records themselves didn't round-trip exactly, which the first integration run caught. The loader now removes the padding, and records match byte for byte.
3. **Timezones.** `ingested_at` comes back in the session's timezone. The loader converts it to UTC, which is proven under two unusual offsets.
4. **Per-account summaries.** Each account sync must store its own validation summary, because the client analysis needs it. This fits the planned `pipeline_runs.data_quality` column.
5. **Synthetic per-account fetches** drop generated rows that name no account. A real API never returns such rows, and supplied test rows are untouched.
6. **One test expectation was wrong.** I had quoted the RFC 7636 PKCE vector from memory. The code was correct, and the test now uses the RFC's actual value.

## 5. Risks and open items

| Item | Status |
|---|---|
| Production TLS `verify-full` | Still required before any real client data. Unchanged by Phase A |
| PostgreSQL 17 coverage of the new loader tests | Runs in CI only once you approve a push |
| `cryptography` is a new compiled dependency | It's locked in `uv.lock`, with wheels for macOS and the CI's Linux, and is used only through its high-level AES-GCM API |
| Token rotation and recovery | Designed and tested. The rotation job and storage of the token key are Milestone 6 work |
| Google's OAuth scope permits changes | Read-only use must be enforced in Milestone 6 code (design section 7.3) |
| Legacy single-account settings | Deprecated but present. To be removed after the multi-account path is proven in Milestone 6 |
| The two new database roles | Approved in principle. Not created, and creating them needs your Phase B approval |

## 6. Next step: Phase B (needs your approval)

Phase B is migrations, tested on local and CI databases only:
- **Migrations 4–8**, including:
  - the backfill, using the fixed legacy UUIDs from `accounts/legacy.py`;
  - the new `analytics_report_breakdowns` table;
  - the two roles, created without login.
- **Checks:** an extended rollback script, verification script and grant manifest.
- **Tests:**
  - an upgrade test from the exact Milestone 4 state;
  - a fresh-install test;
  - isolation tests;
  - role-boundary tests.
- **Report:** `MILESTONE_5_PRE_MIGRATION_REPORT.md`, which stops for your review.

You would then apply the migrations yourself, as in Milestone 4. Nothing in Phase B would touch Supabase.

## 7. Files

**New:**
- `src/growthpulse_ai/accounts/`: `models.py`, `assignment.py`, `legacy.py`, `scheduling.py`
- `src/growthpulse_ai/credentials/`: `crypto.py`, `oauth.py`
- `src/growthpulse_ai/analytics/breakdown.py`
- `src/growthpulse_ai/connectors/factory.py`
- `src/growthpulse_ai/persistence/load.py`
- `src/growthpulse_ai/pipeline/client_analysis.py`
- Tests: `test_accounts.py`, `test_scheduling.py`, `test_credentials.py`, `test_multi_client.py`, `test_persistence_load.py`, and the helpers `multi_client.py` and `fake_oauth.py`

**Changed:**
- `connectors/base.py`, `connectors/synthetic.py`, `connectors/synthetic_data.py`
- `data_quality.py`, `config.py`
- `tests/test_architecture.py`, `tests/test_config.py`, `tests/test_postgres_integration.py`
- `.env.example`, `README.md`, `pyproject.toml`, `uv.lock`
- `MILESTONE_5_DESIGN.md`
