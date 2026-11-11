# Post-migration verification: Milestone 4

**Update, role connections verified (section 9):** both application roles authenticate through the Supabase session pooler with SCRAM-SHA-256 over TLS. They have exactly their intended access, and every security check passed.

**Status: verified.** You ran the read-only script from your own terminal on 2026-11-11 at 12:55 UTC. It completed all 19 checks and ended with `ROLLBACK`. All 15 pass/fail checks **PASS**, and the 4 informational checks show nothing that needs action. The deployed schema, grants, roles and RLS match what was tested, object for object.

The two remaining platform checks were then automated through read-only Management API calls (section 0a):

- The Security Advisor reports **0 findings**.
- The Data API exposes **no schemas**.

No database command was run for either check. The database is ready for credentials. What remains is to set the role passwords and run a read-only connection test, both prepared and waiting for your approval (section 5). Nothing is committed.

## 0a. Automated platform checks

**The method, verified before use.** The Supabase CLI v2.102.0 source shows that its Security Advisor feature calls `GET /v1/projects/{ref}/advisors/security`, and that `supabase link` reads the Data API config with `GET /v1/projects/{ref}/postgrest`. Both are plain HTTP GETs in the generated API client. I didn't use the CLI to make these calls, because every `supabase db ...` command first runs the step that refreshes the temporary login role. Instead, a small script in the session's scratchpad, outside the repository, did the following:

- It read the CLI's existing access token from the macOS keychain (service `Supabase CLI`, account `supabase`) into memory only. The token was never printed, logged or written to disk.
- It sent exactly two `GET` requests to `https://api.supabase.com`, for this project only, with redirects disabled.
- It kept only an allow-list of fields from each response. The PostgREST config response can contain the project's `jwt_secret`, which was discarded unread.

No MCP server for Supabase is connected in this session. I didn't use a third-party connector, so nothing went to another service.

| Check | Result | Evidence |
|---|---|---|
| Security Advisor | **PASS: 0 findings** | `GET .../advisors/security` returned HTTP 200 with `lints: []`. The owner-rights views and `using (true)` policies weren't flagged |
| Data API exposed schemas | **PASS: none exposed** | `GET .../postgrest` returned HTTP 200 with `db_schema: ""`, `db_extra_search_path: "public, extensions"` and `max_rows: 1000`. An empty `db_schema` means the Data API serves no schema at all, so neither GrowthPulse schema nor `public` is reachable through it. This is consistent with the Data API being switched off, and is the most restrictive setting |

The access token is a personal token with account-wide rights. Only read endpoints were called, but anything holding it could also change projects. That makes it a credential to guard like the database password.

## 0. Verified results

