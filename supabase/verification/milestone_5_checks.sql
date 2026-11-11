-- GrowthPulse AI: read-only verification after the five Milestone 5 migrations
-- (20261111200000 to 20261111200400) are applied on top of Milestone 4.
--
-- Safety, as for post_migration_checks.sql:
--   - The whole script runs inside BEGIN TRANSACTION READ ONLY and ends with ROLLBACK,
--     so the server refuses any write. Every other statement is a SELECT over system
--     catalogs, the migration history, and GrowthPulse tables (counts and IDs only).
--   - No DDL, DML, SET, role, password or setting changes. No token, ciphertext, nonce,
--     password hash or setting value other than pgrst.db_schemas is read.
--
-- Output: every query returns check_name, item, expected, actual, ok.
--   ok = true    matches what was tested
--   ok = false   needs review
--   ok = null    informational only
--
-- Expected values were generated from these migrations applied to the exact Milestone 4
-- state on a throwaway PostgreSQL 16.2 server by a non-superuser owner. Owners, which
-- differ there, are not part of any expected value.

begin transaction read only;


-- 01. Migration history: the three Milestone 4 and five Milestone 5 versions.
select '01 migration history' as check_name, e.version as item, 'recorded' as expected,
    coalesce('recorded: ' || m.name, 'missing') as actual,
    m.version is not null as ok
from (values ('20261111100000'), ('20261111100100'), ('20261111100200'), ('20261111200000'), ('20261111200100'), ('20261111200200'), ('20261111200300'), ('20261111200400')) as e (version)
left join supabase_migrations.schema_migrations as m on m.version = e.version
union all
select '01 migration history', 'total versions in history', '8', count(*)::text, count(*) = 8
from supabase_migrations.schema_migrations
order by 2;


-- 02. Schemas exist and belong to postgres.
select '02 schemas' as check_name, e.nspname as item, 'owner postgres' as expected,
    coalesce('owner ' || pg_catalog.pg_get_userbyid(n.nspowner), 'missing') as actual,
    coalesce(pg_catalog.pg_get_userbyid(n.nspowner) = 'postgres', false) as ok
from (values ('growthpulse'), ('growthpulse_read'), ('growthpulse_secrets')) as e (nspname)
left join pg_catalog.pg_namespace as n on n.nspname = e.nspname
order by 2;


