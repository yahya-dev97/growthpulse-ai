-- Reverts only the five Milestone 5 migrations (20261111200000 to 20261111200400) and
-- returns the database to the exact Milestone 4 schema, keeping every Milestone 4 row:
-- runs, records, quarantined rows, reports, findings, proposals and status events.
--
-- DESTRUCTIVE for Milestone 5 data: clients, accounts, connections, sync state, report
-- breakdowns, the owner action log and every stored (encrypted) credential are deleted.
-- Never run this without explicit approval.
--
-- Kept outside supabase/migrations so `supabase db push` can never run it. Run it by hand,
-- as the project owner (postgres), only after reviewing it. Afterwards, mark the five
-- migrations as reverted so the CLI history matches the database:
--
--   supabase migration repair --status reverted \
--       20261111200000 20261111200100 20261111200200 20261111200300 20261111200400
--
-- Every drop is by name with RESTRICT, never CASCADE: anything unexpected that depends on
-- these objects makes the whole transaction fail and roll back, leaving everything in
-- place. The eight dashboard views Milestone 5 extended are recreated exactly as
-- migration 3 defined them; a test compares the result with a pristine Milestone 4
-- database.

begin;

drop view if exists
    growthpulse_read.latest_client_reports,
    growthpulse_read.report_breakdowns,
    growthpulse_read.clients,
    growthpulse_read.ad_accounts,
    growthpulse_read.provider_connections,
    growthpulse_read.discovered_ad_accounts,
    growthpulse_read.account_sync_status,
    growthpulse_read.owner_actions,
    growthpulse_read.pipeline_runs,
    growthpulse_read.campaign_daily_conversions,
    growthpulse_read.campaign_daily_delivery,
    growthpulse_read.analytics_reports,
    growthpulse_read.latest_reports,
    growthpulse_read.findings,
    growthpulse_read.data_quality_issues,
    growthpulse_read.experiment_proposals
    restrict;

drop function if exists
    growthpulse_secrets.store_credential(uuid, text, smallint, smallint, bytea, bytea, timestamptz),
    growthpulse_secrets.read_credentials(uuid),
    growthpulse_secrets.delete_credentials(uuid),
    growthpulse_secrets.mark_connection_needs_reauth(uuid),
    growthpulse_secrets.credentials_needing_rotation(smallint)
    restrict;
drop table if exists growthpulse_secrets.oauth_credentials restrict;
drop schema if exists growthpulse_secrets restrict;

drop function if exists
    growthpulse.create_client(uuid, text, text),
    growthpulse.rename_client(uuid, text),
    growthpulse.archive_client(uuid),
    growthpulse.create_connection(uuid, text, text),
    growthpulse.revoke_connection(uuid),
    growthpulse.add_ad_account(uuid, uuid, text, date),
    growthpulse.add_synthetic_ad_account(uuid, text, text, text, text, date),
    growthpulse.set_account_status(uuid, text),
    growthpulse.set_sync_config(uuid, boolean, text, smallint, smallint, smallint),
    growthpulse.request_sync(uuid),
    growthpulse.decide_experiment(uuid, text, text),
    growthpulse.start_account_sync_tracking(uuid, text, date),
    growthpulse.log_owner_action(text, text, text)
    restrict;

drop trigger if exists ad_performance_records_1_derive_scope
    on growthpulse.ad_performance_records restrict;
drop trigger if exists ad_performance_records_2_check_run_scope
    on growthpulse.ad_performance_records restrict;
drop trigger if exists analytics_reports_derive_scope on growthpulse.analytics_reports restrict;
drop trigger if exists findings_derive_scope on growthpulse.findings restrict;
drop trigger if exists experiment_proposals_derive_scope
    on growthpulse.experiment_proposals restrict;

drop table if exists
    growthpulse.analytics_report_breakdowns,
    growthpulse.owner_action_log,
    growthpulse.account_sync_state,
    growthpulse.account_sync_config
    restrict;

-- Scope columns. Dropping a column also drops its own indexes and constraints. The
-- foreign keys between runs and reports reference each other, so they go first.
alter table growthpulse.experiment_proposals drop column if exists client_id restrict;
alter table growthpulse.findings
    drop column if exists client_id restrict,
    drop column if exists ad_account_id restrict;
alter table growthpulse.quarantined_records drop column if exists ad_account_id restrict;
alter table growthpulse.ad_performance_records drop column if exists ad_account_id restrict;
alter table growthpulse.pipeline_runs
    drop constraint if exists pipeline_runs_report_client_fkey restrict;
alter table growthpulse.analytics_reports
    drop constraint if exists analytics_reports_run_client_fkey restrict;
alter table growthpulse.analytics_reports drop column if exists client_id restrict;
alter table growthpulse.pipeline_runs
    drop column if exists retry_of_run_id restrict,
    drop column if exists data_quality restrict,
    drop column if exists attempt restrict,
    drop column if exists window_end restrict,
    drop column if exists window_start restrict,
    drop column if exists ad_account_id restrict,
    drop column if exists client_id restrict,
    drop column if exists run_kind restrict;

drop table if exists
    growthpulse.ad_accounts,
    growthpulse.discovered_ad_accounts,
    growthpulse.provider_connections,
    growthpulse.clients,
    growthpulse.workspaces
    restrict;

drop function if exists
    growthpulse.derive_scope(),
    growthpulse.check_record_run_scope(),
    growthpulse.check_client_update(),
    growthpulse.check_ad_account_update(),
    growthpulse.check_connection_update()
    restrict;

-- The Milestone 4 dashboard views, exactly as migration 3 created them.
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

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
            execute format(
                'revoke all on all tables in schema growthpulse_read from %I', api_role
            );
        end if;
    end loop;
end
$$;
revoke all on all tables in schema growthpulse_read from public;
grant select on all tables in schema growthpulse_read to growthpulse_dashboard;

-- Fails, and rolls everything back, if either role still holds a privilege or owns an
-- object anywhere else in the database.
do $$
begin
    if exists (select from pg_catalog.pg_roles where rolname = 'growthpulse_manager') then
        revoke usage on schema growthpulse from growthpulse_manager;
    end if;
end
$$;
drop role if exists growthpulse_manager;
drop role if exists growthpulse_token_manager;

commit;
