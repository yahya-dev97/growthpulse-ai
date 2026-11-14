# G4 checklist: applying Milestone 7a.2 (migrations 9 to 12) to Supabase

**Status: preparation only.** Nothing in this checklist has been run against Supabase. Each step marked **[you]** is yours to run, and each **[approval]** gate needs your explicit go-ahead before anything continues. Run it only after Phase D (G3) has been completed and verified, using `PHASE_D_READINESS_CHECKLIST.md`.

| | |
|---|---|
| Project | `sxycykdtgqqyvrlijmlk` (PostgreSQL 17.11, ap-southeast-2) |
| State before G4 | Milestone 5, as Phase D leaves it: 8 migrations; `milestone_5_checks.sql` all `ok`; report `feb1e176…`; 19 findings; 5 pipeline runs if Phase D's synthetic run was made |
| What G4 applies | Exactly the four Milestone 7a.2 migrations, `20261114100000` to `20261114100300`, from the commit approved at G2 |
| What G4 does not apply | Anything in `supabase/proposed/`, or any later migration. No user, membership, session or credential is created |
| Effect on existing data | None changes. One organization row is added (the internal one), and a unique constraint is added on `clients (client_id, workspace_id)`. Afterwards every `growthpulse_read` view returns **no rows** without a signed-in request. That's intended, but it means anything reading those views with the dashboard role sees nothing |

---

## 0. Preconditions

- [ ] **[approval]** G2 final approval, naming the exact commit.
- [ ] **[approval]** G4 approved in writing.
- [ ] G3 is complete: every Phase D section 6 check passed, and the Phase D fingerprints are saved.
- [ ] Both CI jobs are green on PostgreSQL 17.11 for the approved commit.
- [ ] Your checkout is at that commit:
  ```bash
  git rev-parse HEAD                 # must equal the approved commit
  git status --porcelain             # must print nothing
  ls supabase/migrations | wc -l     # must be 12
  ls supabase/migrations | tail -4   # exactly the four 2026111410xxxx files
  shasum -a 256 supabase/migrations/2026111410*.sql   # record these in the approval note
  ```
- [ ] Nothing runs the pipeline during the window.
- [ ] Nothing outside this repository reads the `growthpulse_read` views on Supabase. If something does, it goes blank after G4.

## 1. A restorable backup of the Milestone 5 state

As in Phase D section 2. Use `pg_dump` 17, enter the password with `read -rs`, and write outside the repository:

- [ ] **[you]**
  ```bash
  pg_dump "$CONN" --format=custom --schema=growthpulse --schema=growthpulse_read \
      --schema=growthpulse_secrets --file=m5_growthpulse.dump
  pg_dump "$CONN" --format=custom --table=supabase_migrations.schema_migrations \
      --file=m5_migration_history.dump
  shasum -a 256 *.dump > SHA256SUMS
  ```
- [ ] **[you]** Confirm `growthpulse_secrets.oauth_credentials` holds 0 rows. Count only, never `select *`. G4 must happen before any real credential exists.

`growthpulse_auth` doesn't exist yet, so this dump can't contain the context signing key. After G4, any dump that covers `growthpulse_auth` does. See `docs/M7A_TENANT_SECURITY.md`, "The signing key".

## 2. Rehearsal on a disposable copy

- [ ] **[you]** Start a throwaway `postgres:17.11`. Never point any of the following at Supabase.
- [ ] **[you]** Create, without login:
  - `growthpulse_writer`, `growthpulse_dashboard`, `growthpulse_manager` and `growthpulse_token_manager` (`nologin noinherit`);
  - `anon`, `authenticated` and `service_role` (`nologin`).
- [ ] **[you]** Restore `m5_growthpulse.dump`, then run `milestone_4_fingerprints.sql`. It must match Phase D's final output.
- [ ] **[you]** Rehearse:
  1. Apply the four files with psql, one transaction each, in order.
  2. Run `supabase/verification/m7a_checks.sql`. Every row must be `ok = true`, except check `01 migration history`, which can't pass on a copy without `supabase_migrations`.
  3. Run the fingerprint script again. It must still match.
  4. Run `supabase/rollback/20261114100000_revert_m7a2_tenant_security.sql`. No identity rows exist, so it needs no override.
  5. Run the fingerprints and `milestone_5_checks.sql` again. Both must match the pre-G4 state.
- [ ] Throw the container away.

CI already proves all of this on the frozen Milestone 4 data: `tests/test_postgres_tenancy.py` and `tests/test_postgres_m7a_checks.py`. This step proves it on a copy of **your** data.

## 3. Pre-migration checks on Supabase (read-only)

- [ ] **[you]** `milestone_5_checks.sql`: all `ok = true`.
- [ ] **[you]** `milestone_4_fingerprints.sql`: saved to the gitignored `tmp/` folder. It must equal Phase D's final output.

## 4. Data API exposure

