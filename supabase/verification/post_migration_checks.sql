-- GrowthPulse AI: consolidated read-only verification, run after the three 20261111
-- migrations were applied. Instructions: POST_MIGRATION_VERIFICATION.md.
--
-- Safety:
--   - The whole script runs inside BEGIN TRANSACTION READ ONLY and ends with ROLLBACK,
--     so the server itself refuses any write. Every other statement is a SELECT over
--     system catalogs, GrowthPulse tables (row counts only) and the migration history.
--   - No DDL, DML, SET, SET ROLE, role, password or setting changes. No data rows,
--     passwords, password hashes or setting values other than pgrst.db_schemas are read.
--
-- Output: every query returns check_name, item, expected, actual, ok.
--   ok = true    matches what was tested
--   ok = false   needs review
--   ok = null    informational only
--
-- Expected fingerprints come from these migrations applied to a throwaway PostgreSQL
-- 16.2 server by a non-superuser owner. Owners, which differ there, are not part of
-- any fingerprint. A fingerprint difference with matching counts may be PostgreSQL 17
-- formatting rather than a real change, and is reviewed before anything else.

begin transaction read only;


-- 01. Migration history: exactly the three approved versions.
select '01 migration history' as check_name, e.version as item, 'recorded' as expected,
    coalesce('recorded: ' || m.name, 'missing') as actual,
    m.version is not null as ok
from (values ('20261111100000'), ('20261111100100'), ('20261111100200')) as e (version)
left join supabase_migrations.schema_migrations as m on m.version = e.version
union all
select '01 migration history', 'total versions in history', '3', count(*)::text, count(*) = 3
from supabase_migrations.schema_migrations
order by 2;


-- 02. Schemas exist and belong to postgres.
select '02 schemas' as check_name, e.nspname as item, 'owner postgres' as expected,
    coalesce('owner ' || pg_catalog.pg_get_userbyid(n.nspowner), 'missing') as actual,
    coalesce(pg_catalog.pg_get_userbyid(n.nspowner) = 'postgres', false) as ok
from (values ('growthpulse'), ('growthpulse_read')) as e (nspname)
left join pg_catalog.pg_namespace as n on n.nspname = e.nspname
order by 2;


