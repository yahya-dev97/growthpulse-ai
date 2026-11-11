-- GrowthPulse AI, migration 7: narrow owner actions and the restricted manager role.
--
-- Milestone 5 design: MILESTONE_5_DESIGN.md, section 6.
--
-- growthpulse_manager  Created without LOGIN. Can execute the owner-action functions below
--                      and nothing else: no table, view or token access. It will back the
--                      owner's dashboard actions once you approve its login.
--
-- Every function runs with the owner's rights (SECURITY DEFINER), pins search_path to ''
-- and qualifies every name, validates its arguments, and logs to owner_action_log. Status
-- changes still pass through the lifecycle triggers from migration 4, so these functions
-- cannot skip a rule. Approving an experiment records a decision only: nothing here, or
-- anywhere in GrowthPulse, changes an advertising account.

create role growthpulse_manager nologin noinherit;
comment on role growthpulse_manager is
    'GrowthPulse AI owner actions. EXECUTE on named growthpulse functions only.';
alter role growthpulse_manager connection limit 3;
alter role growthpulse_manager set search_path = growthpulse;
alter role growthpulse_manager set statement_timeout = '15s';
alter role growthpulse_manager set idle_in_transaction_session_timeout = '30s';
alter role growthpulse_manager set lock_timeout = '5s';


-- Internal: called by the functions below, never granted to anyone.
create function growthpulse.log_owner_action(
    p_action text, p_target_type text, p_target_id text
)
returns void
language sql
set search_path = ''
as $$
    insert into growthpulse.owner_action_log (action, target_type, target_id)
    values (p_action, p_target_type, p_target_id);
$$;


