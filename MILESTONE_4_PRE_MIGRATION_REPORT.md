# Milestone 4 pre-migration report: GitHub and Supabase integration

> **Superseded in part by [FINAL_MIGRATION_REVIEW.md](FINAL_MIGRATION_REVIEW.md).** The migrations, the rollback script and the setup steps changed after this report. In particular, `supabase link` does not ask for a database password, and a dry run needs the password set, or the CLI creates a temporary login role. Use the final review's instructions.

Status: **stopped for review before any database change.** The migrations, roles and persistence code are written and tested locally, but nothing has been applied to the Supabase project. Milestone 4 changes are uncommitted.

## 1. Pre-flight results

| Check | Result |
|---|---|
| Working tree before this milestone | Clean, on `main` |
| Milestone commits | `2559893` (M1), `5d3504c` (M2), `fe070fa` (M3) |
| Author and committer on all three | `yahya-dev97 <260440503+yahya-dev97@users.noreply.github.com>` |
| Personal email in any Git object | 0 matches |
| Credential patterns in history | 0 matches (Anthropic, GitHub, Supabase keys, JWTs, URLs with passwords) |
| GitHub CLI | Logged in as `yahya-dev97`, token in the system keyring |
| Supabase CLI | v2.102.0, logged in. Not linked to a project yet. v3.1.0 is available but not installed |

No password, token or connection string was printed during these checks.

## 2. GitHub connection and CI status

- **Repository:** `https://github.com/yahya-dev97/growthpulse-ai`, confirmed **private**, **empty** before the push, and owned by the authenticated account.
- **Remote:** `origin` added. Before pushing, `uv lock --check`, `ruff check`, `ruff format --check` and `pytest` (448 passed) ran locally.
- **Push:** `main` pushed. `git ls-remote` shows the remote `main` at `fe070fa467670a51a0c43f6ae80339a22272a201`. Only the three approved commits were pushed.
- **CI run 43000144421:** completed with **success**. Every step passed: `uv sync --locked`, `ruff check` ("All checks passed!"), `ruff format --check` ("40 files already formatted") and `pytest` ("448 passed in 2.33s" on Linux, Python 3.12.3).

The CI workflow in this milestone adds a `postgres-integration` job. It runs only after Milestone 4 is approved, committed and pushed, so it has **not run yet**.

## 3. Supabase project

| Property | Value |
|---|---|
| Name | `growthpulse-ai` |
| Project ref | `sxycykdtgqqyvrlijmlk` |
| Region | `ap-southeast-2` (Oceania, Sydney) |
| Status | `ACTIVE_HEALTHY` |
| Postgres | 17 (`17.11.0.018`, GA channel) |

These values come from `supabase projects list`, a read-only call. No query has been run against the project database.

`supabase init` created a local `supabase/config.toml`, which configures only the CLI's local development stack. It holds no secrets, only `env(...)` placeholders, and it does not change the hosted project.

## 4. Proposed database schema

Everything lives in two schemas, neither exposed through the Supabase Data API:

- `growthpulse` holds the tables. Only the pipeline role can use it.
- `growthpulse_read` holds views. Only the dashboard role can use it.

| Table | Key | How it is written | Preserves history |
|---|---|---|---|
| `pipeline_runs` | `run_id` (uuid) | Inserted as `running` before any data is read, then finished once | A finished run cannot be changed by the pipeline role |
| `ad_performance_records` | `record_id` (schema hash) | Upsert. Updates only when values differ | `first_seen_run_id` and `last_changed_run_id` |
| `quarantined_records` | Hash of source, connector and payload | Upsert. Issue codes only. Raw payload for synthetic data only, enforced by a `CHECK` | `first_seen_*` and `last_seen_*` |
| `analytics_reports` | `report_id` (content hash) | Insert once, `on conflict do nothing` | Append-only, and a trigger rejects any update |
| `findings` | `finding_id` | Upsert, including suppressed findings. Moves only to a different, newer report | Every earlier state stays in its report |
| `experiment_proposals` | `proposal_id`, plus a unique `idempotency_key` | Insert once | Append-only. `requires_human_approval` must be true |
| `experiment_status_events` | `event_id` | Insert only | Append-only. A trigger enforces the transitions |