-- 03. Object inventory: nothing missing and nothing extra in either schema.
with expected (nspname, objname, kind) as (
    values
        ('growthpulse', 'experiment_status_events_event_id_seq', 'S'),
        ('growthpulse', 'check_experiment_status_transition', 'function'),
        ('growthpulse', 'reject_update', 'function'),
        ('growthpulse', 'ad_performance_records_campaign_idx', 'i'),
        ('growthpulse', 'ad_performance_records_date_idx', 'i'),
        ('growthpulse', 'ad_performance_records_pkey', 'i'),
        ('growthpulse', 'analytics_reports_latest_idx', 'i'),
        ('growthpulse', 'analytics_reports_pkey', 'i'),
        ('growthpulse', 'experiment_proposals_idempotency_key_key', 'i'),
        ('growthpulse', 'experiment_proposals_pkey', 'i'),
        ('growthpulse', 'experiment_status_events_pkey', 'i'),
        ('growthpulse', 'experiment_status_events_sequence_unique', 'i'),
        ('growthpulse', 'findings_campaign_idx', 'i'),
        ('growthpulse', 'findings_latest_report_idx', 'i'),
        ('growthpulse', 'findings_pkey', 'i'),
        ('growthpulse', 'pipeline_runs_pkey', 'i'),
        ('growthpulse', 'pipeline_runs_started_at_idx', 'i'),
        ('growthpulse', 'quarantined_records_last_seen_idx', 'i'),
        ('growthpulse', 'quarantined_records_pkey', 'i'),
        ('growthpulse', 'ad_performance_records', 'r'),
        ('growthpulse', 'analytics_reports', 'r'),
        ('growthpulse', 'experiment_proposals', 'r'),
        ('growthpulse', 'experiment_status_events', 'r'),
        ('growthpulse', 'findings', 'r'),
        ('growthpulse', 'pipeline_runs', 'r'),
        ('growthpulse', 'quarantined_records', 'r'),
        ('growthpulse_read', 'analytics_reports', 'v'),
        ('growthpulse_read', 'campaign_daily_conversions', 'v'),
        ('growthpulse_read', 'campaign_daily_delivery', 'v'),
        ('growthpulse_read', 'data_quality_issues', 'v'),
        ('growthpulse_read', 'experiment_proposals', 'v'),
        ('growthpulse_read', 'experiment_status_history', 'v'),
        ('growthpulse_read', 'findings', 'v'),
        ('growthpulse_read', 'latest_reports', 'v'),
        ('growthpulse_read', 'pipeline_runs', 'v')
),
actual (nspname, objname, kind) as (
    select n.nspname, c.relname, c.relkind::text
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where n.nspname in ('growthpulse', 'growthpulse_read')
    union all
    select n.nspname, p.proname, 'function'
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('growthpulse', 'growthpulse_read')
    -- Standalone types (enums, domains, composite types) the migrations never create.
    union all
    select n.nspname, t.typname, 'type'
    from pg_catalog.pg_type as t
    join pg_catalog.pg_namespace as n on n.oid = t.typnamespace
    where n.nspname in ('growthpulse', 'growthpulse_read')
      and t.typrelid = 0
      and t.typcategory <> 'A'
),
kinds (kind, label) as (
    values ('r', 'tables'), ('v', 'views'), ('i', 'indexes'), ('S', 'sequences'),
        ('function', 'functions'), ('type', 'standalone types')
)
select '03 objects' as check_name, k.label as item,
    (select count(*) from expected as e where e.kind = k.kind)::text as expected,
    (select count(*) from actual as a where a.kind = k.kind)::text as actual,
    (select count(*) from expected as e where e.kind = k.kind)
        = (select count(*) from actual as a where a.kind = k.kind) as ok
from kinds as k
union all
select '03 objects', 'missing objects', 'none',
    coalesce(string_agg(m.nspname || '.' || m.objname, ', '), 'none'), count(*) = 0
from (select * from expected except select * from actual) as m
union all
select '03 objects', 'unexpected objects', 'none',
    coalesce(string_agg(u.nspname || '.' || u.objname || ' (' || u.kind || ')', ', '), 'none'),
    count(*) = 0
from (select * from actual except select * from expected) as u;


-- 04. Ownership: every schema, relation and function belongs to postgres.
select '04 ownership' as check_name, 'owners of all GrowthPulse objects' as item,
    'postgres' as expected,
    string_agg(distinct o.owner, ', ') as actual,
    coalesce(bool_and(o.owner = 'postgres'), false) as ok
from (
    select pg_catalog.pg_get_userbyid(c.relowner) as owner
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where n.nspname in ('growthpulse', 'growthpulse_read')
    union all
    select pg_catalog.pg_get_userbyid(p.proowner)
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('growthpulse', 'growthpulse_read')
    union all
    select pg_catalog.pg_get_userbyid(n.nspowner)
    from pg_catalog.pg_namespace as n
    where n.nspname in ('growthpulse', 'growthpulse_read')
) as o;