create function growthpulse.create_client(
    p_workspace_id uuid, p_name text, p_data_source text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_client_id uuid;
begin
    insert into growthpulse.clients (workspace_id, name, data_source)
    values (p_workspace_id, pg_catalog.btrim(p_name), p_data_source)
    returning client_id into v_client_id;
    perform growthpulse.log_owner_action('create_client', 'client', v_client_id::text);
    return v_client_id;
end;
$$;


create function growthpulse.rename_client(p_client_id uuid, p_name text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.clients as c
    set name = pg_catalog.btrim(p_name)
    where c.client_id = p_client_id and c.status = 'active';
    if not found then
        raise exception 'No active client with this ID' using errcode = 'no_data_found';
    end if;
    perform growthpulse.log_owner_action('rename_client', 'client', p_client_id::text);
end;
$$;


-- Archiving keeps every row. The client's accounts are archived and stop syncing.
create function growthpulse.archive_client(p_client_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.clients as c
    set status = 'archived', archived_at = pg_catalog.now()
    where c.client_id = p_client_id and c.status = 'active';
    if not found then
        raise exception 'No active client with this ID' using errcode = 'no_data_found';
    end if;
    update growthpulse.account_sync_config as s
    set enabled = false, updated_at = pg_catalog.now()
    from growthpulse.ad_accounts as a
    where a.ad_account_id = s.ad_account_id and a.client_id = p_client_id;
    update growthpulse.account_sync_state as s
    set status = 'paused', next_due_at = null
    from growthpulse.ad_accounts as a
    where a.ad_account_id = s.ad_account_id and a.client_id = p_client_id
        and s.status <> 'running';
    update growthpulse.ad_accounts as a
    set status = 'archived', archived_at = pg_catalog.now()
    where a.client_id = p_client_id and a.status <> 'archived';
    perform growthpulse.log_owner_action('archive_client', 'client', p_client_id::text);
end;
$$;


-- Starts an OAuth connection. Credentials arrive later through the token manager.
create function growthpulse.create_connection(
    p_workspace_id uuid, p_provider text, p_provider_subject_id text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_connection_id uuid;
begin
    insert into growthpulse.provider_connections (workspace_id, provider, provider_subject_id)
    values (p_workspace_id, p_provider, p_provider_subject_id)
    returning connection_id into v_connection_id;
    perform growthpulse.log_owner_action(
        'create_connection', 'connection', v_connection_id::text
    );
    return v_connection_id;
end;
$$;


-- Disconnects: deletes the stored credentials and flags the accounts that synced through
-- the connection. Their data is kept.
create function growthpulse.revoke_connection(p_connection_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.provider_connections as c
    set status = 'revoked', revoked_at = pg_catalog.now()
    where c.connection_id = p_connection_id and c.status <> 'revoked';
    if not found then
        raise exception 'No connection to revoke with this ID' using errcode = 'no_data_found';
    end if;
    delete from growthpulse_secrets.oauth_credentials as o
    where o.connection_id = p_connection_id;
    update growthpulse.ad_accounts as a
    set status = 'needs_attention'
    where a.sync_connection_id = p_connection_id and a.status in ('onboarding', 'active');
    perform growthpulse.log_owner_action(
        'revoke_connection', 'connection', p_connection_id::text
    );
end;
$$;


create function growthpulse.start_account_sync_tracking(
    p_ad_account_id uuid, p_frequency text, p_backfill_start_date date
)
returns void
language sql
set search_path = ''
as $$
    insert into growthpulse.account_sync_config (ad_account_id, frequency, backfill_start_date)
    values (p_ad_account_id, p_frequency, p_backfill_start_date);
    insert into growthpulse.account_sync_state (ad_account_id, next_due_at)
    values (p_ad_account_id, pg_catalog.now());
$$;


-- Adds one account that a live connection discovered to a live client. One connection may
-- add many accounts, one call each. An account already in any client is refused by the
-- unique identity constraint.
create function growthpulse.add_ad_account(
    p_client_id uuid,
    p_connection_id uuid,
    p_external_account_id text,
    p_backfill_start_date date
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_client growthpulse.clients;
    v_connection growthpulse.provider_connections;
    v_found growthpulse.discovered_ad_accounts;
    v_ad_account_id uuid;
begin
    select * into v_client from growthpulse.clients as c
    where c.client_id = p_client_id for update;
    if not found or v_client.status <> 'active' or v_client.data_source <> 'live' then
        raise exception 'Accounts can only be added to an active live client'
            using errcode = 'check_violation';
    end if;

    select * into v_connection from growthpulse.provider_connections as c
    where c.connection_id = p_connection_id;
    if not found or v_connection.status <> 'active'
        or v_connection.workspace_id <> v_client.workspace_id then
        raise exception 'The connection must be active and in the client''s workspace'
            using errcode = 'check_violation';
    end if;

    select * into v_found from growthpulse.discovered_ad_accounts as d
    where d.connection_id = p_connection_id
        and d.platform = v_connection.provider
        and d.external_account_id = p_external_account_id;
    if not found then
        raise exception 'This connection has not discovered that account'
            using errcode = 'no_data_found';
    end if;
    if v_found.is_manager or not v_found.accessible then
        raise exception 'Manager or inaccessible accounts cannot be added'
            using errcode = 'check_violation';
    end if;
    if not exists (
        select from pg_catalog.pg_timezone_names as z where z.name = v_found.timezone
    ) then
        raise exception 'The account timezone is not recognised' using errcode = 'check_violation';
    end if;

    insert into growthpulse.ad_accounts (
        client_id, data_source, platform, external_account_id, display_name, currency,
        timezone, sync_connection_id
    ) values (
        p_client_id, 'live', v_found.platform, v_found.external_account_id,
        v_found.display_name, v_found.currency, v_found.timezone, p_connection_id
    )
    returning ad_account_id into v_ad_account_id;
    perform growthpulse.start_account_sync_tracking(
        v_ad_account_id, 'daily', p_backfill_start_date
    );
    perform growthpulse.log_owner_action('add_ad_account', 'ad_account', v_ad_account_id::text);
    return v_ad_account_id;
end;
$$;


-- Adds a fictional account to a synthetic client, for local runs and tests.
create function growthpulse.add_synthetic_ad_account(
    p_client_id uuid,
    p_platform text,
    p_external_account_id text,
    p_currency text,
    p_timezone text,
    p_backfill_start_date date
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_ad_account_id uuid;
begin
    if not exists (
        select from growthpulse.clients as c
        where c.client_id = p_client_id and c.status = 'active' and c.data_source = 'synthetic'
    ) then
        raise exception 'Synthetic accounts can only be added to an active synthetic client'
            using errcode = 'check_violation';
    end if;
    if p_external_account_id not like 'synthetic-%' then
        raise exception 'Synthetic account IDs must start with synthetic-'
            using errcode = 'check_violation';
    end if;
    if not exists (select from pg_catalog.pg_timezone_names as z where z.name = p_timezone) then
        raise exception 'The account timezone is not recognised' using errcode = 'check_violation';
    end if;

    insert into growthpulse.ad_accounts (
        client_id, data_source, platform, external_account_id, display_name, currency, timezone
    ) values (
        p_client_id, 'synthetic', p_platform, p_external_account_id, p_external_account_id,
        p_currency, p_timezone
    )
    returning ad_account_id into v_ad_account_id;
    perform growthpulse.start_account_sync_tracking(
        v_ad_account_id, 'manual', p_backfill_start_date
    );
    perform growthpulse.log_owner_action(
        'add_synthetic_ad_account', 'ad_account', v_ad_account_id::text
    );
    return v_ad_account_id;
end;
$$;


-- Pause, resume or archive an account. The lifecycle trigger refuses anything else.
create function growthpulse.set_account_status(p_ad_account_id uuid, p_status text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if p_status not in ('active', 'paused', 'archived') then
        raise exception 'The owner can only activate, pause or archive an account'
            using errcode = 'check_violation';
    end if;
    if exists (
        select from growthpulse.account_sync_state as s
        where s.ad_account_id = p_ad_account_id and s.status = 'running'
    ) then
        raise exception 'A sync is running for this account; try again when it finishes'
            using errcode = 'lock_not_available';
    end if;

    update growthpulse.ad_accounts as a
    set status = p_status,
        archived_at = case when p_status = 'archived' then pg_catalog.now() end
    where a.ad_account_id = p_ad_account_id;
    if not found then
        raise exception 'No account with this ID' using errcode = 'no_data_found';
    end if;

    if p_status = 'active' then
        update growthpulse.account_sync_state as s
        set status = 'idle', consecutive_failures = 0, last_error_code = null,
            next_due_at = pg_catalog.now()
        where s.ad_account_id = p_ad_account_id;
    else
        update growthpulse.account_sync_state as s
        set status = 'paused', next_due_at = null
        where s.ad_account_id = p_ad_account_id;
    end if;
    if p_status = 'archived' then
        update growthpulse.account_sync_config as s
        set enabled = false, updated_at = pg_catalog.now()
        where s.ad_account_id = p_ad_account_id;
    end if;
    perform growthpulse.log_owner_action(
        'set_account_status_' || p_status, 'ad_account', p_ad_account_id::text
    );
end;
$$;


create function growthpulse.set_sync_config(
    p_ad_account_id uuid,
    p_enabled boolean,
    p_frequency text,
    p_preferred_local_hour smallint,
    p_lookback_days smallint,
    p_max_consecutive_failures smallint
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if exists (
        select from growthpulse.ad_accounts as a
        where a.ad_account_id = p_ad_account_id and a.status = 'archived'
    ) then
        raise exception 'An archived account cannot be reconfigured'
            using errcode = 'check_violation';
    end if;
    update growthpulse.account_sync_config as s
    set enabled = p_enabled,
        frequency = p_frequency,
        preferred_local_hour = p_preferred_local_hour,
        lookback_days = p_lookback_days,
        max_consecutive_failures = p_max_consecutive_failures,
        updated_at = pg_catalog.now()
    where s.ad_account_id = p_ad_account_id;
    if not found then
        raise exception 'No account with this ID' using errcode = 'no_data_found';
    end if;
    perform growthpulse.log_owner_action('set_sync_config', 'sync_config', p_ad_account_id::text);
end;
$$;


-- Asks for a sync as soon as the dispatcher next runs. Waiting retries are brought forward.
create function growthpulse.request_sync(p_ad_account_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.account_sync_state as s
    set status = 'idle', next_due_at = pg_catalog.now()
    from growthpulse.ad_accounts as a
    where a.ad_account_id = s.ad_account_id
        and s.ad_account_id = p_ad_account_id
        and a.status in ('onboarding', 'active')
        and s.status in ('idle', 'retry_wait');
    if not found then
        raise exception 'This account cannot sync now (paused, archived, running or unknown)'
            using errcode = 'check_violation';
    end if;
    perform growthpulse.log_owner_action('request_sync', 'ad_account', p_ad_account_id::text);
end;
$$;


-- Records the owner's decision on an experiment proposal. The status trigger from
-- migration 1 still enforces the allowed transitions.
create function growthpulse.decide_experiment(
    p_proposal_id uuid, p_status text, p_note text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if p_status not in ('approved', 'rejected', 'running', 'completed', 'cancelled') then
        raise exception 'Not an owner decision' using errcode = 'check_violation';
    end if;
    insert into growthpulse.experiment_status_events (proposal_id, status, actor, note)
    values (p_proposal_id, p_status, 'owner', p_note);
    perform growthpulse.log_owner_action(
        'decide_experiment_' || p_status, 'experiment_proposal', p_proposal_id::text
    );
end;
$$;


-- Close everything, then grant the manager exactly its functions.
revoke all on all functions in schema growthpulse from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format(
                'revoke all on all functions in schema growthpulse from %I', api_role
            );
        end if;
    end loop;
end
$$;

grant usage on schema growthpulse to growthpulse_manager;
grant execute on function growthpulse.create_client(uuid, text, text) to growthpulse_manager;
grant execute on function growthpulse.rename_client(uuid, text) to growthpulse_manager;
grant execute on function growthpulse.archive_client(uuid) to growthpulse_manager;
grant execute on function growthpulse.create_connection(uuid, text, text)
    to growthpulse_manager;
grant execute on function growthpulse.revoke_connection(uuid) to growthpulse_manager;
grant execute on function growthpulse.add_ad_account(uuid, uuid, text, date)
    to growthpulse_manager;
grant execute on function growthpulse.add_synthetic_ad_account(
    uuid, text, text, text, text, date
) to growthpulse_manager;
grant execute on function growthpulse.set_account_status(uuid, text) to growthpulse_manager;
grant execute on function growthpulse.set_sync_config(
    uuid, boolean, text, smallint, smallint, smallint
) to growthpulse_manager;
grant execute on function growthpulse.request_sync(uuid) to growthpulse_manager;
grant execute on function growthpulse.decide_experiment(uuid, text, text)
    to growthpulse_manager;
