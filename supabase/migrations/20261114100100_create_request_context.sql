-- GrowthPulse AI, migration 10: signed, transaction-local request and job context.
--
-- Milestone 7a design: docs/M7A_TENANT_SECURITY.md, section 3 (trust model).
--
-- Every tenant-scoped transaction starts with exactly one of:
--   growthpulse_auth.begin_request(session_token, workspace_id, request_id)
--       for a signed-in person. The session token must match a live session of an active
--       user with an active membership of an active organization. Granted to the dashboard,
--       manager and token-manager roles.
--   growthpulse_auth.begin_job(scope_kind, scope_id, job_id)
--       for the pipeline and the token manager. Scopes the transaction to one organization,
--       and to one client when the scope is a client, account or run.
--
-- Why the context cannot be forged. Both functions write two transaction-local settings
-- (set_config(..., true)): the context, and an HMAC-SHA-256 over it, this backend's process
-- ID and this transaction's start time, keyed by a 256-bit secret only the schema owner can
-- read. Any role can set the settings directly, but without the key it cannot produce a
-- valid MAC, and a value copied from another transaction fails because the backend or start
-- time differs. Every reader re-verifies the MAC, and a present but invalid MAC raises.
-- Session-level settings are never used: through a transaction pooler they would leak
-- into the next client's transactions.
--
-- Roles created here (NOLOGIN, no password; the owner enables login manually):
--   growthpulse_authenticator  Records a session after the application has verified the
--                              Supabase JWT. Can execute the session functions only.

create role growthpulse_authenticator nologin noinherit;
comment on role growthpulse_authenticator is
    'GrowthPulse AI sign-in. EXECUTE on growthpulse_auth session functions only.';
alter role growthpulse_authenticator connection limit 3;
alter role growthpulse_authenticator set search_path = growthpulse_auth;
alter role growthpulse_authenticator set statement_timeout = '5s';
alter role growthpulse_authenticator set idle_in_transaction_session_timeout = '10s';


-- The MAC key, generated inside the database at migration time: it is in no file. RLS with
-- no policy and no grant, so only the owner, through the functions below, reads it. The
-- inner and outer HMAC pads are stored precomputed. gen_random_uuid() draws from the
-- server's cryptographic random source; two of them give 244 random bits.
create table growthpulse_auth.context_keys (
    key_id smallint primary key check (key_id = 1),
    inner_pad bytea not null check (octet_length(inner_pad) = 64),
    outer_pad bytea not null check (octet_length(outer_pad) = 64),
    created_at timestamptz not null default now()
);
alter table growthpulse_auth.context_keys enable row level security;

do $$
declare
    v_key bytea := pg_catalog.decode(
        pg_catalog.replace(pg_catalog.gen_random_uuid()::text, '-', '')
        || pg_catalog.replace(pg_catalog.gen_random_uuid()::text, '-', '')
        || pg_catalog.repeat('00', 32),
        'hex'
    );
    v_inner bytea := v_key;
    v_outer bytea := v_key;