-- 05. Tables: columns, constraints, indexes, triggers, policies and RLS, per table.
--     rls=t force=f is expected: RLS on, and the owner (used by the views) not forced.
with expected (relname, structure) as (
    values
        ('ad_performance_records', 'cols=27 cons=25 idx=3 trg=0 pol=3 rls=t force=f fp=07b6965cf0078a1d'),
        ('analytics_reports', 'cols=10 cons=10 idx=2 trg=1 pol=2 rls=t force=f fp=462e0c0ab8f2281e'),
        ('experiment_proposals', 'cols=12 cons=13 idx=2 trg=1 pol=2 rls=t force=f fp=a3a2ccbe0f1c88c5'),
        ('experiment_status_events', 'cols=8 cons=7 idx=2 trg=2 pol=2 rls=t force=f fp=a99405f6600fc2b6'),
        ('findings', 'cols=26 cons=15 idx=3 trg=0 pol=3 rls=t force=f fp=c94b659a59121819'),
        ('pipeline_runs', 'cols=10 cons=12 idx=2 trg=0 pol=3 rls=t force=f fp=a896888255937098'),
        ('quarantined_records', 'cols=12 cons=11 idx=2 trg=0 pol=3 rls=t force=f fp=648b7360ec9d5b23')
),
actual (relname, structure) as (
    select c.relname,
        format('cols=%s cons=%s idx=%s trg=%s pol=%s rls=%s force=%s fp=%s',
            cols.n, cons.n, idx.n, trg.n, pol.n, c.relrowsecurity, c.relforcerowsecurity,
            left(md5(concat_ws(' | ', cols.def, cons.def, idx.def, trg.def, pol.def)), 16))
    from pg_catalog.pg_class as c
    cross join lateral (
        select count(*) as n, string_agg(format('%s %s%s%s%s', a.attname,
            pg_catalog.format_type(a.atttypid, a.atttypmod),
            case when a.attnotnull then ' not null' end,
            case when a.attidentity <> '' then ' identity ' || a.attidentity::text end,
            ' default ' || pg_catalog.pg_get_expr(d.adbin, d.adrelid)), ', '
            order by a.attnum) as def
        from pg_catalog.pg_attribute as a
        left join pg_catalog.pg_attrdef as d on d.adrelid = a.attrelid and d.adnum = a.attnum
        where a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
    ) as cols
    cross join lateral (
        select count(*) as n,
            string_agg(k.conname || ' ' || pg_catalog.pg_get_constraintdef(k.oid), ', '
                order by k.conname) as def
        from pg_catalog.pg_constraint as k where k.conrelid = c.oid
    ) as cons
    cross join lateral (
        select count(*) as n,
            string_agg(pg_catalog.pg_get_indexdef(i.indexrelid), ', '
                order by pg_catalog.pg_get_indexdef(i.indexrelid)) as def
        from pg_catalog.pg_index as i where i.indrelid = c.oid
    ) as idx
    cross join lateral (
        select count(*) as n,
            string_agg(pg_catalog.pg_get_triggerdef(t.oid), ', ' order by t.tgname) as def
        from pg_catalog.pg_trigger as t where t.tgrelid = c.oid and not t.tgisinternal
    ) as trg
    cross join lateral (
        select count(*) as n, string_agg(format('%s %s %s %s %s', p.polname, p.polcmd::text,
            (select string_agg(r.rolname, ',' order by r.rolname)
             from pg_catalog.pg_roles as r where r.oid = any(p.polroles)),
            pg_catalog.pg_get_expr(p.polqual, p.polrelid),
            pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid)), ', ' order by p.polname) as def
        from pg_catalog.pg_policy as p where p.polrelid = c.oid
    ) as pol
    where c.relnamespace = (
        select oid from pg_catalog.pg_namespace where nspname = 'growthpulse'
    )
      and c.relkind = 'r'
)
select '05 tables' as check_name, e.relname as item, e.structure as expected,
    coalesce(a.structure, 'missing') as actual,
    a.structure is not distinct from e.structure as ok
from expected as e
left join actual as a using (relname)
order by 2;


