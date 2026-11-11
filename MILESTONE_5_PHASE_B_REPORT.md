# Milestone 5, Phase B report: database migrations and isolation

**Status: Phase B complete locally, awaiting review.** Phase C has not started.

Everything was tested on 2026-11-11 on disposable local PostgreSQL 16.2 servers only. The PostgreSQL 17 CI job will run the same tests after an approved push.

**What Phase B did not touch:**
- No Supabase connection was made.
- No Meta or Google API connection was made.
- No real token or encryption key exists.
- No role password was set, and no login was enabled outside the throwaway test servers.
- No production data was written.
- Nothing is committed.

## 1. Results at a glance

| Check | Result |
|---|---|
| Offline tests | **798 passed** (775 after Phase A, so 23 new static migration checks) |
| PostgreSQL integration tests, disposable PostgreSQL 16.2 | **224 passed**, up from 94, so 130 new |
| Total | **1,022 tests** |
| `ruff check` (with the bandit `S` rules), `ruff format --check`, `uv lock --check` | Passed |
| Applied Milestone 4 migrations 1–3 | Unchanged byte for byte. The Milestone 4 store, pipeline and analytics code are also unchanged |
| Upgrade from the exact Milestone 4 state | All 7 Milestone 4 tables are row-for-row identical. Report bytes, its hash and `feb1e176…` are unchanged |
| Backfill mapping | 210 Google and 90 Meta records, checked against each record's stored platform and account ID |
| Combined run after the upgrade | 0 records, 0 reports and 0 findings written, same report ID |
| Unexpected data | All 5 cases refused, with Milestone 4 left untouched |
| Reverting Milestone 5 | Restores a schema identical to a pristine Milestone 4 database, and keeps every row |
| Secret scan of new and changed files | Nothing found except the existing `PASSWORD` placeholder in `.env.example` |

## 2. What was built

### Migrations

These are new files. None of the three applied migrations was modified.

| # | File | Contents |
|---|---|---|
| 4 | `20261111200000_create_tenancy_tables.sql` | The tables `workspaces`, `clients`, `provider_connections`, `discovered_ad_accounts`, `ad_accounts`, `account_sync_config`, `account_sync_state` and `owner_action_log`; RLS on all of them; lifecycle and immutability triggers for clients, accounts and connections; the append-only action log |
| 5 | `20261111200100_scope_existing_tables.sql` | Preconditions; the fictional legacy client and its two accounts; scope columns on all six scoped Milestone 4 tables; the backfill; composite foreign keys; the scope-derivation and run-scope triggers; the append-only `analytics_report_breakdowns` table |
| 6 | `20261111200200_create_token_storage.sql` | The `growthpulse_token_manager` role, created without login; the `growthpulse_secrets.oauth_credentials` table (ciphertext, a 12-byte nonce unique per key, key version, expiry); 5 `SECURITY DEFINER` functions |
| 7 | `20261111200300_create_owner_actions.sql` | The `growthpulse_manager` role, created without login; 11 `SECURITY DEFINER` owner actions; 2 internal helpers granted to nobody |
| 8 | `20261111200400_update_grants_and_views.sql` | Writer grants and RLS policies on the new tables; the 8 Milestone 4 views extended by appending columns; 8 new views; dashboard grants |

Each file is one transaction and ends by revoking everything from `PUBLIC` and the Supabase API roles. They are tested under worst-case global default privileges.

### Supporting files
- **`supabase/rollback/20261111200000_revert_milestone_5.sql`** (new, never run against Supabase). It removes only Milestone 5, using restrict-only drops, and keeps all Milestone 4 data. It recreates the 8 Milestone 4 views verbatim.
- **`supabase/rollback/20261111100000_drop_growthpulse.sql`** (extended). It now drops all eight migrations.
- **`supabase/verification/milestone_5_checks.sql`** (new). It holds 11 read-only checks for Supabase after Phase B is applied:
  - migration history;
  - schemas;
  - the inventory of 105 objects;
  - ownership;
  - RLS;
  - definer and invoker settings for every function;
  - the 4 roles;
  - the exact 110-entry grant manifest;
  - API-role isolation;
  - Data API exposure;
  - the backfill counts and IDs.
- **`connection_check.py`** (extended). It now checks the new tables and views, the refused action log, and that neither app role can reach the token schema, token functions or owner actions. **Run it only after Phase B is applied.** Against today's Supabase it would fail, because the new tables don't exist there yet.
- **CI.** The PostgreSQL 17 job now fails if its database URL is missing, rather than silently skipping every test (`GROWTHPULSE_REQUIRE_POSTGRES=1`), and reports skips with `-ra`. The new tests run there automatically, because they use the existing `postgres` marker.

## 3. Design clarifications

None of these changes an approved decision. Each is recorded in section 14 of [MILESTONE_5_DESIGN.md](MILESTONE_5_DESIGN.md).