Allowed status transitions are `proposed → approved | rejected | cancelled`, `approved → running | cancelled`, and `running → completed | cancelled`. The first event must be `proposed`. A `unique (proposal_id, sequence)` constraint makes concurrent changes fail rather than both succeed.

Money columns use `numeric(20, 6)`, which matches the Pydantic limit of 14 digits before the decimal point and 6 after. Every enumerated column has a `CHECK` list that a test compares with the Python enum.

Dashboard views:

| View | Purpose |
|---|---|
| `pipeline_runs` | Run history. Flags a run stuck in `running` for over 6 hours as `appears_abandoned` |
| `campaign_daily_conversions` | Per conversion definition, labeled `platform_attributed_*` |
| `campaign_daily_delivery` | One row per campaign-day, so spend is never double counted |
| `analytics_reports`, `latest_reports` | Full reports, and the newest per data source |
| `findings` | Latest state of each finding, including suppressed ones |
| `data_quality_issues` | Quarantine issue codes, **without** raw payloads |
| `experiment_proposals`, `experiment_status_history` | Proposals with current status, and the full history |

## 5. SQL migration files

| File | Lines | Contents |
|---|---|---|
| [20261111100000_create_growthpulse_tables.sql](supabase/migrations/20261111100000_create_growthpulse_tables.sql) | 296 | Schema, 7 tables, constraints, indexes, transition and append-only triggers |
| [20261111100100_create_access_roles.sql](supabase/migrations/20261111100100_create_access_roles.sql) | 141 | 2 roles, session limits, revokes, column-level grants, RLS and policies |
| [20261111100200_create_dashboard_views.sql](supabase/migrations/20261111100200_create_dashboard_views.sql) | 213 | Read schema, 9 views, dashboard grants |
| [20261111100000_drop_growthpulse.sql](supabase/rollback/20261111100000_drop_growthpulse.sql) | 21 | Manual rollback, kept outside `migrations/` so `db push` can never run it |

The security-critical part of migration 2, the writer's grants, is excerpted here. The full files are linked above.

```sql
create role growthpulse_writer nologin noinherit;
create role growthpulse_dashboard nologin noinherit;

grant usage on schema growthpulse to growthpulse_writer;
grant select, insert on growthpulse.pipeline_runs to growthpulse_writer;
grant update (finished_at, status, counts, error_code, report_id)
    on growthpulse.pipeline_runs to growthpulse_writer;
grant select, insert on growthpulse.analytics_reports to growthpulse_writer;  -- no update

create policy writer_update on growthpulse.pipeline_runs
    for update to growthpulse_writer using (status = 'running') with check (true);
create policy writer_propose_only on growthpulse.experiment_status_events
    for insert to growthpulse_writer with check (status = 'proposed' and actor = 'pipeline');
```

## 6. Access control model

| Role | Used by | Can | Cannot |
|---|---|---|---|
| `postgres` (owner) | You, for migrations and password setup only | Everything | Never stored in the app, `.env`, GitHub or Streamlit |
| `growthpulse_writer` | Pipeline, later GitHub Actions | `SELECT` and `INSERT` on the 7 tables. `UPDATE` on named, non-identity columns of 4 tables | Delete, truncate, create objects, update reports or proposals, change a finished run, approve an experiment, read the dashboard views |
| `growthpulse_dashboard` | Streamlit, from Milestone 6 | `SELECT` on `growthpulse_read` views | Use the `growthpulse` schema, read raw payloads, write anything |
| `anon`, `authenticated`, `service_role` | Supabase API | Nothing on either schema. Explicitly revoked | |

The layers, from strongest to weakest:

