# Milestone 7a.2: tenant security foundation

Status: implemented and tested on disposable PostgreSQL 17 servers only (locally, and in CI on 17.11). Nothing here has been applied to Supabase. Gate G2 was a conditional pass. The G2 follow-up in section 12 resolves the bounded findings, and lists what is still mandatory before the first real OAuth test.

This document implements the M7a.1 Commercial Tenancy Design, with the owner's six amendments. Where this document and M7a.1 differ, this document is the current decision.

## 1. What this phase contains

| In this phase | Deferred, by amendment 5 or to M7b |
|---|---|
| Organizations, keyed 1:1 on the existing workspaces | Invitations and email matching |
| Users (Supabase Auth subject ID only), memberships, client grants | Billing |
| Sessions, stored as SHA-256 hashes | Customer AI keys (BYOK) |
| Signed, transaction-local request and job context | OAuth state, PKCE, authorization links |
| Tenant checks in every writer policy, dashboard view, token function and owner action | Live Google and Meta connectors |
| Audit events with the acting user's ID | The revocation workflow in section 6 (designed, not built) |
| A revert script, and four new migrations (9 to 12) | Retention, purge and organization deletion |

| # | Migration | Creates or changes |
|---|---|---|
| 9 | `20261114100000_create_identity_tables.sql` | Schema `growthpulse_auth`: organizations, users, memberships, client grants, sessions, audit events. It also adds the rules that an organization always keeps one owner and that the internal organization holds synthetic clients only. Every new workspace becomes a customer organization. |
| 10 | `20261114100100_create_request_context.sql` | Role `growthpulse_authenticator` (NOLOGIN). The context key, `begin_request`, `begin_job`, the context readers, the session functions and the dispatcher's due-accounts function. |
| 11 | `20261114100200_harden_definer_functions.sql` | Authorization and organization checks in the five token functions, and in `log_owner_action`, which all eleven owner actions call. Audit events. |
| 12 | `20261114100300_tenant_policies.sql` | Thirty writer policies now require a job context. All 17 dashboard views are tenant-filtered security barriers. New view: `audit_events`. |

No existing table, column, row, foreign key or report body changes. The only additive change to an existing table is one unique constraint on `clients (client_id, workspace_id)`, which client grants reference.

## 2. Organization = workspace

- Organizations are keyed 1:1 on `growthpulse.workspaces.workspace_id`. The fictional workspace from migration 5 becomes the one internal organization. Migration 9 refuses to run if that workspace holds a live client.
- A trigger creates a customer organization for every new workspace. Another trigger refuses a client whose workspace is not an organization, and refuses a live client in the internal organization.
- Report IDs are the SHA-256 of the report's own JSON, which contains no organization, client or membership data. The tests regenerate the frozen Milestone 4 store and check that report `feb1e176…` and the Milestone 4 fingerprints are unchanged, both after the upgrade and after both reverts.

## 3. Trust model: authentication to database

### The chain

1. A person signs in with Supabase Auth. The M7b sign-in service verifies the login token (JWT): signature, issuer, audience and expiry. **This step does not exist yet.**
2. Only then does the sign-in service, connected as `growthpulse_authenticator`, call `create_session(user_id, sha256(token), expires_at)`. The token is 32 random bytes the service generates and sends to the browser as a secure, HttpOnly cookie. The database stores only its hash. A session lasts at most 12 hours.

**Session creation is unverified.** The database cannot check step 1. `create_session` creates a session for whatever active user ID its caller names. Until the M7b sign-in service verifies the login token first, nothing may call it:
- EXECUTE is granted to `growthpulse_authenticator` only, which has no login.
- No application code calls it. A CI test fails if any source file names it or the role.
- `growthpulse_auth` must never be an exposed schema of the Supabase Data API. The G4 checks verify the database setting, and the checklist has you confirm the dashboard setting too.
- Every creation is audited as `session.create_unverified`, and every revocation as `session.revoke`. Both are recorded against the user, with the actor `authenticator`, and with no token, hash or organization. A session belongs to a person, not an organization, so these events don't appear in any organization's audit view. The event name stays "unverified" until M7b verifies the login token.
3. Every request runs in one transaction. Its first statement is `growthpulse_auth.begin_request(token, workspace_id, request_id)`. The workspace comes from the URL path, never from the request body.
4. `begin_request` hashes the token and requires all of these: a live, unrevoked session; an active user; an active membership of that organization; and an active organization. Every refusal returns the same error (42501), so the error doesn't reveal which check failed or whether the organization exists.
5. It then writes the context as two transaction-local settings:
   - `growthpulse.context`: kind, user, organization, optional client, role, membership, request or job ID, and, for a job, its scope kind and scope ID.
   - `growthpulse.context_mac`: an HMAC-SHA-256 over the context, this backend's process ID and this transaction's start time. The key is 256 bits, generated inside the database by migration 10. It lives in `growthpulse_auth.context_keys`, which no role except the owner can read.
