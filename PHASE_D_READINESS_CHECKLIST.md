# Phase D readiness checklist: applying Milestone 5 to Supabase

**Status: preparation only.** Nothing in this checklist has been run against Supabase. Each step marked **[you]** is yours to run, and each **[approval]** gate needs your explicit go-ahead before anything continues. I won't connect to Supabase for any of it unless you approve that step.

| | |
|---|---|
| Project | `sxycykdtgqqyvrlijmlk` (PostgreSQL 17.11, ap-southeast-2) |
| State today | Milestone 4: 3 migrations, 2 runs, 300 records, 1 report (`feb1e176…`), 19 findings, 0 quarantined rows, proposals or status events |
| What Phase D applies | Exactly the five approved Milestone 5 migrations, `20261111200000` to `20261111200400` |
| What Phase D does not apply | `supabase/proposed/20261111200500_remove_scope_fallbacks.sql` (decision 4), and the Milestone 7a.2 migrations `20261114100000` to `20261114100300` (gate G4, a separate approval, with its own `G4_MIGRATION_CHECKLIST.md`). `supabase db push` applies everything in `supabase/migrations`, so run Phase D from a checkout without those four files: `main` at `7b86bac`, for example `git worktree add ../growthpulse-phase-d 7b86bac`. Don't merge PR #3 before Phase D is complete |

---

## 0. Preconditions

- [ ] **[approval]** Phase D is approved in writing.
- [ ] Phase C is committed and both CI jobs are green on PostgreSQL 17.11 for that exact commit.
- [ ] `supabase/migrations/` holds exactly 8 files: the three applied Milestone 4 files and the five Milestone 5 files. No file in `supabase/proposed/` or `supabase/rollback/` is in it.
- [ ] Nothing runs the pipeline during the window. It is manual only today, so simply don't run it.
- [ ] You have about an hour, and you stop at the first unexpected result (section 6).

## 1. Backup availability and its limits

You need to confirm this in the dashboard. I can't verify it from here.

- [ ] **[you]** Open **Project → Database → Backups** and note the following:

| Question | Your answer |
|---|---|
| Are automatic daily backups listed for this project? | |
| Is point-in-time recovery (PITR) enabled? | |
| How far back can a restore go? | |
| Does a restore replace the whole project? | |

**What I understand, which you should confirm:**
- The Free plan doesn't include automatic backups.
- PITR is a paid add-on.
- A dashboard restore returns the **whole project** to an earlier time, discarding everything written since.

Until you've confirmed otherwise, treat the platform backup as **unavailable**, and rely on the backup you take yourself in section 2.

## 2. A restorable backup of today's Milestone 4 state

- [ ] **[you]** Install a `pg_dump` matching the server's major version, then check it:
  ```bash
  pg_dump --version
  ```
  It must report 17.x. Version 16 refuses to dump a 17 server. The Homebrew `libpq` or `postgresql@17` package, or the `postgres:17.11` Docker image, provides it.
- [ ] **[you]** Enter the password without echo, so it never reaches the terminal or shell history:
  ```bash
  read -rs PGPASSWORD && export PGPASSWORD
  ```
- [ ] **[you]** Take two dumps through the session pooler as `postgres`, written **outside the repository**, for example `~/growthpulse-backups/2026-11-…/`:
  ```bash
  CONN="host=aws-1-ap-southeast-2.pooler.supabase.com port=5432 dbname=postgres user=postgres.sxycykdtgqqyvrlijmlk sslmode=verify-full sslrootcert=$HOME/.postgresql/supabase-ca.crt"
  pg_dump "$CONN" --format=custom --schema=growthpulse --schema=growthpulse_read \
      --file=m4_growthpulse.dump
  pg_dump "$CONN" --format=custom --table=supabase_migrations.schema_migrations \
      --file=m4_migration_history.dump
  unset PGPASSWORD
  ```
  If you haven't downloaded Supabase's CA certificate yet, `sslmode=require` works, but it doesn't verify the server's certificate.
- [ ] **[you]** Record checksums:
  ```bash
  shasum -a 256 *.dump > SHA256SUMS
  ```
  Keep the dumps on an encrypted disk (FileVault). They contain only synthetic data, but treat them as sensitive anyway.

**Limits of this backup:**
- It covers the two GrowthPulse schemas and the migration history only.
- Roles aren't dumped, because Supabase doesn't let `postgres` run `pg_dumpall --roles-only`. The roles and their grants are recreated by migration 2. Their passwords stay with Supabase.

## 3. Restore test on a disposable database

This shows the backup is usable, and rehearses the whole upgrade on a copy.

- [ ] **[you]** Start a throwaway PostgreSQL 17.11, for example `docker run --rm -e POSTGRES_HOST_AUTH_METHOD=trust -p 55432:5432 postgres:17.11`. Never point any of the following at Supabase.
- [ ] **[you]** Create the roles the dump refers to, without login:
  - `growthpulse_writer` and `growthpulse_dashboard` with `nologin noinherit`;
  - `anon`, `authenticated` and `service_role` with `nologin`.
- [ ] **[you]** Restore the dump, then run `supabase/verification/milestone_4_fingerprints.sql` on it. **It must match section 4's output exactly.**
  ```bash
  pg_restore --no-owner --dbname "postgresql://postgres@localhost:55432/postgres" m4_growthpulse.dump
  ```
