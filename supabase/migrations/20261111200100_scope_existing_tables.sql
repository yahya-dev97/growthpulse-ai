-- GrowthPulse AI, migration 5: scope the Milestone 4 tables by client and account, and
-- backfill the existing synthetic data into one fictional client.
--
-- Milestone 5 design: MILESTONE_5_DESIGN.md, sections 3.3, 4, 5.2 and 13.
--
-- Safety:
--   - One transaction. Any failed check, constraint or foreign key undoes all of it.
--   - Preconditions run first and refuse anything this backfill was not designed for:
--     non-synthetic rows, records or findings naming an unknown account, records whose
--     currency or timezone differ from their account, and duplicate record identities.
--   - Nothing is deleted. Record, finding and report IDs, report bodies, updated_at and
--     last_changed_run_id are never written. Reports and proposals are append-only and are
--     never updated: their client_id comes from a column default, which PostgreSQL applies
--     to existing rows without an UPDATE, so the append-only triggers never fire.
--
-- Compatibility (MILESTONE_5_PHASE_B_REPORT.md, section 3): the Milestone 4 store code does
-- not pass scope yet (Phase C). Until it does:
--   - a run with no client_id belongs to the fictional legacy client. Its composite foreign
--     key to clients (client_id, data_source) means a live run can never default into it;
--   - every other scope column is derived, never defaulted: a record's or finding's account
--     from its own (data_source, platform, account_id), a finding's client from that
--     account, a report's client from its run, and a proposal's client from its report.
-- An explicit value is always kept and checked by the composite foreign keys, so a
-- mismatch fails closed instead of being re-filed.
--
-- The fixed IDs below equal src/growthpulse_ai/accounts/legacy.py; a test enforces it.

-- preconditions:begin
do $$
begin
    if exists (select from growthpulse.pipeline_runs where data_source <> 'synthetic')
        or exists (
            select from growthpulse.ad_performance_records where data_source <> 'synthetic'
        )
        or exists (select from growthpulse.quarantined_records where data_source <> 'synthetic')
        or exists (select from growthpulse.analytics_reports where data_source <> 'synthetic')
        or exists (select from growthpulse.findings where data_source <> 'synthetic')
        or exists (select from growthpulse.experiment_proposals where data_source <> 'synthetic')
    then
        raise exception 'Milestone 5 backfill refused: non-synthetic rows exist'
            using errcode = 'check_violation';
    end if;

    if exists (
        select from growthpulse.ad_performance_records as r
        where (r.platform, r.account_id, r.currency, r.account_timezone) not in (
            values
                ('google_ads', 'synthetic-google-account-001', 'GBP', 'Europe/London'),
                ('meta_ads', 'synthetic-meta-account-001', 'GBP', 'Europe/London')
        )
    ) then
        raise exception 'Milestone 5 backfill refused: a record does not match a legacy account'
            using errcode = 'check_violation';
    end if;

    if exists (
        select from growthpulse.findings as f
        where (f.platform, f.account_id) not in (
            values
                ('google_ads', 'synthetic-google-account-001'),
                ('meta_ads', 'synthetic-meta-account-001')
        )
    ) then
        raise exception 'Milestone 5 backfill refused: a finding does not match a legacy account'
            using errcode = 'check_violation';
    end if;

    if exists (
        select from growthpulse.ad_performance_records as r
        group by r.data_source, r.reporting_date, r.platform, r.account_id, r.campaign_id,
            r.conversion_event_id, r.attribution_model, r.click_window_days,
            r.view_window_days, r.report_time_basis
        having count(*) > 1
    ) then
        raise exception 'Milestone 5 backfill refused: duplicate record identities'
            using errcode = 'unique_violation';
    end if;
end
$$;
-- preconditions:end


-- The fictional legacy client and its two synthetic accounts.
insert into growthpulse.workspaces (workspace_id, name)
values ('26f3f7ff-8015-413a-8029-06e04727974c', 'GrowthPulse workspace');

insert into growthpulse.clients (client_id, workspace_id, name, data_source)
values (
    '27c669c8-001a-4573-a336-a8b21331ee86',
    '26f3f7ff-8015-413a-8029-06e04727974c',
    'ExampleScan UK (fictional)',
    'synthetic'
);