1. **Compatibility until Phase C.** This is the main one.
   - **The problem.** Requirement 11 needs the unchanged Milestone 4 run to work after these migrations. But the Milestone 4 store doesn't send `client_id` or `ad_account_id`, and changing the store is Phase C. Without help from the database, applying Phase B would break the existing pipeline.
   - **What the database now does.**
     - **Runs:** `pipeline_runs.client_id` defaults to the fictional legacy client. Its foreign key to `clients (client_id, data_source)` makes it impossible for a **live** run to default into that **synthetic** client.
     - **Everything else is derived from its parent row, never defaulted:**
       - a record's or finding's account from its own `(data_source, platform, account_id)`;
       - a finding's client from that account;
       - a report's client from its run;
       - a proposal's client from its report.
     - **Explicit values win.** A value supplied explicitly is always kept and checked by the composite foreign keys, so Phase C code can pass scope directly.
     - **Report and proposal backfill defaults** are dropped straight after the backfill, as the design intended.
   - **Later.** Once Phase C passes scope everywhere, a later migration should remove the run default and the derivation trigger. I recommend that.
2. **New safeguard: records are tied to the run that wrote them.** Records carry no `client_id`, so a run for client A could otherwise have written rows into client B's account. A trigger now refuses that, and refuses an account-sync run writing any account but its own.
3. **Behaviour change: unregistered accounts are refused.** A record or finding naming an account that no client holds is now rejected rather than stored. A combined run containing such rows fails with `database_rejected_data`. That's the intended isolation, but it is new.
4. **Quarantined rows from combined runs stay unscoped**, and the quarantine ID formula is unchanged until Phase C. The Supabase quarantine table is empty.
5. **`latest_reports` keeps its Milestone 4 meaning**, the latest report per data source. The new `latest_client_reports` gives each client's latest report.
6. **The pipeline can't pause or resume an account.** RLS stops the writer from setting `paused` or touching a paused account's sync state.
7. **Two extra owner functions.**
   - `add_synthetic_ad_account` adds fictional accounts, and only to synthetic clients, with an ID starting `synthetic-`.
   - `request_sync` brings a sync forward.
8. **The dashboard's connection view omits `provider_subject_id`.** Stored credentials appear only as a count and the earliest expiry.
9. **Nonce uniqueness** is also enforced in the database, unique on `(key_version, nonce)`, on top of the application's random nonces.
10. **Migration failures are per file.** The Supabase CLI applies each file in its own transaction. If migration 5 refused unexpected data, migration 4, which only creates empty tables, would stay applied, and a later `db push` would resume at 5 once the data was fixed. The tests cover this exact situation.

## 4. Security results

These are tested against a real server, and most use the application roles themselves.

| Boundary | Evidence |
|---|---|
| Writer | Its privileges equal an exact approved set of 15 tables. It can update only named columns, and never a scope column. It has no access to the token schema, token functions, owner functions or the action log. It can't create or modify clients, accounts, connections or sync settings, can't approve experiments, and can't pause or resume accounts |
| Dashboard | It has `SELECT` on exactly 17 `growthpulse_read` views and nothing else. No view exposes ciphertext, nonce, key version, raw payloads or the provider subject ID |
| Manager | It can execute exactly the 11 owner functions. It can't read or write any table or view directly, and can't use token functions or internal helpers |
| Token manager | It can execute exactly the 5 token functions. It can't read the credentials table directly, or any `growthpulse` table, and can't run owner actions. Credentials can't be read unless the connection is active, and can't be stored for a revoked one |
| `SECURITY DEFINER` | Exactly 16 functions. Each is owned by the migration owner, pinned to `search_path=""`, and granted to exactly one role. Every function in the three schemas has a pinned `search_path` |
| Roles | All 4 roles have no elevated attributes, `NOINHERIT` and no memberships. The 2 new roles are created without login |
| API roles and `PUBLIC` | No usage on any of the 3 schemas, and no privilege on any table, view, sequence or function |
| Cross-client attacks, by direct SQL as a superuser | A live record or run can never land in the fictional legacy client, whether its account is derived or forced. 9 kinds of reference are refused: record to another client's account, record written by another client's run, finding to another client's account, finding to another client's report, run to another client's report, a re-labelled report, the same platform account in two clients, a synthetic account under a live client, and a live account without a connection. An account-sync run writing another account is refused. Run kind and scope rules are enforced (5 cases), and sync leases must belong to the same account |
| Breakdowns | Breakdowns linked to another client's report, or labelled with another client, are refused. So are a body/column ID mismatch, a duplicate per report, any update (including by a superuser) and any delete by the writer |
| Lifecycles | Every account and connection status transition in the database matches the Python model: 20 account pairs and 12 connection pairs. Identity and client are immutable, and archived clients stay archived |
| Detection | `milestone_5_checks.sql` flags each of the following when injected: an extra grant, an extra object, a manager login, an extra run, and a table without RLS. `connection_check` flags token, owner-action or secrets-schema access granted to either app role |