6. Every policy, view and function reads the context only through `growthpulse_auth.current_context()`, which re-verifies the MAC. It returns nothing when there is no context. It raises when a context is present but doesn't verify. The readers also re-check the membership on every statement, so a removal takes effect at once.

Jobs use `begin_job(scope_kind, scope_id, job_id)` instead.
- The scope can be the whole organization (`workspace` or `connection`) or one client (`client`, `ad_account` or `run`).
- The pipeline scopes each transaction to the client it is about to touch, using the client ID it already holds, so callers don't change.
- The organization must be active.

### What each kind of compromise can do

| Who or what is compromised | Can it read or change another organization's data? |
|---|---|
| A bug or SQL injection in a dashboard or manager query | No. It can't forge a context (no key), can't replay one (bound to backend and transaction), can't call `create_session`, and can't read `sessions` or `context_keys`. It stays inside the signed-in person's own scope. |
| The dashboard or manager database password alone | No. It needs a live session token, and the database holds only hashes. |
| A stolen session cookie | Yes, but only that person's access, for at most 12 hours or until revoked. This is the normal web-session risk. |
| The authenticator credential, or the process holding it | Yes. It can create a session for any active user ID, which is full impersonation in every organization. This is the main residual risk. Every creation is audited, but the audit can't tell a forged sign-in from a real one. To reduce it, the M7b sign-in service alone holds this credential and verifies the login token first. Verifying the login token inside the database is a later option, and whether it works with Supabase's current signing keys is unknown. |
| The pipeline's writer credential | Yes, by design. Scheduled jobs serve every organization, and a job declares its own scope. The scope stops bugs from crossing organizations (a wrong ID touches nothing). It doesn't stop a malicious writer. The writer can't call any token function. |
| The token manager credential | Yes, for stored ciphertext in every organization. See "The shared token-manager trust boundary" below. |
| A database dump or backup, plus any application credential | Yes. The dump holds the signing key, which forges any context. See "The signing key" below. |
| The database owner or a Supabase administrator | Everything. Not addressed by RLS. |

### The shared token-manager trust boundary

One token-manager credential serves every organization, because the refresh worker must refresh every organization's tokens. The database therefore limits what each context may do, not who may begin one:

| Context | Credential operations allowed |
|---|---|
| None | None. Every token function raises 42501. |
| Signed-in request | Only in the person's own organization, and only with the permission: store (`connection.create`), read and the rotation listing (`connection.read`), delete and mark for reauthorization (`connection.revoke`). Owners and admins hold these. Viewers and analysts are refused, whatever their client grants. |
| Job scoped to the organization (`workspace`) | Every operation, for that organization's connections. This is the refresh and rotation worker's path. |
| Job scoped to one connection (`connection`) | Read, store, delete and mark, for that connection only. The rotation listing is refused. |
| Job scoped to a client, account or run | None. These are pipeline scopes, never credential scopes. |

What this does and doesn't protect:
- A bug can't cross organizations: a wrong connection ID in a job or request behaves exactly like an unknown connection.
- A person can't exceed their role.
- But the token manager may begin a job for **any** organization. So whoever holds its credential can read every organization's ciphertext, one job at a time.
- The ciphertext alone is not a token. Decrypting it needs the encryption key, which lives outside the database. Each ciphertext is bound by AES-GCM associated data to its connection and token type, so it can't be swapped between connections. A host holding both the credential and the key can read every organization's tokens.
- **M7b (before a second customer organization):** allow token-manager jobs only for a connection with a claimed, unexpired refresh or revocation lease. Consider separate encryption keys per organization.