insert into growthpulse.ad_accounts (
    ad_account_id, client_id, data_source, platform, external_account_id, display_name,
    currency, timezone, status
) values
    (
        '3216f4c2-0201-4c18-a7cd-3c2007f5c18e', '27c669c8-001a-4573-a336-a8b21331ee86',
        'synthetic', 'google_ads', 'synthetic-google-account-001',
        'ExampleScan UK (fictional) google_ads', 'GBP', 'Europe/London', 'active'
    ),
    (
        'c8fea272-daaa-41e3-88d3-dbb54c3df6cc', '27c669c8-001a-4573-a336-a8b21331ee86',
        'synthetic', 'meta_ads', 'synthetic-meta-account-001',
        'ExampleScan UK (fictional) meta_ads', 'GBP', 'Europe/London', 'active'
    );

-- Manual schedule: the legacy accounts sync only when asked.
insert into growthpulse.account_sync_config (ad_account_id, frequency, backfill_start_date)
select a.ad_account_id, 'manual', date '2026-10-01'
from growthpulse.ad_accounts as a
where a.client_id = '27c669c8-001a-4573-a336-a8b21331ee86';

insert into growthpulse.account_sync_state (ad_account_id, synced_through)
select a.ad_account_id, (
    select max(r.reporting_date) from growthpulse.ad_performance_records as r
    where r.data_source = a.data_source and r.platform = a.platform
        and r.account_id = a.external_account_id
)
from growthpulse.ad_accounts as a
where a.client_id = '27c669c8-001a-4573-a336-a8b21331ee86';


-- Runs. Existing runs become 'combined' runs of the legacy client.
alter table growthpulse.pipeline_runs
    add column run_kind text not null default 'combined'
        check (run_kind in ('combined', 'account_sync', 'client_analysis')),
    add column client_id uuid not null default '27c669c8-001a-4573-a336-a8b21331ee86',
    add column ad_account_id uuid,
    add column window_start date,
    add column window_end date,
    add column attempt smallint not null default 1 check (attempt between 1 and 1000),
    add column retry_of_run_id uuid,
    add column data_quality jsonb
        check (data_quality is null or jsonb_typeof(data_quality) = 'object'),
    add constraint pipeline_runs_client_source_fkey foreign key (client_id, data_source)
        references growthpulse.clients (client_id, data_source),
    add constraint pipeline_runs_account_client_fkey foreign key (ad_account_id, client_id)
        references growthpulse.ad_accounts (ad_account_id, client_id),
    add constraint pipeline_runs_account_only_for_syncs
        check ((run_kind = 'account_sync') = (ad_account_id is not null)),
    add constraint pipeline_runs_window_for_syncs
        check (run_kind <> 'account_sync' or (window_start is not null and window_end is not null)),
    add constraint pipeline_runs_window_order check (window_end >= window_start),
    add constraint pipeline_runs_no_report_for_syncs
        check (report_id is null or run_kind <> 'account_sync'),
    add constraint pipeline_runs_run_client_unique unique (run_id, client_id),
    add constraint pipeline_runs_run_account_unique unique (run_id, ad_account_id);

-- A retry repeats an existing run: the same account's sync for account syncs, and the same
-- client's run for everything else.
alter table growthpulse.pipeline_runs
    add constraint pipeline_runs_retry_run_fkey foreign key (retry_of_run_id, client_id)
        references growthpulse.pipeline_runs (run_id, client_id),
    add constraint pipeline_runs_retry_same_account_fkey foreign key (retry_of_run_id, ad_account_id)
        references growthpulse.pipeline_runs (run_id, ad_account_id);

create index pipeline_runs_client_started_idx on growthpulse.pipeline_runs
    (client_id, started_at desc);
create index pipeline_runs_account_started_idx on growthpulse.pipeline_runs
    (ad_account_id, started_at desc) where ad_account_id is not null;

alter table growthpulse.account_sync_state
    add constraint account_sync_state_lease_run_fkey foreign key (lease_run_id, ad_account_id)
        references growthpulse.pipeline_runs (run_id, ad_account_id);


