-- GrowthPulse AI: read-only checks to run after `supabase db push`.
--
-- Every statement is a SELECT. Nothing here changes the database. Run each query in the
-- Supabase SQL editor and compare the result with the expectation above it.


-- 1. Migration history. Expect exactly 3 rows: 20261111100000, 20261111100100, 20261111100200.
select version, name
from supabase_migrations.schema_migrations
where version like '20261111%'
order by version;


-- 2. Ownership. Expect one row: postgres. Any other owner means the views may not be able
--    to read the base tables.
select distinct pg_catalog.pg_get_userbyid(c.relowner) as owner
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname in ('growthpulse', 'growthpulse_read')
union
select pg_catalog.pg_get_userbyid(nspowner)
from pg_catalog.pg_namespace
where nspname in ('growthpulse', 'growthpulse_read');


-- 3. Row-level security. Expect 7 rows, all true.
select c.relname, c.relrowsecurity
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'growthpulse' and c.relkind = 'r'
order by c.relname;


-- 4. App role attributes. Expect 2 rows with every column false. rolcanlogin stays false
--    until you enable login when setting the passwords.
select rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication,
    rolinherit, rolcanlogin
from pg_catalog.pg_roles
where rolname in ('growthpulse_writer', 'growthpulse_dashboard');


-- 5. Memberships. Expect no rows where an app role is the member. Rows where postgres is
--    the member of an app role are normal: the creator holds ADMIN on roles it creates.
select pg_catalog.pg_get_userbyid(roleid) as role,
    pg_catalog.pg_get_userbyid(member) as member,
    admin_option, inherit_option, set_option
from pg_catalog.pg_auth_members
where pg_catalog.pg_get_userbyid(roleid) like 'growthpulse%'
   or pg_catalog.pg_get_userbyid(member) like 'growthpulse%';


-- 6. Supabase API roles. Expect 6 rows, all false.
select r.rolname, n.nspname,
    pg_catalog.has_schema_privilege(r.rolname, n.nspname, 'USAGE') as has_usage
from pg_catalog.pg_roles r
cross join pg_catalog.pg_namespace n
where r.rolname in ('anon', 'authenticated', 'service_role')
  and n.nspname in ('growthpulse', 'growthpulse_read')
order by 1, 2;


-- 7. Data API exposure. Expect pgrst.db_schemas, if listed, not to mention growthpulse.
select rolname, rolconfig
from pg_catalog.pg_roles
where rolname = 'authenticator';


-- 8. Schemas the app roles can use. Expect growthpulse for the writer and
--    growthpulse_read for the dashboard, plus anything PUBLIC can use on this project
--    (such as public, extensions, pg_catalog, information_schema). Review any other row.
select r.rolname, n.nspname,
    pg_catalog.has_schema_privilege(r.rolname, n.oid, 'CREATE') as can_create
from pg_catalog.pg_roles r
cross join pg_catalog.pg_namespace n
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard')
  and pg_catalog.has_schema_privilege(r.rolname, n.oid, 'USAGE')
  and n.nspname not like 'pg\_toast%'
  and n.nspname not like 'pg\_temp%'
order by 1, 2;


-- 9. Tables and views outside the GrowthPulse schemas that the app roles can read or
--    write through PUBLIC grants. Expect no rows outside pg_catalog and
--    information_schema. Review any other row.
select r.rolname, n.nspname, c.relname,
    pg_catalog.has_table_privilege(r.rolname, c.oid, 'INSERT, UPDATE, DELETE') as can_write
from pg_catalog.pg_roles r
cross join pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where r.rolname in ('growthpulse_writer', 'growthpulse_dashboard')
  and c.relkind in ('r', 'v', 'm', 'p', 'f')
  and n.nspname not in ('growthpulse', 'growthpulse_read', 'pg_catalog', 'information_schema')
  and pg_catalog.has_schema_privilege(r.rolname, n.oid, 'USAGE')
  and pg_catalog.has_table_privilege(r.rolname, c.oid, 'SELECT, INSERT, UPDATE, DELETE')
order by 1, 2, 3;


-- 10. Roles that can read every table regardless of these grants. Informational: these
--     are platform roles. RLS still blocks those without BYPASSRLS.
select r.rolname, r.rolsuper, r.rolbypassrls, r.rolcanlogin,
    pg_catalog.pg_has_role(r.oid, 'pg_read_all_data', 'USAGE') as reads_all_data
from pg_catalog.pg_roles r
where r.rolsuper
   or r.rolbypassrls
   or pg_catalog.pg_has_role(r.oid, 'pg_read_all_data', 'USAGE')
order by 1;


-- 11. Global default privileges. Informational: shows what future objects will grant
--     automatically. Schema USAGE, checked in query 6, is what keeps those out.
select pg_catalog.pg_get_userbyid(defaclrole) as for_role, defaclobjtype, defaclacl
from pg_catalog.pg_default_acl
where defaclnamespace = 0;