| # | Check | Result | Evidence from the run |
|---|---|---|---|
| 01 | Migration history | **PASS** | All three versions recorded under their file names. 3 versions in total, so nothing else is in history |
| 02 | Schemas | **PASS** | `growthpulse` and `growthpulse_read` exist, both owned by `postgres` |
| 03 | Object inventory | **PASS** | 7 tables, 9 views, 16 indexes, 1 sequence, 2 functions, 0 standalone types. None missing, none extra |
| 04 | Ownership | **PASS** | Every schema, relation and function is owned by `postgres` |
| 05 | Tables | **PASS** | For each of the 7 tables, the column, constraint, index, trigger and policy counts match, RLS is on and not forced, and the definition fingerprint is identical to the tested build |
| 06 | Views | **PASS** | All 9 have `security_invoker=false` and identical definition fingerprints |
| 07 | Functions | **PASS** | Both are not `SECURITY DEFINER`, have `search_path=""`, and identical fingerprints |
| 08 | Roles | **PASS** | Both roles have no superuser, `CREATEROLE`, `CREATEDB`, `BYPASSRLS`, replication or inherit. Both have `login=f`. Connection limits are 3 and 5, and the session defaults are exact. Neither is a member of any role. `postgres` holds `admin=t inherit=f set=f` on each, which is the normal creator grant and doesn't give `postgres` their privileges |
| 09 | Grants | **PASS** | 58 of 58 expected entries. None missing, none unexpected. This covers the writer's table and column grants, the dashboard's view grants and both schemas' `USAGE` |
| 10 | Supabase API roles | **PASS** | `anon`, `authenticated` and `service_role` have no `USAGE` on either schema and reach 0 objects |
| 11 | Schema default privileges | **PASS** | No entries in either schema |
| 12 | Outside dependencies | **PASS** | None, so the reviewed rollback would not be blocked |
| 13 | Data API exposure in the database | **PASS** | `pgrst.db_schemas` isn't set in the database. The exposed-schema list lives in the platform's API settings, so it still needs a dashboard check (section 6) |
| 14 | Row counts | **PASS** | All 7 tables are empty, so the pipeline hasn't run |
| 15 | CLI login roles | Informational, no action | One role, `cli_login_postgres`, with `login=t`, no superuser, `BYPASSRLS` or `CREATEROLE`, and membership in `postgres`. It was valid until 12:45:59 UTC and had already expired when you ran the script at 12:55 UTC. See section 3 |
| 16 | App roles outside their schemas | **PASS** | Neither role can create objects in any schema or write any table outside `growthpulse`. The schemas each role can use are its own, plus `public`, `pg_catalog` and `information_schema` through PUBLIC. Neither can use `extensions` |
| 17 | Platform readers | Informational | `supabase_admin` is a superuser. `postgres`, `supabase_etl_admin` and `supabase_read_only_user` have `BYPASSRLS` and `pg_read_all_data`. `service_role` has `BYPASSRLS` but no schema access (check 10). See section 3 |
| 18 | Global default privileges | Informational | 0 rows. The project has no global defaults, so new objects grant nothing automatically. Worst-case defaults were simulated in testing, so the tests were stricter than reality |
| 19 | Event triggers | Informational | Six Supabase-owned triggers: `pgrst_ddl_watch`, `pgrst_drop_watch`, `issue_graphql_placeholder`, and `issue_pg_graphql_access`, `issue_pg_net_access`, `issue_pg_cron_access`. None was created by these migrations. They react to extension and schema changes, and none grants anything on GrowthPulse objects |

There are no missing objects, no unexpected grants, no ownership problems and no extra privileges on any GrowthPulse object. The results file contains no credentials: I scanned it for password, secret, key and connection-string patterns, and found 0 matches.

## 1. Run the verification

The script is [post_migration_checks.sql](supabase/verification/post_migration_checks.sql): 649 lines, SHA-256 `e22c8042823d90f61f14f29ff578949f8cfaa26de0c97806446f05c9a96b95d9`.

**Why it can't change anything:**
- It opens with `begin transaction read only` and ends with `rollback`, so the server refuses any write.
- Every other statement is a `SELECT`. Tests enforce this, and also forbid side-effect functions such as `set_config` and `nextval`.
- It never reads password hashes or `pg_authid`, and the only setting value it reads is `pgrst.db_schemas`.

Run these in Terminal, in the repository folder:

```bash
cd /Users/mac/Developer/growthpulse-ai
shasum -a 256 supabase/verification/post_migration_checks.sql   # must print e22c8042...
unset PGPASSWORD
mkdir -p tmp
/opt/homebrew/opt/libpq/bin/psql -X -W \
  --set=ON_ERROR_STOP=1 --pset=pager=off \
  "host=aws-1-ap-southeast-2.pooler.supabase.com port=5432 dbname=postgres user=postgres.sxycykdtgqqyvrlijmlk sslmode=require application_name=growthpulse-verification" \
  -f supabase/verification/post_migration_checks.sql \
  -o tmp/post_migration_verification.txt
```

**What each part does:**
- `-W` prompts for the database password. Type it at the prompt only, so it never appears in the command, your shell history or psql's history.
- `-X` skips any `~/.psqlrc`, so only this script runs.
- `ON_ERROR_STOP` halts on the first error, leaving the transaction rolled back.
- The pooler host comes from `supabase/.temp/pooler-url`, written by `supabase link`. The username follows Supabase's documented `[ROLE].[PROJECT-REF]` format.
- Results go to `tmp/post_migration_verification.txt`, which `.gitignore` excludes.

**What to share:**
- When it finishes, tell me. I'll read that file, which needs no database access. Or paste its contents here.
- The output has no credentials: it contains catalog names, counts, fingerprints and true/false results.
- If psql stops with an error, paste the error line. psql never prints your password.
- Never paste the password, a full connection string with a password, or anything from `.env`.

