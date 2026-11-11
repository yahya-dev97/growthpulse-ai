-- GrowthPulse AI, migration 1 of 3: private schema and tables.
--
-- The growthpulse schema is not exposed through the Supabase Data API, and no Supabase
-- API role (anon, authenticated, service_role) is granted anything on it. Migration 2
-- grants access to two dedicated roles. Migration 3 adds read-only views for the dashboard.
--
-- Allowed values in CHECK constraints mirror the Python enums. tests/test_migrations.py
-- fails if they drift apart.

create schema growthpulse;
comment on schema growthpulse is
    'GrowthPulse AI backend tables. Private: never add this schema to the Data API.';
revoke all on schema growthpulse from public;
alter default privileges in schema growthpulse revoke all on tables from public;
alter default privileges in schema growthpulse revoke all on sequences from public;
alter default privileges in schema growthpulse revoke all on functions from public;


-- One row per pipeline run. The row is committed before any data is written, so a run
-- that fails still leaves a record. error_code is a fixed code, never an error message.
create table growthpulse.pipeline_runs (
    run_id uuid primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null default 'running'
        check (status in ('running', 'succeeded', 'failed')),
    data_source text not null check (data_source in ('synthetic', 'live')),
    triggered_by text not null check (triggered_by in ('manual', 'scheduled', 'test')),
    code_version text check (code_version ~ '^[0-9a-f]{7,40}$'),
    counts jsonb not null default '{}'::jsonb check (jsonb_typeof(counts) = 'object'),
    error_code text check (error_code ~ '^[a-z][a-z_]{0,63}$'),
    -- Foreign key added once analytics_reports exists.
    report_id text,
    constraint pipeline_runs_finished_when_done
        check ((status = 'running') = (finished_at is null)),
    constraint pipeline_runs_error_only_on_failure
        check ((status = 'failed') = (error_code is not null)),
    constraint pipeline_runs_report_only_on_success
        check (report_id is null or status = 'succeeded'),
    constraint pipeline_runs_finish_after_start
        check (finished_at >= started_at)
);
create index pipeline_runs_started_at_idx on growthpulse.pipeline_runs (started_at desc);


-- Normalized advertising performance. record_id is the schema's stable identity hash, so
-- re-running a day updates it in place. Conversions are platform-attributed, not bookings.
-- numeric(20, 6) matches the Pydantic schema: at most 14 digits before the point and 6 after.
create table growthpulse.ad_performance_records (
    record_id text primary key check (record_id ~ '^[0-9a-f]{64}$'),
    data_source text not null check (data_source in ('synthetic', 'live')),
    reporting_date date not null,
    platform text not null check (platform in ('google_ads', 'meta_ads')),
    account_id text not null check (char_length(account_id) between 1 and 255),
    campaign_id text not null check (char_length(campaign_id) between 1 and 255),
    campaign_name text not null check (char_length(campaign_name) between 1 and 255),
    campaign_type text not null check (campaign_type in (
        'google_search', 'google_performance_max', 'google_other',
        'meta_leads', 'meta_sales', 'meta_other'
    )),
    impressions bigint not null check (impressions >= 0),
    clicks bigint not null check (clicks >= 0 and clicks <= impressions),
    spend numeric(20, 6) not null check (spend >= 0),
    conversions numeric(20, 6) not null check (conversions >= 0),
    conversion_value numeric(20, 6) not null check (conversion_value >= 0),
    currency text not null check (currency ~ '^[A-Z]{3}$'),
    account_timezone text not null check (char_length(account_timezone) between 1 and 64),
    conversion_event_id text not null check (char_length(conversion_event_id) between 1 and 255),
    conversion_event_name text not null
        check (char_length(conversion_event_name) between 1 and 255),
    conversion_stage text not null check (conversion_stage in (
        'lead', 'booking_request', 'confirmed_booking', 'purchase', 'other'
    )),
    attribution_model text
        check (attribution_model in ('data_driven', 'last_click', 'platform_windowed')),
    click_window_days smallint check (click_window_days between 1 and 90),
    view_window_days smallint check (view_window_days between 1 and 30),
    report_time_basis text check (report_time_basis in ('interaction', 'conversion', 'mixed')),
    ingested_at timestamptz not null,
    first_seen_run_id uuid not null references growthpulse.pipeline_runs (run_id),
    last_changed_run_id uuid not null references growthpulse.pipeline_runs (run_id),
    first_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint ad_performance_records_attribution_complete check (
        (attribution_model is null and click_window_days is null
            and view_window_days is null and report_time_basis is null)
        or (attribution_model is not null and click_window_days is not null
            and report_time_basis is not null)
    )
);
create index ad_performance_records_campaign_idx on growthpulse.ad_performance_records
    (data_source, platform, account_id, campaign_id, reporting_date);
