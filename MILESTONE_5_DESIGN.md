# Milestone 5 design: multi-client and multi-ad-account architecture

**Status: architecture approved on 2026-11-11; Phase A implemented** (see [MILESTONE_5_PHASE_A_REPORT.md](MILESTONE_5_PHASE_A_REPORT.md)). No migration, database role or live connection exists yet. Section 12 records the decisions as approved. Section 13 lists clarifications found while implementing Phase A, and they take precedence over earlier sections where the two differ.

Baseline: commit `0eb3ca4`. On Supabase, the three Milestone 4 migrations are applied, and the database holds 2 synthetic runs, 300 records, 1 report (`feb1e176…`) and 19 findings. The quarantine, proposal and status-event tables are empty.

---

## 1. Summary

- **Hierarchy.** One owner and one workspace. The workspace holds **clients** (brands). Each client has **ad accounts** on Meta or Google. Each ad account is synced through a **provider connection**, an OAuth grant.
- **Two kinds of run.**
  - An **account sync** fetches one ad account's data and stores its records. Each account has its own schedule, history, retries and status.
  - A **client analysis** builds one report from the stored records of every active account a client has.
- **Scoping.** Every scoped row carries `client_id`, and `ad_account_id` where it applies. Composite foreign keys make it impossible for a record, finding, report or proposal to point at an account or client it doesn't belong to.
- **Milestone 4 data is kept, not reset.** One transactional migration assigns the existing 300 records, 1 report and 19 findings to a fictional synthetic client with two accounts. No ID changes, and the analytics engine is untouched.
- **Backwards compatibility is testable.** The Milestone 4 command keeps working as a "combined" run mode. After migration, running it on Supabase must write 0 records, 0 reports and 0 findings, and return the same report ID `feb1e176…`.
- **Four database roles**, each with one job:
  - `growthpulse_writer` and `growthpulse_dashboard` are the two existing roles. They are extended, but no less restricted.
  - **`growthpulse_manager`**, new: can run a short list of owner-action functions (account management and experiment approvals) and nothing else.
  - **`growthpulse_token_manager`**, new: the only role that can touch the separate `growthpulse_secrets` schema, which holds encrypted OAuth tokens.

  Both new roles are created without login in this milestone.
- **OAuth is interfaces only.** This milestone defines the provider, discovery, token-store and credential interfaces and tests them with fakes. No real OAuth client, HTTP call or token exists until the OAuth milestone.

---

## 2. What exists today (inspection findings)

