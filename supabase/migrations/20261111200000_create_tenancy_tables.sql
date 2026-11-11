-- GrowthPulse AI, migration 4: workspaces, clients, provider connections, advertising
-- accounts, per-account sync configuration and state, and the owner action log.
--
-- Milestone 5 design: MILESTONE_5_DESIGN.md, sections 3.1 and 3.2. Only empty tables are
-- created here; migration 5 seeds the fictional legacy client and scopes existing rows.
--
-- Rules enforced by the database, not only by application code:
--   - An advertising account belongs to one client for life. Its identity, client and
--     data source never change, and one platform account can be added to one client only.
--   - A live account syncs through a provider connection on the same platform. A
--     synthetic account has no connection.
--   - Account, connection and client status changes follow the approved lifecycles.
--   - No table here holds a credential. Tokens live in growthpulse_secrets (migration 6).


create table growthpulse.workspaces (
    workspace_id uuid primary key,
    name text not null check (char_length(name) between 1 and 200),
    created_at timestamptz not null default now()
);


create table growthpulse.clients (
    client_id uuid primary key default pg_catalog.gen_random_uuid(),
    workspace_id uuid not null references growthpulse.workspaces (workspace_id),
    name text not null check (char_length(name) between 1 and 200 and name = btrim(name)),
    data_source text not null check (data_source in ('synthetic', 'live')),
    status text not null default 'active' check (status in ('active', 'archived')),
    created_at timestamptz not null default now(),
    archived_at timestamptz,
    constraint clients_archived_at_when_archived
        check ((status = 'archived') = (archived_at is not null)),
    -- Composite target, so scoped rows can require a client of their own data source.
    constraint clients_client_source_unique unique (client_id, data_source)
);
create unique index clients_active_name_idx on growthpulse.clients
    (workspace_id, lower(name)) where status = 'active';


-- One OAuth grant. provider_subject_id is the provider's opaque user or manager ID, never
-- an email address. Credentials for it live only in growthpulse_secrets.
create table growthpulse.provider_connections (
    connection_id uuid primary key default pg_catalog.gen_random_uuid(),
    workspace_id uuid not null references growthpulse.workspaces (workspace_id),
    provider text not null check (provider in ('google_ads', 'meta_ads')),
    provider_subject_id text not null check (char_length(provider_subject_id) between 1 and 255),
    status text not null default 'pending'
        check (status in ('pending', 'active', 'needs_reauth', 'revoked')),
    granted_scopes text[] not null default '{}',
    created_at timestamptz not null default now(),
    last_validated_at timestamptz,
    revoked_at timestamptz,
    constraint provider_connections_revoked_at_when_revoked
        check ((status = 'revoked') = (revoked_at is not null)),
    constraint provider_connections_connection_provider_unique unique (connection_id, provider)
);


-- What one connection can see. Discovery writes it; the owner chooses from it.
create table growthpulse.discovered_ad_accounts (
    connection_id uuid not null,
    platform text not null check (platform in ('google_ads', 'meta_ads')),
    external_account_id text not null check (char_length(external_account_id) between 1 and 255),
    display_name text not null default '' check (char_length(display_name) <= 255),
    currency text not null check (currency ~ '^[A-Z]{3}$'),
    timezone text not null check (char_length(timezone) between 1 and 64),
    is_manager boolean not null default false,
    accessible boolean not null default true,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    primary key (connection_id, platform, external_account_id),
    constraint discovered_ad_accounts_connection_fkey foreign key (connection_id, platform)
        references growthpulse.provider_connections (connection_id, provider)
);


create table growthpulse.ad_accounts (
    ad_account_id uuid primary key default pg_catalog.gen_random_uuid(),
    client_id uuid not null,
    data_source text not null check (data_source in ('synthetic', 'live')),
    platform text not null check (platform in ('google_ads', 'meta_ads')),
    external_account_id text not null check (
        char_length(external_account_id) between 1 and 255
        and external_account_id = btrim(external_account_id)
    ),
    display_name text not null default '' check (char_length(display_name) <= 255),
    currency text not null check (currency ~ '^[A-Z]{3}$'),
    timezone text not null check (char_length(timezone) between 1 and 64),
    sync_connection_id uuid,
    status text not null default 'onboarding' check (status in (
        'onboarding', 'active', 'paused', 'needs_attention', 'archived'
    )),
    created_at timestamptz not null default now(),
    archived_at timestamptz,
    constraint ad_accounts_client_source_fkey foreign key (client_id, data_source)
        references growthpulse.clients (client_id, data_source),
    constraint ad_accounts_connection_platform_fkey foreign key (sync_connection_id, platform)
        references growthpulse.provider_connections (connection_id, provider),
    constraint ad_accounts_connection_only_when_live
        check ((data_source = 'live') = (sync_connection_id is not null)),
    constraint ad_accounts_archived_at_when_archived
        check ((status = 'archived') = (archived_at is not null)),
    -- One platform account, one client, for life.
    constraint ad_accounts_identity_unique unique (data_source, platform, external_account_id),
    -- Composite targets for scoped rows (migration 5).
    constraint ad_accounts_account_client_unique unique (ad_account_id, client_id),
    constraint ad_accounts_record_identity_unique
        unique (ad_account_id, data_source, platform, external_account_id),
    constraint ad_accounts_finding_identity_unique
        unique (ad_account_id, client_id, data_source, platform, external_account_id)
);
create index ad_accounts_client_idx on growthpulse.ad_accounts (client_id);