create index ad_performance_records_date_idx on growthpulse.ad_performance_records
    (data_source, reporting_date);


-- Rows that failed validation. Issues keep code, severity and field only, never values.
-- The raw payload is kept for synthetic data only, which the last constraint enforces.
create table growthpulse.quarantined_records (
    quarantine_id text primary key check (quarantine_id ~ '^[0-9a-f]{64}$'),
    data_source text not null check (data_source in ('synthetic', 'live')),
    connector text not null check (connector ~ '^[a-z][a-z0-9_]{0,63}$'),
    row_index integer not null check (row_index >= 0),
    issue_codes text[] not null check (cardinality(issue_codes) > 0),
    issues jsonb not null check (jsonb_typeof(issues) = 'array'),
    payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
    raw_payload jsonb,
    first_seen_run_id uuid not null references growthpulse.pipeline_runs (run_id),
    last_seen_run_id uuid not null references growthpulse.pipeline_runs (run_id),
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    constraint quarantined_records_raw_payload_synthetic_only
        check (raw_payload is null or data_source = 'synthetic')
);
create index quarantined_records_last_seen_idx on growthpulse.quarantined_records
    (data_source, last_seen_at desc);


-- Analytics reports are append-only. report_id is a hash of the report body, so the same
-- input produces the same ID and is stored once. Earlier reports are never overwritten.
create table growthpulse.analytics_reports (
    report_id text primary key check (report_id ~ '^[0-9a-f]{64}$'),
    run_id uuid not null references growthpulse.pipeline_runs (run_id),
    data_source text not null check (data_source in ('synthetic', 'live')),
    report_schema_version text not null check (report_schema_version ~ '^[0-9]+\.[0-9]+$'),
    data_as_of timestamptz not null,
    period_start date not null,
    period_end date not null,
    record_count integer not null check (record_count > 0),
    report jsonb not null check (jsonb_typeof(report) = 'object'),
    created_at timestamptz not null default now(),
    constraint analytics_reports_period_order check (period_end >= period_start),
    constraint analytics_reports_id_matches_body check (report ->> 'report_id' = report_id),
    constraint analytics_reports_source_matches_body
        check (report ->> 'data_source' = data_source)
);
create index analytics_reports_latest_idx on growthpulse.analytics_reports
    (data_source, data_as_of desc, created_at desc);

alter table growthpulse.pipeline_runs
    add constraint pipeline_runs_report_id_fkey
    foreign key (report_id) references growthpulse.analytics_reports (report_id);


-- Latest state of each finding, including findings the noise guard suppressed.
-- Every earlier state stays in the analytics report that contained it.
create table growthpulse.findings (
    finding_id text primary key check (finding_id ~ '^[0-9a-f]{64}$'),
    data_source text not null check (data_source in ('synthetic', 'live')),
    rule_id text not null check (rule_id in (
        'cost_per_conversion_increase', 'conversion_rate_decline',
        'spend_without_conversion_growth', 'spend_with_zero_conversions',
        'strong_performer', 'roas_deterioration'
    )),
    severity text not null check (severity in ('high', 'medium', 'info')),
    pre_guard_severity text not null check (pre_guard_severity in ('high', 'medium', 'info')),
    direction text not null check (direction in ('adverse', 'positive')),
    guard_outcome text not null check (guard_outcome in (
        'passed', 'downgraded', 'suppressed', 'not_evaluated', 'not_applicable'
    )),
    is_suppressed boolean not null,
    platform text not null check (platform in ('google_ads', 'meta_ads')),
    account_id text not null,
    campaign_id text not null,
    basis_id text not null,
    metric text not null,
    comparison_label text not null,
    comparison_purpose text not null
        check (comparison_purpose in ('recent_change', 'sustained_change', 'custom')),
    baseline_start date not null,
    baseline_end date not null,
    current_start date not null,
    current_end date not null,
    percent_change numeric,
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    first_report_id text not null references growthpulse.analytics_reports (report_id),
    latest_report_id text not null references growthpulse.analytics_reports (report_id),
    latest_data_as_of timestamptz not null,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    constraint findings_suppressed_matches_guard
        check (is_suppressed = (guard_outcome = 'suppressed')),
    constraint findings_id_matches_payload check (payload ->> 'finding_id' = finding_id)
);
create index findings_campaign_idx on growthpulse.findings
    (data_source, platform, account_id, campaign_id);