1. **Grants** are the security boundary.
2. **Row-level security** is on for all 7 tables, with policies for the writer only. A role that gained a grant by mistake would still see no rows.
3. **Triggers** stop history from being changed, even by the owner.
4. **Session defaults** are convenience only. The writer gets a 120 s statement timeout, a 60 s idle-in-transaction timeout and 3 connections. The dashboard gets read-only transactions, a 15 s timeout and 5 connections. A session can turn read-only off, but the grants still refuse writes, which a test confirms.

**Application checks:**

- `connect_backend` requires the session to log in as `growthpulse_writer`, and `connect_dashboard` as `growthpulse_dashboard`. Any other role, including `postgres`, is refused and the connection closed.
- The dashboard never falls back to `DATABASE_URL`.
- TLS is required. `sslmode=require` is added when missing, and `disable`, `allow` and `prefer` are refused.
- Connection errors keep only the exception type, never the driver message.

**How the views reach the data.** The views run with their owner's rights (`security_invoker = false`). That is what lets the dashboard read them without any grant on the base tables. It is safe here because the schema is private and the dashboard has `SELECT` only. Each view also leaves out raw payloads and internal references.

### How role passwords will be set

Migrations create both roles with `NOLOGIN` and no password, so no secret ever enters Git. After the migrations are applied, you set each password yourself:

- **Recommended: `psql` with `\password`.** It hashes the password on your machine (SCRAM-SHA-256) and sends only the hash. The plaintext never reaches the server, its logs, or this chat. `psql` is not installed on this Mac. `brew install libpq` provides it.
- **Fallback: the Supabase SQL editor**, running `alter role ... password '...'`. The plaintext goes through the dashboard, and the statement may be kept in the editor's query history. Use this only if you accept that, and delete the query from history afterwards.

Generate each password in your password manager, and use different passwords for the two roles. Never paste one into this chat.

### How the backend and dashboard will connect

- **Host.** Both use the Supavisor **session pooler**, `<pooler-host>:5432`, copied from the dashboard's **Connect** panel. The direct `db.<ref>.supabase.co` host is IPv6-only, and GitHub-hosted runners have no IPv6.
- **Usernames.** Pooler usernames carry the project ref, for example `growthpulse_writer.sxycykdtgqqyvrlijmlk`. Inside the database, `current_user` is still `growthpulse_writer`, which is what the application checks.
- **Prepared statements** are disabled, so the same code also works through the transaction pooler on port 6543 if that is ever needed.
- **Backend.** `DATABASE_URL` lives in your local `.env` now, and in a GitHub Actions secret in Milestone 7.
- **Dashboard.** `DASHBOARD_DATABASE_URL` lives in Streamlit secrets in Milestone 6. Streamlit connects from the server, so the browser never receives a credential. It reads through the `growthpulse_read` views only.
- **Never used:** the Supabase service-role key, the anon key, and the `postgres` password.

### Rollback and recovery

| Situation | Procedure |
|---|---|
| Before applying | `supabase db push --dry-run` lists what would be applied, without applying it |
| A migration fails partway | Each file is ordinary transactional SQL. I have not confirmed whether the CLI wraps each file in one transaction, so if anything was left behind, run the rollback script. It uses `if exists` and is safe to run on a partly applied state |
| Undo everything | Run [the rollback script](supabase/rollback/20261111100000_drop_growthpulse.sql) as owner, then `supabase migration repair --status reverted 20261111100000 20261111100100 20261111100200` |
| Lost or corrupted data | Data is synthetic and deterministic. Re-running the pipeline rebuilds the same records and the same report IDs. Don't rely on platform backups on the Free plan |
| Leaked role password | `alter role growthpulse_writer nologin;` blocks it at once. Then set a new password with `\password` and update `.env` or the secrets |
| A run stuck in `running` | It shows as `appears_abandoned` in the runs view. It is never treated as success |

The integration tests run the rollback, confirm no `growthpulse*` schema or role remains, and re-apply all three migrations cleanly.

