-- GrowthPulse AI, migration 12: tenant isolation in every writer policy and dashboard view.
--
-- Milestone 7a design: docs/M7A_TENANT_SECURITY.md, sections 3 and 4 (findings 2 and 7).
--
-- Writer (pipeline). Every policy that was "using (true)" now also requires a job context
-- (growthpulse_auth.begin_job) and limits rows to that job's organization, and to its one
-- client when the job is client-scoped. With no job context the writer reads and writes
-- nothing. Policy names, grants and every existing non-tenant condition are unchanged.
--
-- Dashboard views. They still run with the view owner's rights, so the dashboard role
-- needs no grant on any base table and the column choices of migrations 3 and 8 stand.
-- Each view now filters to the clients the signed-in person may read
-- (growthpulse_auth.request_client_ids), or to their organization for organization-level
-- views, and is a security barrier, so no caller-supplied function or operator can see a
-- row before the filter. Without a request context every view returns no rows.
-- Columns and their order are unchanged.


-- Writer policies: Milestone 4 tables (migration 2).

alter policy writer_select on growthpulse.pipeline_runs
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_insert on growthpulse.pipeline_runs
    with check (status = 'running' and client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_update on growthpulse.pipeline_runs
    using (status = 'running' and client_id in (select growthpulse_auth.job_client_ids()))
    with check (client_id in (select growthpulse_auth.job_client_ids()));

alter policy writer_select on growthpulse.ad_performance_records
    using (ad_account_id in (select growthpulse_auth.job_account_ids()));
alter policy writer_insert on growthpulse.ad_performance_records
    with check (ad_account_id in (select growthpulse_auth.job_account_ids()));
alter policy writer_update on growthpulse.ad_performance_records
    using (ad_account_id in (select growthpulse_auth.job_account_ids()))
    with check (ad_account_id in (select growthpulse_auth.job_account_ids()));

-- Quarantined rows of a per-account sync carry their account. Rows of a combined run do
-- not, and belong to the client of the run that last saw them.
alter policy writer_select on growthpulse.quarantined_records
    using (
        ad_account_id in (select growthpulse_auth.job_account_ids())
        or (ad_account_id is null and exists (
            select from growthpulse.pipeline_runs as r where r.run_id = last_seen_run_id
        ))
    );
alter policy writer_insert on growthpulse.quarantined_records
    with check (
        ad_account_id in (select growthpulse_auth.job_account_ids())
        or (ad_account_id is null and exists (
            select from growthpulse.pipeline_runs as r where r.run_id = last_seen_run_id
        ))
    );
alter policy writer_update on growthpulse.quarantined_records
    using (
        ad_account_id in (select growthpulse_auth.job_account_ids())
        or (ad_account_id is null and exists (
            select from growthpulse.pipeline_runs as r where r.run_id = last_seen_run_id
        ))
    )
    with check (
        ad_account_id in (select growthpulse_auth.job_account_ids())
        or (ad_account_id is null and exists (
            select from growthpulse.pipeline_runs as r where r.run_id = last_seen_run_id
        ))
    );

alter policy writer_select on growthpulse.analytics_reports
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_insert on growthpulse.analytics_reports
    with check (client_id in (select growthpulse_auth.job_client_ids()));

alter policy writer_select on growthpulse.findings
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_insert on growthpulse.findings
    with check (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_update on growthpulse.findings
    using (client_id in (select growthpulse_auth.job_client_ids()))
    with check (client_id in (select growthpulse_auth.job_client_ids()));

alter policy writer_select on growthpulse.experiment_proposals
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_insert on growthpulse.experiment_proposals
    with check (
        requires_human_approval and client_id in (select growthpulse_auth.job_client_ids())
    );

-- Status events reach their client through the proposal, which the writer sees only when
-- it is in the job's scope.
alter policy writer_select on growthpulse.experiment_status_events
    using (exists (
        select from growthpulse.experiment_proposals as p where p.proposal_id = experiment_status_events.proposal_id
    ));
alter policy writer_propose_only on growthpulse.experiment_status_events
    with check (
        status = 'proposed' and actor = 'pipeline'
        and exists (
            select from growthpulse.experiment_proposals as p
            where p.proposal_id = experiment_status_events.proposal_id
        )
    );


-- Writer policies: Milestone 5 tables (migration 8).

alter policy writer_select on growthpulse.workspaces
    using (workspace_id = growthpulse_auth.job_workspace());
alter policy writer_select on growthpulse.clients
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_select on growthpulse.ad_accounts
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_select on growthpulse.provider_connections
    using (workspace_id = growthpulse_auth.job_workspace());
alter policy writer_select on growthpulse.account_sync_config
    using (ad_account_id in (select growthpulse_auth.job_account_ids()));

alter policy writer_select on growthpulse.discovered_ad_accounts
    using (exists (
        select from growthpulse.provider_connections as c
        where c.connection_id = discovered_ad_accounts.connection_id
    ));
alter policy writer_insert on growthpulse.discovered_ad_accounts
    with check (exists (
        select from growthpulse.provider_connections as c
        where c.connection_id = discovered_ad_accounts.connection_id
    ));
alter policy writer_update on growthpulse.discovered_ad_accounts
    using (exists (
        select from growthpulse.provider_connections as c
        where c.connection_id = discovered_ad_accounts.connection_id
    ))
    with check (exists (
        select from growthpulse.provider_connections as c
        where c.connection_id = discovered_ad_accounts.connection_id
    ));

alter policy writer_select on growthpulse.account_sync_state
    using (ad_account_id in (select growthpulse_auth.job_account_ids()));
alter policy writer_update_unpaused on growthpulse.account_sync_state
    using (status <> 'paused' and ad_account_id in (select growthpulse_auth.job_account_ids()))
    with check (
        status <> 'paused' and ad_account_id in (select growthpulse_auth.job_account_ids())
    );

alter policy writer_select on growthpulse.analytics_report_breakdowns
    using (client_id in (select growthpulse_auth.job_client_ids()));
alter policy writer_insert on growthpulse.analytics_report_breakdowns
    with check (client_id in (select growthpulse_auth.job_client_ids()));


-- Dashboard views. Bodies are those of migrations 3 and 8 plus one tenant filter each.

create or replace view growthpulse_read.pipeline_runs
with (security_invoker = false, security_barrier = true) as
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
from growthpulse.pipeline_runs
where client_id in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.campaign_daily_conversions
with (security_invoker = false, security_barrier = true) as
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
join growthpulse.ad_accounts as a on a.ad_account_id = r.ad_account_id
where a.client_id in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.campaign_daily_delivery
with (security_invoker = false, security_barrier = true) as
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
where a.client_id in (select growthpulse_auth.request_client_ids())
order by r.data_source, r.reporting_date, r.platform, r.account_id, r.campaign_id,
    r.updated_at desc;


create or replace view growthpulse_read.analytics_reports
with (security_invoker = false, security_barrier = true) as
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
from growthpulse.analytics_reports
where client_id in (select growthpulse_auth.request_client_ids());


-- The latest report per data source among the clients this person may read.
create or replace view growthpulse_read.latest_reports
with (security_invoker = false, security_barrier = true) as
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
where client_id in (select growthpulse_auth.request_client_ids())
order by data_source, data_as_of desc, created_at desc;


create or replace view growthpulse_read.findings
with (security_invoker = false, security_barrier = true) as
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
from growthpulse.findings
where client_id in (select growthpulse_auth.request_client_ids());


-- Quarantined rows: by their account, or by the client of the run that last saw them.
create or replace view growthpulse_read.data_quality_issues
with (security_invoker = false, security_barrier = true) as
select
    q.quarantine_id,
    q.data_source,
    q.connector,
    q.row_index,
    q.issue_codes,
    q.issues,
    q.first_seen_at,
    q.last_seen_at,
    q.ad_account_id
from growthpulse.quarantined_records as q
where coalesce(
    (select a.client_id from growthpulse.ad_accounts as a where a.ad_account_id = q.ad_account_id),
    (select r.client_id from growthpulse.pipeline_runs as r where r.run_id = q.last_seen_run_id)
) in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.experiment_proposals
with (security_invoker = false, security_barrier = true) as
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
) as latest on true
where p.client_id in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.experiment_status_history
with (security_invoker = false, security_barrier = true) as
select
    e.event_id,
    e.proposal_id,
    e.sequence,
    e.status,
    e.previous_status,
    e.actor,
    e.note,
    e.changed_at
from growthpulse.experiment_status_events as e
where (select p.client_id from growthpulse.experiment_proposals as p
       where p.proposal_id = e.proposal_id)
    in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.latest_client_reports
with (security_invoker = false, security_barrier = true) as
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
where client_id in (select growthpulse_auth.request_client_ids())
order by client_id, data_source, data_as_of desc, created_at desc;


create or replace view growthpulse_read.report_breakdowns
with (security_invoker = false, security_barrier = true) as
select report_id, client_id, breakdown_id, breakdown_schema_version, breakdown, created_at
from growthpulse.analytics_report_breakdowns
where client_id in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.clients
with (security_invoker = false, security_barrier = true) as
select client_id, workspace_id, name, data_source, status, created_at, archived_at
from growthpulse.clients
where client_id in (select growthpulse_auth.request_client_ids());


create or replace view growthpulse_read.ad_accounts
with (security_invoker = false, security_barrier = true) as
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
from growthpulse.ad_accounts
where client_id in (select growthpulse_auth.request_client_ids());


-- Organization-level: owners and admins only (permission connection.read).
create or replace view growthpulse_read.provider_connections
with (security_invoker = false, security_barrier = true) as
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
) as credentials on true
where c.workspace_id = growthpulse_auth.request_workspace_if('connection.read');