create index findings_latest_report_idx on growthpulse.findings (latest_report_id);


-- Experiment proposals are advisory. Nothing in this system applies them to a live
-- campaign, and every proposal requires human approval.
create table growthpulse.experiment_proposals (
    proposal_id uuid primary key default pg_catalog.gen_random_uuid(),
    idempotency_key text not null unique check (idempotency_key ~ '^[0-9a-f]{64}$'),
    data_source text not null check (data_source in ('synthetic', 'live')),
    report_id text not null references growthpulse.analytics_reports (report_id),
    finding_id text references growthpulse.findings (finding_id),
    group_id text check (group_id ~ '^[0-9a-f]{64}$'),
    title text not null check (char_length(title) between 1 and 200),
    hypothesis text not null check (char_length(hypothesis) between 1 and 4000),
    proposed_change text not null check (char_length(proposed_change) between 1 and 4000),
    primary_metric text not null check (char_length(primary_metric) between 1 and 100),
    requires_human_approval boolean not null default true check (requires_human_approval),
    created_at timestamptz not null default now(),
    constraint experiment_proposals_have_subject
        check (finding_id is not null or group_id is not null)
);


-- Append-only status history. A trigger validates each transition and numbers events.
-- actor is a role label such as pipeline or analyst, never a person's name or email.
create table growthpulse.experiment_status_events (
    event_id bigint generated always as identity primary key,
    proposal_id uuid not null references growthpulse.experiment_proposals (proposal_id),
    sequence integer not null check (sequence >= 1),
    status text not null check (status in (
        'proposed', 'approved', 'rejected', 'running', 'completed', 'cancelled'
    )),
    previous_status text,
    actor text not null check (actor ~ '^[a-z][a-z_]{0,31}$'),
    note text check (char_length(note) <= 1000),
    changed_at timestamptz not null default now(),
    constraint experiment_status_events_sequence_unique unique (proposal_id, sequence)
);


-- Allowed transitions. The unique (proposal_id, sequence) constraint makes concurrent
-- changes to one proposal fail instead of both succeeding.
create function growthpulse.check_experiment_status_transition()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    latest record;
begin
    select e.status, e.sequence into latest
    from growthpulse.experiment_status_events as e
    where e.proposal_id = new.proposal_id
    order by e.sequence desc
    limit 1;

    if not found then
        if new.status <> 'proposed' then
            raise exception 'The first status of an experiment proposal must be proposed'
                using errcode = 'check_violation';
        end if;
        new.sequence := 1;
        new.previous_status := null;
        return new;
    end if;

    if not (
        (latest.status = 'proposed' and new.status in ('approved', 'rejected', 'cancelled'))
        or (latest.status = 'approved' and new.status in ('running', 'cancelled'))
        or (latest.status = 'running' and new.status in ('completed', 'cancelled'))
    ) then
        raise exception 'Experiment status cannot change from % to %', latest.status, new.status
            using errcode = 'check_violation';
    end if;

    new.sequence := latest.sequence + 1;
    new.previous_status := latest.status;
    return new;
end;
$$;

create trigger experiment_status_events_check_transition
    before insert on growthpulse.experiment_status_events
    for each row execute function growthpulse.check_experiment_status_transition();


-- Reports, proposals and status events are history. Updating them is always an error.
create function growthpulse.reject_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'Rows in growthpulse.% are append-only', tg_table_name
        using errcode = 'insufficient_privilege';
end;
$$;

create trigger analytics_reports_append_only
    before update on growthpulse.analytics_reports
    for each row execute function growthpulse.reject_update();
create trigger experiment_proposals_append_only
    before update on growthpulse.experiment_proposals
    for each row execute function growthpulse.reject_update();
create trigger experiment_status_events_append_only
    before update on growthpulse.experiment_status_events
    for each row execute function growthpulse.reject_update();

-- Close the schema before this transaction commits. Global default privileges, if the
-- project has any, may have granted the Supabase API roles access to what was created
-- above, so revoke from PUBLIC and from each API role explicitly. The roles are looked up
-- so the file also runs on plain PostgreSQL, where they do not exist.
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