**Security warnings:** open the dashboard at **Advisors → Security Advisor** and copy the findings listed there. That route doesn't use the CLI. Don't run `supabase db advisors --linked` or `supabase db query --linked` without `SUPABASE_DB_PASSWORD` set: the CLI's setup step would refresh the temporary login role again.

## 2. Database checks

Each row of the script reports `expected`, `actual` and `ok`: true, false, or empty for informational rows. Before handing it over, I ran the script itself with psql against a throwaway local PostgreSQL 16.2 database migrated by a non-superuser owner. The last column shows that result.

| # | Covers | Expected on Supabase | Supabase | Local run of the script |
|---|---|---|---|---|
| 01 | Migration history | Exactly the three `20261111…` versions | See section 0 | PASS, against a stand-in history table |
| 02 | Schemas | Both exist, owned by `postgres` | See section 0 | Differs as expected: the test owner isn't `postgres` |
| 03 | Inventory: 7 tables, 9 views, 16 indexes, 1 sequence, 2 functions | Nothing missing, nothing extra | See section 0 | PASS |
| 04 | Ownership of every object | `postgres` only | See section 0 | Differs as expected, as in 02 |
| 05 | Per table: columns, constraints, indexes, triggers, policies, RLS on, not forced | Counts and fingerprint match | See section 0 | PASS |
| 06 | Views | `security_invoker=false`, definitions unchanged | See section 0 | PASS |
| 07 | Trigger functions | Not `SECURITY DEFINER`, `search_path=""`, unchanged | See section 0 | PASS |
| 08 | App roles | No elevated attributes, `login=f`, limits 3 and 5, session defaults, members of nothing | See section 0 | PASS |
| 09 | Exact grant manifest | 58 entries: writer 48, dashboard 10. None missing, none extra | See section 0 | PASS |
| 10 | `anon`, `authenticated`, `service_role` | No schema `USAGE`, 0 objects reachable | See section 0 | PASS |
| 11 | Default privileges inside the schemas | Grant nothing to anyone else | See section 0 | PASS |
| 12 | Outside dependencies on GrowthPulse objects | None | See section 0 | PASS |
| 13 | Data API exposure (`pgrst.db_schemas`) | Doesn't mention GrowthPulse | See section 0 | PASS (not set locally) |
| 14 | Row counts in all 7 tables | 0, since the pipeline hasn't run | See section 0 | PASS |
| 15 | `cli_login_%` roles | Informational | See section 0 | No such role locally |
| 16 | App roles outside the schemas | Can't create objects anywhere, can't write outside `growthpulse`, schema list shown | See section 0 | PASS |
| 17 | Superuser, `BYPASSRLS` and `pg_read_all_data` roles | Informational | See section 0 | Informational |
| 18 | Global default privileges | Informational | See section 0 | Informational |
| 19 | Event triggers | Informational, none from these migrations | See section 0 | Informational |
| Advisors | Supabase Security Advisor | No GrowthPulse finding, or only the deliberate items in section 3 | **PASS, 0 findings (section 0a)** | n/a |

**How these map to your list:**
- Tables with columns and constraints: 03 and 05.
- Views: 03 and 06.
- Roles: 08.
- Ownership: 02 and 04.
- RLS: 05.
- API roles: 09 and 10.
- Writer and dashboard limits: 09 and 16.
- Migration history: 01.
- Unexpected objects, privileges or dependencies: 03, 07, 09, 11 and 12.
- CLI login roles: 15.
- Security warnings: Advisors.

**About the fingerprints.** They hash each object's full definition, as printed by Postgres, and owners are excluded. They were computed on 16.2. If a row differs but its counts match, it may be Postgres 17 formatting rather than a real change. I'd give you a follow-up read-only query that shows the definitions before treating it as a failure.

**Already verified, independent of your results:**
- The migration files are byte-identical to the approved ones. The three checksums are unchanged from FINAL_MIGRATION_REVIEW.md.
- The same files passed 69 integration tests on PostgreSQL **17.11** in CI run 43020438034.
- The project database is PostgreSQL 17.11. My earlier probe showed this inside a read-only transaction.

**New local tests:**

| Run | Result |
|---|---|
| `pytest`, no database | 567 passed, 71 skipped |
| `pytest`, throwaway PostgreSQL 16.2 | **638 passed** |
| `ruff check`, `ruff format --check`, `uv lock --check` | Passed |

