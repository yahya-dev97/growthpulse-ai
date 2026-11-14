-- GrowthPulse AI: revert migrations 9 to 12 (Milestone 7a.2, tenant security foundation).
--
-- Returns the database to the exact Milestone 5 state (migrations 1 to 8): the writer
-- policies, dashboard views, token functions and owner-action log function get their
-- Milestone 5 text back, and everything migrations 9 to 12 created is dropped. CI applies
-- migrations 9 to 12 and this file, and compares a catalog snapshot with the one taken
-- before.
--
-- WARNING, destructive: this drops growthpulse_auth with every user, membership, client
-- grant, session and audit event in it. Those rows cannot be recovered from this database
-- afterwards; take a dump first. Removing tenant isolation is also a security downgrade:
-- with more than one organization, Milestone 5 policies let every app role see every
-- organization. The file therefore refuses to run when any identity row exists, unless
-- the operator has first run, in the same psql session:
--     set growthpulse.allow_identity_data_loss = 'yes';
--
-- Drops are explicit and restrict-only (no CASCADE), so anything unexpected that depends
-- on these objects makes the revert fail instead of silently removing it. One transaction:
-- any failure undoes all of it.
--
-- Afterwards, mark the migrations as reverted so the CLI history matches the database:
--   supabase migration repair --status reverted \
--       20261114100000 20261114100100 20261114100200 20261114100300

begin;

-- preconditions:begin
do $$
begin
    if coalesce(pg_catalog.current_setting('growthpulse.allow_identity_data_loss', true), '')
        <> 'yes'
        and (
            exists (select from growthpulse_auth.users)
            or exists (select from growthpulse_auth.memberships)
            or exists (select from growthpulse_auth.sessions)
            or exists (select from growthpulse_auth.client_grants)
            or exists (select from growthpulse_auth.audit_events)
        ) then
        raise exception 'Revert refused: identity rows exist (see the header)'
            using errcode = 'check_violation';
    end if;
end
$$;
-- preconditions:end


-- Writer policies, as migrations 2 and 8 created them.
alter policy writer_select on growthpulse.pipeline_runs using (true);
alter policy writer_insert on growthpulse.pipeline_runs with check (status = 'running');
alter policy writer_update on growthpulse.pipeline_runs
    using (status = 'running') with check (true);
alter policy writer_select on growthpulse.ad_performance_records using (true);
alter policy writer_insert on growthpulse.ad_performance_records with check (true);
alter policy writer_update on growthpulse.ad_performance_records using (true) with check (true);
alter policy writer_select on growthpulse.quarantined_records using (true);
alter policy writer_insert on growthpulse.quarantined_records with check (true);
alter policy writer_update on growthpulse.quarantined_records using (true) with check (true);
alter policy writer_select on growthpulse.analytics_reports using (true);
alter policy writer_insert on growthpulse.analytics_reports with check (true);
alter policy writer_select on growthpulse.findings using (true);
alter policy writer_insert on growthpulse.findings with check (true);
alter policy writer_update on growthpulse.findings using (true) with check (true);
alter policy writer_select on growthpulse.experiment_proposals using (true);
alter policy writer_insert on growthpulse.experiment_proposals
    with check (requires_human_approval);
alter policy writer_select on growthpulse.experiment_status_events using (true);
alter policy writer_propose_only on growthpulse.experiment_status_events
    with check (status = 'proposed' and actor = 'pipeline');
alter policy writer_select on growthpulse.workspaces using (true);
alter policy writer_select on growthpulse.clients using (true);
alter policy writer_select on growthpulse.ad_accounts using (true);
alter policy writer_select on growthpulse.provider_connections using (true);
alter policy writer_select on growthpulse.account_sync_config using (true);
alter policy writer_select on growthpulse.discovered_ad_accounts using (true);
alter policy writer_insert on growthpulse.discovered_ad_accounts with check (true);
alter policy writer_update on growthpulse.discovered_ad_accounts using (true) with check (true);
alter policy writer_select on growthpulse.account_sync_state using (true);
alter policy writer_update_unpaused on growthpulse.account_sync_state
    using (status <> 'paused') with check (status <> 'paused');