create or replace view growthpulse_read.discovered_ad_accounts
with (security_invoker = false, security_barrier = true) as
select
    d.connection_id,
    d.platform,
    d.external_account_id,
    d.display_name,
    d.currency,
    d.timezone,
    d.is_manager,
    d.accessible,
    d.first_seen_at,
    d.last_seen_at
from growthpulse.discovered_ad_accounts as d
join growthpulse.provider_connections as c on c.connection_id = d.connection_id
where c.workspace_id = growthpulse_auth.request_workspace_if('connection.read');


create or replace view growthpulse_read.account_sync_status
with (security_invoker = false, security_barrier = true) as
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
left join growthpulse.account_sync_state as s on s.ad_account_id = a.ad_account_id
where a.client_id in (select growthpulse_auth.request_client_ids());


-- The legacy owner log has no organization column: each row is placed by its target.
-- Owners and admins only (permission audit.read). A function of its own, so the dashboard
-- is never granted target_workspace, which would map any ID to its organization.
create function growthpulse_auth.request_owner_action_ids()
returns setof bigint
language sql
stable
security definer
set search_path = ''
as $$
    select l.action_id
    from growthpulse.owner_action_log as l
    where growthpulse_auth.target_workspace(l.target_type, l.target_id)
        = growthpulse_auth.request_workspace_if('audit.read');