-- 06. Views: owner-rights mode and definition, per view.
with expected (relname, definition) as (
    values
        ('analytics_reports', 'options=security_invoker=false fp=812184e7f224636b'),
        ('campaign_daily_conversions', 'options=security_invoker=false fp=3e254f2008e63451'),
        ('campaign_daily_delivery', 'options=security_invoker=false fp=32b4db0f816c82a4'),
        ('data_quality_issues', 'options=security_invoker=false fp=1357a93396993e05'),
        ('experiment_proposals', 'options=security_invoker=false fp=6a517a8216058f79'),
        ('experiment_status_history', 'options=security_invoker=false fp=dd628a9b394d4941'),
        ('findings', 'options=security_invoker=false fp=ddb8b239cd8de7d4'),
        ('latest_reports', 'options=security_invoker=false fp=2c8ff2c7409460a6'),
        ('pipeline_runs', 'options=security_invoker=false fp=9b0583eaade771f9')
),
actual (relname, definition) as (
    select c.relname,
        format('options=%s fp=%s', array_to_string(c.reloptions, ','),
            left(md5(pg_catalog.pg_get_viewdef(c.oid)), 16))
    from pg_catalog.pg_class as c
    where c.relnamespace = (
        select oid from pg_catalog.pg_namespace where nspname = 'growthpulse_read'
    )
      and c.relkind = 'v'
)
select '06 views' as check_name, e.relname as item, e.definition as expected,
    coalesce(a.definition, 'missing') as actual,
    a.definition is not distinct from e.definition as ok
from expected as e
left join actual as a using (relname)
order by 2;


-- 07. Trigger functions: not SECURITY DEFINER, search_path pinned, definition unchanged.
with expected (proname, definition) as (
    values
        ('check_experiment_status_transition', 'secdef=f config=search_path="" fp=d18b5c3eb0df6864'),
        ('reject_update', 'secdef=f config=search_path="" fp=b5f97ee84e165af3')
),
actual (proname, definition) as (
    select p.proname,
        format('secdef=%s config=%s fp=%s', p.prosecdef, array_to_string(p.proconfig, ','),
            left(md5(pg_catalog.pg_get_functiondef(p.oid)), 16))
    from pg_catalog.pg_proc as p
    where p.pronamespace = (
        select oid from pg_catalog.pg_namespace where nspname = 'growthpulse'
    )
)
select '07 functions' as check_name, e.proname as item, e.definition as expected,
    coalesce(a.definition, 'missing') as actual,
    a.definition is not distinct from e.definition as ok
from expected as e
left join actual as a using (proname)
order by 2;


-- 08. App roles: no elevated attributes, login still disabled, session defaults set.
select '08 roles' as check_name, e.rolname as item, e.expected,
    coalesce(format(
        'super=%s createrole=%s createdb=%s bypassrls=%s replication=%s inherit=%s '
        'login=%s connlimit=%s',
        r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication,
        r.rolinherit, r.rolcanlogin, r.rolconnlimit), 'missing') as actual,
    coalesce(format(
        'super=%s createrole=%s createdb=%s bypassrls=%s replication=%s inherit=%s '
        'login=%s connlimit=%s',
        r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication,
        r.rolinherit, r.rolcanlogin, r.rolconnlimit) = e.expected, false) as ok
from (
    values
        ('growthpulse_writer',
         'super=f createrole=f createdb=f bypassrls=f replication=f inherit=f login=f connlimit=3'),
        ('growthpulse_dashboard',
         'super=f createrole=f createdb=f bypassrls=f replication=f inherit=f login=f connlimit=5')
) as e (rolname, expected)
left join pg_catalog.pg_roles as r on r.rolname = e.rolname
union all
select '08 roles', e.rolname || ' session defaults', e.expected,
    coalesce(s.settings, 'missing'), s.settings is not distinct from e.expected
from (
    values
        ('growthpulse_writer',
         'idle_in_transaction_session_timeout=60s; lock_timeout=10s; '
         'search_path=growthpulse; statement_timeout=120s'),
        ('growthpulse_dashboard',
         'default_transaction_read_only=on; idle_in_transaction_session_timeout=30s; '
         'search_path=growthpulse_read; statement_timeout=15s')
) as e (rolname, expected)
left join lateral (
    select string_agg(cfg, '; ' order by cfg) as settings
    from pg_catalog.pg_db_role_setting as d
    cross join lateral unnest(d.setconfig) as cfg
    where d.setrole = (select oid from pg_catalog.pg_roles where rolname = e.rolname)
      and d.setdatabase = 0
) as s on true
union all
select '08 roles', 'app roles as members of another role', 'none',
    coalesce(string_agg(pg_catalog.pg_get_userbyid(m.member) || ' in '
        || pg_catalog.pg_get_userbyid(m.roleid), ', '), 'none'),
    count(*) = 0