- [ ] **[you]** In the Supabase dashboard, open **Project Settings → Data API → Exposed schemas**. It must not list `growthpulse`, `growthpulse_read`, `growthpulse_secrets` or `growthpulse_auth`. The session functions are unverified, and must never be reachable through the API.
- [ ] Don't give `growthpulse_authenticator` a login or a password. Nothing may sign in as it until the M7b sign-in service exists.

## 5. Apply exactly the four approved migrations

- [ ] **[approval]** Go-ahead to apply.
- [ ] **[you]** `supabase db push --dry-run` must list **exactly** `20261114100000`, `20261114100100`, `20261114100200` and `20261114100300`, and nothing else. If it lists anything else, or anything from `proposed/`, stop.
- [ ] **[you]** `supabase db push`, with `SUPABASE_DB_PASSWORD` set, as in Phase D.
- [ ] Each file applies in its own transaction. Migration 9 refuses to run if the internal workspace holds a live client, and rolls back completely.

## 6. Verification, and when to stop

Run straight after applying.

- [ ] **[you]** `supabase/verification/m7a_checks.sql` (read-only): **every row `ok = true`**. It checks:
  - the 12 versions in history;
  - all 18 views fail closed, returning 0 rows even for `postgres` without a request context;
  - every view is a security barrier;
  - no context, and a signing key present (not read);
  - the authenticator has no login, and the session functions are executable by it alone;
  - no function is open to PUBLIC or API roles;
  - no role can touch identity tables directly;
  - the 30 writer policies, none `using (true)`;
  - the Data API setting;
  - one internal organization and no identity rows.
- [ ] **[you]** `milestone_4_fingerprints.sql`: **identical** to step 3.
- [ ] **[you]** `connection_check` for the writer and dashboard: every check passes. The dashboard's views now return no rows, which is expected.
- [ ] **[you]** **Transaction-pooler isolation**, live and read-only, through Supavisor's transaction mode (port 6543) as `postgres`:
  ```bash
  for i in $(seq 30); do psql "$POOLER_6543" -X -A -t -F ' ' -f supabase/verification/m7a_pooler_check.sql; done > tmp/pooler.txt
  grep -c ' f$' tmp/pooler.txt                                    # must print 0
  awk '$1=="backend" {print $2}' tmp/pooler.txt | sort | uniq -d | head   # must print at least one PID
  ```
  The first count shows no context survived a commit. The second shows that server connections really were reused. If it prints nothing, run more iterations. The script writes nothing. It uses a job context, which exists only inside its own read-only transaction.
- [ ] **[approval]** One synthetic run (`persisted_run`), with the code at the approved commit.
  - **Expected:** the same results as Phase D's synthetic run. The fingerprints are identical except `01 pipeline_runs`, which grows by 3.

**Stop immediately, change nothing more, and report if:**
- the dry run lists anything other than the four versions;
- any migration fails;
- any `m7a_checks.sql` row is `ok = false`;
- any fingerprint differs, other than the run count;
- `connection_check` fails;
- any pooler line ends in `f`;
- the synthetic run fails or returns a different report ID;
- anything at all is unexpected.

## 7. Remove the pre-migration fallback, immediately after G4

The pipeline code skips `begin_job` on a database without migration 10. That kept Phase D working. After G4 it has no purpose, and it would turn a broken installation into quiet empty reads.

- [ ] **[approval]** A small change, reviewed and CI-green, that:
  - deletes `context_installed` and `CONTEXT_INSTALLED` (`persistence/context.py`), and `SELECT_DUE_ACCOUNTS_M5` with its branch (`persistence/tenancy.py`);
  - makes `begin_job` always call the database;
  - removes the test that runs the pipeline on a Milestone 5-only database, and the docstring check in `tests/test_m7a_static.py`.
- [ ] Merge it before any further pipeline run on Supabase. No M7b work starts until it's merged.

## 8. Recovery options and their limits

| Case | Recovery | Limits |
|---|---|---|
| **A.** A migration fails while applying | Nothing to do for that file: its transaction rolled back. Earlier files stay applied. Investigate, fix the cause with approval, then push again, which resumes at the failed file | The history shows a partial state until the push completes. A partial state is no less safe than Milestone 5: until migration 12 the writer policies and views are Milestone 5's, and from migration 11 the token functions refuse without a context |
| **B.** Applied, data intact, but a check fails | Run `supabase/rollback/20261114100000_revert_m7a2_tenant_security.sql` as `postgres` (one transaction), then `supabase migration repair --status reverted 20261114100000 20261114100100 20261114100200 20261114100300`. This returns the exact Milestone 5 schema, which is tested in CI | Deletes all identity rows, of which there are none at G4. It also restores the Milestone 5 access model, where every app role sees every row. That's acceptable only while one organization exists |
| **C.** Data damaged | Restore from step 1 into a reverted database, rehearsed first on a disposable copy | As in Phase D section 7, case C |

**Still prerequisites before any real client data:** TLS `verify-full` for the application's connections, and the mandatory items in `docs/M7A_TENANT_SECURITY.md`, section 12.
