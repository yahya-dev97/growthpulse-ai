-- GrowthPulse AI, migration 2 of 3: least-privilege roles, grants and row-level security.
--
-- growthpulse_writer     Used by the pipeline. Reads and writes named tables and columns.
--                        Cannot delete, truncate, change the schema, or edit history.
-- growthpulse_dashboard  Used by Streamlit. SELECT on growthpulse_read views only
--                        (migration 3). No access to the base tables at all.
--
-- Both roles are created without LOGIN and without a password, so this file contains no
-- secret. The project owner enables login and sets each password manually afterwards.
-- CREATE ROLE fails if a role already exists, so an unexpected existing role is never
-- silently adopted.

create role growthpulse_writer nologin noinherit;
create role growthpulse_dashboard nologin noinherit;
comment on role growthpulse_writer is 'GrowthPulse AI pipeline. DML on named tables only.';
comment on role growthpulse_dashboard is 'GrowthPulse AI dashboard. SELECT on views only.';

-- Session defaults. These limit mistakes; the grants below are the security boundary.
alter role growthpulse_writer connection limit 3;
alter role growthpulse_writer set search_path = growthpulse;
alter role growthpulse_writer set statement_timeout = '120s';
alter role growthpulse_writer set idle_in_transaction_session_timeout = '60s';
alter role growthpulse_writer set lock_timeout = '10s';

alter role growthpulse_dashboard connection limit 5;
alter role growthpulse_dashboard set search_path = growthpulse_read;
alter role growthpulse_dashboard set default_transaction_read_only = on;
alter role growthpulse_dashboard set statement_timeout = '15s';
alter role growthpulse_dashboard set idle_in_transaction_session_timeout = '30s';


-- Migration 1 already revoked everything from PUBLIC and the Supabase API roles, in the
-- same transaction that created the objects. This file only adds grants.

-- Writer grants. UPDATE is limited to the columns an upsert changes, so identity,
-- first-seen fields and history cannot be rewritten. No DELETE or TRUNCATE anywhere.
grant usage on schema growthpulse to growthpulse_writer;

grant select, insert on growthpulse.pipeline_runs to growthpulse_writer;
grant update (finished_at, status, counts, error_code, report_id)
    on growthpulse.pipeline_runs to growthpulse_writer;

grant select, insert on growthpulse.ad_performance_records to growthpulse_writer;
grant update (
    campaign_name, campaign_type, impressions, clicks, spend, conversions, conversion_value,
    currency, account_timezone, conversion_event_name, conversion_stage, ingested_at,
    last_changed_run_id, updated_at
) on growthpulse.ad_performance_records to growthpulse_writer;

grant select, insert on growthpulse.quarantined_records to growthpulse_writer;
grant update (row_index, issue_codes, issues, last_seen_run_id, last_seen_at)
    on growthpulse.quarantined_records to growthpulse_writer;

-- Reports are append-only: no UPDATE grant, and a trigger rejects updates by anyone.
grant select, insert on growthpulse.analytics_reports to growthpulse_writer;

grant select, insert on growthpulse.findings to growthpulse_writer;
grant update (
    severity, pre_guard_severity, guard_outcome, is_suppressed, percent_change, payload,
    latest_report_id, latest_data_as_of, last_seen_at
) on growthpulse.findings to growthpulse_writer;

grant select, insert on growthpulse.experiment_proposals to growthpulse_writer;
grant select, insert on growthpulse.experiment_status_events to growthpulse_writer;


-- Row-level security, as a second layer behind the grants. With RLS on and no policy,
-- a role that somehow gained a grant would still see and change no rows.
alter table growthpulse.pipeline_runs enable row level security;
alter table growthpulse.ad_performance_records enable row level security;
alter table growthpulse.quarantined_records enable row level security;
alter table growthpulse.analytics_reports enable row level security;
alter table growthpulse.findings enable row level security;
alter table growthpulse.experiment_proposals enable row level security;
alter table growthpulse.experiment_status_events enable row level security;

create policy writer_select on growthpulse.pipeline_runs
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.pipeline_runs
    for insert to growthpulse_writer with check (status = 'running');
create policy writer_update on growthpulse.pipeline_runs
    for update to growthpulse_writer using (status = 'running') with check (true);

create policy writer_select on growthpulse.ad_performance_records
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.ad_performance_records
    for insert to growthpulse_writer with check (true);
create policy writer_update on growthpulse.ad_performance_records
    for update to growthpulse_writer using (true) with check (true);

create policy writer_select on growthpulse.quarantined_records
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.quarantined_records
    for insert to growthpulse_writer with check (true);
create policy writer_update on growthpulse.quarantined_records
    for update to growthpulse_writer using (true) with check (true);

create policy writer_select on growthpulse.analytics_reports
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.analytics_reports
    for insert to growthpulse_writer with check (true);

create policy writer_select on growthpulse.findings
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.findings
    for insert to growthpulse_writer with check (true);
create policy writer_update on growthpulse.findings
    for update to growthpulse_writer using (true) with check (true);

create policy writer_select on growthpulse.experiment_proposals
    for select to growthpulse_writer using (true);
create policy writer_insert on growthpulse.experiment_proposals
    for insert to growthpulse_writer with check (requires_human_approval);

-- The pipeline may propose an experiment but never approve, start or complete one.
create policy writer_select on growthpulse.experiment_status_events
    for select to growthpulse_writer using (true);
create policy writer_propose_only on growthpulse.experiment_status_events
    for insert to growthpulse_writer with check (status = 'proposed' and actor = 'pipeline');
