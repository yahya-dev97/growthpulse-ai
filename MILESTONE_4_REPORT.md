# Milestone 4 report: Supabase persistence

**Status: functionally complete.** Synthetic pipeline results now persist to Supabase PostgreSQL, idempotently, through least-privilege roles. All results below were observed on 2026-11-11 against project `sxycykdtgqqyvrlijmlk` (PostgreSQL 17.11, Sydney).

The persistence code, migrations and CI job were committed as `f4d1d48`. The verification and credential tooling, the SCRAM hardening and these reports follow in the Milestone 4 finalisation commit.

## 1. What was built

- **Three migrations.** They create:
  - a private `growthpulse` schema with 7 tables;
  - a `growthpulse_read` schema with 9 dashboard views;
  - two roles, the pipeline's `growthpulse_writer` and the dashboard's `growthpulse_dashboard`;
  - RLS on every table;
  - append-only triggers for reports, proposals and status history;
  - a database-enforced experiment status workflow.
- **Persistence code (psycopg 3).** Each run first commits a `running` record. All data is then written in one transaction, so a failure rolls back cleanly and leaves a run marked failed with a fixed error code. Upserts are idempotent. The code refuses the wrong login role, weak TLS, and any authentication other than SCRAM-SHA-256 (`require_auth=scram-sha-256`, added to every app connection).
- **Verification and operations tooling:**
  - a read-only 19-check verification script;
  - `provision_roles`, which sets SCRAM passwords hashed on your Mac and writes a mode-600 `.env`;
  - `connection_check`, a read-only check of both roles;
  - a manual, restrict-only rollback script.
- **CI.** A PostgreSQL 17.11 integration job runs alongside the standard checks.

## 2. Live verification

| Stage | Result |
|---|---|
| Migrations applied | All 3 are recorded in Supabase migration history |
| Post-migration verification, run as owner | All 15 pass/fail checks passed. Objects, definitions, the 58 grants, RLS, ownership and API-role isolation match the tested build exactly |
| Security Advisor and Data API | 0 advisor findings. The Data API exposes no schemas |
| Credential provisioning | Login enabled for both roles, with 40-character random passwords set as SCRAM hashes. `.env` is mode 600 and gitignored. No leftover staging file |
| Connection check | 32 of 32 passed for both roles, through the session pooler over TLS |
| SCRAM | Confirmed for both roles, using `require_auth=scram-sha-256` |
| Role inspection | No elevated attributes, memberships or `CREATE` rights anywhere |

## 3. First persisted synthetic runs

The data source is `synthetic`, and the pipeline uses only the synthetic connector: no advertising-platform client code exists. Before the first run, all 7 tables were empty. Both runs used:

```bash
uv run --offline --frozen python -m growthpulse_ai.pipeline.persisted_run
```

| | Run 1 (`08e1efda…`) | Run 2 (`47f41ad1…`) | Expected |
|---|---|---|---|
| Status | succeeded | succeeded | succeeded |
| Records received / written | 300 / **300** | 300 / **0** | 300 / 300, then 0 |
| Quarantined rows | 0 | 0 | 0 |
| Reports written | **1** | **0** | 1, then 0 |
| Findings received / written | 19 / **19** | 19 / **0** | 19 / 19, then 0 |
| Report ID | `feb1e176…e601` | same | the sample report's ID |

The connector also logged 12 data-quality warnings in each run. These are the synthetic dataset's planted warning cases from Milestone 2, not errors.

**Read-only verification of the stored data: 25 of 25 checks passed.**
- **Counts:** 2 runs, 300 records, 1 report, 19 findings, 0 quarantined rows, 0 proposals, 0 status events.
- **Runs:** both succeeded with no error code and point to the same report. Their recorded counts match the printed output.
- **Records:** no duplicates on the record ID, the full natural key or the campaign-day delivery key. All 300 were written by run 1 and left unchanged by run 2. The stored record IDs, total spend and total conversions equal the source exactly. Dates run from 1 to 30 October, across 10 campaigns.
- **Report:** its hash, recomputed from the stored body, matches its ID, and the stored report is identical to the one built offline from the same code.
- **Findings:** the 19 findings equal the offline set, 5 of them suppressed by the noise guard, all tied to the one report.
- **Dashboard:** reading through the dashboard role and its views returns the same report and both runs, neither flagged as abandoned.

No connection string, password or hash appeared in any output.

## 4. Issues found during Milestone 4

| Issue | Outcome |
|---|---|
| An inspection command I ran created or refreshed Supabase's temporary `cli_login_postgres` role, without a database password set | Reported when it happened. The role has no elevated rights and its password expired at 12:45:59 UTC. It was left alone, as you instructed. Database checks now use psql, or the app roles with read-only transactions |
| Three `alter default privileges` lines in migration 1 have no effect | No security impact. The explicit revokes and schema isolation are what protect the data, and both are verified. No corrective migration needed |
| My first connection-check run hit a bug: `AttributeError` from a TLS method psycopg doesn't have | It stopped before any check ran and printed no secret. Fixed, and a real-connection test was added |
| The definition fingerprints differed when the verification ran as the writer | A `search_path` display effect, not drift: reproduced locally with 16 of 16 identical values. Fingerprint checks apply only when run as `postgres` |

## 5. Limitations

- **Server certificate verification (outstanding, required before production).** The app connections encrypt traffic with `sslmode=require`, but that mode doesn't verify the server's certificate, so it doesn't rule out an active man-in-the-middle. Before production use, set `sslmode=verify-full` with `sslrootcert` pointing to Supabase's CA certificate. The code already accepts it. The pooler also offers no SCRAM channel binding.
- **Authentication.** SCRAM-SHA-256 is now enforced on every app connection. libpq refuses a server that skips authentication or asks for a clear-text or MD5 password. This setting already passed against the Supabase pooler in the live connection check. It needs libpq 16 or newer, which the bundled `psycopg[binary]` provides.
- **Code version.** `code_version` is empty for local runs. It is filled from `GITHUB_SHA` once runs are scheduled in GitHub Actions (Milestone 7).
- **Free plan.** The project can pause when inactive, and scheduled runs would then fail with `database_unavailable`.
- **Experiment approval.** There's no approval path for experiments yet, by design. The pipeline can only propose.

## 6. Final test results

Run locally before the finalisation commit, all offline (no Supabase connection):

| Check | Result |
|---|---|
| Offline test suite | 619 passed, 88 PostgreSQL tests skipped |
| PostgreSQL integration tests, throwaway local PostgreSQL 16.2 | 88 passed |
| Total | 707 tests |
| `ruff check` (including the bandit `S` security rules) | passed |
| `ruff format --check` | passed |
| Secret scan of every committed file | no credentials, hashes, tokens or `.env` values |

The new integration test builds a connection string exactly as the app does and confirms a real PostgreSQL server that skips SCRAM refuses it. GitHub Actions runs both jobs on push, including PostgreSQL 17.11 integration.

## 7. Next steps

1. **Certificate verification** (`sslmode=verify-full` with Supabase's CA certificate), before any production or client data.
2. **Milestone 5, AI insights and experiment recommendations.** As requested, I'll report before starting it.