create table growthpulse.account_sync_config (
    ad_account_id uuid primary key references growthpulse.ad_accounts (ad_account_id),
    enabled boolean not null default true,
    frequency text not null default 'daily'
        check (frequency in ('daily', 'every_6_hours', 'manual')),
    preferred_local_hour smallint not null default 6 check (preferred_local_hour between 0 and 23),
    lookback_days smallint not null default 7 check (lookback_days between 1 and 90),
    backfill_start_date date not null,
    max_consecutive_failures smallint not null default 5
        check (max_consecutive_failures between 1 and 10),
    updated_at timestamptz not null default now()
);


-- The sync history is pipeline_runs with run_kind = 'account_sync'. This is the latest
-- state only. The lease foreign key to pipeline_runs is added in migration 5.
create table growthpulse.account_sync_state (
    ad_account_id uuid primary key references growthpulse.ad_accounts (ad_account_id),
    status text not null default 'idle'
        check (status in ('idle', 'running', 'retry_wait', 'needs_attention', 'paused')),
    synced_through date,
    last_success_at timestamptz,
    last_attempt_at timestamptz,
    next_due_at timestamptz,
    consecutive_failures smallint not null default 0 check (consecutive_failures between 0 and 100),
    last_error_code text check (last_error_code ~ '^[a-z][a-z_]{0,63}$'),
    lease_run_id uuid,
    lease_expires_at timestamptz,
    constraint account_sync_state_lease_complete
        check ((lease_run_id is null) = (lease_expires_at is null)),
    constraint account_sync_state_running_holds_lease
        check ((status = 'running') = (lease_run_id is not null))
);
create index account_sync_state_due_idx on growthpulse.account_sync_state (next_due_at)
    where status in ('idle', 'retry_wait');


-- Append-only record of every owner action. Holds no personal data.
create table growthpulse.owner_action_log (
    action_id bigint generated always as identity primary key,
    action text not null check (action ~ '^[a-z][a-z_]{0,63}$'),
    target_type text not null check (target_type in (
        'client', 'ad_account', 'connection', 'sync_config', 'experiment_proposal'
    )),
    target_id text not null check (char_length(target_id) between 1 and 64),
    actor text not null default 'owner' check (actor = 'owner'),
    acted_at timestamptz not null default now()
);
create trigger owner_action_log_append_only
    before update on growthpulse.owner_action_log
    for each row execute function growthpulse.reject_update();


-- Identity never changes, and status follows the approved lifecycles. These run for
-- every role, including the owner, so no code path can move an account between clients.
create function growthpulse.check_client_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.client_id, new.workspace_id, new.data_source, new.created_at)
        is distinct from (old.client_id, old.workspace_id, old.data_source, old.created_at) then
        raise exception 'A client''s identity and data source never change'
            using errcode = 'check_violation';
    end if;
    if old.status = 'archived' and new.status <> 'archived' then
        raise exception 'An archived client cannot be reactivated'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger clients_check_update
    before update on growthpulse.clients
    for each row execute function growthpulse.check_client_update();


create function growthpulse.check_ad_account_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.ad_account_id, new.client_id, new.data_source, new.platform,
        new.external_account_id, new.created_at)
        is distinct from (old.ad_account_id, old.client_id, old.data_source, old.platform,
        old.external_account_id, old.created_at) then
        raise exception 'An advertising account''s identity and client never change'
            using errcode = 'check_violation';
    end if;
    if new.status <> old.status and not (
        (old.status = 'onboarding' and new.status in ('active', 'needs_attention', 'archived'))
        or (old.status = 'active' and new.status in ('paused', 'needs_attention', 'archived'))
        or (old.status = 'paused' and new.status in ('active', 'archived'))
        or (old.status = 'needs_attention' and new.status in ('active', 'paused', 'archived'))
    ) then
        raise exception 'Advertising account status cannot change from % to %',
            old.status, new.status
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger ad_accounts_check_update
    before update on growthpulse.ad_accounts
    for each row execute function growthpulse.check_ad_account_update();


create function growthpulse.check_connection_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.connection_id, new.workspace_id, new.provider, new.provider_subject_id,
        new.created_at)
        is distinct from (old.connection_id, old.workspace_id, old.provider,
        old.provider_subject_id, old.created_at) then
        raise exception 'A provider connection''s identity never changes'
            using errcode = 'check_violation';
    end if;
    if new.status <> old.status and not (
        (old.status = 'pending' and new.status in ('active', 'revoked'))
        or (old.status = 'active' and new.status in ('needs_reauth', 'revoked'))
        or (old.status = 'needs_reauth' and new.status in ('active', 'revoked'))
    ) then
        raise exception 'Connection status cannot change from % to %', old.status, new.status
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger provider_connections_check_update
    before update on growthpulse.provider_connections
    for each row execute function growthpulse.check_connection_update();


-- Row-level security on every new table. Policies for the app roles come in migration 8;
-- until then no app role can see a row.
alter table growthpulse.workspaces enable row level security;
alter table growthpulse.clients enable row level security;
alter table growthpulse.provider_connections enable row level security;
alter table growthpulse.discovered_ad_accounts enable row level security;
alter table growthpulse.ad_accounts enable row level security;
alter table growthpulse.account_sync_config enable row level security;
alter table growthpulse.account_sync_state enable row level security;
alter table growthpulse.owner_action_log enable row level security;


-- Close everything this file created before it commits, against PUBLIC and the Supabase
-- API roles, in case global default privileges granted them anything.
revoke all on all tables in schema growthpulse from public;
revoke all on all sequences in schema growthpulse from public;
revoke all on all functions in schema growthpulse from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on schema growthpulse from %I', api_role);
            execute format('revoke all on all tables in schema growthpulse from %I', api_role);
            execute format(
                'revoke all on all sequences in schema growthpulse from %I', api_role
            );
            execute format(
                'revoke all on all functions in schema growthpulse from %I', api_role
            );
        end if;
    end loop;
end
$$;
