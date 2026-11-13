-- GrowthPulse AI, migration 9: organizations, users, memberships, client grants, sessions
-- and audit events.
--
-- Milestone 7a design: docs/M7A_TENANT_SECURITY.md, sections 2 and 3.
--
-- An organization is an existing workspace: organizations is keyed 1:1 on workspace_id, so
-- no existing table, row, foreign key or report ID changes. The fictional workspace from
-- migration 5 becomes the one internal organization, which may hold synthetic clients only.
--
-- Not in this phase, by owner decision: invitations, billing, customer AI keys (BYOK),
-- OAuth state and authorization links. Users and memberships are created by a reviewed
-- one-off script; no app role can create them.
--
-- Identity comes from Supabase Auth. GrowthPulse stores no password and no email address:
-- a user row holds the Auth subject ID only. Sessions hold a SHA-256 of a random token the
-- application generates; the token itself never reaches the database.

create schema growthpulse_auth;
comment on schema growthpulse_auth is
    'GrowthPulse AI identity, membership and request context. Private: never add to the Data API.';
revoke all on schema growthpulse_auth from public;

-- Finding 5 of the M7a.1 review asked for per-schema default-privilege revokes here and in
-- growthpulse_secrets. They would do nothing: PostgreSQL cannot revoke a global or built-in
-- default (such as PUBLIC EXECUTE on new functions) per schema. Instead every migration
-- that creates objects in these schemas revokes PUBLIC and the Supabase API roles before
-- it commits, as below, and a CI catalog test fails if any function in growthpulse,
-- growthpulse_auth or growthpulse_secrets is executable by PUBLIC or an API role.


create table growthpulse_auth.organizations (
    workspace_id uuid primary key references growthpulse.workspaces (workspace_id),
    kind text not null check (kind in ('internal', 'customer')),
    status text not null default 'active' check (status in ('active', 'suspended')),
    created_at timestamptz not null default now()
);


create table growthpulse_auth.users (
    -- The Supabase Auth subject (auth.users.id). Never joined to the auth schema by app roles.
    user_id uuid primary key,
    status text not null default 'active' check (status in ('active', 'disabled')),
    created_at timestamptz not null default now()
);


create table growthpulse_auth.memberships (
    membership_id uuid primary key default pg_catalog.gen_random_uuid(),
    workspace_id uuid not null references growthpulse_auth.organizations (workspace_id),
    user_id uuid not null references growthpulse_auth.users (user_id),
    role text not null check (role in ('owner', 'admin', 'analyst', 'viewer')),
    status text not null default 'active' check (status in ('active', 'suspended', 'removed')),
    created_at timestamptz not null default now(),
    removed_at timestamptz,
    constraint memberships_removed_at_when_removed
        check ((status = 'removed') = (removed_at is not null)),
    constraint memberships_one_per_user unique (workspace_id, user_id),
    -- Composite target, so a client grant can require a client of the same organization.
    constraint memberships_membership_workspace_unique unique (membership_id, workspace_id)
);


-- Composite target for client grants. Additive: the existing primary key is unchanged.
alter table growthpulse.clients
    add constraint clients_client_workspace_unique unique (client_id, workspace_id);


-- Analysts and viewers see only the clients granted here. Owners and admins see every
-- client of their organization and hold no grants.
create table growthpulse_auth.client_grants (
    membership_id uuid not null,
    workspace_id uuid not null,
    client_id uuid not null,
    access text not null check (access in ('view', 'analyse')),
    created_at timestamptz not null default now(),
    primary key (membership_id, client_id),
    constraint client_grants_membership_fkey foreign key (membership_id, workspace_id)
        references growthpulse_auth.memberships (membership_id, workspace_id),
    constraint client_grants_client_fkey foreign key (client_id, workspace_id)
        references growthpulse.clients (client_id, workspace_id)
);


-- One signed-in browser session. Created only by growthpulse_authenticator (migration 10)
-- after the application has verified the Supabase JWT.
create table growthpulse_auth.sessions (
    session_id uuid primary key default pg_catalog.gen_random_uuid(),
    user_id uuid not null references growthpulse_auth.users (user_id),
    token_hash bytea not null unique check (octet_length(token_hash) = 32),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    constraint sessions_expiry_window
        check (expires_at > created_at and expires_at <= created_at + interval '12 hours')
);
create index sessions_user_idx on growthpulse_auth.sessions (user_id);


-- Append-only. Codes and IDs only: no free text and no personal data.
create table growthpulse_auth.audit_events (
    event_id bigint generated always as identity primary key,
    workspace_id uuid not null references growthpulse_auth.organizations (workspace_id),
    actor_user_id uuid references growthpulse_auth.users (user_id),
    actor_service text check (actor_service in ('pipeline', 'token_manager', 'operator')),
    action text not null check (action ~ '^[a-z][a-z_.]{0,63}$'),
    target_type text not null check (target_type ~ '^[a-z][a-z_]{0,31}$'),
    target_id text not null check (char_length(target_id) between 1 and 64),
    request_id uuid,
    occurred_at timestamptz not null default now(),
    constraint audit_events_one_actor check ((actor_user_id is null) <> (actor_service is null))
);
create index audit_events_workspace_idx on growthpulse_auth.audit_events
    (workspace_id, occurred_at desc);