alter policy writer_select on growthpulse.analytics_report_breakdowns using (true);
alter policy writer_insert on growthpulse.analytics_report_breakdowns with check (true);


-- Views, functions: the exact Milestone 5 text (migrations 3, 6, 7 and 8).
drop view growthpulse_read.audit_events;

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
    order by o.token_type;
$$;


create or replace function growthpulse_secrets.delete_credentials(p_connection_id uuid)
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


create or replace function growthpulse_secrets.mark_connection_needs_reauth(p_connection_id uuid)
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


create or replace function growthpulse_secrets.credentials_needing_rotation(p_active_key_version smallint)
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


create or replace function growthpulse.log_owner_action(
    p_action text, p_target_type text, p_target_id text
)
returns void
language sql
set search_path = ''
as $$
    insert into growthpulse.owner_action_log (action, target_type, target_id)
    values (p_action, p_target_type, p_target_id);
$$;


create or replace view growthpulse_read.pipeline_runs with (security_invoker = false) as
select
    run_id,
    started_at,
    finished_at,
    status,
    data_source,
    triggered_by,
    code_version,
    counts,
    error_code,
    report_id,
    (status = 'running' and started_at < now() - interval '6 hours') as appears_abandoned,
    run_kind,
    client_id,
    ad_account_id,
    window_start,
    window_end,
    attempt,
    retry_of_run_id,
    data_quality
from growthpulse.pipeline_runs;


alter view growthpulse_read.pipeline_runs reset (security_barrier);


create or replace view growthpulse_read.campaign_daily_conversions
with (security_invoker = false) as
select
    r.record_id,
    r.data_source,
    r.reporting_date,
    r.platform,
    r.account_id,
    r.campaign_id,
    r.campaign_name,
    r.campaign_type,
    r.currency,
    r.account_timezone,
    r.conversion_event_id,
    r.conversion_event_name,
    r.conversion_stage,
    r.attribution_model,
    r.click_window_days,
    r.view_window_days,
    r.report_time_basis,
    r.conversions as platform_attributed_conversions,
    r.conversion_value as platform_attributed_conversion_value,
    r.ingested_at,
    r.updated_at,
    r.ad_account_id,
    a.client_id
from growthpulse.ad_performance_records as r
join growthpulse.ad_accounts as a on a.ad_account_id = r.ad_account_id;


alter view growthpulse_read.campaign_daily_conversions reset (security_barrier);


create or replace view growthpulse_read.campaign_daily_delivery
with (security_invoker = false) as
select distinct on (r.data_source, r.reporting_date, r.platform, r.account_id, r.campaign_id)
    r.data_source,
    r.reporting_date,
    r.platform,
    r.account_id,
    r.campaign_id,
    r.campaign_name,
    r.campaign_type,
    r.currency,
    r.account_timezone,
    r.impressions,
    r.clicks,
    r.spend,
    r.updated_at,
    r.ad_account_id,
    a.client_id
from growthpulse.ad_performance_records as r
join growthpulse.ad_accounts as a on a.ad_account_id = r.ad_account_id
order by r.data_source, r.reporting_date, r.platform, r.account_id, r.campaign_id,
    r.updated_at desc;


alter view growthpulse_read.campaign_daily_delivery reset (security_barrier);


create or replace view growthpulse_read.analytics_reports with (security_invoker = false) as
select
    report_id,
    data_source,
    report_schema_version,
    data_as_of,
    period_start,
    period_end,
    record_count,
    created_at,
    report,
    client_id
from growthpulse.analytics_reports;


alter view growthpulse_read.analytics_reports reset (security_barrier);


create or replace view growthpulse_read.latest_reports with (security_invoker = false) as
select distinct on (data_source)
    report_id,
    data_source,
    report_schema_version,
    data_as_of,
    period_start,
    period_end,
    record_count,
    created_at,
    report,
    client_id
from growthpulse.analytics_reports
order by data_source, data_as_of desc, created_at desc;


