-- GrowthPulse AI, migration 3 of 3: read-only views for the dashboard.
--
-- The dashboard role can use this schema and nothing else. The views run with their
-- owner's rights (security_invoker = false), which is what lets the dashboard read them
-- without any grant on the base tables. That is safe here because:
--   - this schema is not exposed through the Data API, and no API role can use it;
--   - each view leaves out what the dashboard does not need, such as raw quarantined
--     payloads and internal run references;
--   - growthpulse_dashboard has SELECT only, so these views cannot be written through.

create schema growthpulse_read;
comment on schema growthpulse_read is
    'GrowthPulse AI dashboard views. Private: never add this schema to the Data API.';
revoke all on schema growthpulse_read from public;


create view growthpulse_read.pipeline_runs with (security_invoker = false) as
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
    -- A run whose failure could not be recorded, for example because the database was
    -- unreachable, stays "running". Flag those instead of trusting the status.
    (status = 'running' and started_at < now() - interval '6 hours') as appears_abandoned
from growthpulse.pipeline_runs;


-- One row per campaign, day and conversion definition. Conversions are platform-attributed
-- and must not be summed across conversion definitions or attribution settings.
create view growthpulse_read.campaign_daily_conversions with (security_invoker = false) as
select
    record_id,
    data_source,
    reporting_date,
    platform,
    account_id,
    campaign_id,
    campaign_name,
    campaign_type,
    currency,
    account_timezone,
    conversion_event_id,
    conversion_event_name,
    conversion_stage,
    attribution_model,
    click_window_days,
    view_window_days,
    report_time_basis,
    conversions as platform_attributed_conversions,
    conversion_value as platform_attributed_conversion_value,
    ingested_at,
    updated_at
from growthpulse.ad_performance_records;


-- One row per campaign and day. Delivery metrics repeat on every conversion row for the
-- same campaign-day, so they are de-duplicated here to avoid double counting spend.
create view growthpulse_read.campaign_daily_delivery with (security_invoker = false) as
select distinct on (data_source, reporting_date, platform, account_id, campaign_id)
    data_source,
    reporting_date,
    platform,
    account_id,
    campaign_id,
    campaign_name,
    campaign_type,
    currency,
    account_timezone,
    impressions,
    clicks,
    spend,
    updated_at
from growthpulse.ad_performance_records
order by data_source, reporting_date, platform, account_id, campaign_id, updated_at desc;


create view growthpulse_read.analytics_reports with (security_invoker = false) as
select
    report_id,
    data_source,
    report_schema_version,
    data_as_of,
    period_start,
    period_end,
    record_count,
    created_at,
    report
from growthpulse.analytics_reports;


create view growthpulse_read.latest_reports with (security_invoker = false) as
select distinct on (data_source)
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
order by data_source, data_as_of desc, created_at desc;


create view growthpulse_read.findings with (security_invoker = false) as
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
    last_seen_at
from growthpulse.findings;


-- Issue codes only. Raw payloads stay in the base table, out of the dashboard's reach.
create view growthpulse_read.data_quality_issues with (security_invoker = false) as
select
    quarantine_id,
    data_source,
    connector,
    row_index,
    issue_codes,
    issues,
    first_seen_at,
    last_seen_at
from growthpulse.quarantined_records;


create view growthpulse_read.experiment_proposals with (security_invoker = false) as
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
    latest.changed_at as status_changed_at
from growthpulse.experiment_proposals as p
left join lateral (
    select e.status, e.changed_at
    from growthpulse.experiment_status_events as e
    where e.proposal_id = p.proposal_id
    order by e.sequence desc
    limit 1
) as latest on true;


create view growthpulse_read.experiment_status_history with (security_invoker = false) as
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


-- Grants. Revoke from the Supabase API roles first, in case of default privileges.
do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format('revoke all on schema growthpulse_read from %I', api_role);
            execute format(
                'revoke all on all tables in schema growthpulse_read from %I', api_role
            );
        end if;
    end loop;
end
$$;
revoke all on all tables in schema growthpulse_read from public;

grant usage on schema growthpulse_read to growthpulse_dashboard;
grant select on all tables in schema growthpulse_read to growthpulse_dashboard;