### The signing key: rotation and backup exposure

- The context signing key is a single row in `growthpulse_auth.context_keys`, generated inside the database by migration 10. It's in no file. Only the owner role can read it.
- **Backups include it.** Any `pg_dump` that covers `growthpulse_auth`, and any Supabase backup or point-in-time restore, contains the key. Someone holding the key and any application credential with `growthpulse_auth` usage can sign a request context for any organization without a session. Treat such a dump like the owner password. For routine dumps, use `--exclude-table-data=growthpulse_auth.context_keys`.
- **A missing key fails closed.** Every `begin_request` and `begin_job` then raises ("The context key is missing"), and every reader refuses. A restore without the key row therefore needs a rotation before anything works.
- **Rotation** replaces the row, as the owner, in one transaction. It's the same generation code as migration 10:
  ```sql
  begin;
  delete from growthpulse_auth.context_keys;
  -- then the DO block from migration 10 that inserts key_id 1
  commit;
  ```
  Nothing persisted depends on the key, because contexts last one transaction. Rotating only makes transactions in flight fail their next read and roll back, which is safe to retry. Rotate after any restore, after any suspected exposure of a dump or the owner credential, and when an administrator leaves. A rotation function and a schedule are M7b work, not built yet.

### SECURITY DEFINER abuse

- Every SECURITY DEFINER function sets `search_path = ''` and qualifies every name.
- None is executable by PUBLIC or a Supabase API role. CI checks this in the catalog.
- Internal functions are granted to no role: `context_mac`, `set_context`, `current_context`, `target_workspace`, `action_permission`, `role_permits`, `credential_permits`, `require_credential_permission` and `log_owner_action`.
- **One context per transaction is a guard against application mistakes, not a security boundary.** A second `begin_request` or `begin_job` is refused while a context is present. But any role can clear the two settings itself and then begin another context in the same transaction. That gains nothing an attacker couldn't get from a second transaction, because each context still needs its own live session or its own EXECUTE grant. The boundary is the signed context's contents, not how many there are.
- PostgreSQL checks EXECUTE on the functions a view calls against the person querying the view. So the dashboard role is granted the read filters, and nothing that maps an arbitrary ID to its organization.

### Transaction pooling

- Context is set with `set_config(..., true)` only. It ends at commit or rollback, so a pooled server connection never carries it into the next client's transaction.
- A session-level setting can't carry a context either: in a later transaction the MAC no longer verifies, and the read raises.
- A test checks that application code never issues a session-level `SET` or `set_config(..., false)`.
- The application connects in autocommit mode with prepared statements off (`persistence/connection.py`), so every `connection.transaction()` block is a real BEGIN and COMMIT. Inside an already-open transaction it would only be a savepoint, and the context would outlive the block. Any new connection code must keep autocommit on.
- Tested by reusing one server connection across transactions. Not yet tested through Supabase's own transaction pooler (Supavisor, port 6543). The G4 checklist does that, without writing anything.

## 4. Isolation coverage

| Object | Enforcement |
|---|---|
| 15 writer-visible tables (30 policies) | Each policy requires a job context. Rows are limited to the job's organization, or to its client. Without a context the writer sees and writes nothing. |
| 18 dashboard views | Security barrier views. They're filtered to the person's clients (owners and admins see every client; analysts and viewers see their granted clients). Organization-level views (connections, discovered accounts, owner actions, audit events) are for owners and admins only. Without a context, every view returns no rows, even for a superuser. |
| 5 token functions | Each first requires a permitting context (see "The shared token-manager trust boundary"), and refuses with 42501 otherwise. It then acts only on a connection of the context's organization. Another organization's connection behaves exactly like an unknown one. Key rotation listings are per organization. Store, delete and mark for reauthorization are audited. |
| 11 owner actions | `log_owner_action` runs last in each action's transaction. It requires a request context in the target's organization, plus the action's permission, and otherwise rolls back the whole action. A test checks that every function granted to `growthpulse_manager` calls it. |
| `growthpulse_auth` tables | No app role can read or write them directly. |
| The dispatcher | `due_account_syncs(now)` is the one cross-organization read. It returns account IDs only, and each sync then runs in its own client's scope. |