alter view growthpulse_read.latest_reports reset (security_barrier);


create or replace view growthpulse_read.findings with (security_invoker = false) as
select
    finding_id,
    data_source,
    rule_id,
    severity,
    pre_guard_severity,
    direction,
    guard_outcome,
    is_suppressed,
    platform,
    account_id,
    campaign_id,
    basis_id,
    metric,
    comparison_label,
    comparison_purpose,
    baseline_start,
    baseline_end,
    current_start,
    current_end,
    percent_change,
    payload,
    latest_report_id,
    latest_data_as_of,
    first_seen_at,
    last_seen_at,
    client_id,
    ad_account_id
from growthpulse.findings;


alter view growthpulse_read.findings reset (security_barrier);


create or replace view growthpulse_read.data_quality_issues with (security_invoker = false) as
select
    quarantine_id,
    data_source,
    connector,
    row_index,
    issue_codes,
    issues,
    first_seen_at,
    last_seen_at,
    ad_account_id
from growthpulse.quarantined_records;


alter view growthpulse_read.data_quality_issues reset (security_barrier);


create or replace view growthpulse_read.experiment_proposals with (security_invoker = false) as
select
    p.proposal_id,
    p.data_source,
    p.report_id,
    p.finding_id,
    p.group_id,
    p.title,
    p.hypothesis,
    p.proposed_change,
    p.primary_metric,
    p.requires_human_approval,
    p.created_at,
    latest.status as current_status,
    latest.changed_at as status_changed_at,
    p.client_id
from growthpulse.experiment_proposals as p
left join lateral (
    select e.status, e.changed_at
    from growthpulse.experiment_status_events as e
    where e.proposal_id = p.proposal_id
    order by e.sequence desc
    limit 1
) as latest on true;


alter view growthpulse_read.experiment_proposals reset (security_barrier);


create or replace view growthpulse_read.latest_client_reports with (security_invoker = false) as
select distinct on (client_id, data_source)
    client_id,
    report_id,
    data_source,
    report_schema_version,
    data_as_of,
    period_start,
    period_end,
    record_count,
    created_at,
    report
from growthpulse.analytics_reports
order by client_id, data_source, data_as_of desc, created_at desc;


alter view growthpulse_read.latest_client_reports reset (security_barrier);


create or replace view growthpulse_read.report_breakdowns with (security_invoker = false) as
select report_id, client_id, breakdown_id, breakdown_schema_version, breakdown, created_at
from growthpulse.analytics_report_breakdowns;


alter view growthpulse_read.report_breakdowns reset (security_barrier);


create or replace view growthpulse_read.clients with (security_invoker = false) as
select client_id, workspace_id, name, data_source, status, created_at, archived_at
from growthpulse.clients;


alter view growthpulse_read.clients reset (security_barrier);


create or replace view growthpulse_read.ad_accounts with (security_invoker = false) as
select
    ad_account_id,
    client_id,
    data_source,
    platform,
    external_account_id,
    display_name,
    currency,
    timezone,
    sync_connection_id,
    status,
    created_at,
    archived_at
from growthpulse.ad_accounts;


alter view growthpulse_read.ad_accounts reset (security_barrier);


create or replace view growthpulse_read.provider_connections with (security_invoker = false) as
select
    c.connection_id,
    c.workspace_id,
    c.provider,
    c.status,
    c.granted_scopes,
    c.created_at,
    c.last_validated_at,
    c.revoked_at,
    coalesce(credentials.stored, 0) as stored_credentials,
    credentials.earliest_expiry as credential_expires_at
from growthpulse.provider_connections as c
left join lateral (
    select count(*) as stored, min(o.expires_at) as earliest_expiry
    from growthpulse_secrets.oauth_credentials as o
    where o.connection_id = c.connection_id
) as credentials on true;


alter view growthpulse_read.provider_connections reset (security_barrier);


create or replace view growthpulse_read.discovered_ad_accounts with (security_invoker = false) as
select
    connection_id,
    platform,
    external_account_id,
    display_name,
    currency,
    timezone,
    is_manager,
    accessible,
    first_seen_at,
    last_seen_at
