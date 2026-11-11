# Final migration review: Milestone 4

**Verdict: ready for your final approval, once the dry run below has been run and checked.** I recommend one more step first, described in section 7. Nothing has been applied to Supabase. No password has been set, the persisted pipeline has not run, and Milestone 4 is not committed. The dry run was **not** performed, because it needs your database password.

## 1. Security findings and corrections

| # | Finding | Severity | Correction | New test |
|---|---|---|---|---|
| 1 | Without a database password, CLI v2.102.0 creates a temporary login role (`cli_login_postgres`) through the Management API, even for `db push --dry-run`. That is a database change | High for the "no changes" rule | Instructions now set `SUPABASE_DB_PASSWORD`, so the CLI connects as `postgres` and creates nothing | Not testable here. Confirmed by reading the CLI source |
| 2 | The revokes for the API roles were in migration 2, but each file commits separately. If the project has global default privileges, objects from migration 1 could have been reachable until migration 2 ran | Medium | Migration 1 now closes its own schema in the same transaction. Migration 3 already did | The integration setup now creates worst-case global defaults for the API roles. Removing any revoke now fails a test |
| 3 | The rollback used `DROP SCHEMA ... CASCADE`, which would silently drop anything outside the schemas that depends on them, such as a view in `public` | Medium | Rewritten to drop every object by name with `RESTRICT`. Any outside dependency aborts the whole transaction and leaves everything in place | A view in `public` blocks the rollback, and nothing is dropped |
| 4 | Integration tests ran migrations as a superuser. Supabase's `postgres` is not one | Low | Migrations and rollback now run as a non-superuser owner with only `CREATEROLE` | Ownership of every object. The owner can enable login, but cannot grant `SUPERUSER`, `BYPASSRLS` or `REPLICATION` |
| 5 | Two unqualified references: `gen_random_uuid()` and `pg_roles` | Low | Now `pg_catalog.gen_random_uuid()` and `pg_catalog.pg_roles` | A static test requires every reference to be qualified with `growthpulse`, `growthpulse_read` or `pg_catalog` |
| 6 | The CI image floated on `postgres:17` | Low | Pinned to `postgres:17.11`, the version Supabase reports for this project. actionlint found no issues | n/a |
| 7 | There was no repeatable way to check the result after applying | n/a | Added [post_migration_checks.sql](supabase/verification/post_migration_checks.sql), 11 SELECT-only queries, each with its expected result | A static test confirms it contains only SELECTs. It runs in a read-only transaction against the test database |

These items were checked and need no change:

- **Roles that later get login:** `NOLOGIN NOINHERIT` roles keep exactly their grants after login is enabled.
- **No self-escalation:** neither app role can create a role, grant itself the other role, `SET ROLE` to the other role or the owner, give itself `BYPASSRLS` or `CREATEROLE`, set the other role's password, or create a schema or a table in `public`. That is 18 cases, all refused.
- **No memberships:** neither app role is a member of any role.
- **Dashboard isolation:** the dashboard has no `USAGE` on `growthpulse`, so it cannot select base tables or raw payloads, and turning read-only off still leaves every write refused.
- **Views:** the views run with the owner's rights, but filter no rows, so leaky functions gain nothing. Both trigger functions pin `search_path = ''`.
- **Future objects:** a table created later without revokes still gets grants from global default privileges. The API roles still cannot reach it, because they have no schema `USAGE`, and a test proves it.

These residual risks are accepted, not fixed:

- **Self-changes:** a login role can change its own password and its own `ALTER ROLE ... SET` defaults, so the dashboard could switch off its read-only default. The grants remain the boundary.
- **PUBLIC privileges:** app roles also get whatever PUBLIC has on this project, such as `CONNECT`, `TEMP` and usage of `public`. Queries 8 and 9 list this after applying.
- **Platform roles:** Supabase platform roles, such as superuser `supabase_admin` and any role with `BYPASSRLS` or `pg_read_all_data`, can read these schemas regardless of grants. Query 10 lists them.

## 2. Compatibility and execution review

| Area | Result | Basis |
|---|---|---|
| `CREATE ROLE`, `ALTER ROLE ... SET`, `CONNECTION LIMIT`, `COMMENT ON ROLE` | Work for a non-superuser with `CREATEROLE`. Postgres 16 and later give the creator `ADMIN` on roles it creates | Tested on PG 16.2 as a non-superuser owner. PG 17 has the same rules, but I haven't run it on 17 |
| RLS, policies, triggers, `security_invoker` views (PG 15+), `gen_random_uuid` (PG 13+) | Standard features on PG 17 | Tested on 16.2 |
| Each migration file and its history row run as one transaction | Yes. The CLI sends a file's statements plus its history insert as one pipelined batch, which Postgres runs as a single implicit transaction | CLI v2.102.0 source: `ExecBatch` |
| Statement splitting of `$$` and `$tag$` bodies | Handled by the CLI's parser | CLI source: `DollarState`, `TagState`. Not executed |
| Order | Files apply in timestamp order. Each depends only on earlier ones | CLI `ListLocalMigrations`, static test |
| Idempotency | Not re-runnable by design: `create schema` and `create role` fail if the object exists, so an unknown role is never adopted. The history table stops the CLI re-running a file | Tests |
| A failure partway | The failing file, and its history row, roll back. Fix it, then push again: only the pending files run | CLI source plus Postgres batch semantics |
| Recovery after a rollback | Run the rollback script as `postgres`, then `supabase migration repair --status reverted 20261111100000 20261111100100 20261111100200`. Don't rename applied files | Rollback and re-apply are tested |
| Ownership if the CLI uses a temporary login role | Still `postgres`. The CLI runs `SET SESSION ROLE postgres` for `cli_login_*` users, and `RESET ALL` doesn't undo a role change | CLI source: `connect.go`. The password route avoids the temporary role entirely |
| Supavisor and custom roles | The documented username is `[ROLE].[PROJECT-REF]` in session mode on port 5432. The docs don't explicitly confirm that a role created with `CREATE ROLE` authenticates | Supabase docs, "Connecting to Postgres" |