The tests:
- run the whole script on the tested schema;
- prove it runs inside a read-only transaction;
- prove it catches drift: an extra grant to `anon` and an extra table both make it fail;
- statically forbid writes and side-effect functions.

## 3. Security findings and risks

| # | Finding | Status |
|---|---|---|
| 1 | **The temporary CLI login role.** My `supabase db query --linked` call made the Management API create or refresh `cli_login_postgres`. Check 15 shows it: `login=t`, no superuser, `BYPASSRLS` or `CREATEROLE`, member of `postgres`, and an expiry of 12:45:59 UTC, which had passed before your 12:55 UTC run. | **No action needed. Leave it alone.** Details below the table |
| 2 | **No-op statements in migration 1.** Its three `alter default privileges in schema growthpulse revoke ... from public` lines do nothing. Postgres can't revoke a global or built-in default per schema, and check 11 confirms no entries on Supabase. Check 18 shows the project has no global defaults, and check 09 shows nothing was granted beyond the 58 expected entries. | **No corrective migration needed.** Details below the table |
| 3 | **Inherited PUBLIC access.** Login roles get whatever PUBLIC has. Check 16 shows the app roles can use `public`, `pg_catalog` and `information_schema`, but cannot create objects anywhere, cannot write outside `growthpulse`, and cannot use `extensions`. | **Verified, acceptable.** If tables are ever added to `public`, grant them to named roles only, never PUBLIC, or they would become readable by both app roles |
| 4 | **Platform roles.** Check 17 shows `supabase_admin` (superuser), plus `postgres`, `supabase_etl_admin` and `supabase_read_only_user`, which have `BYPASSRLS` and `pg_read_all_data`. They can read every row, including synthetic raw quarantine payloads, whatever the grants say. Their credentials are platform-managed, apart from `postgres`, which is yours. | **Inherent to a managed service, acceptable for synthetic data.** It confirms the multi-account plan: OAuth tokens must be encrypted with a key held outside the database, so reading the rows reveals nothing usable |
| 5 | **Self-changes.** A login role can change its own password and its own session defaults, for example turning off the dashboard's read-only default. | Accepted. Grants are the boundary, and tests prove writes are still refused |
| 6 | **TLS without certificate checks.** `sslmode=require` encrypts but doesn't verify the server certificate. | Recommended later: `verify-full` with Supabase's CA certificate (section 5) |
| 7 | **Security Advisor.** It could have flagged the owner-rights views or the `using (true)` writer policies. | **Verified: 0 findings** (section 0a). No change needed |
| 8 | **Data API.** An exposed schema would put tables behind the public API keys. | **Verified: no schema exposed** (section 0a). If you ever turn the Data API on, keep `growthpulse` and `growthpulse_read` out of the list. The revokes in check 10 would still block the API roles, but one layer would be gone |

### Should `cli_login_postgres` be left alone?

Yes.

- **It's Supabase's own role.** The Management API creates it for the CLI, and the CLI connects as it only after running `SET SESSION ROLE postgres`.
- **It can't be used now.** Its password is generated by the platform and never shown to anyone, and that password expired at 12:45:59 UTC. Postgres refuses password logins after `valid_until`.
- **Using it again requires your access token.** It only becomes usable again if something with your Supabase access token asks the Management API to refresh it.
- **Dropping it buys nothing.** The next CLI command without a database password would simply recreate it.
- **The way to keep it dormant:** always set `SUPABASE_DB_PASSWORD` before any `supabase db ...` command, or use psql as you just did.
- **If you ever want it removed,** do that as its own reviewed step, with nothing else changing at the same time.

### Is a corrective migration needed for the default-privileges finding?

No.

- **The lines do nothing.** The three statements change nothing, either for good or for harm.
- **The real protection is in place.** It comes from:
  - explicit revokes in the same transaction;
  - no schema `USAGE` for the API roles, verified in check 10;
  - the exact grant manifest, verified in check 09.
- **Nothing gets granted automatically.** With no global default privileges (check 18), new objects grant nothing on their own.
- **A corrective migration would only touch a comment.** Applied migrations should stay unchanged, so it isn't worth one.

The rule for future migrations: every migration that creates objects in `growthpulse` or `growthpulse_read` must revoke from PUBLIC and the API roles explicitly, in the same file, and must never grant schema `USAGE` to an API role. `test_each_migration_closes_the_schemas_it_creates` enforces this only for files that create a schema. Extending it to any file that creates tables, views or functions is a small change for the next migration.