from growthpulse.discovered_ad_accounts;


alter view growthpulse_read.discovered_ad_accounts reset (security_barrier);


create or replace view growthpulse_read.account_sync_status with (security_invoker = false) as
select
    a.ad_account_id,
    a.client_id,
    a.platform,
    a.status as account_status,
    c.enabled,
    c.frequency,
    c.preferred_local_hour,
    c.lookback_days,
    c.backfill_start_date,
    c.max_consecutive_failures,
    s.status as sync_status,
    s.synced_through,
    s.last_success_at,
    s.last_attempt_at,
    s.next_due_at,
    s.consecutive_failures,
    s.last_error_code,
    (s.lease_expires_at is not null and s.lease_expires_at < now()) as lease_expired
from growthpulse.ad_accounts as a
left join growthpulse.account_sync_config as c on c.ad_account_id = a.ad_account_id
left join growthpulse.account_sync_state as s on s.ad_account_id = a.ad_account_id;


alter view growthpulse_read.account_sync_status reset (security_barrier);


create or replace view growthpulse_read.owner_actions with (security_invoker = false) as
select action_id, action, target_type, target_id, actor, acted_at
from growthpulse.owner_action_log;


alter view growthpulse_read.owner_actions reset (security_barrier);


create or replace view growthpulse_read.experiment_status_history with (security_invoker = false) as
select
    event_id,
    proposal_id,
    sequence,
    status,
    previous_status,
    actor,
    note,
    changed_at
from growthpulse.experiment_status_events;


alter view growthpulse_read.experiment_status_history reset (security_barrier);


-- Everything migrations 9 to 12 created, in dependency order.
drop trigger workspaces_create_organization on growthpulse.workspaces;
drop trigger clients_check_organization on growthpulse.clients;

drop function growthpulse_auth.request_owner_action_ids();
drop function growthpulse_auth.require_credential_permission(uuid, text);
drop function growthpulse_auth.credential_permits(uuid, text);
drop function growthpulse_auth.action_permission(text);
drop function growthpulse_auth.target_workspace(text, text);
drop function growthpulse_auth.due_account_syncs(timestamptz);
drop function growthpulse_auth.revoke_session(bytea);
drop function growthpulse_auth.create_session(uuid, bytea, timestamptz);
drop function growthpulse_auth.request_workspace_if(text);
drop function growthpulse_auth.request_permits(text);
drop function growthpulse_auth.request_client_ids();
drop function growthpulse_auth.job_account_ids();
drop function growthpulse_auth.job_client_ids();
drop function growthpulse_auth.job_workspace();
drop function growthpulse_auth.context_workspace();
drop function growthpulse_auth.begin_job(text, uuid, uuid);
drop function growthpulse_auth.begin_request(bytea, uuid, uuid);
drop function growthpulse_auth.role_permits(text, text);
drop function growthpulse_auth.current_context();
drop function growthpulse_auth.set_context(text, uuid, uuid, uuid, text, uuid, uuid, text, uuid);
drop function growthpulse_auth.context_mac(text);
drop table growthpulse_auth.context_keys;

drop table growthpulse_auth.audit_events;
drop table growthpulse_auth.sessions;
drop table growthpulse_auth.client_grants;
drop table growthpulse_auth.memberships;
drop table growthpulse_auth.users;
drop table growthpulse_auth.organizations;
drop function growthpulse_auth.check_client_grant();
drop function growthpulse_auth.check_membership_update();
drop function growthpulse_auth.check_client_organization();
drop function growthpulse_auth.check_organization_update();
drop function growthpulse_auth.create_organization_for_workspace();

alter table growthpulse.clients drop constraint clients_client_workspace_unique;

revoke all on schema growthpulse_auth from
    growthpulse_authenticator, growthpulse_dashboard, growthpulse_manager,
    growthpulse_token_manager, growthpulse_writer;
drop schema growthpulse_auth;
drop role growthpulse_authenticator;

commit;