-- 03. Object inventory: tables, views, sequences, functions, triggers and policies.
with expected (nspname, kind, name) as (
    values
        ('growthpulse', 'function', 'add_ad_account(uuid, uuid, text, date)'),
        ('growthpulse', 'function', 'add_synthetic_ad_account(uuid, text, text, text, text, date)'),
        ('growthpulse', 'function', 'archive_client(uuid)'),
        ('growthpulse', 'function', 'check_ad_account_update()'),
        ('growthpulse', 'function', 'check_client_update()'),
        ('growthpulse', 'function', 'check_connection_update()'),
        ('growthpulse', 'function', 'check_experiment_status_transition()'),
        ('growthpulse', 'function', 'check_record_run_scope()'),
        ('growthpulse', 'function', 'create_client(uuid, text, text)'),
        ('growthpulse', 'function', 'create_connection(uuid, text, text)'),
        ('growthpulse', 'function', 'decide_experiment(uuid, text, text)'),
        ('growthpulse', 'function', 'derive_scope()'),
        ('growthpulse', 'function', 'log_owner_action(text, text, text)'),
        ('growthpulse', 'function', 'reject_update()'),
        ('growthpulse', 'function', 'rename_client(uuid, text)'),
        ('growthpulse', 'function', 'request_sync(uuid)'),
        ('growthpulse', 'function', 'revoke_connection(uuid)'),
        ('growthpulse', 'function', 'set_account_status(uuid, text)'),
        ('growthpulse', 'function', 'set_sync_config(uuid, boolean, text, smallint, smallint, smalli'),
        ('growthpulse', 'function', 'start_account_sync_tracking(uuid, text, date)'),
        ('growthpulse', 'policy', 'account_sync_config.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'account_sync_state.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'account_sync_state.writer_update_unpaused UPDATE growthpulse_wr'),
        ('growthpulse', 'policy', 'ad_accounts.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'ad_performance_records.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'ad_performance_records.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'ad_performance_records.writer_update UPDATE growthpulse_writer'),
        ('growthpulse', 'policy', 'analytics_report_breakdowns.writer_insert INSERT growthpulse_wr'),
        ('growthpulse', 'policy', 'analytics_report_breakdowns.writer_select SELECT growthpulse_wr'),
        ('growthpulse', 'policy', 'analytics_reports.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'analytics_reports.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'clients.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'discovered_ad_accounts.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'discovered_ad_accounts.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'discovered_ad_accounts.writer_update UPDATE growthpulse_writer'),
        ('growthpulse', 'policy', 'experiment_proposals.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'experiment_proposals.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'experiment_status_events.writer_propose_only INSERT growthpulse'),
        ('growthpulse', 'policy', 'experiment_status_events.writer_select SELECT growthpulse_write'),
        ('growthpulse', 'policy', 'findings.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'findings.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'findings.writer_update UPDATE growthpulse_writer'),
        ('growthpulse', 'policy', 'pipeline_runs.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'pipeline_runs.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'pipeline_runs.writer_update UPDATE growthpulse_writer'),
        ('growthpulse', 'policy', 'provider_connections.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'quarantined_records.writer_insert INSERT growthpulse_writer'),
        ('growthpulse', 'policy', 'quarantined_records.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'policy', 'quarantined_records.writer_update UPDATE growthpulse_writer'),
        ('growthpulse', 'policy', 'workspaces.writer_select SELECT growthpulse_writer'),
        ('growthpulse', 'sequence', 'experiment_status_events_event_id_seq'),
        ('growthpulse', 'sequence', 'owner_action_log_action_id_seq'),
        ('growthpulse', 'table', 'account_sync_config'),
        ('growthpulse', 'table', 'account_sync_state'),
        ('growthpulse', 'table', 'ad_accounts'),
        ('growthpulse', 'table', 'ad_performance_records'),
        ('growthpulse', 'table', 'analytics_report_breakdowns'),
        ('growthpulse', 'table', 'analytics_reports'),
        ('growthpulse', 'table', 'clients'),
        ('growthpulse', 'table', 'discovered_ad_accounts'),
        ('growthpulse', 'table', 'experiment_proposals'),
        ('growthpulse', 'table', 'experiment_status_events'),
        ('growthpulse', 'table', 'findings'),
        ('growthpulse', 'table', 'owner_action_log'),
        ('growthpulse', 'table', 'pipeline_runs'),
        ('growthpulse', 'table', 'provider_connections'),
        ('growthpulse', 'table', 'quarantined_records'),
        ('growthpulse', 'table', 'workspaces'),
        ('growthpulse', 'trigger', 'ad_accounts.ad_accounts_check_update'),
        ('growthpulse', 'trigger', 'ad_performance_records.ad_performance_records_1_derive_scope'),
        ('growthpulse', 'trigger', 'ad_performance_records.ad_performance_records_2_check_run_scope'),
        ('growthpulse', 'trigger', 'analytics_report_breakdowns.analytics_report_breakdowns_append_'),
        ('growthpulse', 'trigger', 'analytics_reports.analytics_reports_append_only'),
        ('growthpulse', 'trigger', 'analytics_reports.analytics_reports_derive_scope'),
        ('growthpulse', 'trigger', 'clients.clients_check_update'),
        ('growthpulse', 'trigger', 'experiment_proposals.experiment_proposals_append_only'),
        ('growthpulse', 'trigger', 'experiment_proposals.experiment_proposals_derive_scope'),
        ('growthpulse', 'trigger', 'experiment_status_events.experiment_status_events_append_only'),
        ('growthpulse', 'trigger', 'experiment_status_events.experiment_status_events_check_transit'),
        ('growthpulse', 'trigger', 'findings.findings_derive_scope'),
        ('growthpulse', 'trigger', 'owner_action_log.owner_action_log_append_only'),
        ('growthpulse', 'trigger', 'provider_connections.provider_connections_check_update'),
        ('growthpulse_read', 'view', 'account_sync_status'),
        ('growthpulse_read', 'view', 'ad_accounts'),
        ('growthpulse_read', 'view', 'analytics_reports'),
        ('growthpulse_read', 'view', 'campaign_daily_conversions'),
        ('growthpulse_read', 'view', 'campaign_daily_delivery'),
        ('growthpulse_read', 'view', 'clients'),
        ('growthpulse_read', 'view', 'data_quality_issues'),
        ('growthpulse_read', 'view', 'discovered_ad_accounts'),
        ('growthpulse_read', 'view', 'experiment_proposals'),
        ('growthpulse_read', 'view', 'experiment_status_history'),
        ('growthpulse_read', 'view', 'findings'),
        ('growthpulse_read', 'view', 'latest_client_reports'),
        ('growthpulse_read', 'view', 'latest_reports'),
        ('growthpulse_read', 'view', 'owner_actions'),
        ('growthpulse_read', 'view', 'pipeline_runs'),
        ('growthpulse_read', 'view', 'provider_connections'),
        ('growthpulse_read', 'view', 'report_breakdowns'),
        ('growthpulse_secrets', 'function', 'credentials_needing_rotation(smallint)'),
        ('growthpulse_secrets', 'function', 'delete_credentials(uuid)'),
        ('growthpulse_secrets', 'function', 'mark_connection_needs_reauth(uuid)'),
        ('growthpulse_secrets', 'function', 'read_credentials(uuid)'),
        ('growthpulse_secrets', 'function', 'store_credential(uuid, text, smallint, smallint, bytea, bytea, '),
        ('growthpulse_secrets', 'table', 'oauth_credentials')
),
actual (nspname, kind, name) as (
    select n.nspname,
        case c.relkind when 'r' then 'table' when 'v' then 'view' when 'S' then 'sequence' end,
        c.relname
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets') and c.relkind in ('r', 'v', 'S')
    union all
    select n.nspname, 'function',
        p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')'
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
    union all
    select n.nspname, 'trigger', c.relname || '.' || t.tgname
    from pg_catalog.pg_trigger as t
    join pg_catalog.pg_class as c on c.oid = t.tgrelid
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where n.nspname in ('growthpulse', 'growthpulse_secrets') and not t.tgisinternal
    union all
    select p.schemaname, 'policy',
        p.tablename || '.' || p.policyname || ' ' || p.cmd || ' '
            || pg_catalog.array_to_string(p.roles, ',')
    from pg_catalog.pg_policies as p
    where p.schemaname in ('growthpulse', 'growthpulse_secrets')
)
select '03 objects' as check_name, k.kind as item,
    (select count(*) from expected as e where e.kind = k.kind)::text as expected,
    (select count(*) from actual as a where a.kind = k.kind)::text as actual,
    (select count(*) from expected as e where e.kind = k.kind)
        = (select count(*) from actual as a where a.kind = k.kind) as ok
from (values ('table'), ('view'), ('sequence'), ('function'), ('trigger'), ('policy')) as k (kind)
union all
select '03 objects', 'missing objects', 'none',
    coalesce(string_agg(e.nspname || ' ' || e.kind || ' ' || e.name, '; '), 'none'),
    count(*) = 0
from (select * from expected except select * from actual) as e
union all
select '03 objects', 'unexpected objects', 'none',
    coalesce(string_agg(a.nspname || ' ' || a.kind || ' ' || a.name, '; '), 'none'),
    count(*) = 0
from (select * from actual except select * from expected) as a
order by 2;


-- 04. Ownership: every schema, relation and function belongs to postgres.
select '04 ownership' as check_name, 'owners of all GrowthPulse objects' as item,
    'postgres' as expected,
    coalesce(string_agg(distinct o.owner, ', '), 'none') as actual,
    coalesce(bool_and(o.owner = 'postgres'), false) as ok
from (
    select pg_catalog.pg_get_userbyid(n.nspowner) as owner
    from pg_catalog.pg_namespace as n where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
    union all
    select pg_catalog.pg_get_userbyid(c.relowner)
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
    union all
    select pg_catalog.pg_get_userbyid(p.proowner)
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
) as o;


-- 05. Row-level security is on for every table, and forced for none.
select '05 row level security' as check_name, n.nspname || '.' || c.relname as item,
    'rls=t force=f' as expected,
    format('rls=%s force=%s', c.relrowsecurity, c.relforcerowsecurity) as actual,
    c.relrowsecurity and not c.relforcerowsecurity as ok
from pg_catalog.pg_class as c
join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
where n.nspname in ('growthpulse', 'growthpulse_secrets') and c.relkind = 'r'
order by 2;


-- 06. Functions: SECURITY DEFINER only where approved, search_path pinned everywhere.
select '06 functions' as check_name,
    n.nspname || '.' || p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')'
        as item,
    case when n.nspname = 'growthpulse_secrets' or p.proname in (
        'create_client', 'rename_client', 'archive_client', 'create_connection',
        'revoke_connection', 'add_ad_account', 'add_synthetic_ad_account',
        'set_account_status', 'set_sync_config', 'request_sync', 'decide_experiment'
    ) then 'definer search_path=""' else 'invoker search_path=""' end as expected,
    format('%s %s', case when p.prosecdef then 'definer' else 'invoker' end,
        coalesce(pg_catalog.array_to_string(p.proconfig, ','), 'search_path unset')) as actual,
    format('%s %s', case when p.prosecdef then 'definer' else 'invoker' end,
        coalesce(pg_catalog.array_to_string(p.proconfig, ','), 'search_path unset'))
    = case when n.nspname = 'growthpulse_secrets' or p.proname in (
        'create_client', 'rename_client', 'archive_client', 'create_connection',
        'revoke_connection', 'add_ad_account', 'add_synthetic_ad_account',
        'set_account_status', 'set_sync_config', 'request_sync', 'decide_experiment'
    ) then 'definer search_path=""' else 'invoker search_path=""' end as ok
from pg_catalog.pg_proc as p
join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
order by 2;


-- 07. App roles: no elevated attributes or memberships. The two new roles must not be
--     able to log in yet; the writer and dashboard logins were enabled in Milestone 4.
select '07 roles' as check_name, r.rolname as item,
    case when r.rolname in ('growthpulse_manager', 'growthpulse_token_manager')
        then 'login=f elevated=f inherit=f' else 'login=t elevated=f inherit=f' end
        as expected,
    format('login=%s elevated=%s inherit=%s', r.rolcanlogin,
        r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolbypassrls or r.rolreplication,
        r.rolinherit) as actual,
    format('login=%s elevated=%s inherit=%s', r.rolcanlogin,
        r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolbypassrls or r.rolreplication,
        r.rolinherit)
    = case when r.rolname in ('growthpulse_manager', 'growthpulse_token_manager')
        then 'login=f elevated=f inherit=f' else 'login=t elevated=f inherit=f' end as ok
from pg_catalog.pg_roles as r
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard', 'growthpulse_manager',
    'growthpulse_token_manager')
union all
select '07 roles', 'app roles as members of another role', 'none',
    coalesce(string_agg(pg_catalog.pg_get_userbyid(m.member) || ' in '
        || pg_catalog.pg_get_userbyid(m.roleid), '; '), 'none'),
    count(*) = 0
from pg_catalog.pg_auth_members as m
where pg_catalog.pg_get_userbyid(m.member) in ('growthpulse_writer', 'growthpulse_dashboard',
    'growthpulse_manager', 'growthpulse_token_manager')
order by 2;


-- 08. Grants: the exact set of privileges anyone other than the owner holds on the three
--     GrowthPulse schemas and their tables, columns, views and functions.
with expected (grantee, object, privilege) as (
    values
        ('growthpulse_dashboard', 'growthpulse_read', 'USAGE'),
        ('growthpulse_dashboard', 'growthpulse_read.account_sync_status', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.ad_accounts', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.analytics_reports', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.campaign_daily_conversions', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.campaign_daily_delivery', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.clients', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.data_quality_issues', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.discovered_ad_accounts', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.experiment_proposals', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.experiment_status_history', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.findings', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.latest_client_reports', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.latest_reports', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.owner_actions', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.pipeline_runs', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.provider_connections', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.report_breakdowns', 'SELECT'),
        ('growthpulse_manager', 'growthpulse', 'USAGE'),
        ('growthpulse_manager', 'growthpulse.add_ad_account(uuid, uuid, text, date)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.add_synthetic_ad_account(uuid, text, text, text, te', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.archive_client(uuid)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.create_client(uuid, text, text)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.create_connection(uuid, text, text)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.decide_experiment(uuid, text, text)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.rename_client(uuid, text)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.request_sync(uuid)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.revoke_connection(uuid)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.set_account_status(uuid, text)', 'EXECUTE'),
        ('growthpulse_manager', 'growthpulse.set_sync_config(uuid, boolean, text, smallint, smal', 'EXECUTE'),
        ('growthpulse_token_manager', 'growthpulse_secrets', 'USAGE'),
        ('growthpulse_token_manager', 'growthpulse_secrets.credentials_needing_rotation(smallint)', 'EXECUTE'),
        ('growthpulse_token_manager', 'growthpulse_secrets.delete_credentials(uuid)', 'EXECUTE'),
        ('growthpulse_token_manager', 'growthpulse_secrets.mark_connection_needs_reauth(uuid)', 'EXECUTE'),
        ('growthpulse_token_manager', 'growthpulse_secrets.read_credentials(uuid)', 'EXECUTE'),
        ('growthpulse_token_manager', 'growthpulse_secrets.store_credential(uuid, text, smallint, smal', 'EXECUTE'),
        ('growthpulse_writer', 'growthpulse', 'USAGE'),
        ('growthpulse_writer', 'growthpulse.account_sync_config', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.account_sync_state', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.consecutive_failures', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.last_attempt_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.last_error_code', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.last_success_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.lease_expires_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.lease_run_id', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.next_due_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.status', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.account_sync_state.synced_through', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_accounts', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.account_timezone', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.campaign_name', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.campaign_type', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.clicks', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.conversion_event_name', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.conversion_stage', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.conversion_value', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.conversions', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.currency', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.impressions', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.ingested_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.last_changed_run_id', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.spend', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records.updated_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.analytics_report_breakdowns', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.analytics_report_breakdowns', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.analytics_reports', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.analytics_reports', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.clients', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.accessible', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.currency', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.display_name', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.is_manager', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.last_seen_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.discovered_ad_accounts.timezone', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.experiment_proposals', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.experiment_proposals', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.experiment_status_events', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.experiment_status_events', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.findings', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.findings', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.findings.guard_outcome', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.is_suppressed', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.last_seen_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.latest_data_as_of', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.latest_report_id', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.payload', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.percent_change', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.pre_guard_severity', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings.severity', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.counts', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.data_quality', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.error_code', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.finished_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.report_id', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs.status', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.provider_connections', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.quarantined_records', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.quarantined_records', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.quarantined_records.issue_codes', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records.issues', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records.last_seen_at', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records.last_seen_run_id', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records.row_index', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.workspaces', 'SELECT')
),
actual (grantee, object, privilege) as (
    select g.grantee, g.object, g.privilege from (
        select pg_catalog.pg_get_userbyid(a.grantee) as grantee, n.nspname as object,
            a.privilege_type as privilege
        from pg_catalog.pg_namespace as n, pg_catalog.aclexplode(n.nspacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
            and a.grantee <> n.nspowner
        union all
        select pg_catalog.pg_get_userbyid(a.grantee), n.nspname || '.' || c.relname, a.privilege_type
        from pg_catalog.pg_class as c
        join pg_catalog.pg_namespace as n on n.oid = c.relnamespace,
            pg_catalog.aclexplode(c.relacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
            and a.grantee <> c.relowner
        union all
        select pg_catalog.pg_get_userbyid(a.grantee), n.nspname || '.' || c.relname || '.' || t.attname,
            a.privilege_type
        from pg_catalog.pg_attribute as t
        join pg_catalog.pg_class as c on c.oid = t.attrelid
        join pg_catalog.pg_namespace as n on n.oid = c.relnamespace,
            pg_catalog.aclexplode(t.attacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
            and t.attnum > 0 and not t.attisdropped
        union all
        select pg_catalog.pg_get_userbyid(a.grantee),
            n.nspname || '.' || p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')',
            a.privilege_type
        from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace,
            pg_catalog.aclexplode(coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))) as a
        where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
            and a.grantee <> p.proowner
    ) as g
    order by 1, 2, 3
)
select '08 grants' as check_name, 'grant entries' as item,
    (select count(*) from expected)::text as expected,
    (select count(*) from actual)::text as actual,
    (select count(*) from expected) = (select count(*) from actual) as ok
union all
select '08 grants', 'expected grants that are missing', 'none',
    coalesce(string_agg(e.grantee || ' ' || e.privilege || ' ' || e.object, '; '), 'none'),
    count(*) = 0
from (select * from expected except select * from actual) as e
union all
select '08 grants', 'grants that were not expected', 'none',
    coalesce(string_agg(a.grantee || ' ' || a.privilege || ' ' || a.object, '; '), 'none'),
    count(*) = 0
from (select * from actual except select * from expected) as a
order by 2;


-- 09. Supabase API roles: no route into any of the three schemas.
select '09 api roles' as check_name, r.rolname as item, 'usage=f,f,f objects=0' as expected,
    format('usage=%s,%s,%s objects=%s',
        pg_catalog.has_schema_privilege(r.oid, 'growthpulse', 'USAGE'),
        pg_catalog.has_schema_privilege(r.oid, 'growthpulse_read', 'USAGE'),
        pg_catalog.has_schema_privilege(r.oid, 'growthpulse_secrets', 'USAGE'),
        (select count(*)
         from pg_catalog.pg_class as c
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
           and c.relkind in ('r', 'v', 'S')
           and (pg_catalog.has_table_privilege(r.oid, c.oid,
                    'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
                or (c.relkind = 'S'
                    and pg_catalog.has_sequence_privilege(r.oid, c.oid, 'USAGE'))))
        + (select count(*)
           from pg_catalog.pg_proc as p
           join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
           where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets')
             and pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE'))) as actual,
    not pg_catalog.has_schema_privilege(r.oid, 'growthpulse', 'USAGE')
        and not pg_catalog.has_schema_privilege(r.oid, 'growthpulse_read', 'USAGE')
        and not pg_catalog.has_schema_privilege(r.oid, 'growthpulse_secrets', 'USAGE') as ok
from pg_catalog.pg_roles as r
where r.rolname in ('anon', 'authenticated', 'service_role')
order by 2;


-- 10. Data API: the exposed schemas, if configured in the database, exclude GrowthPulse.
select '10 data api exposure' as check_name, 'pgrst.db_schemas' as item,
    'does not mention growthpulse' as expected,
    coalesce(string_agg(cfg, '; '), 'not set in the database') as actual,
    coalesce(bool_and(cfg not like '%growthpulse%'), true) as ok
from pg_catalog.pg_db_role_setting as d
cross join lateral unnest(d.setconfig) as cfg
where cfg like 'pgrst.db\_schemas=%';


-- 11. Backfill: the Milestone 4 data, unchanged in number, scoped to the fictional legacy
--     client. Counts and IDs only. Expected right after the migrations, before any new run.
select '11 backfill' as check_name, x.item, x.expected, x.actual, x.expected = x.actual as ok
from (
    select 'pipeline runs (combined, legacy client)' as item, '2' as expected,
        (select count(*) from growthpulse.pipeline_runs
         where run_kind = 'combined'
           and client_id = '27c669c8-001a-4573-a336-a8b21331ee86')::text as actual
    union all
    select 'records on the legacy Google account', '210',
        (select count(*) from growthpulse.ad_performance_records
         where ad_account_id = '3216f4c2-0201-4c18-a7cd-3c2007f5c18e'
           and platform = 'google_ads' and account_id = 'synthetic-google-account-001')::text
    union all
    select 'records on the legacy Meta account', '90',
        (select count(*) from growthpulse.ad_performance_records
         where ad_account_id = 'c8fea272-daaa-41e3-88d3-dbb54c3df6cc'
           and platform = 'meta_ads' and account_id = 'synthetic-meta-account-001')::text
    union all
    select 'records in total', '300',
        (select count(*) from growthpulse.ad_performance_records)::text
    union all
    select 'report on the legacy client',
        'feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601',
        (select string_agg(report_id, ', ') from growthpulse.analytics_reports
         where client_id = '27c669c8-001a-4573-a336-a8b21331ee86'
           and report ->> 'report_id' = report_id)
    union all
    select 'reports in total', '1', (select count(*) from growthpulse.analytics_reports)::text
    union all
    select 'findings on the legacy client, matching their account', '19',
        (select count(*) from growthpulse.findings as f
         join growthpulse.ad_accounts as a on a.ad_account_id = f.ad_account_id
         where f.client_id = '27c669c8-001a-4573-a336-a8b21331ee86'
           and a.client_id = f.client_id and a.platform = f.platform
           and a.external_account_id = f.account_id)::text
    union all
    select 'findings in total', '19', (select count(*) from growthpulse.findings)::text
    union all
    select 'non-synthetic rows anywhere', '0',
        ((select count(*) from growthpulse.pipeline_runs where data_source <> 'synthetic')
         + (select count(*) from growthpulse.ad_performance_records
            where data_source <> 'synthetic')
         + (select count(*) from growthpulse.analytics_reports where data_source <> 'synthetic')
         + (select count(*) from growthpulse.findings where data_source <> 'synthetic'))::text
    union all
    select 'clients, accounts, connections, stored credentials', '1, 2, 0, 0',
        (select count(*) from growthpulse.clients)::text || ', '
        || (select count(*) from growthpulse.ad_accounts)::text || ', '
        || (select count(*) from growthpulse.provider_connections)::text || ', '
        || (select count(*) from growthpulse_secrets.oauth_credentials)::text
) as x
order by 2;


rollback;