## 4. Supabase compatibility findings

- **Verified from CLI v2.102.0 source:**
  - `db push` applies each file and its history row as one implicit transaction.
  - `--dry-run` only reads the history.
  - A `cli_login_*` session runs `SET SESSION ROLE postgres`, so migrated objects are owned by `postgres`.
- **Correction:** I had concluded that `db query --linked` and `db advisors --linked` avoid a database login. They don't: a shared setup step runs before every `db` command, and without `SUPABASE_DB_PASSWORD` it initialises the temporary login role. That setup step is `ParseDatabaseConfig`, called from the root command's `PersistentPreRunE`.
- **Management API queries** run as `postgres`. My probe confirmed `read_only=on` inside the transaction.
- **Documented:** custom roles connect through the shared pooler as `[ROLE].[PROJECT-REF]` in session mode on port 5432. Not yet tested with a live login.
- **Pooler host:** the linked project's pooler is `aws-1-ap-southeast-2.pooler.supabase.com`, from `supabase/.temp/pooler-url`.

## 5. Connection readiness

| Question | Assessment |
|---|---|
| Session pooler with custom roles | **Partly verified.** Your verification run logged in through the session pooler, `aws-1-ap-southeast-2…:5432` with user `postgres.sxycykdtgqqyvrlijmlk` and TLS, so the host, port, username format and TLS all work for the owner role. The two custom roles are still **unverified**: both are `login=f` with no password (check 08), so neither can authenticate yet |
| Backend and dashboard configuration | Shown below |
| Password setup with `psql \password` | Ready. `psql` 18.3 is installed at `/opt/homebrew/opt/libpq/bin/psql`. Steps below |
| `.env.example` | Has `DATABASE_URL` and `DASHBOARD_DATABASE_URL` with the pooler shape and the role rules. Missing: an example of the `verify-full` options. `GROWTHPULSE_TEST_DATABASE_URL` is test-only, documented in the README, and belongs only in CI or a throwaway shell |
| Login role and TLS enforced by the application | **Yes**, tested. Details below |
| Ready for a read-only connection test | After the steps below, and your approval of the test |

**What the application enforces.** These are covered by the 16 connection tests and the integration tests:
- `connect_backend` refuses any login role other than `growthpulse_writer`, and `connect_dashboard` any other than `growthpulse_dashboard`.
- The dashboard never falls back to `DATABASE_URL`.
- `sslmode=require` is added when missing, and `disable`, `allow` and `prefer` are refused.
- Driver errors are reduced to their type, so no message text is kept.
- No test reads your real `.env` to connect anywhere, and the integration tests refuse Supabase hosts.

**Configuration** for your local `.env` now, and later for GitHub and Streamlit secrets:

```text
DATABASE_URL=postgresql://growthpulse_writer.sxycykdtgqqyvrlijmlk:WRITER_PASSWORD@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
DASHBOARD_DATABASE_URL=postgresql://growthpulse_dashboard.sxycykdtgqqyvrlijmlk:DASHBOARD_PASSWORD@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
```

- The upgrade to certificate checking is to replace `sslmode=require` with `sslmode=verify-full&sslrootcert=/path/to/supabase-ca.crt`. The certificate is under **Project Settings → Database → SSL Configuration**. I haven't confirmed the pooler serves a certificate signed by that CA, so test it once before relying on it.

### Recommended password setup

This is prepared and tested, but **not run**. It needs your approval because it changes two role settings, which is a database change. Nothing else changes.

It's automated by [provision_roles.py](src/growthpulse_ai/persistence/provision_roles.py), so you type one command and the owner password, and never see or handle the new passwords:

```bash
cd /Users/mac/Developer/growthpulse-ai
uv run --offline --frozen python -m growthpulse_ai.persistence.provision_roles \
  --project-ref sxycykdtgqqyvrlijmlk \
  --pooler-host aws-1-ap-southeast-2.pooler.supabase.com
```

**What it does, in order:**
1. **Prompts once** for the `postgres` password, without echo. It's used for this one TLS connection through the session pooler and never stored. The command refuses to continue if the connection isn't using TLS.
2. **Checks, read-only, before changing anything:**
   - that it's logged in as `postgres`;
   - that `password_encryption` is `scram-sha-256`;
   - that both roles exist with no elevated attribute and can't log in yet;
   - that `.env` holds no database URLs yet.

   If any check fails, it stops with nothing changed.