create trigger audit_events_append_only
    before update on growthpulse_auth.audit_events
    for each row execute function growthpulse.reject_update();


-- Every workspace is an organization. A new workspace becomes a customer organization in
-- the same statement; only this migration creates the internal one.
create function growthpulse_auth.create_organization_for_workspace()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    insert into growthpulse_auth.organizations (workspace_id, kind)
    values (new.workspace_id, 'customer');
    return new;
end;
$$;

create trigger workspaces_create_organization
    after insert on growthpulse.workspaces
    for each row execute function growthpulse_auth.create_organization_for_workspace();


-- An organization's kind and identity never change.
create function growthpulse_auth.check_organization_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.workspace_id, new.kind, new.created_at)
        is distinct from (old.workspace_id, old.kind, old.created_at) then
        raise exception 'An organization''s identity and kind never change'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger organizations_check_update
    before update on growthpulse_auth.organizations
    for each row execute function growthpulse_auth.check_organization_update();


-- The internal (demo) organization holds synthetic clients only, so fictional data can
-- never sit beside a real client.
create function growthpulse_auth.check_client_organization()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    v_kind text;
begin
    select o.kind into v_kind
    from growthpulse_auth.organizations as o
    where o.workspace_id = new.workspace_id;
    if not found then
        raise exception 'The client''s workspace is not an organization'
            using errcode = 'foreign_key_violation';
    end if;
    if v_kind = 'internal' and new.data_source <> 'synthetic' then
        raise exception 'The internal organization holds synthetic clients only'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger clients_check_organization
    before insert on growthpulse.clients
    for each row execute function growthpulse_auth.check_client_organization();


-- A membership's identity never changes, a removed membership stays removed, and the last
-- active owner can be neither demoted, suspended nor removed.
create function growthpulse_auth.check_membership_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.membership_id, new.workspace_id, new.user_id, new.created_at)
        is distinct from (old.membership_id, old.workspace_id, old.user_id, old.created_at) then
        raise exception 'A membership''s identity never changes' using errcode = 'check_violation';
    end if;
    if old.status = 'removed' and new.status <> 'removed' then
        raise exception 'A removed membership cannot be restored' using errcode = 'check_violation';
    end if;
    if old.role = 'owner' and old.status = 'active'
        and (new.role <> 'owner' or new.status <> 'active')
        and not exists (
            select from growthpulse_auth.memberships as m
            where m.workspace_id = old.workspace_id and m.role = 'owner'
                and m.status = 'active' and m.membership_id <> old.membership_id
        ) then
        raise exception 'An organization must keep at least one active owner'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger memberships_check_update
    before update on growthpulse_auth.memberships
    for each row execute function growthpulse_auth.check_membership_update();


-- Client grants are for analysts and viewers only; owners and admins already see every
-- client of their organization.
create function growthpulse_auth.check_client_grant()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if not exists (
        select from growthpulse_auth.memberships as m
        where m.membership_id = new.membership_id and m.role in ('analyst', 'viewer')
    ) then
        raise exception 'Client grants are for analyst and viewer memberships only'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger client_grants_check
    before insert or update on growthpulse_auth.client_grants
    for each row execute function growthpulse_auth.check_client_grant();


-- The fictional workspace from migration 5 is the internal organization. Any other
-- workspace that already exists (none on Supabase) becomes a customer organization.
insert into growthpulse_auth.organizations (workspace_id, kind)
select w.workspace_id,
    case when w.workspace_id = '26f3f7ff-8015-413a-8029-06e04727974c'
        then 'internal' else 'customer' end
from growthpulse.workspaces as w;

do $$
begin
    if exists (
        select from growthpulse.clients as c
        join growthpulse_auth.organizations as o on o.workspace_id = c.workspace_id
        where o.kind = 'internal' and c.data_source <> 'synthetic'
    ) then
        raise exception 'Migration 9 refused: the internal organization holds a live client'
            using errcode = 'check_violation';
    end if;
end
$$;


-- Row-level security on every table, with no policy: until migration 10 grants functions,
-- no app role reaches any row.
alter table growthpulse_auth.organizations enable row level security;
alter table growthpulse_auth.users enable row level security;
alter table growthpulse_auth.memberships enable row level security;
alter table growthpulse_auth.client_grants enable row level security;
alter table growthpulse_auth.sessions enable row level security;
alter table growthpulse_auth.audit_events enable row level security;


-- Close everything this file created before it commits.
revoke all on all tables in schema growthpulse_auth from public;
revoke all on all sequences in schema growthpulse_auth from public;
revoke all on all functions in schema growthpulse_auth from public;
revoke all on all functions in schema growthpulse from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on schema growthpulse_auth from %I', api_role);
            execute format('revoke all on all tables in schema growthpulse_auth from %I', api_role);
            execute format(
                'revoke all on all sequences in schema growthpulse_auth from %I', api_role
            );
            execute format(
                'revoke all on all functions in schema growthpulse_auth from %I', api_role
            );
        end if;
    end loop;
end
$$;