-- Derives missing scope columns from the row's parents (see the header). Raises when a
-- record or finding names an account that is not registered.
create function growthpulse.derive_scope()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    -- Each table is handled in its own branch: PL/pgSQL does not short-circuit, and a
    -- column one table lacks must never be referenced for it.
    if tg_table_name in ('ad_performance_records', 'findings') then
        if new.ad_account_id is null then
            select a.ad_account_id into new.ad_account_id
            from growthpulse.ad_accounts as a
            where a.data_source = new.data_source
                and a.platform = new.platform
                and a.external_account_id = new.account_id;
            if not found then
                raise exception 'No advertising account is registered for this row'
                    using errcode = 'foreign_key_violation';
            end if;
        end if;
    end if;
    if tg_table_name = 'findings' then
        if new.client_id is null then
            select a.client_id into new.client_id
            from growthpulse.ad_accounts as a
            where a.ad_account_id = new.ad_account_id;
        end if;
    elsif tg_table_name = 'analytics_reports' then
        if new.client_id is null then
            select r.client_id into new.client_id
            from growthpulse.pipeline_runs as r
            where r.run_id = new.run_id;
        end if;
    elsif tg_table_name = 'experiment_proposals' then
        if new.client_id is null then
            select r.client_id into new.client_id
            from growthpulse.analytics_reports as r
            where r.report_id = new.report_id;
        end if;
    end if;
    return new;
end;
$$;


-- A record may only be written by a run of its own client, and an account-sync run may
-- only write its own account. Checked on insert and on every upsert that changes it.
create function growthpulse.check_record_run_scope()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    v_run_client uuid;
    v_run_account uuid;
    v_account_client uuid;
begin
    select r.client_id, r.ad_account_id into v_run_client, v_run_account
    from growthpulse.pipeline_runs as r
    where r.run_id = new.last_changed_run_id;
    select a.client_id into v_account_client
    from growthpulse.ad_accounts as a
    where a.ad_account_id = new.ad_account_id;
    if v_run_client is distinct from v_account_client
        or (v_run_account is not null and v_run_account <> new.ad_account_id) then
        raise exception 'A run may only write records of its own client and account'
            using errcode = 'foreign_key_violation';
    end if;
    return new;
end;
$$;


-- Records belong to an account. The client is reached through the account.
alter table growthpulse.ad_performance_records add column ad_account_id uuid;

update growthpulse.ad_performance_records as r
set ad_account_id = a.ad_account_id
from growthpulse.ad_accounts as a
where a.data_source = r.data_source
    and a.platform = r.platform
    and a.external_account_id = r.account_id;

alter table growthpulse.ad_performance_records
    alter column ad_account_id set not null,
    add constraint ad_performance_records_account_fkey
        foreign key (ad_account_id, data_source, platform, account_id)
        references growthpulse.ad_accounts (ad_account_id, data_source, platform, external_account_id);

create index ad_performance_records_account_date_idx on growthpulse.ad_performance_records
    (ad_account_id, reporting_date);

-- Triggers fire in name order: derive the account first, then check the run's scope.
create trigger ad_performance_records_1_derive_scope
    before insert on growthpulse.ad_performance_records
    for each row execute function growthpulse.derive_scope();
create trigger ad_performance_records_2_check_run_scope
    before insert or update on growthpulse.ad_performance_records
    for each row execute function growthpulse.check_record_run_scope();


-- Quarantined rows from a per-account sync carry its account. Rows from combined runs,
-- which may not name a usable account, stay unscoped.
alter table growthpulse.quarantined_records
    add column ad_account_id uuid,
    add constraint quarantined_records_account_fkey foreign key (ad_account_id)
        references growthpulse.ad_accounts (ad_account_id);


-- Reports belong to a client, the same client as the run that wrote them.
alter table growthpulse.analytics_reports
    add column client_id uuid not null default '27c669c8-001a-4573-a336-a8b21331ee86',
    add constraint analytics_reports_client_source_fkey foreign key (client_id, data_source)
        references growthpulse.clients (client_id, data_source),
    add constraint analytics_reports_run_client_fkey foreign key (run_id, client_id)
        references growthpulse.pipeline_runs (run_id, client_id),
    add constraint analytics_reports_report_client_unique unique (report_id, client_id);