3. **Generates two passwords:** 40 random letters and digits each, different from each other.
4. **Hashes each password on your Mac** with SCRAM-SHA-256, through libpq. It sends `ALTER ROLE <role> WITH LOGIN PASSWORD '<hash>'`, so login and password are set together and the plaintext never reaches Supabase, its logs, or any history.
5. **Stages the new `.env`** (mode 600, from your `.env` or `.env.example`) while the transaction is still open, then commits. If the commit fails, the staged file is deleted and neither role has changed.
6. **Moves `.env` into place** and prints only the role names. It never prints a password or connection string.

**How it's tested:** with removal checks and a throwaway PostgreSQL server.
- An integration test records the exact SQL sent and confirms that it contains only SCRAM hashes, never a password.
- The stored hashes match what was sent, and `.env` is mode 600 with the correct URLs.
- Other tests prove it refuses without SCRAM, refuses an elevated role, refuses a non-owner, refuses existing logins, and leaves no role changed and no file behind when writing or committing fails.

**Afterwards:**
- Re-running the section 1 verification should change exactly two rows: the check 08 attribute rows show `login=t`, so they read `ok = f`. That's the expected difference.
- Run the connection test below.

**Rotation:** `--rotate` replaces both passwords and `.env` entries in one run. If a password ever leaks, the fastest block is `alter role <role> nologin;` from psql, then a `--rotate` run.

### Prepared connection test (read-only)

```bash
uv run --offline --frozen python -m growthpulse_ai.persistence.connection_check
```

[connection_check.py](src/growthpulse_ai/persistence/connection_check.py) uses the `.env` URLs and the normal application connection path, which refuses the wrong role and weak TLS.

**What it checks:**
- **TLS, on your Mac's side:** TLS is in use, at version 1.2 or newer.
- **Writer:** it logs in as `growthpulse_writer`, can use `growthpulse` but not `growthpulse_read`, and can read all 7 tables. It holds the `INSERT` privilege on runs (checked, not used), and can't delete records or update reports.
- **Dashboard:** it logs in as `growthpulse_dashboard`, its session defaults to read-only, it can't use `growthpulse` and is refused on a base table, and it can read all 9 views.
- **It writes nothing:** every query runs in a `READ ONLY` transaction that always rolls back.

It prints only `PASS` or `FAIL` and a check name per line: 2 TLS checks and 14 writer checks for the writer, then 2 TLS checks and 14 dashboard checks for the dashboard. Integration tests show it passes on the tested schema and catches privilege drift, such as a `DELETE` grant to the writer or the dashboard reaching a base table.

It needs the provisioning step first, and your approval to connect.

## 6. Remaining blockers

Resolved by the verification run:
- All 19 checks ran.
- No fingerprint differences: the deployed definitions match the tested build exactly.
- The temporary CLI role's state is known: expired, and no action is needed.
- The pooler route works for the owner role.

Resolved through the automated platform checks (section 0a):
- Security Advisor: 0 findings.
- Data API: no schema exposed.

Resolved by provisioning and the live connection verification (section 9):
- Credentials are set.
- Both roles log in through the pooler with SCRAM over TLS.

Still open, needing your approval:

1. **First persisted synthetic run:** run it, then repeat it to confirm idempotency. This is the first write of data.

## 9. Live connection verification

You provisioned the two roles. On 2026-11-11 I then ran, with your authorization:
- `connection_check`;
- a supplementary script from the session scratchpad.

Both used only the `.env` credentials, through the app's own connection path. Every query ran in a `READ ONLY` transaction that rolled back. No Supabase CLI was involved, and nothing was printed except check names and catalog results.

**`.env`:**
- It is mode 600 and gitignored.
- It holds exactly one URL per role, each using the pooler username, host `aws-1-ap-southeast-2.pooler.supabase.com`, port 5432 and `sslmode=require`.
- The other settings were kept.
- `.env.provisioning` is absent, so no cleanup is needed.