from pg_catalog.pg_auth_members as m
where pg_catalog.pg_get_userbyid(m.member) in ('growthpulse_writer', 'growthpulse_dashboard')
union all
select '08 roles', 'roles holding membership in an app role', 'postgres (admin only)',
    coalesce(string_agg(format('%s admin=%s inherit=%s set=%s',
        pg_catalog.pg_get_userbyid(m.member), m.admin_option, m.inherit_option, m.set_option),
        ', '), 'none'),
    null
from pg_catalog.pg_auth_members as m
where pg_catalog.pg_get_userbyid(m.roleid) in ('growthpulse_writer', 'growthpulse_dashboard')
order by 2;


-- 09. Grants: the exact set of privileges anyone other than the owner holds on
--     GrowthPulse schemas, tables, columns, views, sequences and functions.
with expected (grantee, object, privilege) as (
    values
        ('growthpulse_dashboard', 'growthpulse_read.analytics_reports', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.campaign_daily_conversions', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.campaign_daily_delivery', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.data_quality_issues', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.experiment_proposals', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.experiment_status_history', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.findings', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.latest_reports', 'SELECT'),
        ('growthpulse_dashboard', 'growthpulse_read.pipeline_runs', 'SELECT'),
        ('growthpulse_dashboard', 'schema growthpulse_read', 'USAGE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(account_timezone)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(campaign_name)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(campaign_type)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(clicks)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(conversion_event_name)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(conversion_stage)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(conversion_value)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(conversions)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(currency)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(impressions)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(ingested_at)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(last_changed_run_id)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(spend)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.ad_performance_records(updated_at)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.analytics_reports', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.analytics_reports', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.experiment_proposals', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.experiment_proposals', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.experiment_status_events', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.experiment_status_events', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.findings', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.findings', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.findings(guard_outcome)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(is_suppressed)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(last_seen_at)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(latest_data_as_of)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(latest_report_id)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(payload)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(percent_change)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(pre_guard_severity)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.findings(severity)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs(counts)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs(error_code)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs(finished_at)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs(report_id)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.pipeline_runs(status)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records', 'INSERT'),
        ('growthpulse_writer', 'growthpulse.quarantined_records', 'SELECT'),
        ('growthpulse_writer', 'growthpulse.quarantined_records(issue_codes)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records(issues)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records(last_seen_at)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records(last_seen_run_id)', 'UPDATE'),
        ('growthpulse_writer', 'growthpulse.quarantined_records(row_index)', 'UPDATE'),
        ('growthpulse_writer', 'schema growthpulse', 'USAGE')
),
actual (grantee, object, privilege) as (
    select coalesce(r.rolname, 'PUBLIC'), x.object, x.privilege
    from (
        select c.relowner as owner, a.grantee, n.nspname || '.' || c.relname as object,
            a.privilege_type as privilege
        from pg_catalog.pg_class as c
        join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
        cross join lateral pg_catalog.aclexplode(c.relacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read')
        union all
        select c.relowner, a.grantee,
            n.nspname || '.' || c.relname || '(' || att.attname || ')', a.privilege_type
        from pg_catalog.pg_attribute as att
        join pg_catalog.pg_class as c on c.oid = att.attrelid
        join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
        cross join lateral pg_catalog.aclexplode(att.attacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read')
        union all
        select n.nspowner, a.grantee, 'schema ' || n.nspname, a.privilege_type
        from pg_catalog.pg_namespace as n
        cross join lateral pg_catalog.aclexplode(n.nspacl) as a
        where n.nspname in ('growthpulse', 'growthpulse_read')
        union all
        select p.proowner, a.grantee, 'function ' || n.nspname || '.' || p.proname,
            a.privilege_type
        from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
        cross join lateral pg_catalog.aclexplode(
            coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))) as a
        where n.nspname in ('growthpulse', 'growthpulse_read')
    ) as x
    left join pg_catalog.pg_roles as r on r.oid = x.grantee
    where x.grantee <> x.owner
)
select '09 grants' as check_name, 'grant entries' as item,
    (select count(*) from expected)::text as expected,
    (select count(*) from actual)::text as actual,
    (select count(*) from expected) = (select count(*) from actual) as ok
union all
select '09 grants', 'expected grants that are missing', 'none',
    coalesce(string_agg(format('%s %s on %s', m.grantee, m.privilege, m.object), ', '), 'none'),
    count(*) = 0
from (select * from expected except select * from actual) as m
union all
select '09 grants', 'grants that were not expected', 'none',
    coalesce(string_agg(format('%s %s on %s', u.grantee, u.privilege, u.object), ', '), 'none'),
    count(*) = 0
from (select * from actual except select * from expected) as u;


-- 10. Supabase API roles: no route into either schema. Effective privileges, which
--     include anything inherited from PUBLIC or another role.
select '10 api roles' as check_name, r.rolname as item, 'usage=f,f objects=0' as expected,
    format('usage=%s,%s objects=%s',
        pg_catalog.has_schema_privilege(r.oid, 'growthpulse', 'USAGE'),
        pg_catalog.has_schema_privilege(r.oid, 'growthpulse_read', 'USAGE'),
        (select count(*)
         from pg_catalog.pg_class as c
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where n.nspname in ('growthpulse', 'growthpulse_read')
           and c.relkind in ('r', 'v', 'S')
           and (pg_catalog.has_table_privilege(r.oid, c.oid,
                    'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
                or (c.relkind = 'S'
                    and pg_catalog.has_sequence_privilege(r.oid, c.oid, 'USAGE'))))
        + (select count(*)
           from pg_catalog.pg_proc as p
           join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
           where n.nspname = 'growthpulse'
             and pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE'))) as actual,
    not pg_catalog.has_schema_privilege(r.oid, 'growthpulse', 'USAGE')
        and not pg_catalog.has_schema_privilege(r.oid, 'growthpulse_read', 'USAGE') as ok
from pg_catalog.pg_roles as r
where r.rolname in ('anon', 'authenticated', 'service_role')
order by 2;


-- 11. Default privileges inside the GrowthPulse schemas grant nothing to anyone else.
select '11 schema default privileges' as check_name,
    n.nspname || coalesce(' type ' || d.defaclobjtype::text, ' (no entries)') as item,
    'grants only to the defining role' as expected,
    coalesce(d.defaclacl::text, 'none') as actual,
    coalesce(not exists (
        select 1 from pg_catalog.aclexplode(d.defaclacl) as a where a.grantee <> d.defaclrole
    ), true) as ok
from pg_catalog.pg_namespace as n
left join pg_catalog.pg_default_acl as d on d.defaclnamespace = n.oid
where n.nspname in ('growthpulse', 'growthpulse_read')
order by 2;


-- 12. Nothing outside the GrowthPulse schemas depends on objects inside them, so the
--     reviewed rollback would not be blocked or reach anything else.
select '12 outside dependencies' as check_name, 'objects outside depending on GrowthPulse' as item,
    'none' as expected,
    coalesce(string_agg(distinct pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid),
        ', '), 'none') as actual,
    count(*) = 0 as ok
from pg_catalog.pg_depend as d
where d.refobjid in (
        select c.oid from pg_catalog.pg_class as c
        join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
        where n.nspname in ('growthpulse', 'growthpulse_read')
        union all
        select p.oid from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
        where n.nspname in ('growthpulse', 'growthpulse_read')
        union all
        select t.oid from pg_catalog.pg_type as t
        join pg_catalog.pg_namespace as n on n.oid = t.typnamespace
        where n.nspname in ('growthpulse', 'growthpulse_read')
    )
  and d.deptype in ('n', 'a')
  and coalesce(
        (select n.nspname from pg_catalog.pg_class as c
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where d.classid = 'pg_catalog.pg_class'::regclass and c.oid = d.objid),
        (select n.nspname from pg_catalog.pg_rewrite as w
         join pg_catalog.pg_class as c on c.oid = w.ev_class
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where d.classid = 'pg_catalog.pg_rewrite'::regclass and w.oid = d.objid),
        (select n.nspname from pg_catalog.pg_constraint as k
         join pg_catalog.pg_namespace as n on n.oid = k.connamespace
         where d.classid = 'pg_catalog.pg_constraint'::regclass and k.oid = d.objid),
        (select n.nspname from pg_catalog.pg_proc as p
         join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
         where d.classid = 'pg_catalog.pg_proc'::regclass and p.oid = d.objid),
        (select n.nspname from pg_catalog.pg_trigger as t
         join pg_catalog.pg_class as c on c.oid = t.tgrelid
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where d.classid = 'pg_catalog.pg_trigger'::regclass and t.oid = d.objid),
        (select n.nspname from pg_catalog.pg_attrdef as ad
         join pg_catalog.pg_class as c on c.oid = ad.adrelid
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where d.classid = 'pg_catalog.pg_attrdef'::regclass and ad.oid = d.objid),
        (select n.nspname from pg_catalog.pg_policy as pol
         join pg_catalog.pg_class as c on c.oid = pol.polrelid
         join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
         where d.classid = 'pg_catalog.pg_policy'::regclass and pol.oid = d.objid),
        (select n.nspname from pg_catalog.pg_type as t
         join pg_catalog.pg_namespace as n on n.oid = t.typnamespace
         where d.classid = 'pg_catalog.pg_type'::regclass and t.oid = d.objid),
        'unknown'
    ) not in ('growthpulse', 'growthpulse_read');


-- 13. Data API: the exposed schemas, if configured in the database, exclude GrowthPulse.
--     Only the pgrst.db_schemas setting is read; no other setting value is shown.
select '13 data api exposure' as check_name, 'pgrst.db_schemas' as item,
    'does not mention growthpulse' as expected,
    coalesce(string_agg(cfg, '; '), 'not set in the database') as actual,
    coalesce(bool_and(cfg not like '%growthpulse%'), true) as ok
from pg_catalog.pg_db_role_setting as d
cross join lateral unnest(d.setconfig) as cfg
where cfg like 'pgrst.db\_schemas=%';


-- 14. No data has been written yet: the persisted pipeline has not run.
select '14 row counts' as check_name, t.relname as item, '0' as expected, t.n::text as actual,
    t.n = 0 as ok
from (
    select 'pipeline_runs' as relname, (select count(*) from growthpulse.pipeline_runs) as n
    union all select 'ad_performance_records',
        (select count(*) from growthpulse.ad_performance_records)
    union all select 'quarantined_records',
        (select count(*) from growthpulse.quarantined_records)
    union all select 'analytics_reports', (select count(*) from growthpulse.analytics_reports)
    union all select 'findings', (select count(*) from growthpulse.findings)
    union all select 'experiment_proposals',
        (select count(*) from growthpulse.experiment_proposals)
    union all select 'experiment_status_events',
        (select count(*) from growthpulse.experiment_status_events)
) as t
order by 2;


-- 15. Temporary CLI login roles. Informational: attributes, expiry and memberships only.
select '15 cli login roles' as check_name, r.rolname as item, 'informational' as expected,
    format('login=%s super=%s bypassrls=%s createrole=%s valid_until=%s member_of=%s',
        r.rolcanlogin, r.rolsuper, r.rolbypassrls, r.rolcreaterole,
        coalesce(r.rolvaliduntil::text, 'never'),
        coalesce((select string_agg(pg_catalog.pg_get_userbyid(m.roleid), ',' order by 1)
                  from pg_catalog.pg_auth_members as m where m.member = r.oid), 'none')) as actual,
    null::boolean as ok
from pg_catalog.pg_roles as r
where r.rolname like 'cli\_login\_%'
union all
select '15 cli login roles', 'count', 'informational', count(*)::text, null
from pg_catalog.pg_roles
where rolname like 'cli\_login\_%'
order by 2;


-- 16. App role access outside the GrowthPulse schemas, through PUBLIC grants.
select '16 outside access' as check_name, r.rolname || ' can create objects in' as item,
    'no schema' as expected, coalesce(s.schemas, 'no schema') as actual,
    s.schemas is null as ok
from pg_catalog.pg_roles as r
cross join lateral (
    select string_agg(n.nspname, ', ' order by n.nspname) as schemas
    from pg_catalog.pg_namespace as n
    where pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE')
      and n.nspname not like 'pg\_temp%' and n.nspname not like 'pg\_toast\_temp%'
) as s
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard')
union all
-- The writer's own tables in growthpulse are excluded; growthpulse_read is not.
select '16 outside access', r.rolname || ' can write tables outside growthpulse', 'none',
    coalesce(w.tables, 'none'), w.tables is null
from pg_catalog.pg_roles as r
cross join lateral (
    select string_agg(n.nspname || '.' || c.relname, ', ' order by 1) as tables
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    where c.relkind in ('r', 'v', 'm', 'p', 'f')
      and n.nspname not in ('growthpulse', 'pg_catalog', 'information_schema')
      and pg_catalog.has_schema_privilege(r.oid, n.oid, 'USAGE')
      and pg_catalog.has_table_privilege(r.oid, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
) as w
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard')
union all
select '16 outside access', r.rolname || ' can use schemas', 'informational',
    string_agg(n.nspname, ', ' order by n.nspname), null
from pg_catalog.pg_roles as r
join pg_catalog.pg_namespace as n on pg_catalog.has_schema_privilege(r.oid, n.oid, 'USAGE')
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard')
  and n.nspname not like 'pg\_toast%' and n.nspname not like 'pg\_temp%'
group by r.rolname
order by 2;


-- 17. Roles that can read every table regardless of these grants. Informational: these
--     are platform roles. RLS still blocks a role without BYPASSRLS from reading rows.
select '17 platform readers' as check_name, r.rolname as item, 'informational' as expected,
    format('super=%s bypassrls=%s login=%s reads_all_data=%s', r.rolsuper, r.rolbypassrls,
        r.rolcanlogin, pg_catalog.pg_has_role(r.oid, 'pg_read_all_data', 'USAGE')) as actual,
    null::boolean as ok
from pg_catalog.pg_roles as r
where (r.rolsuper or r.rolbypassrls or pg_catalog.pg_has_role(r.oid, 'pg_read_all_data', 'USAGE'))
  and r.rolname not like 'pg\_%'
order by 2;


-- 18. Global default privileges. Informational: what future objects grant automatically.
--     Schema USAGE, checked in 10, is what keeps those out of the GrowthPulse schemas.
select '18 global default privileges' as check_name,
    pg_catalog.pg_get_userbyid(d.defaclrole) || ' ' || d.defaclobjtype::text as item,
    'informational' as expected, d.defaclacl::text as actual, null::boolean as ok
from pg_catalog.pg_default_acl as d
where d.defaclnamespace = 0
order by 2;


-- 19. Event triggers. Informational: none were created by these migrations.
select '19 event triggers' as check_name, e.evtname as item, 'informational' as expected,
    format('event=%s owner=%s enabled=%s', e.evtevent,
        pg_catalog.pg_get_userbyid(e.evtowner), e.evtenabled) as actual,
    null::boolean as ok
from pg_catalog.pg_event_trigger as e
order by 2;


rollback;
