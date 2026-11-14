-- GrowthPulse AI: read-only verification after the four Milestone 7a.2 migrations
-- (20261114100000 to 20261114100300) are applied on top of Milestone 5 (gate G4).
--
-- Run as postgres with psql, through the session pooler (port 5432), right after the push:
--     psql "$CONN" -f supabase/verification/m7a_checks.sql
--
-- Safety, as for milestone_5_checks.sql:
--   - The whole script runs inside BEGIN TRANSACTION READ ONLY and ends with ROLLBACK, so
--     the server refuses any write. Every statement is a SELECT over system catalogs, the
--     migration history and GrowthPulse objects (counts, names and IDs only).
--   - It never begins a request or job context, and reads no key, token, hash, ciphertext
--     or setting value other than pgrst.db_schemas and this transaction's own context.
--
-- Output: every query returns check_name, item, expected, actual, ok.
--   ok = true    matches what was tested
--   ok = false   needs review: stop (G4_MIGRATION_CHECKLIST.md, section 6)
--
-- CI runs this script on the exact Milestone 4 state upgraded through migrations 4 to 12
-- (tests/test_postgres_m7a_checks.py), and checks that drift makes it fail.

begin transaction read only;


-- 01. Migration history: all twelve versions, and nothing else.
select '01 migration history' as check_name, e.version as item, 'recorded' as expected,
    coalesce('recorded: ' || m.name, 'missing') as actual,
    m.version is not null as ok
from (values ('20261111100000'), ('20261111100100'), ('20261111100200'), ('20261111200000'), ('20261111200100'), ('20261111200200'), ('20261111200300'), ('20261111200400'), ('20261114100000'), ('20261114100100'), ('20261114100200'), ('20261114100300')) as e (version)
left join supabase_migrations.schema_migrations as m on m.version = e.version
union all
select '01 migration history', 'total versions in history', '12', count(*)::text, count(*) = 12
from supabase_migrations.schema_migrations
order by 2;


-- 02. Every dashboard view fails closed: no request context, so no rows, even for postgres.
select '02 views fail closed' as check_name, v.viewname as item, '0 rows' as expected,
    x.n::text || ' rows' as actual, x.n = 0 as ok
from pg_catalog.pg_views as v
cross join lateral (
    select ((pg_catalog.xpath('/row/n/text()', pg_catalog.query_to_xml(
        pg_catalog.format('select count(*) as n from %I.%I', v.schemaname, v.viewname),
        false, true, ''
    )))[1])::text::bigint as n
) as x
where v.schemaname = 'growthpulse_read'
union all
select '02 views fail closed', 'number of dashboard views', '18', count(*)::text, count(*) = 18
from pg_catalog.pg_views where schemaname = 'growthpulse_read'
order by 2;


-- 03. Every dashboard view is a security barrier running with its owner's rights.
select '03 view options' as check_name, c.relname as item,
    'security_barrier=true, not security_invoker' as expected,
    coalesce(pg_catalog.array_to_string(c.reloptions, ', '), 'none') as actual,
    coalesce('security_barrier=true' = any(c.reloptions), false)
        and not coalesce('security_invoker=true' = any(c.reloptions), false) as ok
from pg_catalog.pg_class as c
join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
where n.nspname = 'growthpulse_read' and c.relkind = 'v'
order by 2;


-- 04. Context: none in this transaction, and the signing key is present (not read).
select '04 context' as check_name, 'context in this transaction' as item, 'none' as expected,
    pg_catalog.count(*)::text || ' contexts' as actual, pg_catalog.count(*) = 0 as ok
from growthpulse_auth.current_context()
union all
select '04 context', 'signing key rows', '1', pg_catalog.count(*)::text, pg_catalog.count(*) = 1
from growthpulse_auth.context_keys;


-- 05. The sign-in role exists without login, and the session functions are its alone.
select '05 sign-in' as check_name, 'growthpulse_authenticator' as item,
    'nologin, connection limit 3' as expected,
    coalesce(
        case when r.rolcanlogin then 'login' else 'nologin' end
            || ', connection limit ' || r.rolconnlimit,
        'missing'
    ) as actual,
    coalesce(not r.rolcanlogin and r.rolconnlimit = 3, false) as ok
from (values (1)) as one (x)
left join pg_catalog.pg_roles as r on r.rolname = 'growthpulse_authenticator'
union all
select '05 sign-in', p.proname, 'execute: growthpulse_authenticator only',
    coalesce(pg_catalog.string_agg(
        case when a.grantee = 0 then 'PUBLIC' else pg_catalog.pg_get_userbyid(a.grantee) end,
        ', ' order by a.grantee
    ), 'nobody'),
    coalesce(pg_catalog.array_agg(
        case when a.grantee = 0 then 'PUBLIC' else pg_catalog.pg_get_userbyid(a.grantee) end
    ) = array['growthpulse_authenticator']::name[], false)