| Area | Result |
|---|---|
| `connection_check` | **32 of 32 PASS.** Both roles: TLS in use, client requires TLS 1.2 or newer, logged in as the expected role. Writer: uses only `growthpulse`, reads all 7 tables, holds `INSERT` (not exercised), cannot delete records or update reports, cannot use `growthpulse_read`. Dashboard: session read-only by default, cannot use `growthpulse`, refused on a base table, reads all 9 views. Both test transactions stayed read-only |
| SCRAM | **Confirmed for both roles.** Connections with `require_auth=scram-sha-256` succeeded over TLS, and libpq refuses that setting with any other method. Channel binding (`SCRAM-SHA-256-PLUS`) is **not** offered by the pooler |
| Elevation, memberships, `CREATE` | **PASS for both.** No superuser, `CREATEROLE`, `CREATEDB`, `BYPASSRLS` or replication. `NOINHERIT`, connection limits 3 and 5, member of no role, no `CREATE` on any schema or on the database. `TEMP` is inherited from PUBLIC and is informational: session-only, invisible to other sessions |
| Re-run of the 19-check verification, as the writer | **Every grant, API-role, ownership, dependency, row-count and access check passed.** Check 08 shows `login=t` for both roles, the expected change, with every other attribute unchanged. `cli_login_postgres` is unchanged and still expired. Check 01 (migration history) was skipped, because the writer cannot read `supabase_migrations`; nothing has been migrated since the owner's run confirmed it |
| Fingerprints in checks 05 and 06 | **Measurement artifact, no drift.** All 16 differed from the owner-run values while every count matched. Postgres prints names without their schema when the schema is on the session's `search_path`, and the writer's sessions default to `search_path = growthpulse`. On a throwaway local server, the same migrations viewed as the writer gave **16 of 16 identical** fingerprints to Supabase's. So the deployed definitions equal the tested build under both views. This needed no further Supabase access. The fingerprint checks are only meaningful when the script runs as `postgres`, as section 1 instructs |

**A bug fixed during the run.** The first `connection_check` attempt stopped with an `AttributeError` before any check had run. It printed no secret, and it had already confirmed the writer's login. The TLS check called `ssl_attribute`, which psycopg doesn't provide; the unit-test fake had it, which hid the gap.

The check now reads the client's enforced minimum TLS version instead, libpq's `ssl_min_protocol_version`, and reads no other connection option. The fake now mirrors the real API, a test asserts that, and an integration test runs the TLS check on a real connection. With those changes, 697 tests pass against a throwaway server.

**Limitations and recommendations:**
- **No certificate verification.** With `sslmode=require` the server's certificate isn't checked, and the pooler offers no channel binding. A network attacker able to impersonate the pooler could relay a login. Moving the app connections to `sslmode=verify-full` with Supabase's CA certificate closes this. So would adding `require_auth=scram-sha-256` to `secure_conninfo`, which is now proven to work with the pooler.
- **TLS version:** the negotiated version isn't observable through psycopg. The check confirms the client refuses anything below TLS 1.2.

## 7. Future multi-account architecture

These are recorded decisions for later milestones. Nothing here is implemented in Milestone 4.

**Product scope:**
- Single user: only the owner logs in.
- The owner creates client or brand workspaces, and each can hold several Meta Ads and Google Ads accounts.
- Accounts are connected through user-facing OAuth, then discovered and selected for sync. They can be disconnected, reauthorised, enabled and disabled.
- Integrations are read-only, and syncing runs off the MacBook.
- No client logins, team permissions or billing.

The current design extends without breaking changes. Every change below adds something, and the existing synthetic path becomes a default "Synthetic demo" workspace.

**Database (new migrations, all additive):**
- **`workspaces`:** ID, name, created and archived timestamps.
- **`ad_platform_connections`:** one row per OAuth grant. It holds the provider, the external user ID, granted scopes, status (`active`, `needs_reauth`, `revoked`, `disconnected`), last refresh, and a reference to an encrypted token. It never holds a token in plain text.
- **`ad_accounts`:** platform, external account ID, workspace, connection, currency, timezone, `sync_enabled`, discovery and last-sync timestamps, and whether it's a manager account. `unique (platform, external_account_id)` makes an account belong to one workspace and be synced once, even when several connections can reach it. That is the main protection against duplicate spend.
- **Per-account run detail:** a child table of `pipeline_runs` (`pipeline_run_accounts`), giving each account its own status and fixed error code. One failing account must not hide the others.
- **Workspace columns:** a nullable `workspace_id` on `analytics_reports` and `findings`, later backfilled. `ad_performance_records` keeps its current identity, which already includes platform and account. Its workspace comes through `ad_accounts`, so moving an account between workspaces never rewrites record IDs.
- **Token storage:**
  - Tokens go in a separate table, or Supabase Vault, encrypted with a key held outside the database, for example a GitHub Actions secret used for application-level AES-GCM.
  - That storage gets its own role, `growthpulse_connector`. The writer, the dashboard and the API roles never get access.
  - Tokens never appear in logs, reports or error codes.