- [ ] **[you]** Rehearse the upgrade on the copy:
  1. Apply the five Milestone 5 files with psql, one transaction each.
  2. Run `milestone_5_checks.sql`. All checks should pass, except that the owner shows as the local user.
  3. Run the fingerprint script again. It must still match.
  4. Run `supabase/rollback/20261111200000_revert_milestone_5.sql`.
  5. Run the fingerprint script again. It must still match.
- [ ] Throw the container away.

**Already proven in CI and locally:**
- the upgrade from the exact Milestone 4 state;
- refusal of unexpected data;
- the revert;
- fingerprint stability across the upgrade, a Phase D-style run and the revert.

This step proves the same things on a copy of **your** data.

## 4. Pre-migration fingerprints on Supabase

These are read-only and run as `postgres` with psql, as in Milestone 4.

- [ ] **[you]** `supabase/verification/post_migration_checks.sql`, the Milestone 4 state check. Every check must pass, as it did on 2026-11-11.
- [ ] **[you]** `supabase/verification/milestone_4_fingerprints.sql`. Save the output to the gitignored `tmp/` folder.
- [ ] Expected:
  - `02 ad_performance_records` 300;
  - `04 analytics_reports` 1;
  - `05 findings` 19;
  - `01 pipeline_runs` 2;
  - `03 quarantined_records`, `06 experiment_proposals` and `07 experiment_status_events` at 0;
  - `08 report id` `feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601`.

  These must equal the restored copy in section 3.

## 5. Apply the five approved migrations, and only those

- [ ] **[approval]** Go-ahead to apply.
- [ ] **[you]** Apply them the way you did in Milestone 4.
  - Running `supabase db push` with `SUPABASE_DB_PASSWORD` set avoids the temporary `cli_login_postgres` role, which a push without it creates or refreshes.
  - Run `--dry-run` first. It must list **exactly** the five `202611112000xx`–`202611112004xx` versions, and nothing from `proposed/`. If it also lists any `2026111410xxxx` version, stop: the checkout includes Milestone 7a.2.
- [ ] Each file applies in its own transaction. A refusal in migration 5, for example on unexpected data, rolls that file back completely, and leaves only migration 4's empty tables (section 7, case A).

## 6. Verification, and when to stop

Run straight after applying, all read-only:

- [ ] `supabase/verification/milestone_5_checks.sql`: all 11 checks `ok = true`. These include the 8 versions in history, the 105-object inventory, the 110-entry grant manifest, the new roles without login, the backfill showing 210 Google and 90 Meta records, report `feb1e176…` and 19 findings.
- [ ] `milestone_4_fingerprints.sql`: **identical** to section 4.
- [ ] `connection_check` for the writer and dashboard: every check passes. The updated version expects the Phase B schema.
- [ ] **[approval]** One synthetic run: `persisted_run`.
  - **Expected:** two account syncs, each receiving 210 or 90 records and writing 0; one client analysis writing 0 reports, 0 findings and 1 breakdown, with report `feb1e176…`.
  - **Then:** the fingerprints are identical to section 4, except `01 pipeline_runs`, which goes from 2 to 5.

**Stop immediately, change nothing more, and report if:**
- the dry run lists anything other than the five versions;
- any migration fails;
- any `milestone_5_checks.sql` row is `ok = false`;
- any fingerprint differs, other than the run count after the synthetic run;
- `connection_check` fails, or either app role can reach `growthpulse_secrets` or an owner action;
- the synthetic run writes any record, report or finding, or returns a different report ID;
- anything at all is unexpected.

## 7. Recovery options and their limits

| Case | Recovery | Limits |
|---|---|---|
| **A.** A migration fails while applying | Nothing to do for that file: its transaction rolled back. Earlier files stay applied. If migration 5 refused data, only migration 4's empty tables remain. Investigate, fix the cause with approval, then push again, which resumes at the failed file | The history shows a partial state until the push completes |
| **B.** Applied, data intact, but a check fails or something looks wrong | Run `supabase/rollback/20261111200000_revert_milestone_5.sql` as `postgres` (restrict-only, one transaction), then `supabase migration repair --status reverted 20261111200000 20261111200100 20261111200200 20261111200300 20261111200400`. This returns the exact Milestone 4 schema and keeps every Milestone 4 row, which is tested | Deletes all Milestone 5 rows: clients, accounts, sync state, breakdowns, the owner log and credentials (there are none yet). Runs made after the upgrade keep their rows but lose their Milestone 5 columns. Tested locally and in CI, and in section 3 on your copy, but not yet on Supabase |
| **C.** Milestone 4 data damaged | Restore the schemas from section 2 into a reverted database. Rehearse it on a disposable copy first. Then rerun section 4 and compare | The most involved path. The full drop script also drops the app roles, which would remove their logins and passwords and require re-provisioning, so prefer case B, then a data-only restore. As `postgres` (not a superuser) on Supabase, ownership and grants must be recreated by the migrations, not the dump |
| **D.** Dashboard backup or PITR | Only if section 1 confirms it's available | Restores the **whole project** to an earlier point and discards everything after it, outside GrowthPulse too. Likely unavailable on the Free plan; confirm |

**Still a prerequisite before any real client data:** TLS `verify-full` for the application's connections.