| Area | Current state | Consequence for Milestone 5 |
|---|---|---|
| Tables (`migrations/…100000`) | 7 tables. Advertising scope is a free-text `account_id` column. There is no client, workspace or account entity | New entity tables are needed, plus scope columns on the existing tables |
| Identity hashes | `record_id` covers data source, date, platform, **account_id**, campaign, conversion event and attribution ([schema.py:391](src/growthpulse_ai/schema.py#L391)). `finding_id` also includes **account_id** ([anomalies.py:220](src/growthpulse_ai/analytics/anomalies.py#L220)). `report_id` hashes the whole report body | Platform account IDs are unique per platform, so the existing IDs are already account-scoped. **No ID formula needs to change**, and the Milestone 4 idempotency guarantees still hold |
| `quarantine_id` | A hash of data source, connector and payload, **not the account** ([rows.py:87](src/growthpulse_ai/persistence/rows.py#L87)) | Two accounts' identical bad rows would merge. The account needs adding to the formula. The table is empty on Supabase, so that's safe |
| Runs | `pipeline_runs` covers one combined fetch-and-analyse run, with no scope | Add a run kind and scope columns. Records already reference this table, so keeping one run table avoids rewriting foreign keys |
| Append-only triggers | Reports, proposals and status events reject every `UPDATE`, including the owner's ([migration 1:273](supabase/migrations/20261111100000_create_growthpulse_tables.sql#L273)) | The backfill can't `UPDATE` reports. It uses a column default instead (section 5.2) |
| Grants | The writer has table-level `INSERT` and column-level `UPDATE`. The dashboard has `SELECT` on views only. Every table has RLS. There are 58 grants | Table-level `INSERT` automatically covers new columns. New `UPDATE` grants must list columns, and the scope columns must never be updatable |
| Views | 9 owner-rights views. `latest_reports` takes one report per **data source** | `create or replace view` can only add columns at the end. `latest_reports` changes to one per client and data source |
| Connector interface | `fetch(start, end)` on a connector that holds its own configuration ([base.py](src/growthpulse_ai/connectors/base.py)). It validates rows and never writes | Keep this. Add an account context and a check that rejects rows from any other account |
| Synthetic data | One fictional business, with account IDs fixed to `synthetic-google-account-001` and `synthetic-meta-account-001` ([synthetic_data.py:33](src/growthpulse_ai/connectors/synthetic_data.py#L33)) | Parameterise it into several fictional clients. The default settings must reproduce today's data byte for byte |
| Pipeline | Builds the report from the connector's output in memory ([persisted_run.py:121](src/growthpulse_ai/pipeline/persisted_run.py#L121)) | With independent syncs, analysis has to read **stored** records. This needs a loader that turns database rows back into records, which is the biggest new code path |
| Data-quality summary | Counts plus counters by code ([data_quality.py:77](src/growthpulse_ai/data_quality.py#L77)). Dataset warnings are grouped per account and campaign | Summaries from separate accounts add up to the combined summary, so a client report can reuse them |
| Currency | KPIs refuse to mix currencies, and the report groups by currency ([kpis.py:188](src/growthpulse_ai/analytics/kpis.py#L188)) | Clients whose accounts use different currencies already work |
| Configuration | `GOOGLE_ADS_CUSTOMER_ID`, `META_AD_ACCOUNT_ID`, `GOOGLE_ADS_REFRESH_TOKEN` and `META_ACCESS_TOKEN` are single-account settings ([config.py:17](src/growthpulse_ai/config.py#L17)) | These break the "no hardcoded account IDs" rule. Retire them. Only app-level secrets stay in the environment |
| Roadmap | The README says Milestone 5 is AI insights | Needs renumbering (question 1) |

### Reuse without change
- `AdPerformanceRecord`, `record_id` and validation rules.
- The whole analytics engine: KPIs, trends, anomalies, noise guard and `build_report`.
- `connection.py`, including the TLS and SCRAM rules, and error classification.
- `fake_db`, the CI jobs, `provision_roles` and `connection_check` (both extended later for the new roles).

### Change
- Connector base (account context), synthetic generator (several clients).
- `store`, `rows` (quarantine ID and scope), `persisted_run` (run modes), `read` (client filter), `config` (retire single-account settings).
- New migrations, an extended rollback script, an extended verification script and the grant manifest.

---

## 3. Proposed schema

All new tables live in the private `growthpulse` schema, except tokens, which get their own schema. Every table has RLS enabled, and no Supabase API role gets anything. Columns shown are the important ones, not a complete list.

### 3.1 Entities

```
workspaces            workspace_id uuid PK, name, created_at
                      (one row; the root for future team features)

clients               client_id uuid PK, workspace_id FK, name (1-200),
                      data_source ('synthetic'|'live'), status ('active'|'archived'),
                      created_at, archived_at
                      UNIQUE (client_id, data_source)   -- composite FK target
                      UNIQUE (workspace_id, lower(name)) where status = 'active'

provider_connections  connection_id uuid PK, workspace_id FK,
                      provider ('google_ads'|'meta_ads'),
                      provider_subject_id text   -- opaque provider user/manager ID, no email
                      status ('pending'|'active'|'needs_reauth'|'revoked'),
                      granted_scopes text[], last_validated_at, created_at, revoked_at
                      -- never holds a token

discovered_ad_accounts connection_id FK, platform, external_account_id,
                      display_name, currency, timezone, is_manager,
                      access ('available'|'no_longer_accessible'), last_seen_at
                      PK (connection_id, platform, external_account_id)
                      -- what a connection can see; written by discovery, not by the owner

ad_accounts           ad_account_id uuid PK, client_id, data_source,
                      platform, external_account_id, display_name, currency, timezone,
                      sync_connection_id FK provider_connections (null for synthetic),
                      status ('onboarding'|'active'|'paused'|'needs_attention'|'archived'),
                      created_at, archived_at
                      FK (client_id, data_source) -> clients (client_id, data_source)
                      UNIQUE (data_source, platform, external_account_id)
                      UNIQUE (ad_account_id, client_id)
                      UNIQUE (ad_account_id, data_source, platform, external_account_id)
                      CHECK (data_source = 'live') = (sync_connection_id is not null)
                      trigger: client_id and external identity can never change
```

**Assignment model.** "Assigning" an account means adding a discovered account to a client, which creates an `ad_accounts` row. In version 1, an account belongs to **one client for its whole life**:
- `client_id` is immutable, enforced by the trigger and by column grants;
- the unique external identity stops the same platform account from being added to a second client;
- removing an account from a client archives it and keeps its history.

This gives strict isolation without date-ranged assignment history. Moving an account between clients is deferred (question 3).

### 3.2 Sync configuration and state

```
account_sync_config   ad_account_id PK FK,
                      enabled bool, frequency ('daily'|'every_6_hours'|'manual'),
                      preferred_local_hour smallint (0-23, account timezone),
                      lookback_days smallint (1-90)        -- restatement window
                      backfill_start_date date, max_consecutive_failures smallint (1-10)
                      updated_at
                      -- written only through owner-action functions

account_sync_state    ad_account_id PK FK,
                      status ('idle'|'running'|'retry_wait'|'needs_attention'|'paused'),
                      synced_through date,                 -- watermark
                      last_success_at, last_attempt_at, next_due_at,
                      consecutive_failures, last_error_code,
                      lease_run_id uuid, lease_expires_at
                      -- written by the pipeline (column grants only)
```

### 3.3 Existing tables gain scope

| Table | New columns | Constraints |
|---|---|---|
| `pipeline_runs` | `run_kind` ('combined', 'account_sync', 'client_analysis'), `client_id`, `ad_account_id`, `window_start`, `window_end`, `attempt`, `retry_of_run_id`, `data_quality jsonb` | `client_id` not null. An account sync must have an account, and an analysis or combined run must not. Composite FK (`ad_account_id`, `client_id`). Only analysis and combined runs carry a `report_id` |
| `ad_performance_records` | `ad_account_id` not null | Composite FK (`ad_account_id`, `data_source`, `platform`, `account_id`) to `ad_accounts`. The database itself refuses a record filed under the wrong account. The column is not updatable |
| `quarantined_records` | `ad_account_id` (null only for combined runs, whose bad rows may lack an account) | `quarantine_id` adds the account to its hash |
| `analytics_reports` | `client_id` not null | FK (`client_id`, `data_source`) to `clients`. UNIQUE (`report_id`, `client_id`) as a composite target |
| `findings` | `client_id`, `ad_account_id`, both not null | Composite FK (`ad_account_id`, `client_id`). FK (`latest_report_id`, `client_id`) to `analytics_reports` |
| `experiment_proposals` | `client_id` not null | FK (`report_id`, `client_id`) to `analytics_reports`, so a proposal can't refer to another client's report |
| `experiment_status_events` | none | The client is reached through the proposal. The `actor` values `owner` and `pipeline` are distinguished by RLS |
| `owner_action_log` (new) | `action`, `target_type`, `target_id`, `acted_at`, `actor` ('owner') | Append-only. Covers every owner-action function. Holds no personal data |

Each scoped row has a `client_id`, so per-client RLS can be added later, when client logins arrive, without reshaping any table.

---

## 4. How analytics records, reports and findings are scoped

- **Records belong to an account.** The client is reached through the account, which never changes client. Record IDs are unchanged.
- **Reports belong to a client.** A report is built from that client's active accounts. Its body stays exactly what `build_report` produces today. The scope sits in row columns, not in the JSON, so the engine and the sample report stay byte-identical.
  - Report IDs can't collide across clients, because record IDs include the account and an account has one client.
  - The store also checks this. If an insert hits an existing `report_id` that belongs to a different client, it raises `RunStateError` rather than silently doing nothing.
- **Findings belong to a client and an account.** The account comes from each finding's `(platform, account_id)`, looked up among the client's accounts. The upsert gets the same collision check as reports.
- **Reading data back.** The dashboard API gains a client parameter, for example `latest_report(connection, client_id, data_source)`. The Milestone 4 signature is kept only while the combined mode exists, and resolves to the default synthetic client.
- **Account-mismatch guard.** A sync for account X runs every validated row past a check that its `(platform, account_id)` matches X. Rows that don't match are quarantined with a new issue code, `account_mismatch`, and never stored as records. If a connector bug lets one through anyway, the composite foreign key rejects the whole transaction.
- **Analysis reads stored records.** A new loader maps rows back to `AdPerformanceRecord`. The analysis's data-quality summary is the sum of the latest summaries from the account syncs it uses.
  - **Resolved in Phase A** (section 13.2). Padded decimals don't change the report, because it serialises decimals by value. The loader still restores the canonical form, so records round-trip exactly. A report built from stored records reproduces report `feb1e176…`.

---

## 5. Migrations and backwards compatibility

### 5.1 Files

Each is a single transaction, like the Milestone 4 migrations:

| # | File | Contents |
|---|---|---|
| 4 | `…_create_tenancy_tables.sql` | `workspaces`, `clients`, `provider_connections`, `discovered_ad_accounts`, `ad_accounts`, `account_sync_config`, `account_sync_state`, `owner_action_log`, plus RLS, triggers and revokes |
| 5 | `…_scope_existing_tables.sql` | Seeds the fictional synthetic client, then adds scope columns, backfills them, makes them not null, and adds the composite keys and indexes (section 5.2) |
| 6 | `…_create_token_storage.sql` | The `growthpulse_secrets` schema, `oauth_credentials`, the `growthpulse_token_manager` role (no login) and the token functions |
| 7 | `…_create_owner_actions.sql` | The `growthpulse_manager` role (no login), the owner-action functions and `owner_action_log` grants |
| 8 | `…_update_grants_and_views.sql` | Writer grants on the new tables and columns, plus updated and new dashboard views |

Also in this milestone:
- the rollback script grows a Milestone 5 section, still restrict-only and never run against Supabase;
- the post-migration verification script and the grant manifest are extended;
- `tests/test_migrations.py` gains the same static checks for the new files.

### 5.2 The backfill (migration 5)

1. **Preconditions** that abort the whole transaction if they fail:
   - every existing row in every table is `data_source = 'synthetic'`;
   - every record's `(platform, account_id)` is one of the two known synthetic accounts;
   - no proposals or status events reference anything else.

   These checks look at the data's shape, not at fixed counts, so the same file also runs on an empty CI database.
2. **Seed** one workspace, the client "ExampleScan UK (fictional)" with `data_source='synthetic'`, and its two synthetic ad accounts. These use fixed UUIDs written in the file. They are fictional fixtures, not advertising account IDs.
3. **Plain updates** where no append-only trigger exists:
   - `ad_performance_records.ad_account_id`, matched on platform and account ID;
   - `findings.client_id` and `findings.ad_account_id`;
   - `pipeline_runs` (`run_kind='combined'` and `client_id`).

   No `updated_at` or `last_changed_run_id` value changes, so the record history stays exactly as it was.
4. **Append-only tables** (`analytics_reports`, `experiment_proposals`): add `client_id` as `NOT NULL DEFAULT '<synthetic client uuid>'`, then immediately drop the default.
   - In PostgreSQL 11 and later, a constant default fills existing rows without an `UPDATE`, so the append-only trigger never fires and is never disabled.
   - The precondition guarantees every existing row belongs to that one client.
5. Add the not-null constraints, composite foreign keys and indexes, then make the run-kind checks strict.

**Rejected alternative: reset the synthetic data.** It would destroy verified history, need delete rights or the rollback, and wouldn't exercise the upgrade path that matters once live data exists.

### 5.3 Proving it

- **Upgrade test (new, CI on PostgreSQL 17).**
  1. Apply migrations 1–3.
  2. Insert the Milestone 4 state using a frozen copy of the Milestone 4 statements.
  3. Apply migrations 4–8.
  4. Assert: counts unchanged; every record mapped to the correct account; report and findings scoped to the synthetic client; IDs, `updated_at` and `last_changed_run_id` unchanged; the report body byte-identical.
- **Fresh-install test.** Apply all eight migrations to an empty database, then run the grant manifest, RLS and view checks.
- **Live check after you apply the migrations.**
  1. The extended read-only verification script.
  2. One combined run, which must write 0 records, 0 reports and 0 findings and return report `feb1e176…`.
  3. One per-account sync of each synthetic account, then one client analysis, checked read-only.

---

## 6. Security boundaries

| Role | Can | Cannot |
|---|---|---|
| `growthpulse_writer` (pipeline) | Everything it can do today, scoped. `SELECT` on clients, ad accounts, sync config and connections (status only). `UPDATE` on named `account_sync_state` columns. Insert runs of every kind. Propose experiments (`actor='pipeline'`) | Create, rename or archive clients or accounts. Change sync config. Change any scope column. Approve experiments. Touch `growthpulse_secrets`. Delete anything |
| `growthpulse_dashboard` | `SELECT` on `growthpulse_read` views only. New views cover clients, accounts, connection status (no tokens, just whether a credential exists and when it expires), sync status and sync history | Everything else. Still read-only for the session and every transaction |
| `growthpulse_manager` (new, no login) | `EXECUTE` on a fixed list of owner-action functions: create, rename or archive a client; add a discovered account to a client; pause, resume or archive an account; set sync config; request a manual sync; approve, reject, cancel or mark an experiment running or completed; disconnect a connection | Any direct table read or write. Running any other function. Applying anything to an ad account: approving an experiment only records a decision |
| `growthpulse_token_manager` (new, no login) | `EXECUTE` on `store_credential`, `read_credential` and `delete_credential` in `growthpulse_secrets` | Everything in `growthpulse` except the functions that set connection status. It can't read reports, records or clients |
| `postgres` (owner) | Runs migrations | Not used by the application. Every role check refuses it |

**Function rules.**
- Owner-action and token functions are `SECURITY DEFINER`, owned by the migration owner, with `search_path=''`.
- They validate every argument and log to `owner_action_log`.
- `EXECUTE` is revoked from `PUBLIC` and granted to one role each.
- Tests assert this list exactly, the same way they assert the 58-entry grant manifest today.

**Tokens.**
- Tokens are encrypted **in the application** with AES-256-GCM before they reach the database. The key sits in GitHub and Streamlit secrets, never in the database. Versioned keys allow rotation.
- The additional authenticated data is `connection_id`, token type and key version, so a ciphertext can't be swapped onto another connection.
- A database leak alone exposes no usable token.
- Google access tokens are never stored: they're short-lived and kept in memory only. Only refresh tokens, and Meta's long-lived tokens, are stored.
- Revoking a connection deletes its credential row.

**Logins.**
- No new role gets a login in this milestone.
- Enabling one creates credentials, so each needs your approval: the manager role when the dashboard needs it, the token role when OAuth needs it.
- `provision_roles` and `connection_check` will be extended for them then.

---

## 7. Account lifecycle and future OAuth interfaces

### 7.1 Lifecycles

**Connection lifecycle**

`pending` → `active` → `needs_reauth` → `active` (after the owner signs in again) → `revoked`

- A connection becomes `needs_reauth` when a refresh fails or the provider revokes access. Every account syncing through it pauses until it is fixed.
- Revoking a connection deletes its stored credential.

**Ad account lifecycle**

```
discovered (in discovered_ad_accounts)
  ─ owner adds to client ─▶ onboarding (backfill from backfill_start_date, chunked)
  ─ backfill done ─▶ active ⇄ paused (owner)
  active ─ repeated failures / access lost ─▶ needs_attention ─ owner resolves ─▶ active
  any ─ owner archives ─▶ archived (no syncs; data kept, read-only)
```

Deleting an account and its data is deferred until there's a retention policy (question 7).

### 7.2 Interfaces

These are defined in Milestone 5, with fakes only:

```python
@dataclass(frozen=True)
class AccountContext:  # what a connector is allowed to read
    ad_account_id: UUID
    client_id: UUID
    data_source: DataSource
    platform: Platform
    external_account_id: str
    currency: str
    timezone: str


class ConnectorFactory(Protocol):
    def for_account(self, account: AccountContext) -> AdDataConnector: ...


class OAuthProvider(Protocol):  # OAuth milestone: Google and Meta adapters
    provider: Platform

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...
    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet: ...
    def refresh(self, credential: SecretStr) -> TokenSet: ...
    def revoke(self, credential: SecretStr) -> None: ...


class AccountDiscovery(Protocol):  # read-only listing of accessible accounts
    def list_accounts(self, access_token: SecretStr) -> list[DiscoveredAccount]: ...


class TokenStore(Protocol):  # encrypts and decrypts; talks to growthpulse_secrets only
    def save(self, connection_id: UUID, tokens: TokenSet) -> None: ...
    def load(self, connection_id: UUID) -> TokenSet: ...
    def delete(self, connection_id: UUID) -> None: ...


class CredentialProvider(Protocol):  # used by live connectors; refreshes when near expiry
    def access_token(self, connection_id: UUID) -> SecretStr: ...
```

`TokenSet` holds every token as `SecretStr`, and its `repr` never shows a value. Live connectors take a `CredentialProvider` and an `AccountContext`, never raw settings. `SyntheticConnector` takes an `AccountContext` and a synthetic client spec.

### 7.3 Things the OAuth milestone must handle (recorded now)

- **Google read-only access.** The Google Ads API has a single OAuth scope, `adwords`, which permits changes. Read-only behaviour must therefore be enforced in code: an allowlist of read-only service calls, plus a test that no mutate method is reachable.
- **Meta read-only access.** Meta has `ads_read`, which is read-only. Use only that.
- **Google refresh-token expiry.** A Google OAuth app left in "testing" status issues refresh tokens that expire after 7 days.
- **Meta token expiry.** Long-lived Meta tokens expire after about 60 days and can't be refreshed silently, so the status must warn before expiry.
- **Approvals and access levels.** Meta app review and the Google Ads developer-token access level may both be needed before real accounts can be read.
- **OAuth callback.** It needs PKCE, a single-use `state` with a short lifetime, and an exact redirect URI. Where the callback runs depends on the dashboard host.

---

## 8. Scheduling and retries

- **Dispatcher.**
  - A scheduled job (GitHub Actions cron, Milestone 7) starts one dispatcher process.
  - The dispatcher claims due accounts one at a time: `next_due_at <= now()`, enabled, account active, connection active, no live lease.
  - It claims them with `SELECT … FOR UPDATE SKIP LOCKED`, and records a lease (run ID plus expiry) in the same transaction.
  - It runs accounts one after another, because the writer is limited to 3 connections.
  - GitHub Actions `concurrency` stops overlapping workflow runs. The lease also protects against a crashed runner.
- **Account sync.**
  - The window runs from `synced_through − lookback_days` to yesterday in the account's timezone, so late restatements are fetched again and upserted idempotently.
  - Records, quarantine rows, the run's finish and the sync-state update (watermark, failures reset, next due time) all commit in **one transaction**.
  - A day with zero delivery is a successful sync, not `no_valid_records`.
- **Failure isolation.** One account's failure only changes that account's state and run row. The dispatcher moves on to the next account.
- **Retries.**
  - Retryable codes are `database_unavailable`, `connector_unavailable` and `rate_limited`. These wait `min(15 min × 2^(n−1), 6 h)` with ±20% jitter, as `retry_wait`.
  - After `max_consecutive_failures` the account becomes `needs_attention` and stops until you resume it.
  - Non-retryable codes act immediately: `auth_failed` marks the connection `needs_reauth`, and `account_access_lost` marks the account `needs_attention`.
  - Each retry is a new run row linked by `retry_of_run_id`. Failures are never retried inside a run, which matches your "no automatic retry of a failed write" rule within a run.
- **Schedule calculation.** A pure function computes `next_due_at` from frequency, preferred local hour, timezone (including daylight-saving changes) and the last success. It is unit-tested and has no database dependency.
- **Client analysis.**
  - Due when every active account of the client has synced since the client's last analysis, and all of them are synced through the same end date.
  - Accounts in `needs_attention` are left out, and the run records which accounts it used.
  - Same data in, same report ID out, so a repeat run writes nothing.
- **Abandoned runs.** The existing `appears_abandoned` flag is reused. An expired lease lets the next dispatcher reclaim the account and mark the stale run failed with `abandoned`.

---

## 9. Phased plan

Every phase ends with a written report and stops for your review.

### Phase A: domain and connector code (no database changes)
- Build `AccountContext`, `ConnectorFactory`, the account-mismatch guard and the `account_mismatch` issue code.
- Parameterise the synthetic generator into several fictional clients and accounts, including one in another currency.
- Build the database-row-to-record loader (tested against a local server), summary aggregation, the schedule function and the backoff function.
- Add the OAuth, discovery and token-store interfaces with fakes, and the AES-GCM token envelope with tests.
- Retire the single-account settings.

**Acceptance:**
- All 707 existing tests pass.
- The default synthetic output and the sample report are byte-identical, and the report ID is still `feb1e176…`.
- A report built from loaded records equals one built in memory, or the gap is documented and fixed in the loader.
- No secret appears in any `repr`, log line or exception.

### Phase B: migrations (local and CI only)
- Write migrations 4–8, the rollback section, the extended verification script and the grant manifest.
- Add static migration tests, the upgrade test, the fresh-install test, isolation tests and role-boundary tests for all four roles. That includes every forbidden action, such as the writer approving an experiment, the manager reading a table, or the token manager reading a report.
- Write `MILESTONE_5_PRE_MIGRATION_REPORT.md`.

**Acceptance:** green on local PostgreSQL 16 and CI PostgreSQL 17, and the upgrade test proves section 5.3.

**Gate:** your review. **You** apply the migrations, as in Milestone 4.

### Phase C: persistence and pipeline
- Update the scoped store with collision checks.
- Add the account-sync run, client-analysis run, combined run and sync-state handling with leases.
- Add Python wrappers for the owner actions and the token functions, tested locally only, and the client parameter in `read`.

**Acceptance:**
- Two fictional clients run end to end locally and stay isolated: no row, report or finding refers across clients.
- Retry, backoff, lease-expiry and needs-attention paths are covered.
- The combined run is idempotent on an upgraded database.

### Phase D: live verification on Supabase (needs your authorisation)
- Read-only post-migration checks.
- One combined run, which must write 0/0/0 and return the same report ID.
- One sync per synthetic account, then one client analysis, checked read-only.

**Gate:** your approval before any write.

### Phase E: finalise
Documentation, `MILESTONE_5_REPORT.md`, then commit, push and CI. **Gate:** your approval to commit.

### Testing totals to track

| | Count |
|---|---|
| Offline tests today | 619 |
| PostgreSQL integration tests today | 88 |
| Expected new tests (rough estimate) | 150–250 |

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| The backfill migration fails halfway on Supabase | It's a single transaction with preconditions, so a failure leaves nothing applied. It's exercised first by the upgrade test against the exact Milestone 4 state |
| A report loaded from stored records differs from one built in memory (decimal scale) | Phase A test. The fix goes in the loader. The combined mode keeps the Milestone 4 path intact either way |
| Cross-client contamination through a connector bug | Mismatch guard, composite foreign keys, collision checks and isolation tests |
| A `SECURITY DEFINER` function is too broad | Pinned `search_path`, argument checks, one granted role each, an exact `EXECUTE` manifest test, and owner action logging |
| Token encryption key lost or leaked | Versioned key ring and a rotation procedure. Losing the key only forces a re-sign-in, and leaking it doesn't help without database access |
| GitHub Actions cron is late or skipped | Watermarks and lookback make each sync self-healing, and the dashboard shows sync freshness |
| The free Supabase plan pauses | It appears as `database_unavailable`, which is retryable, and is flagged in sync status |
| The writer's connection limit is too low for many accounts | Run accounts one after another. Revisit the limit when a real account count is known |
| Scope grows into the OAuth or dashboard milestones | The deferral list in section 11 is binding unless you change it |

---

## 11. Deferred

- **To the OAuth milestone:**
  - real Google and Meta OAuth adapters and discovery;
  - hosting the callback;
  - enabling the token manager's login;
  - creating the encryption key;
  - the read-only enforcement for Google;
  - token refresh and expiry handling;
  - live connectors behind the `CredentialProvider`;
  - pending-authorisation storage.
- **To the AI milestone:**
  - Claude-generated insights and experiment proposals built on client-scoped reports;
  - deciding whether client scope goes into the report body (a report schema version bump).
- **To the dashboard milestone:**
  - the owner login;
  - client and account management and the experiment-approval screens, which call the manager functions;
  - enabling the manager role's login;
  - sync status pages.
- **To the scheduling milestone:** the GitHub Actions cron workflow running the dispatcher, and run alerts.
- **Not planned:** client logins, team permissions, billing, reassigning accounts between clients, data deletion and retention, and any write to an advertising platform.

---

## 12. Decisions (approved 2026-11-11)

1. **Roadmap.** Milestone 5 is multi-client and multi-account architecture, Milestone 6 is OAuth and live advertising integrations, and Milestone 7 is AI insights. The dashboard and cloud automation follow as explicit separate deliverables. Wherever this document says "the OAuth milestone" it means Milestone 6, and "the AI milestone" means Milestone 7.
2. **Report scope.** One consolidated report per client, with account-level breakdowns (section 13.1).
3. **Account reassignment.** Not allowed in version 1.
4. **Token encryption.** Application-level AES-256-GCM with unique nonces, authenticated context and key versioning. Rotation and recovery are documented in section 13.4. The key is never stored in PostgreSQL.
5. **Milestone 4 data.** Preserved through a verified backfill. The database is never reset.
6. **New roles.** The two restricted roles are approved in principle, created without login. **Creating them still needs separate approval at the migration phase.**
7. **Retention.** Archived clients and accounts keep their historical data until a retention policy is documented.
8. **Legacy settings.** The single-account settings are retired only once compatibility is proven. In Phase A they are deprecated, not removed.
9. **Live verification.** Synthetic data only, and only after separate approval.

**Further requirements set at approval:**
- one OAuth connection may discover and authorise several accounts;
- account sync and client analysis are independent operations;
- existing record, finding and report IDs are preserved;
- currency, timezone, conversion-definition and attribution differences are handled explicitly and never aggregated silently;
- the two-account backfill must map every existing record unambiguously, without changing historical report contents or IDs;
- TLS `verify-full` is a prerequisite before any real client data.

---

## 13. Clarifications from Phase A

### 13.1 Account-level breakdowns live beside the report, not inside it

If the per-account breakdown went into the report body, every report ID would change, including `feb1e176…`. Instead, each client analysis produces two documents:
- the **report**, exactly as `build_report` makes it today;
- an **account breakdown**, with its own schema version and `breakdown_id` hash, linked to the report by `report_id`.

Phase B stores the breakdown in a new append-only table, `analytics_report_breakdowns`:
- `report_id` primary key and foreign key, `client_id`, `breakdown_id`, `breakdown jsonb`;
- a composite FK (`report_id`, `client_id`).

Both documents are deterministic, and the breakdown doesn't depend on input order.

The breakdown reports each account in its own currency and timezone, with KPIs per conversion basis. It also has a compatibility section, which states:
- the currencies, timezones and conversion bases involved;
- which bases each account has;
- the attribution settings;
- flags for mixed currencies, mixed timezones, multiple conversion bases and mixed attribution settings.

It never gives a cross-currency total, and never adds conversions across bases. A basis already fixes the platform, event, stage and attribution window.

### 13.2 Stored records round-trip exactly

The loader (`persistence/load.py`) normalises two values:
- **Timestamps:** `ingested_at` is converted to UTC.
- **Amounts:** database padding is removed, as in `237.340000` → `237.34` and `100.000000` → `100.00`. No value changes.

Tested on PostgreSQL 16 with session timezones UTC, `Pacific/Chatham` (+13:45) and `America/St_Johns` (−3:30):
- the loaded records equal the source records byte for byte;
- the client analysis reproduces report `feb1e176…`.

Padding alone was also shown not to change the report ID.

### 13.3 Per-account validation adds up exactly

Every validation check groups rows within one account, so validating accounts separately gives the same result as validating them together. `combine_summaries` adds per-account summaries, and the combined Milestone 4 summary is reproduced exactly: 300 rows, 0 quarantined, 12 warnings.

**Phase B and C consequence:** each account-sync run stores its own validation summary (`pipeline_runs.data_quality`), and the client analysis combines the latest one from each included account.

An account whose sync produced only quarantined rows is still included, so its quarantined rows count in the client's data quality.

### 13.4 Token key rotation and recovery

- **Format.** `TOKEN_ENCRYPTION_KEYS` = `1:<base64url 32 bytes>,2:<…>`, plus `TOKEN_ENCRYPTION_ACTIVE_VERSION`. Both settings are required together. Versions run 1–32767 and two versions can't share a key.
- **Ciphertext.** Format version, key version, a 12-byte random nonce, and the ciphertext with its tag. The authenticated context is `["growthpulse-token", format, connection_id, token_type, key_version]`.
- **Rotation.**
  1. Add a new version.
  2. Make it active.
  3. Re-encrypt every stored token with `TokenCipher.rotate`, a Milestone 6 job run as the token manager role.
  4. Once nothing uses the old version, remove it from the ring.
- **Lost key.** Tokens under it can't be recovered. Each affected connection becomes `needs_reauth`, and the owner signs in again. No advertising data is lost.
- **Leaked key.** Rotate immediately, then revoke and reauthorise every connection, because the leaked key plus a copy of the database would expose those tokens.
- **Nonce limit.** With random nonces, one key should stay well under 2³² encryptions. Token volume here is tiny, and rotation resets the count.

### 13.5 Other findings

- **Synthetic data.** The generator now produces several fictional clients. The default reproduces the Milestone 2–4 dataset byte for byte, with SHA-256 `fcf86a71…`. The second client deliberately reuses campaign IDs on different accounts, and the third mixes three currencies and timezones across two Google accounts and one Meta account.
- **Synthetic per-account fetches** drop rows that name no account, as a platform API never returns them. Supplied test rows are passed through untouched, so the account guard can still be exercised.
- **Legacy client IDs** are fixed in `accounts/legacy.py`, and the Phase B seed must use exactly those values. A test ties them to the generator's account IDs, currency and timezone.
- **Package boundaries.** `accounts` sits below every layer. `credentials` imports no layer. Analytics, AI analysis and the dashboard may never import `credentials`. The architecture test enforces all of this.
- **Legacy settings.** They are kept but marked deprecated in `config.py` and `.env.example`. `Settings.legacy_account_settings_in_use` reports any that are set. They are removed once the multi-account path is proven in Milestone 6.
- **Google OAuth scope.** `.env.example` now says Google's scope has no read-only variant, so read-only use is enforced in code (section 7.3).

---

## 14. Clarifications from Phase B

These are recorded in full in [MILESTONE_5_PHASE_B_REPORT.md](MILESTONE_5_PHASE_B_REPORT.md), section 3. None changes an approved decision.

1. **Compatibility until Phase C.** The Milestone 4 store doesn't pass scope yet, so the database supplies it, and fails closed:
   - `pipeline_runs.client_id` defaults to the fictional legacy client. Its composite foreign key to `clients (client_id, data_source)` means a live run can never default into that synthetic client.
   - Every other scope column is derived from its parent row and never defaulted:
     - a record's or finding's account from its own `(data_source, platform, account_id)`;
     - a finding's client from that account;
     - a report's client from its run;
     - a proposal's client from its report.
   - An explicit value always wins and is checked by the composite foreign keys.

   The report and proposal backfill defaults are dropped right after the backfill, as section 5.2 intended.
2. **Records are tied to the run that wrote them.** A new trigger refuses a record whose writing run belongs to another client or, for an account sync, to another account. This closes a gap section 3.3 left open, because records carry no `client_id`.
3. **Unregistered accounts are refused.** A record or finding naming an account no client holds is now rejected, not stored. A combined run containing such rows fails with `database_rejected_data`.
4. **Quarantine.** Rows from combined runs stay unscoped, and `quarantine_id` is unchanged until Phase C.
5. **Views.** `latest_reports` keeps its Milestone 4 meaning, the latest report per data source. The new `latest_client_reports` gives one per client.
6. **Sync state.** The writer can't pause, resume or touch a paused account's sync state, which is enforced by RLS. Pausing is an owner decision.
7. **Extra owner functions.** `add_synthetic_ad_account` (fictional accounts only) and `request_sync`, plus two internal helpers granted to nobody.
8. **Dashboard connection view.** It omits `provider_subject_id`, and shows credentials only as a count and earliest expiry.
9. **Nonce uniqueness** is also enforced in the database, unique on `(key_version, nonce)`.
10. **Verification and revert.**
    - `supabase/verification/milestone_5_checks.sql` verifies the post-Milestone 5 state.
    - `post_migration_checks.sql` remains the Milestone 4 state check.
    - A restrict-only `supabase/rollback/20261111200000_revert_milestone_5.sql` returns the database to the exact Milestone 4 schema while keeping its data. This is proven by a schema comparison against a pristine Milestone 4 database.

---

## 15. Clarifications from Phase C

These are recorded in full in [MILESTONE_5_PHASE_C_REPORT.md](MILESTONE_5_PHASE_C_REPORT.md), section 4. Items 1 and 2 need your decision before scheduled operation. None changes an approved decision.

1. **The report's data-quality section covers each account's latest sync window.** A client report includes the latest successful sync summary of each active account. If that sync was a short lookback re-fetch, the section describes those days, not the whole analysis window. Report IDs stay deterministic, but they depend on the window of the latest syncs. **Needs a decision before scheduling.**
2. **Onboarding accounts are synced but not analysed.** Analysis reads only accounts in the `active` state. Nothing moves an account from `onboarding` to `active` automatically: the owner does it with `set_account_status`. **Needs a decision before scheduling.**
3. **Accounts needing attention stay stopped.** A direct sync call is refused until the owner resumes the account.
4. **`auth_failed` stops the account but doesn't flag the connection.** The account goes to `needs_attention`. Marking the connection `needs_reauth` needs the token manager, which is Milestone 6 work.
5. **Abandoned leases don't count as failures.** When an expired lease is taken over, its run is marked `abandoned`, but the consecutive-failure count doesn't rise.
6. **An empty window still advances the watermark.** A sync window with no data is a successful sync of zero records.
7. **Removing the scope fallbacks is a proposal only.** It is `supabase/proposed/20261111200500_remove_scope_fallbacks.sql`, tested but outside the migrations folder. If it's adopted, `milestone_5_checks.sql` needs its expected inventory updated.

**Resolutions (2026-11-12):**
1. **Data quality** is computed from what's analysed:
   - records and their warnings come from the stored, deduplicated records in the window;
   - quarantined rows come from sync evidence covering the window, merged by quarantine ID, independent of order;
   - a window that successful syncs don't fully cover is refused.
2. **Manual activation is kept for V1.**
3. **Watermarks advance only on responses the connector confirms complete.** An unconfirmed response fails with `incomplete_response` (retryable), empty or not.
4. **The fallback-removal migration stays proposed and unapplied.**