The rollback script is destructive, and I have **not** run it against Supabase. It ran only against the throwaway test database.

## 3. Exact files to apply

Apply only these three, in this order, with these checksums:

| File | Lines | SHA-256 |
|---|---|---|
| `supabase/migrations/20261111100000_create_growthpulse_tables.sql` | 320 | `3fc1b26f16c572805386bebb2b4ca28df62008096d4ecc29b9583f5eab318889` |
| `supabase/migrations/20261111100100_create_access_roles.sql` | 119 | `f657b81035723c5ea904ac8104a8e0f730078d4f42364ef68c27dd625fc6de6e` |
| `supabase/migrations/20261111100200_create_dashboard_views.sql` | 213 | `813e29e01aae8009d46c78ad9e5c866a3908339f477c37ca27255cc4430b71d1` |

These are not migrations and are never pushed:

| File | SHA-256 |
|---|---|
| `supabase/rollback/20261111100000_drop_growthpulse.sql`, destructive, only with explicit approval | `b7fd7752887a77c9ecbd37e892babe3437d5d95d3a681d15c8dbe45e927f3b36` |
| `supabase/verification/post_migration_checks.sql`, read-only | `a535b7c8da94aa068c321a36d18577c110e3cd62273f855b835c4f1b8bdaf9a4` |

To check a checksum, run `shasum -a 256 supabase/migrations/*.sql`.

## 4. Remaining unverified assumptions

Each of these needs a live connection or a run I couldn't do.

1. **Supavisor authentication:** that it authenticates `growthpulse_writer.sxycykdtgqqyvrlijmlk` and `growthpulse_dashboard.sxycykdtgqqyvrlijmlk` with SCRAM passwords. The username format is documented, but the behaviour isn't confirmed.
2. **Supabase safeguards:** that Supabase's role safeguards accept `CREATE ROLE`, `ALTER ROLE ... SET` and `CONNECTION LIMIT` from `postgres`. If they don't, the push fails and the failing file rolls back.
3. **PG 17.11:** that the migrations behave the same there as on 16.2. The CI job will show this once it runs.
4. **Default privileges:** whether the project has global default privileges or extra PUBLIC grants. Queries 8, 9 and 11 show them.
5. **Pooler session settings:** whether Supavisor's session pooler resets `SET` state between clients. The app sets read-only on every dashboard connection anyway.
6. **Batching:** that psycopg's batched `executemany` works through the session pooler.
7. **Security Advisor:** whether it flags the owner-rights views or the `using (true)` policies. Both are deliberate.

## 5. Test results

All were executed in this session.

| Check | Result |
|---|---|
| `uv lock --check` | Passed |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 54 files already formatted |
| `pytest`, no database | 566 passed, 69 skipped (the integration tests) |
| `pytest` with a throwaway PostgreSQL 16.2 server | **635 passed** |
| Removal checks | 51 safeguards removed one at a time, all 51 caught. That includes 8 new ones for this review's corrections |
| actionlint on `ci.yml` | No issues |
| Secret and personal-email scan of uncommitted changes | Only the deliberately fake test credentials matched |

There are 34 new tests since the pre-migration report: 5 static and 29 integration. One old static rollback test was replaced.

**The PostgreSQL 17 CI job cannot run without pushing Milestone 4.** Workflows run only on refs pushed to GitHub. Running it locally would need Docker and `act`, and neither is installed.

## 6. Dry run, not performed

Run these yourself, in a separate terminal in the repository folder.

1. **Link.** In v2.102.0 this doesn't connect to the database. It makes Management API reads, including fetching the API keys into memory for version checks, and writes gitignored files under `supabase/.temp`.
   ```bash
   supabase link --project-ref sxycykdtgqqyvrlijmlk
   ```
2. **Dry run, with the password in an environment variable only.** This stops the CLI from creating a temporary login role. Avoid `-p`, which leaves the password in your shell history.
   ```bash
   read -rs SUPABASE_DB_PASSWORD && export SUPABASE_DB_PASSWORD
   test -n "$SUPABASE_DB_PASSWORD" && echo "password is set"   # stop if this prints nothing
   supabase db push --dry-run
   unset SUPABASE_DB_PASSWORD
   ```
   The dry run only reads `supabase_migrations.schema_migrations`, which is treated as empty if it doesn't exist. It returns before creating anything.

   Expect `DRY RUN: migrations will *not* be pushed to the database.`, then `Would push these migrations:` followed by exactly the three files in section 3. If you see `Initialising login role...`, the password wasn't picked up, and the CLI has just created a temporary role. Tell me if that happens.
3. Paste the output here, without the password.

## 7. Recommended order and approvals

1. **Run the dry run**, as in section 6, and share the output.
2. **Approve committing and pushing Milestone 4**, so the PostgreSQL 17.11 CI job runs against these exact files. I recommend this before applying, since it's the only way to test on Postgres 17 first.
3. **After CI passes, give final approval to apply the three files.** You run the push yourself, with the same password-in-environment pattern, using `supabase db push`.
4. **Run the read-only checks** in [post_migration_checks.sql](supabase/verification/post_migration_checks.sql) in the SQL editor and share the results.
5. **Set the passwords**, then approve my read-only connection check, then separately approve the first persisted run.