from pg_catalog.pg_proc as p
join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
left join lateral pg_catalog.aclexplode(p.proacl) as a
    on a.privilege_type = 'EXECUTE' and a.grantee <> p.proowner
where n.nspname = 'growthpulse_auth' and p.proname in ('create_session', 'revoke_session')
group by p.proname
order by 2;


-- 06. No GrowthPulse function is executable by PUBLIC or a Supabase API role. A null ACL
-- means PostgreSQL's default, which includes PUBLIC, so it fails too.
select '06 function privileges' as check_name, 'functions open to PUBLIC or API roles' as item,
    'none' as expected,
    coalesce(pg_catalog.string_agg(f.name, ', ' order by f.name), 'none') as actual,
    pg_catalog.count(f.name) = 0 as ok
from (
    select n.nspname || '.' || p.proname as name
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('growthpulse', 'growthpulse_read', 'growthpulse_secrets', 'growthpulse_auth')
        and (
            p.proacl is null
            or exists (
                select from pg_catalog.aclexplode(p.proacl) as a
                where a.privilege_type = 'EXECUTE' and (
                    a.grantee = 0 or pg_catalog.pg_get_userbyid(a.grantee)
                        in ('anon', 'authenticated', 'service_role')
                )
            )
        )
) as f;


-- 07. No application role can touch a growthpulse_auth table directly.
select '07 identity tables' as check_name, 'table privileges held by any role but the owner' as item,
    'none' as expected,
    coalesce(pg_catalog.string_agg(t.grant_line, ', ' order by t.grant_line), 'none') as actual,
    pg_catalog.count(t.grant_line) = 0 as ok
from (
    select c.relname || ': ' || a.privilege_type || ' to '
        || case when a.grantee = 0 then 'PUBLIC' else pg_catalog.pg_get_userbyid(a.grantee) end
        as grant_line
    from pg_catalog.pg_class as c
    join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    cross join lateral pg_catalog.aclexplode(c.relacl) as a
    where n.nspname = 'growthpulse_auth' and c.relkind = 'r' and a.grantee <> c.relowner
) as t;


-- 08. Writer policies: all thirty replaced, none still admitting every row.
select '08 writer policies' as check_name, 'policies for growthpulse_writer' as item,
    '30, none using (true)' as expected,
    pg_catalog.count(*)::text || ', ' || pg_catalog.count(*) filter (
        where p.qual = 'true' or p.with_check = 'true'
    )::text || ' using (true)' as actual,
    pg_catalog.count(*) = 30
        and pg_catalog.count(*) filter (where p.qual = 'true' or p.with_check = 'true') = 0 as ok
from pg_catalog.pg_policies as p
where p.schemaname = 'growthpulse' and 'growthpulse_writer' = any(p.roles);


-- 09. Data API: the exposed schemas, if configured in the database, exclude every
-- GrowthPulse schema, growthpulse_auth included. Also check the dashboard setting.
select '09 data api exposure' as check_name, 'pgrst.db_schemas' as item,
    'does not mention growthpulse' as expected,
    coalesce(pg_catalog.string_agg(cfg, '; '), 'not set in the database') as actual,
    coalesce(pg_catalog.bool_and(cfg not like '%growthpulse%'), true) as ok
from pg_catalog.pg_db_role_setting as d
cross join lateral pg_catalog.unnest(d.setconfig) as cfg
where cfg like 'pgrst.db\_schemas=%';


-- 10. Organizations and identity right after G4: the one internal organization, and no
-- user, membership, grant, session or audit event yet.
select '10 identity state' as check_name, e.item, e.expected, e.actual, e.actual = e.expected as ok
from (
    select 'organizations' as item, '1 internal' as expected,
        pg_catalog.count(*)::text || ' ' || coalesce(pg_catalog.string_agg(kind, ','), '') as actual
    from growthpulse_auth.organizations
    union all
    select 'internal organization is the legacy workspace', 'true',
        (pg_catalog.count(*) = 1)::text
    from growthpulse_auth.organizations
    where kind = 'internal' and workspace_id = '26f3f7ff-8015-413a-8029-06e04727974c'
    union all
    select 'users', '0', pg_catalog.count(*)::text from growthpulse_auth.users
    union all
    select 'memberships', '0', pg_catalog.count(*)::text from growthpulse_auth.memberships
    union all
    select 'client grants', '0', pg_catalog.count(*)::text from growthpulse_auth.client_grants
    union all
    select 'sessions', '0', pg_catalog.count(*)::text from growthpulse_auth.sessions
    union all
    select 'audit events', '0', pg_catalog.count(*)::text from growthpulse_auth.audit_events
) as e
order by 2;


rollback;