## 7. Persistence code

Files changed:

- **Dependency:** `psycopg[binary]` 3.3.8 was added with `uv add`. It is the only new runtime dependency.
- **Persistence package:**
  - [connection.py](src/growthpulse_ai/persistence/connection.py) checks TLS and the login role.
  - [rows.py](src/growthpulse_ai/persistence/rows.py) maps records, rows and reports to database parameters, with no I/O.
  - [store.py](src/growthpulse_ai/persistence/store.py) holds every SQL statement and the transaction boundaries.
  - [read.py](src/growthpulse_ai/persistence/read.py) holds the dashboard's queries.
  - [errors.py](src/growthpulse_ai/persistence/errors.py) defines the fixed error codes.
- **Pipeline:** [persisted_run.py](src/growthpulse_ai/pipeline/persisted_run.py) runs the pipeline and records failures.

The persistence package still imports no analytics, connector or pipeline code, and the existing architecture test enforces this.

**How a run is handled:**

1. A `running` row is committed on its own.
2. Records, quarantine, the report, findings and the `succeeded` status are written in **one** transaction.
3. On any exception, that transaction rolls back. A fresh connection then marks the run `failed`, with a fixed code such as `connector_unavailable`, `database_rejected_data` or `database_unavailable`, plus progress counts. Messages are never stored.
4. If even that write fails, the error says `failure not recorded`, and the run later shows as abandoned.

The CLI command is `uv run python -m growthpulse_ai.pipeline.persisted_run`. It has **not** been run against Supabase.

## 8. Test results

All of the following were executed in this session.

| Run | Result |
|---|---|
| `uv lock --check` | Passed |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 53 files already formatted |
| `pytest`, no database | **561 passed, 40 skipped.** The skips are the integration tests, which need a server |
| `pytest` against a throwaway local PostgreSQL 16.2 server | **601 passed** |
| `pytest -m postgres` on that server | 40 passed |

New tests: 153 in total.

| File | Tests | Covers |
|---|---|---|
| `test_migrations.py` | 38 | Static SQL checks: enum drift, columns used by each statement exist and are granted, no grants to API roles, RLS on every table, no secrets or elevated attributes, pinned `search_path`, views without raw payloads |
| `test_persistence_connection.py` | 16 | TLS enforcement, role refusal, no message leaks, dashboard isolation |
| `test_persistence_rows.py` | 17 | Parameter mapping, quarantine IDs and redaction |
| `test_persistence_store.py` | 21 | Transaction boundaries and rollback, against a recording fake connection |
| `test_persisted_run.py` | 21 | Failure records, error codes, no secrets in errors |
| `test_postgres_integration.py` | 40 | Real SQL: idempotent re-runs, in-place restatement, rollback on rejected data, writer and dashboard denials, transitions, append-only triggers, role settings, exact largest values, rollback and re-apply |

**Removal checks:** each of 43 safeguards was removed one at a time from a scratch copy.

- The first pass caught 40. The three survivors were real gaps: the live raw-payload `CHECK`, the delivery de-duplication view, and the report data-source guard.
- I added a test for each, and all 43 are now caught.

**Limits of this testing:**

- The integration server ran PostgreSQL **16.2**, and as a superuser. Supabase runs **17** with a non-superuser `postgres` role. The new CI job uses a PostgreSQL 17 container, but it has not run yet.
- Nothing has been tested against Supabase itself. The temporary server was in the scratchpad, outside the repository, and was stopped and deleted afterwards.

## 9. Required manual setup steps

Run these in your own terminal. In Claude Code, typing `!` before a command runs it in this session's shell, but a password prompt is safer in a separate terminal. Steps 2 and 3 need your approval first.

1. **Link the CLI** (asks for the database password, so enter it only at that prompt):
   `supabase link --project-ref sxycykdtgqqyvrlijmlk`
