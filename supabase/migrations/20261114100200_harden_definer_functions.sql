-- GrowthPulse AI, migration 11: tenant checks in every existing SECURITY DEFINER function.
--
-- Milestone 7a design: docs/M7A_TENANT_SECURITY.md, section 4 (findings 1, 5, 8, 9, 10).
--
-- Token functions (migration 6). Each now acts only on a connection of the organization in
-- this transaction's verified context (growthpulse_auth.begin_request or begin_job). A
-- connection of another organization is reported exactly like an unknown one, so a caller
-- cannot even learn that it exists. The rotation listing is limited the same way. The
-- function signatures, return types, grants and the encrypted format are unchanged.
--
-- Owner actions (migration 7). All eleven functions end by calling
-- growthpulse.log_owner_action in the same transaction. It now resolves the action's
-- target to its organization and refuses, rolling the whole action back, unless this
-- transaction is a signed-in request of that organization whose person holds the action's
-- permission. It also records an audit event with the person's user ID. A CI test checks
-- that every function granted to growthpulse_manager calls it.
--
-- Deliberately unchanged in this phase (docs/M7A_TENANT_SECURITY.md, section 6): store
-- activating a connection (finding 3), delete leaving the status (finding 4) and the
-- revoke workflow, which change with the live OAuth flow in M7b; and function ownership
-- (finding 6).