alter table growthpulse.analytics_reports alter column client_id drop default;
create trigger analytics_reports_derive_scope
    before insert on growthpulse.analytics_reports
    for each row execute function growthpulse.derive_scope();

create index analytics_reports_client_latest_idx on growthpulse.analytics_reports
    (client_id, data_source, data_as_of desc, created_at desc);

-- A run's report must be its own client's.
alter table growthpulse.pipeline_runs
    add constraint pipeline_runs_report_client_fkey foreign key (report_id, client_id)
        references growthpulse.analytics_reports (report_id, client_id);


-- Findings belong to a client and to one of that client's accounts.
alter table growthpulse.findings
    add column client_id uuid not null default '27c669c8-001a-4573-a336-a8b21331ee86',
    add column ad_account_id uuid;

update growthpulse.findings as f
set ad_account_id = a.ad_account_id
from growthpulse.ad_accounts as a
where a.data_source = f.data_source
    and a.platform = f.platform
    and a.external_account_id = f.account_id;

alter table growthpulse.findings
    alter column client_id drop default,
    alter column ad_account_id set not null,
    add constraint findings_account_fkey
        foreign key (ad_account_id, client_id, data_source, platform, account_id)
        references growthpulse.ad_accounts
            (ad_account_id, client_id, data_source, platform, external_account_id),
    add constraint findings_first_report_client_fkey foreign key (first_report_id, client_id)
        references growthpulse.analytics_reports (report_id, client_id),
    add constraint findings_latest_report_client_fkey foreign key (latest_report_id, client_id)
        references growthpulse.analytics_reports (report_id, client_id),
    add constraint findings_finding_client_unique unique (finding_id, client_id);

create index findings_client_idx on growthpulse.findings (client_id);
create index findings_account_idx on growthpulse.findings (ad_account_id);

create trigger findings_derive_scope
    before insert on growthpulse.findings
    for each row execute function growthpulse.derive_scope();


-- Proposals belong to the client of their report and finding.
alter table growthpulse.experiment_proposals
    add column client_id uuid not null default '27c669c8-001a-4573-a336-a8b21331ee86',
    add constraint experiment_proposals_report_client_fkey foreign key (report_id, client_id)
        references growthpulse.analytics_reports (report_id, client_id),
    add constraint experiment_proposals_finding_client_fkey foreign key (finding_id, client_id)
        references growthpulse.findings (finding_id, client_id);
alter table growthpulse.experiment_proposals alter column client_id drop default;
create trigger experiment_proposals_derive_scope
    before insert on growthpulse.experiment_proposals
    for each row execute function growthpulse.derive_scope();


-- Account-level breakdown of a client report. Stored beside the report, never inside it,
-- so report IDs do not change. One per report, append-only, same client as its report.
create table growthpulse.analytics_report_breakdowns (
    report_id text primary key,
    client_id uuid not null,
    breakdown_id text not null unique check (breakdown_id ~ '^[0-9a-f]{64}$'),
    breakdown_schema_version text not null
        check (breakdown_schema_version ~ '^[0-9]+\.[0-9]+$'),
    breakdown jsonb not null check (jsonb_typeof(breakdown) = 'object'),
    created_at timestamptz not null default now(),
    constraint analytics_report_breakdowns_report_client_fkey foreign key (report_id, client_id)
        references growthpulse.analytics_reports (report_id, client_id),
    constraint analytics_report_breakdowns_id_matches_body
        check (breakdown ->> 'breakdown_id' = breakdown_id),
    constraint analytics_report_breakdowns_report_matches_body
        check (breakdown ->> 'report_id' = report_id),
    constraint analytics_report_breakdowns_version_matches_body
        check (breakdown ->> 'breakdown_schema_version' = breakdown_schema_version)
);
create trigger analytics_report_breakdowns_append_only
    before update on growthpulse.analytics_report_breakdowns
    for each row execute function growthpulse.reject_update();
alter table growthpulse.analytics_report_breakdowns enable row level security;


revoke all on all tables in schema growthpulse from public;
revoke all on all sequences in schema growthpulse from public;
revoke all on all functions in schema growthpulse from public;

do $$
declare
    api_role text;
begin
    foreach api_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select from pg_catalog.pg_roles where rolname = api_role) then
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