2. **Preview:** `supabase db push --dry-run`. It should list exactly the three `20261111…` migrations.
3. **Apply, after approval:** `supabase db push`.
4. **Verify ownership** in the SQL editor. This is read-only, and every row should show `postgres`:
   `select schemaname, tablename, tableowner from pg_tables where schemaname = 'growthpulse' union all select schemaname, viewname, viewowner from pg_views where schemaname = 'growthpulse_read';`
5. **Set the passwords:**
   - Run `brew install libpq`.
   - Connect with `psql "postgresql://postgres.sxycykdtgqqyvrlijmlk@<pooler-host>:5432/postgres?sslmode=require"` and enter the owner password at the prompt.
   - Run `alter role growthpulse_writer login;`, then `\password growthpulse_writer`.
   - Repeat for `growthpulse_dashboard`.
6. **Fill in `.env` yourself**, following the shape in [.env.example](.env.example). Never commit it or paste it here.
7. **Tell me when it's done.** I'll then run a read-only connection check: login role, privileges and TLS. It writes nothing.

## 10. Risks and limitations

- **Custom roles through Supavisor are unverified.** Supabase documents custom roles, but I haven't confirmed that the pooler authenticates them for this project. Step 7 checks it before any data is written.
- **The owner of the migrated objects matters.** If the CLI applies migrations as a role other than `postgres`, the views may not bypass RLS on the base tables, and the dashboard would see no rows. Step 4 checks it.
- **Supabase restrictions:** Supabase's role-management safeguards may refuse a statement such as `alter role ... set` or `connection limit`. Postgres 17 behavior is also untested here. A failure would surface at step 3, and the rollback script clears a partial result.
- **Security Advisor warnings:** Supabase's linter may flag the owner-rights views or the `using (true)` writer policies. Both are deliberate, since the schemas are private and the grants are the boundary.
- **Data API:** never add `growthpulse` or `growthpulse_read` to the exposed schemas. The revokes would still block the API roles, but exposing them removes a layer.
- **TLS:** `sslmode=require` encrypts the connection but doesn't verify the server certificate. Moving to `verify-full`, with the CA certificate from the Supabase dashboard, is a recommended later step.
- **Network:** the project accepts connections from any IP by default. GitHub runner IPs vary, so network restrictions are a trade-off to decide in Milestone 7.
- **Region:** the project is in Sydney. That's fine for synthetic data. Before live advertising data arrives in Milestone 8, confirm Sydney is acceptable for data residency and latency.
- **History:** restated records are updated in place. Earlier values survive only inside the reports that used them, not as row versions.
- **Experiments:** approving or advancing an experiment has no implemented path yet. The pipeline is blocked from doing it by design, and a reviewer role or CLI is still your decision.
- **CLI version:** CLI v3.1.0 is available. I recommend staying on v2.102.0 for this migration, since a major upgrade could change `db push` behavior.
- **Free plan:** a Free project can pause after inactivity, which would make scheduled runs fail with `database_unavailable`.
- **No live data:** nothing in this milestone touches advertising APIs or live data, and no paid AI API was called.

## 11. Approvals needed

Before applying the migrations, I need explicit approval for each of these:

1. **The schema and access model** as written in the three migration files, or the changes you want.
2. **Who runs `supabase link` and `supabase db push`.** I recommend you run both yourself, so the owner password never enters this session.
3. **Applying the three migrations** to project `sxycykdtgqqyvrlijmlk`. This creates the `growthpulse` and `growthpulse_read` schemas, and the `growthpulse_writer` and `growthpulse_dashboard` roles.
4. **The password method:** `psql \password`, which is recommended, or the SQL editor.
5. **After the passwords are set, a read-only connection check** by me, using the `.env` credentials.
6. **Separately, the first persisted synthetic pipeline run** against Supabase. That is the first write of data.
7. **Committing and pushing Milestone 4**, which triggers the first run of the PostgreSQL 17 CI job. I can commit before or after the migrations, as you prefer.