$$;
revoke all on function growthpulse_auth.request_owner_action_ids() from public;
grant execute on function growthpulse_auth.request_owner_action_ids() to growthpulse_dashboard;

create or replace view growthpulse_read.owner_actions
with (security_invoker = false, security_barrier = true) as
select l.action_id, l.action, l.target_type, l.target_id, l.actor, l.acted_at
from growthpulse.owner_action_log as l
where l.action_id in (select growthpulse_auth.request_owner_action_ids());


-- New: the organization's audit events, owners and admins only.
create view growthpulse_read.audit_events
with (security_invoker = false, security_barrier = true) as
select
    e.event_id,
    e.workspace_id,
    e.actor_user_id,
    e.actor_service,
    e.action,
    e.target_type,
    e.target_id,
    e.request_id,
    e.occurred_at
from growthpulse_auth.audit_events as e
where e.workspace_id = growthpulse_auth.request_workspace_if('audit.read');


do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format(
                'revoke all on all tables in schema growthpulse_read from %I', api_role
            );
            execute format(
                'revoke all on all functions in schema growthpulse_auth from %I', api_role
            );
        end if;
    end loop;
end
$$;
revoke all on all tables in schema growthpulse_read from public;
grant select on growthpulse_read.audit_events to growthpulse_dashboard;