begin
    for i in 0..63 loop
        v_inner := pg_catalog.set_byte(v_inner, i, pg_catalog.get_byte(v_key, i) # 54);
        v_outer := pg_catalog.set_byte(v_outer, i, pg_catalog.get_byte(v_key, i) # 92);
    end loop;
    insert into growthpulse_auth.context_keys (key_id, inner_pad, outer_pad)
    values (1, v_inner, v_outer);
end
$$;


-- Internal. HMAC-SHA-256 of the context, bound to this backend and this transaction.
create function growthpulse_auth.context_mac(p_context text)
returns text
language sql
stable
security definer
set search_path = ''
as $$
    select pg_catalog.encode(
        pg_catalog.sha256(k.outer_pad || pg_catalog.sha256(
            k.inner_pad || pg_catalog.convert_to(
                p_context
                || ';' || pg_catalog.pg_backend_pid()::text
                || ';' || (extract(epoch from pg_catalog.transaction_timestamp()) * 1000000)
                    ::bigint::text,
                'UTF8'
            )
        )),
        'hex'
    )
    from growthpulse_auth.context_keys as k
    where k.key_id = 1;
$$;


-- Internal. Signs and stores a context for the rest of this transaction only.
create function growthpulse_auth.set_context(
    p_kind text,
    p_user_id uuid,
    p_workspace_id uuid,
    p_client_id uuid,
    p_member_role text,
    p_membership_id uuid,
    p_ref_id uuid,
    p_scope_kind text,
    p_scope_id uuid
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    v_context text;
begin
    -- A guard against accidental double scoping in application code, not a security
    -- boundary: any role can clear both settings and then begin a second context. That
    -- gains nothing, because each context still needs its own valid session or grant.
    if pg_catalog.current_setting('growthpulse.context', true) is not null
        and pg_catalog.current_setting('growthpulse.context', true) <> '' then
        raise exception 'This transaction already has a context; one per transaction'
            using errcode = 'insufficient_privilege';
    end if;
    v_context := pg_catalog.concat_ws(';',
        'v1', p_kind, coalesce(p_user_id::text, ''), p_workspace_id::text,
        coalesce(p_client_id::text, ''), coalesce(p_member_role, ''),
        coalesce(p_membership_id::text, ''), coalesce(p_ref_id::text, ''),
        coalesce(p_scope_kind, ''), coalesce(p_scope_id::text, '')
    );
    if growthpulse_auth.context_mac(v_context) is null then
        raise exception 'The context key is missing' using errcode = 'insufficient_privilege';
    end if;
    perform pg_catalog.set_config('growthpulse.context', v_context, true);
    perform pg_catalog.set_config(
        'growthpulse.context_mac', growthpulse_auth.context_mac(v_context), true
    );
end;
$$;


-- The verified context of this transaction: no row when there is none. Raises when a
-- context is present but its MAC does not verify (forged, edited or replayed).
create function growthpulse_auth.current_context()
returns table (
    kind text,
    user_id uuid,
    workspace_id uuid,
    client_id uuid,
    member_role text,
    membership_id uuid,
    ref_id uuid,
    scope_kind text,
    scope_id uuid
)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
    v_context text := coalesce(pg_catalog.current_setting('growthpulse.context', true), '');
    v_mac text := coalesce(pg_catalog.current_setting('growthpulse.context_mac', true), '');
    v_parts text[];
begin
    if v_context = '' and v_mac = '' then
        return;
    end if;
    -- "is distinct from", so a missing key (a null MAC) can never verify anything.
    if v_mac is distinct from growthpulse_auth.context_mac(v_context)
        or growthpulse_auth.context_mac(v_context) is null then
        raise exception 'The request context is not valid for this transaction'
            using errcode = 'insufficient_privilege';
    end if;
    v_parts := pg_catalog.string_to_array(v_context, ';');
    if pg_catalog.array_length(v_parts, 1) <> 10 or v_parts[1] <> 'v1' then
        raise exception 'The request context is not valid for this transaction'
            using errcode = 'insufficient_privilege';
    end if;
    kind := v_parts[2];
    user_id := nullif(v_parts[3], '')::uuid;
    workspace_id := v_parts[4]::uuid;
    client_id := nullif(v_parts[5], '')::uuid;
    member_role := nullif(v_parts[6], '');
    membership_id := nullif(v_parts[7], '')::uuid;
    ref_id := nullif(v_parts[8], '')::uuid;
    scope_kind := nullif(v_parts[9], '');
    scope_id := nullif(v_parts[10], '')::uuid;
    return next;
end;
$$;


-- Fixed role permissions (M7a.1, section 3). Named permissions are checked here only.
create function growthpulse_auth.role_permits(p_role text, p_permission text)
returns boolean
language sql
immutable
set search_path = ''
as $$
    select case
        when p_role = 'owner' then p_permission in (
            'report.read', 'analysis.run', 'experiment.propose', 'experiment.approve',
            'client.manage', 'sync.configure', 'connection.read', 'connection.create',
            'connection.revoke', 'audit.read', 'member.manage', 'organization.delete'
        )
        when p_role = 'admin' then p_permission in (
            'report.read', 'analysis.run', 'experiment.propose', 'experiment.approve',
            'client.manage', 'sync.configure', 'connection.read', 'connection.create',
            'connection.revoke', 'audit.read', 'member.manage'
        )
        when p_role = 'analyst' then p_permission in (
            'report.read', 'analysis.run', 'experiment.propose'
        )
        when p_role = 'viewer' then p_permission in ('report.read')
        else false
    end;
$$;


-- A signed-in person's request. The same error for every refusal, so it reveals nothing
-- about which check failed or whether the organization exists.
create function growthpulse_auth.begin_request(
    p_session_token bytea, p_workspace_id uuid, p_request_id uuid
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    v_user_id uuid;
    v_membership_id uuid;
    v_role text;
begin
    if p_session_token is null or pg_catalog.octet_length(p_session_token) <> 32
        or p_workspace_id is null or p_request_id is null then
        raise exception 'Not signed in to this organization'
            using errcode = 'insufficient_privilege';
    end if;
    select s.user_id into v_user_id
    from growthpulse_auth.sessions as s
    join growthpulse_auth.users as u on u.user_id = s.user_id
    where s.token_hash = pg_catalog.sha256(p_session_token)
        and s.revoked_at is null
        and s.expires_at > pg_catalog.now()
        and u.status = 'active';
    if found then
        select m.membership_id, m.role into v_membership_id, v_role
        from growthpulse_auth.memberships as m
        join growthpulse_auth.organizations as o on o.workspace_id = m.workspace_id
        where m.workspace_id = p_workspace_id and m.user_id = v_user_id
            and m.status = 'active' and o.status = 'active';
    end if;
    if v_membership_id is null then
        raise exception 'Not signed in to this organization'
            using errcode = 'insufficient_privilege';
    end if;
    perform growthpulse_auth.set_context(
        'request', v_user_id, p_workspace_id, null, v_role, v_membership_id, p_request_id,
        null, null
    );
end;
$$;


-- A scheduled or worker job, scoped to one organization and optionally one client.
-- scope_kind: 'workspace', 'connection' (organization-wide), or 'client', 'ad_account',
-- 'run' (one client). The organization must be active.
create function growthpulse_auth.begin_job(
    p_scope_kind text, p_scope_id uuid, p_job_id uuid
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    v_workspace_id uuid;
    v_client_id uuid;
begin
    if p_scope_kind = 'workspace' then
        v_workspace_id := p_scope_id;
    elsif p_scope_kind = 'connection' then
        select c.workspace_id into v_workspace_id
        from growthpulse.provider_connections as c where c.connection_id = p_scope_id;
    elsif p_scope_kind = 'client' then
        select c.workspace_id, c.client_id into v_workspace_id, v_client_id
        from growthpulse.clients as c where c.client_id = p_scope_id;
    elsif p_scope_kind = 'ad_account' then
        select c.workspace_id, c.client_id into v_workspace_id, v_client_id
        from growthpulse.ad_accounts as a
        join growthpulse.clients as c on c.client_id = a.client_id
        where a.ad_account_id = p_scope_id;
    elsif p_scope_kind = 'run' then
        select c.workspace_id, c.client_id into v_workspace_id, v_client_id
        from growthpulse.pipeline_runs as r
        join growthpulse.clients as c on c.client_id = r.client_id
        where r.run_id = p_scope_id;
    else
        raise exception 'Unknown job scope' using errcode = 'invalid_parameter_value';
    end if;
    if v_workspace_id is null or not exists (
        select from growthpulse_auth.organizations as o
        where o.workspace_id = v_workspace_id and o.status = 'active'
    ) then
        raise exception 'No active organization for this job scope'
            using errcode = 'insufficient_privilege';
    end if;
    perform growthpulse_auth.set_context(
        'job', null, v_workspace_id, v_client_id, null, null, p_job_id,
        p_scope_kind, p_scope_id
    );
end;
$$;


-- Readers used by policies, views and functions. Each returns nothing without a context.

create function growthpulse_auth.context_workspace()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
    select c.workspace_id from growthpulse_auth.current_context() as c;
$$;

create function growthpulse_auth.job_workspace()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
    select c.workspace_id from growthpulse_auth.current_context() as c where c.kind = 'job';
$$;

-- The clients a job may touch: its one client, or every client of its organization.
create function growthpulse_auth.job_client_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
    select cl.client_id
    from growthpulse_auth.current_context() as c
    join growthpulse.clients as cl on cl.workspace_id = c.workspace_id
    where c.kind = 'job' and (c.client_id is null or cl.client_id = c.client_id);
$$;

create function growthpulse_auth.job_account_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
    select a.ad_account_id
    from growthpulse.ad_accounts as a
    where a.client_id in (select growthpulse_auth.job_client_ids());
$$;

-- The clients a signed-in person may read. Owners and admins: every client of the
-- organization. Analysts and viewers: their granted clients. The membership is re-checked,
-- so a removal takes effect at the next statement.
create function growthpulse_auth.request_client_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
    select cl.client_id
    from growthpulse_auth.current_context() as c
    join growthpulse_auth.memberships as m
        on m.membership_id = c.membership_id and m.status = 'active'
    join growthpulse.clients as cl on cl.workspace_id = c.workspace_id
    where c.kind = 'request'
        and (
            m.role in ('owner', 'admin')
            or exists (
                select from growthpulse_auth.client_grants as g
                where g.membership_id = m.membership_id and g.client_id = cl.client_id
            )
        );
$$;

-- Whether this request's person holds a permission in its organization.
create function growthpulse_auth.request_permits(p_permission text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select coalesce((
        select growthpulse_auth.role_permits(m.role, p_permission)
        from growthpulse_auth.current_context() as c
        join growthpulse_auth.memberships as m
            on m.membership_id = c.membership_id and m.status = 'active'
        where c.kind = 'request'
    ), false);
$$;

-- The organization of this request, when its person holds the permission.
create function growthpulse_auth.request_workspace_if(p_permission text)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
    select c.workspace_id
    from growthpulse_auth.current_context() as c
    where c.kind = 'request' and growthpulse_auth.request_permits(p_permission);
$$;


-- Sessions. The application generates 32 random bytes, sends them to the browser in a
-- secure cookie, and passes only their SHA-256 here.
--
-- UNVERIFIED. The database does not check who the caller signed in as: it trusts the
-- authenticator role to have verified the Supabase login token, which nothing does until
-- M7b. Anyone holding that role's credential can create a session for any active user.
-- So: EXECUTE is granted only to growthpulse_authenticator (NOLOGIN), the growthpulse_auth
-- schema must never be exposed through the Supabase Data API, and no application code may
-- call these functions until the M7b sign-in service verifies the login token first.
-- Every call is audited (no token or hash is recorded) as 'session.create_unverified' and
-- 'session.revoke', against the user, with no organization: a session is not one
-- organization's, and the organization audit view shows only organization events.
create function growthpulse_auth.create_session(
    p_user_id uuid, p_token_hash bytea, p_expires_at timestamptz
)
returns uuid
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    v_session_id uuid;
begin
    if not exists (
        select from growthpulse_auth.users as u
        where u.user_id = p_user_id and u.status = 'active'
    ) then
        raise exception 'No active user with this ID' using errcode = 'insufficient_privilege';
    end if;
    insert into growthpulse_auth.sessions (user_id, token_hash, expires_at)
    values (p_user_id, p_token_hash, p_expires_at)
    returning session_id into v_session_id;
    insert into growthpulse_auth.audit_events (
        workspace_id, actor_service, action, target_type, target_id
    ) values (
        null, 'authenticator', 'session.create_unverified', 'user', p_user_id::text
    );
    return v_session_id;
end;
$$;

create function growthpulse_auth.revoke_session(p_token_hash bytea)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    v_user_id uuid;
begin
    update growthpulse_auth.sessions as s
    set revoked_at = pg_catalog.now()
    where s.token_hash = p_token_hash and s.revoked_at is null
    returning s.user_id into v_user_id;
    if v_user_id is null then
        return false;
    end if;
    insert into growthpulse_auth.audit_events (
        workspace_id, actor_service, action, target_type, target_id
    ) values (
        null, 'authenticator', 'session.revoke', 'user', v_user_id::text
    );
    return true;
end;
$$;

comment on function growthpulse_auth.create_session(uuid, bytea, timestamptz) is
    'UNVERIFIED: trusts the caller to have verified the Supabase sign-in. Authenticator only; '
    'never expose through the Data API. Audited as session.create_unverified.';


-- The dispatcher's cross-organization question: which accounts are due? Returns account
-- IDs only. Each sync then runs in its own job context. Same conditions as before
-- (tenancy.SELECT_DUE_ACCOUNTS), plus an active organization.
create function growthpulse_auth.due_account_syncs(p_now timestamptz)
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
    select a.ad_account_id
    from growthpulse.account_sync_state as s
    join growthpulse.account_sync_config as c on c.ad_account_id = s.ad_account_id
    join growthpulse.ad_accounts as a on a.ad_account_id = s.ad_account_id
    join growthpulse.clients as cl on cl.client_id = a.client_id
    join growthpulse_auth.organizations as o on o.workspace_id = cl.workspace_id
    left join growthpulse.provider_connections as p on p.connection_id = a.sync_connection_id
    where c.enabled and a.status in ('onboarding', 'active') and cl.status = 'active'
        and o.status = 'active'
        and s.status in ('idle', 'retry_wait') and s.next_due_at <= p_now
        and (a.data_source = 'synthetic' or p.status = 'active')
    order by s.next_due_at, a.ad_account_id;
$$;


-- Close everything, then grant exactly what each role needs.
revoke all on all tables in schema growthpulse_auth from public;
revoke all on all functions in schema growthpulse_auth from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on all tables in schema growthpulse_auth from %I', api_role);
            execute format(
                'revoke all on all functions in schema growthpulse_auth from %I', api_role
            );
        end if;
    end loop;
end
$$;

grant usage on schema growthpulse_auth to
    growthpulse_authenticator, growthpulse_dashboard, growthpulse_manager,
    growthpulse_token_manager, growthpulse_writer;

grant execute on function growthpulse_auth.create_session(uuid, bytea, timestamptz)
    to growthpulse_authenticator;
grant execute on function growthpulse_auth.revoke_session(bytea) to growthpulse_authenticator;

grant execute on function growthpulse_auth.begin_request(bytea, uuid, uuid)
    to growthpulse_dashboard, growthpulse_manager, growthpulse_token_manager;
grant execute on function growthpulse_auth.begin_job(text, uuid, uuid)
    to growthpulse_writer, growthpulse_token_manager;

-- Views run with their owner's rights, but PostgreSQL checks EXECUTE on the functions a
-- view calls against the person querying it, so the dashboard needs these two.
grant execute on function growthpulse_auth.request_client_ids() to growthpulse_dashboard;
grant execute on function growthpulse_auth.request_workspace_if(text) to growthpulse_dashboard;

-- Writer policies (migration 12) call these as the writer.
grant execute on function growthpulse_auth.job_workspace() to growthpulse_writer;
grant execute on function growthpulse_auth.job_client_ids() to growthpulse_writer;
grant execute on function growthpulse_auth.job_account_ids() to growthpulse_writer;
grant execute on function growthpulse_auth.due_account_syncs(timestamptz) to growthpulse_writer;