**Does the two-role setup fit many clients and ad accounts?** Yes for data separation, with one gap to plan for.

- **Data roles stay global.** One `growthpulse_writer` for every sync and one `growthpulse_dashboard` for the owner are right for a single-user platform. Client separation belongs in the data, through workspace and account keys, unique account ownership and per-workspace reports, not in a database role per client. Per-client roles would be needed only if clients ever log in themselves. That would be a different design, using Supabase Auth with RLS policies keyed on workspace membership.
- **Nothing in the current grants blocks the plan.** New tables get their own explicit grants. The writer's column-level `UPDATE` grants and the dashboard's view-only access carry over.
- **Gap 1, owner actions need a write path.** Connecting, disconnecting, enabling or disabling an account, and approving experiments, are owner writes, but the dashboard is read-only by design. Recommended: a separate narrow control-plane role, such as `growthpulse_control`, used only server-side. It would get `INSERT` and `UPDATE` on the connection and account-selection tables and on experiment status events. It would get nothing on performance data or reports, and it would never be the dashboard's credential. The alternative is a few validated database functions exposed to that role alone.
- **Gap 2, tokens need their own role.** The `growthpulse_connector` role from the token storage plan above. Neither existing role should ever be able to read tokens.
- **Limits to revisit.** The writer's connection limit of 3 and its 120 s statement timeout suit one sequential sync. Raise them deliberately if accounts sync in parallel.
- **The Data API stays off.** None of this needs it, and keeping it off (section 0a) removes a whole access path.

**Connectors and sync:**
- **OAuth callbacks** need a small server-side endpoint at a registered HTTPS redirect URL. Candidates are a Supabase Edge Function or a minimal Python service. Streamlit alone isn't a safe place to exchange codes for tokens.
- **Google Ads:**
  - It has no read-only OAuth scope, so read-only behaviour is enforced in code. Only the reporting endpoints are used, and no mutate services are ever called.
  - Sync only non-manager client accounts, and pass `login_customer_id` per account. Syncing manager (MCC) accounts would double-count their children.
- **Meta:**
  - Request `ads_read` only.
  - Accounts outside your own Business may need App Review for advanced access.
  - De-duplicate by the `act_` account ID across businesses.
- **Scheduling:** GitHub Actions cron, planned for Milestone 7. It holds only the writer and connector credentials and the token encryption key, never tokens themselves.

**Analytics:**
- **Scope:** reports run per workspace, on the records of its enabled accounts.
- **Separation:** the existing rules already keep currencies, attribution settings and conversion definitions apart. Workspace roll-ups must group by currency and never convert or sum across currencies.
- **Time zones:** daily totals across time zones don't share date boundaries. Keep per-account days, and label any cross-account daily view.
- **Disconnected accounts:** they keep their history, but stop syncing and are flagged in reports, so missing days aren't mistaken for zero spend.
- **Owner access:** the deployed dashboard needs owner-only authentication before any real data is loaded. That belongs to Milestone 6 or 8.

## 8. Recommended next steps to finish Milestone 4

Done:
- The database verification: every check passed (section 0).
- The Security Advisor and Data API checks, automated: 0 findings and no schema exposed (section 0a).
- The credential setup and connection test, prepared and tested.

Remaining, in order:

1. **You:** run the provisioning command (section 5) and enter the `postgres` password at its prompt. That is your only manual step, and it needs your approval because it changes the two roles.
2. **Me, with your approval:** run the read-only connection test, and re-run the verification script from your terminal or by reading its output file. In the verification, only the two `login` rows should change.
3. **Me, with separate approval:** the first persisted synthetic pipeline run, then a second run to confirm idempotency, with 0 records, 0 reports and 0 findings written.
4. **Me, with your approval:** commit the verification work and close Milestone 4 with its final report.
