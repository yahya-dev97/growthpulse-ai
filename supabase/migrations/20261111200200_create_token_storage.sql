-- GrowthPulse AI, migration 6: encrypted OAuth token storage and its restricted role.
--
-- Milestone 5 design: MILESTONE_5_DESIGN.md, sections 6 and 13.4.
--
-- growthpulse_token_manager  Created without LOGIN. Can only execute the functions below.
--                            Cannot read clients, accounts, records, reports or findings.
--
-- Tokens are encrypted by the application (AES-256-GCM) before they reach this schema, and
-- the key never enters the database. This schema holds ciphertext, nonce, key version and
-- expiry only. No other role, including the pipeline and dashboard roles, can use it.
-- The functions run with the owner's rights (SECURITY DEFINER), pin search_path to '' and
-- qualify every name, and EXECUTE on each is granted to the token manager only.

create role growthpulse_token_manager nologin noinherit;
comment on role growthpulse_token_manager is
    'GrowthPulse AI token storage. EXECUTE on growthpulse_secrets functions only.';
alter role growthpulse_token_manager connection limit 2;
alter role growthpulse_token_manager set search_path = growthpulse_secrets;
alter role growthpulse_token_manager set statement_timeout = '15s';
alter role growthpulse_token_manager set idle_in_transaction_session_timeout = '30s';


create schema growthpulse_secrets;
comment on schema growthpulse_secrets is
    'GrowthPulse AI encrypted OAuth credentials. Private: never add to the Data API.';
revoke all on schema growthpulse_secrets from public;


create table growthpulse_secrets.oauth_credentials (
    connection_id uuid not null
        references growthpulse.provider_connections (connection_id),
    token_type text not null check (token_type in ('refresh', 'long_lived_access')),
    format_version smallint not null check (format_version = 1),
    key_version smallint not null check (key_version between 1 and 32767),
    nonce bytea not null check (octet_length(nonce) = 12),
    -- At least one byte of token plus the 16-byte GCM tag; at most 8192 bytes of token.
    ciphertext bytea not null check (octet_length(ciphertext) between 17 and 8208),
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (connection_id, token_type),
    -- A nonce is never reused under the same key.
    constraint oauth_credentials_nonce_unique unique (key_version, nonce)
);
-- RLS with no policy: only the owner, through the functions below, reaches any row.
alter table growthpulse_secrets.oauth_credentials enable row level security;


-- Store or replace one encrypted credential. Storing a credential for a pending or
-- needs_reauth connection activates it. A revoked connection accepts nothing.
create function growthpulse_secrets.store_credential(
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
begin
    select c.status into v_status
    from growthpulse.provider_connections as c
    where c.connection_id = p_connection_id
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
end;
$$;


-- Encrypted credentials of an active connection. Nothing for any other status.
create function growthpulse_secrets.read_credentials(p_connection_id uuid)
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
    order by o.token_type;
$$;


create function growthpulse_secrets.delete_credentials(p_connection_id uuid)
returns integer
language sql
security definer
set search_path = ''
as $$
    with deleted as (
        delete from growthpulse_secrets.oauth_credentials as o
        where o.connection_id = p_connection_id
        returning 1
    )
    select count(*)::integer from deleted;
$$;


-- A refresh failed: the owner must sign in again. Accounts on the connection stop syncing
-- because the dispatcher only picks accounts whose connection is active.
create function growthpulse_secrets.mark_connection_needs_reauth(p_connection_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
    update growthpulse.provider_connections as c
    set status = 'needs_reauth'
    where c.connection_id = p_connection_id and c.status = 'active';
    return found;
end;
$$;


-- What a key rotation still has to re-encrypt. No ciphertext is returned.
create function growthpulse_secrets.credentials_needing_rotation(p_active_key_version smallint)
returns table (connection_id uuid, token_type text, key_version smallint)
language sql
stable
security definer
set search_path = ''
as $$
    select o.connection_id, o.token_type, o.key_version
    from growthpulse_secrets.oauth_credentials as o
    where o.key_version <> p_active_key_version
    order by o.connection_id, o.token_type;
$$;


-- Close everything, then grant the token manager exactly its functions.
revoke all on all tables in schema growthpulse_secrets from public;
revoke all on all functions in schema growthpulse_secrets from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on schema growthpulse_secrets from %I', api_role);
            execute format(
                'revoke all on all tables in schema growthpulse_secrets from %I', api_role
            );
            execute format(
                'revoke all on all functions in schema growthpulse_secrets from %I', api_role
            );
        end if;
    end loop;
end
$$;

grant usage on schema growthpulse_secrets to growthpulse_token_manager;
grant execute on function growthpulse_secrets.store_credential(
    uuid, text, smallint, smallint, bytea, bytea, timestamptz
) to growthpulse_token_manager;
grant execute on function growthpulse_secrets.read_credentials(uuid)
    to growthpulse_token_manager;
grant execute on function growthpulse_secrets.delete_credentials(uuid)
    to growthpulse_token_manager;
grant execute on function growthpulse_secrets.mark_connection_needs_reauth(uuid)
    to growthpulse_token_manager;
grant execute on function growthpulse_secrets.credentials_needing_rotation(smallint)
    to growthpulse_token_manager;