create or replace function growthpulse_secrets.store_credential(
    p_connection_id uuid,
    p_token_type text,
    p_format_version smallint,
    p_key_version smallint,
    p_nonce bytea,
    p_ciphertext bytea,
    p_expires_at timestamptz
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_status text;
    v_workspace_id uuid;
    v_user_id uuid;
    v_request_id uuid;
begin
    select c.status, c.workspace_id into v_status, v_workspace_id
    from growthpulse.provider_connections as c
    where c.connection_id = p_connection_id
        and c.workspace_id = growthpulse_auth.context_workspace()
    for update;
    if not found then
        raise exception 'Unknown provider connection' using errcode = 'foreign_key_violation';
    end if;
    if v_status = 'revoked' then
        raise exception 'A revoked connection cannot store credentials'
            using errcode = 'check_violation';
    end if;

    insert into growthpulse_secrets.oauth_credentials as existing (
        connection_id, token_type, format_version, key_version, nonce, ciphertext, expires_at
    ) values (
        p_connection_id, p_token_type, p_format_version, p_key_version, p_nonce, p_ciphertext,
        p_expires_at
    )
    on conflict (connection_id, token_type) do update set
        format_version = excluded.format_version,
        key_version = excluded.key_version,
        nonce = excluded.nonce,
        ciphertext = excluded.ciphertext,
        expires_at = excluded.expires_at,
        updated_at = pg_catalog.now();

    update growthpulse.provider_connections as c
    set status = 'active', last_validated_at = pg_catalog.now()
    where c.connection_id = p_connection_id;

    select c.user_id, c.ref_id into v_user_id, v_request_id
    from growthpulse_auth.current_context() as c;
    insert into growthpulse_auth.audit_events (
        workspace_id, actor_user_id, actor_service, action, target_type, target_id, request_id
    ) values (
        v_workspace_id, v_user_id, case when v_user_id is null then 'token_manager' end,
        'credential.store', 'connection', p_connection_id::text, v_request_id
    );
end;
$$;


create or replace function growthpulse_secrets.read_credentials(p_connection_id uuid)
returns table (
    token_type text,
    format_version smallint,
    key_version smallint,
    nonce bytea,
    ciphertext bytea,
    expires_at timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
    select o.token_type, o.format_version, o.key_version, o.nonce, o.ciphertext, o.expires_at
    from growthpulse_secrets.oauth_credentials as o
    join growthpulse.provider_connections as c on c.connection_id = o.connection_id
    where o.connection_id = p_connection_id and c.status = 'active'
        and c.workspace_id = growthpulse_auth.context_workspace()
    order by o.token_type;
$$;


create or replace function growthpulse_secrets.delete_credentials(p_connection_id uuid)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_workspace_id uuid;
    v_deleted integer;
    v_user_id uuid;
    v_request_id uuid;
begin
    select c.workspace_id into v_workspace_id
    from growthpulse.provider_connections as c
    where c.connection_id = p_connection_id
        and c.workspace_id = growthpulse_auth.context_workspace();
    if not found then
        return 0;
    end if;
    delete from growthpulse_secrets.oauth_credentials as o
    where o.connection_id = p_connection_id;
    get diagnostics v_deleted = row_count;

    select c.user_id, c.ref_id into v_user_id, v_request_id
    from growthpulse_auth.current_context() as c;
    insert into growthpulse_auth.audit_events (
        workspace_id, actor_user_id, actor_service, action, target_type, target_id, request_id
    ) values (
        v_workspace_id, v_user_id, case when v_user_id is null then 'token_manager' end,
        'credential.delete', 'connection', p_connection_id::text, v_request_id
    );
    return v_deleted;
end;
$$;


create or replace function growthpulse_secrets.mark_connection_needs_reauth(p_connection_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.provider_connections as c
    set status = 'needs_reauth'
    where c.connection_id = p_connection_id and c.status = 'active'
        and c.workspace_id = growthpulse_auth.context_workspace();
    return found;
end;
$$;


-- Finding 10: limited to the organization in context. A rotation job runs once per
-- organization, each in its own job context.
create or replace function growthpulse_secrets.credentials_needing_rotation(
    p_active_key_version smallint
)
returns table (connection_id uuid, token_type text, key_version smallint)
language sql
stable
security definer
set search_path = ''
as $$
    select o.connection_id, o.token_type, o.key_version
    from growthpulse_secrets.oauth_credentials as o
    join growthpulse.provider_connections as c on c.connection_id = o.connection_id
    where o.key_version <> p_active_key_version
        and c.workspace_id = growthpulse_auth.context_workspace()
    order by o.connection_id, o.token_type;
$$;


-- Internal: the organization an owner action's target belongs to, or null.
create function growthpulse_auth.target_workspace(p_target_type text, p_target_id text)
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
    v_id uuid;
    v_workspace_id uuid;
begin
    begin
        v_id := p_target_id::uuid;
    exception when invalid_text_representation then
        return null;
    end;
    if p_target_type = 'client' then
        select c.workspace_id into v_workspace_id
        from growthpulse.clients as c where c.client_id = v_id;
    elsif p_target_type in ('ad_account', 'sync_config') then
        select c.workspace_id into v_workspace_id
        from growthpulse.ad_accounts as a
        join growthpulse.clients as c on c.client_id = a.client_id
        where a.ad_account_id = v_id;
    elsif p_target_type = 'connection' then
        select c.workspace_id into v_workspace_id
        from growthpulse.provider_connections as c where c.connection_id = v_id;
    elsif p_target_type = 'experiment_proposal' then
        select c.workspace_id into v_workspace_id
        from growthpulse.experiment_proposals as p
        join growthpulse.clients as c on c.client_id = p.client_id
        where p.proposal_id = v_id;
    end if;
    return v_workspace_id;
end;
$$;


-- Internal: the permission each owner action needs.
create function growthpulse_auth.action_permission(p_action text)
returns text
language sql
immutable
set search_path = ''
as $$
    select case
        when p_action in ('create_client', 'rename_client', 'archive_client') then 'client.manage'
        when p_action = 'create_connection' then 'connection.create'
        when p_action = 'revoke_connection' then 'connection.revoke'
        when p_action in (
            'add_ad_account', 'add_synthetic_ad_account', 'set_sync_config', 'request_sync'
        ) or p_action like 'set_account_status\_%' then 'sync.configure'
        when p_action like 'decide_experiment\_%' then 'experiment.approve'
    end;
$$;


-- Every owner action ends here, inside its own transaction. Refusing rolls it all back.
-- Not SECURITY DEFINER itself: it is only ever called by the definer functions above it,
-- and EXECUTE is granted to no role.
create or replace function growthpulse.log_owner_action(
    p_action text, p_target_type text, p_target_id text
)
returns void
language plpgsql
set search_path = ''
as $$
declare
    v_context record;
    v_permission text := growthpulse_auth.action_permission(p_action);
begin
    select * into v_context from growthpulse_auth.current_context() as c where c.kind = 'request';
    if v_context is null or v_permission is null
        or growthpulse_auth.target_workspace(p_target_type, p_target_id)
            is distinct from v_context.workspace_id
        or not growthpulse_auth.request_permits(v_permission) then
        raise exception 'Not permitted in this organization' using errcode = 'insufficient_privilege';
    end if;
    insert into growthpulse.owner_action_log (action, target_type, target_id)
    values (p_action, p_target_type, p_target_id);
    insert into growthpulse_auth.audit_events (
        workspace_id, actor_user_id, action, target_type, target_id, request_id
    ) values (
        v_context.workspace_id, v_context.user_id, p_action, p_target_type, p_target_id,
        v_context.ref_id
    );
end;
$$;


revoke all on all functions in schema growthpulse from public;
revoke all on all functions in schema growthpulse_auth from public;
revoke all on all functions in schema growthpulse_secrets from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on all functions in schema growthpulse from %I', api_role);
            execute format(
                'revoke all on all functions in schema growthpulse_auth from %I', api_role
            );
            execute format(
                'revoke all on all functions in schema growthpulse_secrets from %I', api_role
            );
        end if;
    end loop;
end
$$;
