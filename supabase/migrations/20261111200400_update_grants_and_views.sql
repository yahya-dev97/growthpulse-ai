-- GrowthPulse AI, migration 8: pipeline grants on the new tables, and dashboard views
-- scoped by client and account.
--
-- Milestone 5 design: MILESTONE_5_DESIGN.md, section 6.
--
-- growthpulse_writer gains:
--   - SELECT on the client, account, connection and sync tables (connection status only;
--     it has no access to growthpulse_secrets);
--   - INSERT and named-column UPDATE on discovered accounts and sync state;
--   - INSERT on report breakdowns; UPDATE of pipeline_runs.data_quality.
-- It still cannot create, rename or archive clients or accounts, change sync settings,
-- approve experiments, read tokens or the owner action log, or delete anything.
--
-- growthpulse_dashboard keeps SELECT on growthpulse_read views only. Existing views keep
-- their columns and meaning; scope columns are appended. New views never expose tokens:
-- connection credentials appear as a count and an expiry time only.

grant select on growthpulse.workspaces to growthpulse_writer;
grant select on growthpulse.clients to growthpulse_writer;
grant select on growthpulse.ad_accounts to growthpulse_writer;
grant select on growthpulse.provider_connections to growthpulse_writer;
grant select on growthpulse.account_sync_config to growthpulse_writer;

grant select, insert on growthpulse.discovered_ad_accounts to growthpulse_writer;
grant update (display_name, currency, timezone, is_manager, accessible, last_seen_at)
    on growthpulse.discovered_ad_accounts to growthpulse_writer;

grant select on growthpulse.account_sync_state to growthpulse_writer;
grant update (
    status, synced_through, last_success_at, last_attempt_at, next_due_at,
    consecutive_failures, last_error_code, lease_run_id, lease_expires_at
) on growthpulse.account_sync_state to growthpulse_writer;

grant select, insert on growthpulse.analytics_report_breakdowns to growthpulse_writer;
grant update (data_quality) on growthpulse.pipeline_runs to growthpulse_writer;


create policy writer_select on growthpulse.workspaces
    for select to growthpulse_writer using (true);
create policy writer_select on growthpulse.clients
    for select to growthpulse_writer using (true);
create policy writer_select on growthpulse.ad_accounts
    for select to growthpulse_writer using (true);
create policy writer_select on growthpulse.provider_connections
    for select to growthpulse_writer using (true);
create policy writer_select on growthpulse.account_sync_config
    for select to growthpulse_writer using (true);

create policy writer_select on growthpulse.discovered_ad_accounts
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.discovered_ad_accounts
    for insert to growthpulse_writer with check (true);
create policy writer_update on growthpulse.discovered_ad_accounts
    for update to growthpulse_writer using (true) with check (true);

-- The pipeline never pauses an account and never touches a paused one: pausing and
-- resuming are owner decisions.
create policy writer_select on growthpulse.account_sync_state
    for select to growthpulse_writer using (true);
create policy writer_update_unpaused on growthpulse.account_sync_state
    for update to growthpulse_writer using (status <> 'paused') with check (status <> 'paused');

create policy writer_select on growthpulse.analytics_report_breakdowns
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.analytics_report_breakdowns
    for insert to growthpulse_writer with check (true);


-- Existing views: same columns in the same order, scope columns appended.

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


-- Unchanged meaning: the latest report per data source across all clients. Use
-- latest_client_reports for one client's latest report.
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


-- New views.

create view growthpulse_read.latest_client_reports with (security_invoker = false) as
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


create view growthpulse_read.report_breakdowns with (security_invoker = false) as
select report_id, client_id, breakdown_id, breakdown_schema_version, breakdown, created_at
from growthpulse.analytics_report_breakdowns;


create view growthpulse_read.clients with (security_invoker = false) as
select client_id, workspace_id, name, data_source, status, created_at, archived_at
from growthpulse.clients;


create view growthpulse_read.ad_accounts with (security_invoker = false) as
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


-- Connection status for the dashboard. The provider subject ID is left out, and stored
-- credentials appear only as a count and the earliest expiry. No ciphertext, nonce or key
-- version is reachable from here.
create view growthpulse_read.provider_connections with (security_invoker = false) as
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


create view growthpulse_read.discovered_ad_accounts with (security_invoker = false) as
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


create view growthpulse_read.account_sync_status with (security_invoker = false) as
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


create view growthpulse_read.owner_actions with (security_invoker = false) as
select action_id, action, target_type, target_id, actor, acted_at
from growthpulse.owner_action_log;


-- Grants. Revoke from the Supabase API roles first, in case of default privileges.
do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on all tables in schema growthpulse from %I', api_role);
            execute format(
                'revoke all on all tables in schema growthpulse_read from %I', api_role
            );
        end if;
    end loop;
end
$$;
revoke all on all tables in schema growthpulse from public;
revoke all on all tables in schema growthpulse_read from public;

grant select on all tables in schema growthpulse_read to growthpulse_dashboard;