**Upgrade compatibility.**
- `context.begin_job` does nothing on a database that doesn't have migration 10 yet. That is the Milestone 5 state that Phase D creates on Supabase.
- This weakens nothing, because Milestone 5 writer policies admit every row anyway.
- It keeps the Phase D order (apply Milestone 5, run the synthetic command) working with this code.
- Once migration 10 exists, the call is always made. On a database with migration 12, a skipped call would see and write nothing, so the check can't weaken isolation. But it would turn a broken installation into quiet empty reads rather than an error.
- **It must be removed immediately after G4.** That's step 7 of `G4_MIGRATION_CHECKLIST.md`: one small change that deletes `context_installed`, `CONTEXT_INSTALLED` and `SELECT_DUE_ACCOUNTS_M5`. After it, a missing `begin_job` raises.

## 5. SECURITY DEFINER review (M7a.1 findings)

| # | Finding | Status |
|---|---|---|
| 1 | Token functions never checked the organization | Fixed (migration 11) |
| 2 | Views bypassed RLS | Fixed: tenant filter in each view, plus security barrier (migration 12) |
| 3 | `store_credential` activates a connection before discovery proves it | Deferred to M7b, together with the OAuth flow that adds `activate_connection` |
| 4 | Deleting credentials leaves the connection active | Deferred to M7b: replaced by the revocation workflow in section 6 |
| 5 | No default-privilege protection in `growthpulse_secrets` | Changed approach. PostgreSQL can't revoke a global or built-in default per schema, so a per-schema statement would do nothing. Instead, each migration revokes before it commits, and a CI catalog test fails if any function in the four schemas is executable by PUBLIC or an API role. |
| 6 | Definer functions owned by the migration role | Deferred. A dedicated owner role would need its own RLS policies or BYPASSRLS, which Supabase doesn't let migrations grant. Low to medium risk. |
| 7 | Writer and dashboard policies were `using (true)` | Fixed (migration 12) |
| 8 | No tenant context existed | Fixed (migration 10), with a signed context rather than the plain settings M7a.1 proposed |
| 9 | Owner actions couldn't be attributed to a person | Fixed. Audit events carry the user ID. `owner_action_log` is kept for compatibility. |
| 10 | The rotation listing was cross-tenant | Fixed: per organization |

## 6. Recoverable OAuth revocation (amendment 4, design only)

The Milestone 5 `revoke_connection` deletes the ciphertext at once. After that, a failed call to the provider can't be retried, because the token is gone. The replacement, built in M7b with the live connectors:

1. **`begin_revocation(connection_id)`** runs as a request in the connection's organization and needs `connection.revoke`. In one transaction it:
   - sets the connection to `revoked`, so `read_credentials` returns nothing and no sync can use it;
   - pauses or archives its accounts (`onboarding` can't move to `paused`, so those are archived);
   - moves the ciphertext into `growthpulse_secrets.pending_revocations`;
   - creates a `connection_revocations` row with status `pending`;
   - writes an audit event.
2. **The revocation worker** calls `claim_revocation(revocation_id)`, its only way to read the held ciphertext. It decrypts in memory, calls the provider, and records the outcome with `record_revocation_attempt(revocation_id, outcome, error_code)`. Error codes are fixed, never provider text.
3. **Outcomes:**
   - `revoked`: delete the held ciphertext.
   - `already_invalid`: the provider says the token no longer works; treat it as done.
   - `failed`: retry with backoff (for example 5 attempts over 24 hours).
   - After the last attempt, or after 7 days, the held ciphertext is deleted anyway. The status becomes `manual_action_required`, and the owners and admins are shown how to remove the grant from the provider's own account settings.
4. Every attempt is kept: count, time, error code and next attempt, so failures are visible and auditable.

Platform facts here are **not verified** for this phase and must be checked against official documentation before M7b encodes them:
- Google's token revocation endpoint and its responses for an already-revoked token;
- Meta's permission-removal call and its errors;
- whether revoking one Google refresh token revokes the user's other grants to the same client.

## 7. One advertising account in multiple organizations (amendment 3)

Not enabled in the pilot. The global identity constraint `ad_accounts (data_source, platform, external_account_id)` from migration 4 is unchanged, and a test confirms a second organization can't add the same account.

Nothing added in this phase keys on the platform account ID: scoping goes through `ad_account_id`, `client_id` and `connection_id`. Enabling it later means:
- replacing that constraint with one per organization;
- deciding whether two organizations' connections may sync the same account, which doubles API quota use, or share one sync;
- adding a test that records of one organization's copy never appear in the other's.

## 8. Platform assumptions (amendment 6)

This phase encodes no Google- or Meta-specific behavior. The M7a.1 platform facts (read from official documentation on 2026-11-13) remain the input for M7b. Among them:
- Google retired developer tokens on 9 September 2026.
- Explorer access allows 2,880 operations a day on production accounts.
- Refresh tokens expire after 7 days while the consent screen is in Testing.
- Meta needs advanced access and Business Verification for accounts of other businesses.

These are still **unknown**:
- whether the `adwords` scope is classified as sensitive;
- Meta system-user token lifetimes;
- whether standard Meta `ads_read` access covers accounts shared with the developer's business;
- Supabase backup and point-in-time-recovery coverage on the current plan;
- the revocation details in section 6.

`config.py` still lists `GOOGLE_ADS_DEVELOPER_TOKEN` as required for `DATA_SOURCE=live`. Removing it is an M7b change, made together with the Google connector.

## 9. Migrations, rollback and warnings

- `supabase/rollback/20261114100000_revert_m7a2_tenant_security.sql` restores the exact Milestone 5 text of every replaced policy, view and function, then drops everything migrations 9 to 12 created. It runs in one transaction, with explicit drops and no CASCADE. CI checks that the catalog afterwards is identical to the one taken before the upgrade, and that the data is kept.
- **Destructive.** The revert deletes every user, membership, client grant, session and audit event. It refuses when any exist unless the operator first runs `set growthpulse.allow_identity_data_loss = 'yes';` in the same session. Take a dump first.
- **Security downgrade.** After a revert, every app role sees every organization again. Never revert with more than one organization present.
- `supabase/rollback/20261111100000_drop_growthpulse.sql` (drop everything) now covers migrations 9 to 12.
- The proposed fallback-removal migration (`supabase/proposed/20261111200500…`) now sorts before migrations 9 to 12. If it's ever approved, it must be renamed to the next free number before it moves.
- On Supabase, apply 9 to 12 only after Phase D, with a custom-format `pg_dump` of the GrowthPulse schemas first and the fingerprints checked after (gate G4).

## 10. Unresolved risks

1. Session creation is unverified (section 3). Whoever holds the authenticator credential can impersonate any user. It's audited, but not prevented. Mitigation: no login, no caller, authenticator only. Required before the first real OAuth test: the M7b sign-in service verifies the login token and alone holds this credential.
2. Job contexts are self-declared. They stop bugs from crossing organizations, but not a malicious pipeline or token-manager credential. See "The shared token-manager trust boundary" in section 3. Required before a second customer organization: lease-bound token-manager jobs.
3. The owner and Supabase administrators are unrestricted, and definer functions are owned by the migration role (finding 6).
4. Some owner actions report "not found" before the organization check runs, so a caller can learn whether a random UUID exists in another organization. Low risk, because UUIDs are unguessable. A later refactor can move the check first.
5. Findings 3 and 4 remain until M7b: credentials activate a connection before discovery, and Milestone 5 revocation is not recoverable.
6. The context key is readable by the owner and present in backups, where with any application credential it can forge a context. See "The signing key" in section 3 for the exposure, the rotation procedure and why a missing key fails closed. A rotation function is M7b work.
7. Replay protection relies on the backend process ID plus the transaction start time in microseconds. Two transactions on one backend starting within the same microsecond would share a binding. That's not reachable through a network round trip, but it is theoretical.
8. Quarantined rows from legacy combined runs carry no account. If two organizations' combined runs ever produced the same quarantine ID, the second write would be refused and its run would fail. It fails closed. Only the legacy combined path is affected.
9. Pooling was tested by reusing one server connection across transactions, not through Supabase's own pooler. G4 step 6 checks it live, read-only.
10. Policy helpers run once per statement. Performance hasn't been measured at multi-organization scale.
11. `provision_roles.py` doesn't yet provision a login for `growthpulse_authenticator`. None is needed until the API exists.

## 11. Requirements for the first real Google OAuth test

The test (gate G6): you connect **your own** Google Ads account with read-only access, discovery lists it, one day of data syncs, and you disconnect. No client account.

1. **Approvals, in order:**
   - G2: this phase reviewed.
   - G3: Phase D applied on Supabase, with backups confirmed.
   - G4: migrations 9 to 12 applied on Supabase, with a dump before and fingerprints after.
   - G5: approval to create the Google Cloud project and OAuth client.
2. **Code (M7b, not started):**
   - OAuth state with PKCE (S256) and a single-use, hashed, 10-minute state;
   - the read-only Google connector, with an allowlist of `GoogleAdsService.SearchStream`/`Search` and `CustomerService.ListAccessibleCustomers`;
   - discovery, then a separate `activate_connection` (finding 3);
   - the recoverable revocation workflow (section 6);
   - removing `GOOGLE_ADS_DEVELOPER_TOKEN` from the required live settings;
   - the sign-in service: login-token verification before `create_session`, the session cookie, and the authenticator credential held by that service only (section 12, item A);
   - the pre-migration fallback removed (G4 step 7).
3. **Google side:**
   - a Cloud project with the Google Ads API enabled;
   - an OAuth consent screen. External in Testing gives 7-day refresh tokens, which is acceptable for a one-day test.
   - a web OAuth client with one exact HTTPS redirect URI;
   - Explorer access requested from the project's Google Ads API page;
   - your Google account set up for passkey sign-in, which generating a refresh token now requires;
   - read access to your own Google Ads account.
4. **Secrets:**
   - `TOKEN_ENCRYPTION_KEYS` and `TOKEN_ENCRYPTION_ACTIVE_VERSION`, generated on your machine and kept in the host's secret store, never in the repository or chat;
   - logins and passwords for the roles involved, set by you.
5. **Identity:** your Supabase Auth login, and your owner membership seeded by a reviewed one-off script.
6. **Limits:** your account only, the read-only scope, one day of data, and a disconnect at the end. Confirm that the provider grant is removed.

## 12. G2 security review follow-up

The G2 review (CI run 43537460083, commit `c74cd9f`) gave a conditional pass with eight findings. Status after the follow-up:

| G2 finding | Status | Where |
|---|---|---|
| 2. Token functions checked the organization but not the person's permission | **Resolved.** Each function requires a permitting context first. Viewers and analysts are refused, and client, account and run jobs are refused. Positive and negative tests cover each role, each job scope and the refresh path. | Migration 11; section 3 |
| 8a. Mark for reauthorization was unaudited | **Resolved.** Audited as `connection.needs_reauth`, as the person or the token manager. | Migration 11 |
| 1a. Session creation was unaudited, and documented as if it were verified | **Resolved.** `session.create_unverified` and `session.revoke` events, without token material. Documented as unverified. A CI test keeps every application caller out. | Migrations 9 and 10; section 3 |
| 4. The one-context rule was described as a boundary | **Resolved.** Documented as a guard. | Section 3 |
| 5. Signing-key rotation and backup exposure were undocumented | **Resolved** as documentation and a procedure. The rotation function is M7b. | Section 3 |
| 6. The fallback could persist unnoticed | **Resolved** as a process: G4 step 7 removes it immediately after G4. | G4 checklist |
| 7. The live pooler was untested | **Planned:** a read-only check through Supavisor at G4 step 6. | G4 checklist |
| 1b. Session creation trusts its caller (impersonation) | **Open. Mandatory before the first real OAuth test** (item A). | M7b |
| 3. Token-manager jobs are self-declared (a cross-organization credential) | **Open. Mandatory before a second customer organization** (item B). Not required for the owner-only OAuth test, which has one customer organization. | M7b |
| 8b. Definer functions owned by the migration role (M7a.1 finding 6) | **Open.** Before customers. | M7b |

**Mandatory before the first real OAuth test:**
- **A.** The M7b sign-in service: it verifies the Supabase login token (signature, issuer, audience, expiry), then calls `create_session`. It alone holds the authenticator credential. Then remove the static test that forbids application callers, and rename the audit event once verification is in place.
- **B-lite.** The token-manager credential and the encryption keys live only on the token-manager host, never on the web host.
- **C.** M7a.1 findings 3 and 4: discovery before activation, and the recoverable revocation workflow (section 6).
- **D.** The G4 checklist completed, including the pooler check and the fallback removal.