## 5. Compatibility checks

| Requirement | Evidence (all from integration tests) |
|---|---|
| Lossless backfill from the exact Milestone 4 state | The state is built exactly as on Supabase (migrations 1–3, then the pipeline run twice). All 7 tables are identical before and after, compared row by row as JSON over the Milestone 4 columns. Counts are 2 runs, 300 records, 0 quarantined, 1 report, 19 findings, 0 proposals and 0 status events |
| 210 / 90 mapping from stored identities | Every record joins to the account whose platform, external ID and data source equal the record's own. Google has 210, Meta has 90, and none is unmapped |
| Historical report unchanged | Stored bytes are identical. The ID recomputed from the stored body equals `feb1e176…`, and the body equals the committed sample report. Stored record and finding IDs equal the source |
| Combined run after upgrade | Writes 0/0/0 with the same report ID. A real change still writes, correctly scoped |
| Clean install | All 8 migrations on an empty database. The pipeline writes 300 records and 19 findings, then 0/0/0 |
| Refusal on unexpected data | Each case is refused with Milestone 4 left untouched: a non-synthetic run, a record on an unknown account, a record whose currency differs from its account, a duplicate record identity, and a finding on an unknown account |
| Defence in depth | With the preconditions removed from the file, the constraints still abort: a live run fails on its foreign key, and unknown accounts fail on `NOT NULL` |
| Two clients side by side | A second fictional client is stored through the unchanged store, with its scope derived. Each client's stored records rebuild its own stored report ID |
| Views | All 9 Milestone 4 views keep their columns and order, with scope columns appended at the end. The dashboard reads each client's latest report and delivery separately (300 and 150 rows) |
| Revert | Upgrade, run the pipeline, then revert. The schema snapshot equals the pristine Milestone 4 snapshot: columns, constraints, indexes, triggers, policies, views, functions, table and column grants, schemas and roles. All data, including the post-upgrade run, is kept, and the upgrade re-applies cleanly |

## 6. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_postgres_milestone5.py` (new) | 124 | Upgrade, backfill, refusal cases, cross-client attacks, breakdowns, lifecycles, role boundaries, owner and token functions, views, revert, the verification script |
| `tests/test_postgres_integration.py` | 100 (6 new) | The Milestone 4 suite, now run against all 8 migrations. Connection-check drift for token and owner access. The Milestone 4 verification script, run on a Milestone 4-only database |
| `tests/test_migrations.py` | 67 (23 new) | Enum lists against Python; legacy IDs against `accounts/legacy.py`; definer functions and their single grantee; the backfill never deletes or rewrites history; preconditions come first; the revert recreates Milestone 4 views verbatim; restrict-only rollbacks; read-only verification scripts |
| Helpers | | `tests/pg_helpers.py` and `tests/pg_seed.py`, both new |

## 7. Deployment risks and recommended procedure

Applying Phase B to Supabase is Phase D, and needs your separate approval.

**Risks**
1. **PostgreSQL 17 is untested.** All Phase B tests ran on 16.2. Push, and get green CI on `postgres:17.11`, before applying anything.
2. **Backups.** Check what backup your Supabase plan provides before applying. If something goes wrong, the tested revert script is the fallback, but it deletes all Milestone 5 rows.
3. **Login-role side effect.** `supabase db push` without `SUPABASE_DB_PASSWORD` creates or refreshes the temporary `cli_login_postgres` role, as seen in Milestone 4. Apply the migrations the way you did then.
4. **The compatibility defaults stay** until a later migration removes them after Phase C.
5. **The updated `connection_check` expects the Phase B schema.** Don't run it before the migrations are applied.
6. **Each file is its own transaction.** If migration 5 refuses the data, migration 4's empty tables remain, which is safe and resumable.
7. **Locks.** The new constraints validate tables of at most a few hundred rows, so locks last milliseconds. Even so, apply while no pipeline run is in progress.

**Recommended procedure (Phase D)**
1. Approve a commit and push, then wait for both CI jobs to pass.
2. Confirm your backup.
3. Apply the five migrations as in Milestone 4.
4. Run `milestone_5_checks.sql` as `postgres` through psql. Expect all checks to pass.
5. Run `connection_check`.
6. Run one combined synthetic run. Expect 0/0/0 written and the same report ID.

## 8. Next steps (each needs your approval)

1. **Commit and push Phases A and B**, so CI runs all 1,022 tests, including the 224 PostgreSQL tests on version 17.
2. **Phase C:** scoped persistence, account-sync and client-analysis runs, leases and retries, the owner-action and token wrappers, and client-aware reads.
3. **Phase D:** apply the migrations to Supabase and verify, using synthetic data only.

TLS `verify-full` remains a prerequisite before any real client data.
