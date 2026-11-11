"""Run the migrations and the persistence code against a real, throwaway PostgreSQL server.

Skipped unless GROWTHPULSE_TEST_DATABASE_URL is set. The tests drop and recreate the
growthpulse schemas and roles, so the URL must point to a disposable database that
accepts the test roles without a password (trust authentication), such as the CI service
container. Supabase hosts are refused outright.
"""

import os
from collections.abc import Iterator
from datetime import date

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo
from psycopg import errors as pg_errors
from psycopg import sql

from factories import make_row
from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import generate_synthetic_rows
from growthpulse_ai.persistence import read, store
from growthpulse_ai.persistence.connection import (
    BACKEND_ROLE,
    DASHBOARD_ROLE,
    open_connection,
)
from growthpulse_ai.persistence.store import ExperimentProposal, record_experiment_proposal
from growthpulse_ai.pipeline.persisted_run import PipelineRunError, run_and_persist
from growthpulse_ai.schema import DataSource

TEST_URL = os.environ.get("GROWTHPULSE_TEST_DATABASE_URL", "")
MIGRATIONS = sorted((PROJECT_ROOT / "supabase" / "migrations").glob("*.sql"))
ROLLBACK = next((PROJECT_ROOT / "supabase" / "rollback").glob("*.sql"))
API_ROLES = ("anon", "authenticated", "service_role")
OWNER = "growthpulse_test_owner"
START, END = date(2026, 10, 1), date(2026, 10, 30)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_URL, reason="GROWTHPULSE_TEST_DATABASE_URL is not set"),
]


def admin() -> psycopg.Connection:
    return psycopg.connect(TEST_URL, autocommit=True)


def as_role(role: str) -> psycopg.Connection:
    params = pg_conninfo.conninfo_to_dict(TEST_URL)
    params["user"] = role
    params.pop("password", None)
    return open_connection(
        pg_conninfo.make_conninfo("", **params),
        expected_role=role,
        application_name="growthpulse-tests",
        read_only=role == DASHBOARD_ROLE,
    )


def writer() -> psycopg.Connection:
    return as_role(BACKEND_ROLE)


def owner() -> psycopg.Connection:
    """Act as the migration owner: like Supabase's postgres, not a superuser."""
    connection = admin()
    connection.execute(sql.SQL("set role {}").format(sql.Identifier(OWNER)))
    return connection


def apply_migrations() -> None:
    with owner() as connection:
        for path in MIGRATIONS:
            # One transaction per file, as the Supabase CLI applies them.
            with connection.transaction():
                connection.execute(path.read_text())
        # Test database only: log in without a password through trust authentication.
        connection.execute("alter role growthpulse_writer login")
        connection.execute("alter role growthpulse_dashboard login")


def run_rollback() -> None:
    with owner() as connection:
        connection.execute(ROLLBACK.read_text())


def create_role_once(connection: psycopg.Connection, role: str, attributes: str) -> None:
    if not connection.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
        connection.execute(
            sql.SQL("create role {} " + attributes).format(sql.Identifier(role))  # noqa: S608
        )


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    host = str(pg_conninfo.conninfo_to_dict(TEST_URL).get("host", ""))
    if "supabase" in host or "pooler" in host:
        raise RuntimeError("Integration tests drop schemas and must never target Supabase")
    with admin() as connection:
        for role in API_ROLES:
            create_role_once(connection, role, "nologin")
        create_role_once(connection, OWNER, "nologin createrole")
        database = connection.execute("select current_database()").fetchone()[0]
        connection.execute(
            sql.SQL("grant create on database {} to {}").format(
                sql.Identifier(database), sql.Identifier(OWNER)
            )
        )
        # Worst case for Supabase: global default privileges that hand every new schema,
        # table, sequence and function to the API roles. The migrations must undo them.
        api = sql.SQL(", ").join(sql.Identifier(role) for role in API_ROLES)
        for objects in (
            "usage on schemas",
            "all on tables",
            "all on sequences",
            "all on functions",
        ):
            connection.execute(
                sql.SQL("alter default privileges for role {} grant " + objects + " to {}").format(
                    sql.Identifier(OWNER), api
                )
            )
    run_rollback()
    apply_migrations()
    yield
    run_rollback()


@pytest.fixture(autouse=True)
def empty_tables() -> None:
    with admin() as connection:
        tables = connection.execute(
            "select tablename from pg_tables where schemaname = 'growthpulse'"
        ).fetchall()
        connection.execute(
            sql.SQL("truncate {} restart identity cascade").format(
                sql.SQL(", ").join(sql.Identifier("growthpulse", t) for (t,) in tables)
            )
        )


def run(connector: SyntheticConnector | None = None):
    return run_and_persist(
        connector or SyntheticConnector(), writer, START, END, triggered_by="test"
    )


def scalar(query: str, params: tuple = ()) -> object:
    with admin() as connection:
        return connection.execute(query, params).fetchone()[0]


# Schema and access control


def test_every_table_has_row_level_security_enabled():
    with admin() as connection:
        rows = connection.execute(
            """
            select c.relname, c.relrowsecurity from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'growthpulse' and c.relkind = 'r'
            """
        ).fetchall()
    assert len(rows) == 7
    assert all(enabled for _, enabled in rows)


@pytest.mark.parametrize("role", [*API_ROLES, DASHBOARD_ROLE])
def test_only_the_writer_can_use_the_private_schema(role: str):
    assert scalar("select has_schema_privilege(%s, 'growthpulse', 'usage')", (role,)) is False


@pytest.mark.parametrize("role", [*API_ROLES, BACKEND_ROLE])
def test_only_the_dashboard_can_use_the_read_schema(role: str):
    assert scalar("select has_schema_privilege(%s, 'growthpulse_read', 'usage')", (role,)) is False


def test_roles_carry_no_elevated_attributes():
    with admin() as connection:
        rows = connection.execute(
            """
            select rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication,
                rolinherit
            from pg_roles where rolname in (%s, %s)
            """,
            (BACKEND_ROLE, DASHBOARD_ROLE),
        ).fetchall()
    assert len(rows) == 2
    assert all(not any(flags) for _, *flags in rows)


# Pipeline runs


def test_run_persists_everything_and_rerun_changes_nothing():
    rows = [*generate_synthetic_rows(), make_row(spend="-1.00")]
    first = run(SyntheticConnector(rows=rows))
    assert first.counts["records_written"] == 300
    assert first.counts["reports_written"] == 1
    assert first.counts["findings_written"] == first.counts["findings_received"] > 0
    assert first.counts["quarantine_entries_written"] == 1

    second = run(SyntheticConnector(rows=rows))
    assert second.report_id == first.report_id
    assert second.counts["records_written"] == 0
    assert second.counts["reports_written"] == 0
    assert second.counts["findings_written"] == 0

    assert scalar("select count(*) from growthpulse.ad_performance_records") == 300
    assert scalar("select count(*) from growthpulse.analytics_reports") == 1
    assert scalar("select count(*) from growthpulse.quarantined_records") == 1
    assert scalar("select count(*) from growthpulse.pipeline_runs where status = 'succeeded'") == 2


def test_restated_day_updates_in_place_and_keeps_the_earlier_report():
    rows = generate_synthetic_rows()
    first = run(SyntheticConnector(rows=rows))
    restated = [dict(row) for row in rows]
    restated[0]["conversions"] = str(float(restated[0]["conversions"]) + 3)
    second = run(SyntheticConnector(rows=restated))

    assert second.counts["records_written"] == 1
    assert second.report_id != first.report_id
    assert scalar("select count(*) from growthpulse.analytics_reports") == 2
    with admin() as connection:
        changed = connection.execute(
            """
            select first_seen_run_id, last_changed_run_id
            from growthpulse.ad_performance_records
            where first_seen_run_id <> last_changed_run_id
            """
        ).fetchall()
    assert changed == [(first.run_id, second.run_id)]


def test_connector_failure_leaves_only_a_sanitized_failed_run():
    class Broken(SyntheticConnector):
        def _fetch_raw(self, start_date, end_date):
            raise RuntimeError("secret detail")

    with pytest.raises(PipelineRunError):
        run(Broken())
    with admin() as connection:
        status, code, finished = connection.execute(
            "select status, error_code, finished_at from growthpulse.pipeline_runs"
        ).fetchone()
    assert (status, code) == ("failed", "connector_unavailable")
    assert finished is not None
    assert scalar("select count(*) from growthpulse.ad_performance_records") == 0


def test_database_rejection_rolls_back_the_whole_run(monkeypatch: pytest.MonkeyPatch):
    original = store.finding_rows

    def corrupt(report):
        rows = original(report)
        rows[0]["severity"] = "catastrophic"
        return rows

    monkeypatch.setattr(store, "finding_rows", corrupt)
    with pytest.raises(PipelineRunError) as caught:
        run()
    assert caught.value.error_code == "database_rejected_data"
    assert caught.value.recorded is True
    assert scalar("select count(*) from growthpulse.ad_performance_records") == 0
    assert scalar("select count(*) from growthpulse.analytics_reports") == 0
    assert scalar("select error_code from growthpulse.pipeline_runs") == "database_rejected_data"


# Writer limits


@pytest.mark.parametrize(
    "statement",
    [
        "delete from growthpulse.ad_performance_records",
        "truncate growthpulse.pipeline_runs",
        "update growthpulse.analytics_reports set data_source = 'live'",
        "update growthpulse.ad_performance_records set record_id = record_id",
        "update growthpulse.ad_performance_records set first_seen_run_id = last_changed_run_id",
        "create table growthpulse.extra (id int)",
        "select * from growthpulse_read.findings",
    ],
)
def test_writer_cannot_exceed_its_grants(statement: str):
    run()
    with writer() as connection, pytest.raises(pg_errors.InsufficientPrivilege):
        connection.execute(statement)


def test_writer_cannot_reopen_or_rewrite_a_finished_run():
    outcome = run()
    with writer() as connection:
        cursor = connection.execute(
            "update growthpulse.pipeline_runs set counts = '{}'::jsonb where run_id = %s",
            (outcome.run_id,),
        )
        assert cursor.rowcount == 0


def test_writer_can_propose_but_never_approve():
    outcome = run()
    finding_id = scalar("select finding_id from growthpulse.findings limit 1")
    proposal = ExperimentProposal(
        data_source=DataSource.SYNTHETIC,
        report_id=outcome.report_id,
        finding_id=finding_id,
        title="Test narrower targeting",
        hypothesis="Narrower targeting lowers cost per platform-attributed conversion.",
        proposed_change="A person runs a split test manually.",
        primary_metric="cost_per_platform_attributed_conversion",
    )
    with writer() as connection:
        proposal_id = record_experiment_proposal(connection, proposal)
        assert record_experiment_proposal(connection, proposal) == proposal_id
        with pytest.raises(pg_errors.InsufficientPrivilege):
            connection.execute(
                "insert into growthpulse.experiment_status_events (proposal_id, status, actor) "
                "values (%s, 'approved', 'pipeline')",
                (proposal_id,),
            )
    assert scalar("select count(*) from growthpulse.experiment_status_events") == 1


# Database-enforced history rules, which apply even to the owner


def test_reports_cannot_be_updated_even_by_the_owner():
    run()
    with admin() as connection, pytest.raises(pg_errors.InsufficientPrivilege, match="append"):
        connection.execute("update growthpulse.analytics_reports set data_source = 'synthetic'")


def test_status_transitions_are_enforced():
    outcome = run()
    finding_id = scalar("select finding_id from growthpulse.findings limit 1")
    with admin() as connection:
        proposal_id = connection.execute(
            """
            insert into growthpulse.experiment_proposals (idempotency_key, data_source,
                report_id, finding_id, title, hypothesis, proposed_change, primary_metric)
            values (repeat('c', 64), 'synthetic', %s, %s, 't', 'h', 'c', 'm')
            returning proposal_id
            """,
            (outcome.report_id, finding_id),
        ).fetchone()[0]

        def add(status: str) -> None:
            connection.execute(
                "insert into growthpulse.experiment_status_events (proposal_id, status, actor) "
                "values (%s, %s, 'analyst')",
                (proposal_id, status),
            )

        with pytest.raises(pg_errors.CheckViolation, match="must be proposed"):
            add("approved")
        add("proposed")
        with pytest.raises(pg_errors.CheckViolation, match="from proposed to completed"):
            add("completed")
        add("approved")
        add("running")
        add("completed")
        with pytest.raises(pg_errors.CheckViolation):
            add("cancelled")
        history = connection.execute(
            "select sequence, status, previous_status from growthpulse.experiment_status_events "
            "order by sequence"
        ).fetchall()
    assert history == [
        (1, "proposed", None),
        (2, "approved", "proposed"),
        (3, "running", "approved"),
        (4, "completed", "running"),
    ]


# Dashboard


def test_dashboard_reads_the_latest_report_and_runs():
    outcome = run()
    with as_role(DASHBOARD_ROLE) as connection:
        report = read.latest_report(connection, "synthetic")
        runs = read.recent_runs(connection)
    assert report["report_id"] == outcome.report_id
    assert [r["run_id"] for r in runs] == [outcome.run_id]
    assert runs[0]["appears_abandoned"] is False


@pytest.mark.parametrize(
    "statement",
    [
        "select * from growthpulse.ad_performance_records",
        "select raw_payload from growthpulse.quarantined_records",
        "insert into growthpulse_read.pipeline_runs (run_id) values (gen_random_uuid())",
        "delete from growthpulse_read.findings",
    ],
)
def test_dashboard_cannot_read_base_tables_or_write(statement: str):
    run()
    with as_role(DASHBOARD_ROLE) as connection:
        # Turning read-only off does not help: the grants still refuse.
        connection.execute("set default_transaction_read_only = off")
        with pytest.raises(pg_errors.InsufficientPrivilege):
            connection.execute(statement)


def test_dashboard_session_is_read_only():
    with as_role(DASHBOARD_ROLE) as connection:
        assert connection.execute("show default_transaction_read_only").fetchone()[0] == "on"


def test_quarantine_view_hides_raw_payloads():
    with admin() as connection:
        columns = connection.execute(
            """
            select column_name from information_schema.columns
            where table_schema = 'growthpulse_read' and table_name = 'data_quality_issues'
            """
        ).fetchall()
    assert columns
    assert "raw_payload" not in {name for (name,) in columns}


def test_delivery_view_counts_each_campaign_day_once():
    # Two conversion definitions per campaign-day repeat the same delivery metrics.
    second_event = {"event_id": "second", "event_name": "Second event", "stage": "lead"}
    rows = [
        make_row(reporting_date=f"2026-10-{day:02d}", **extra)
        for day in range(1, 31)
        for extra in ({}, {"conversion_definition": second_event})
    ]
    run(SyntheticConnector(rows=rows))
    assert scalar("select count(*) from growthpulse.ad_performance_records") == 60
    assert scalar("select count(*) from growthpulse_read.campaign_daily_delivery") == 30


def test_database_refuses_raw_payloads_for_live_data():
    outcome = run()
    with admin() as connection, pytest.raises(pg_errors.CheckViolation):
        connection.execute(
            """
            insert into growthpulse.quarantined_records (quarantine_id, data_source, connector,
                row_index, issue_codes, issues, payload_sha256, raw_payload,
                first_seen_run_id, last_seen_run_id)
            values (repeat('d', 64), 'live', 'meta_ads', 0, array['invalid_field'], '[]',
                repeat('e', 64), '{"spend": "1"}', %(run)s, %(run)s)
            """,
            {"run": outcome.run_id},
        )


def test_largest_values_the_schema_accepts_are_stored_exactly():
    largest = "99999999999999.999999"
    rows = [
        make_row(reporting_date=f"2026-10-{day:02d}", spend=largest, conversion_value=largest)
        for day in range(1, 31)
    ]
    run(SyntheticConnector(rows=rows))
    assert str(scalar("select max(spend) from growthpulse.ad_performance_records")) == largest


@pytest.mark.parametrize(
    ("role", "setting"),
    [
        (BACKEND_ROLE, "statement_timeout=120s"),
        (BACKEND_ROLE, "idle_in_transaction_session_timeout=60s"),
        (DASHBOARD_ROLE, "default_transaction_read_only=on"),
        (DASHBOARD_ROLE, "statement_timeout=15s"),
    ],
)
def test_role_session_defaults_are_set(role: str, setting: str):
    settings = scalar(
        """
        select s.setconfig from pg_db_role_setting s
        join pg_roles r on r.oid = s.setrole where r.rolname = %s
        """,
        (role,),
    )
    assert setting in settings


# Ownership, default privileges and privilege escalation


def test_every_object_belongs_to_the_migration_owner():
    with admin() as connection:
        owners = connection.execute(
            """
            select distinct pg_get_userbyid(c.relowner) from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname in ('growthpulse', 'growthpulse_read')
            union
            select distinct pg_get_userbyid(nspowner) from pg_namespace
            where nspname in ('growthpulse', 'growthpulse_read')
            """
        ).fetchall()
    assert owners == [(OWNER,)]


def test_simulated_global_defaults_are_in_effect():
    """Guards the next tests: without these defaults, the revokes would not be exercised."""
    assert (
        scalar(
            "select count(*) from pg_default_acl where defaclrole = %s::regrole "
            "and defaclnamespace = 0",
            (OWNER,),
        )
        == 4
    )


@pytest.mark.parametrize("role", API_ROLES)
def test_api_roles_hold_no_privilege_on_any_object(role: str):
    with admin() as connection:
        granted = connection.execute(
            """
            select c.relname from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname in ('growthpulse', 'growthpulse_read')
              and c.relkind in ('r', 'v', 'S')
              and (has_table_privilege(%(role)s, c.oid,
                       'select, insert, update, delete, truncate, references, trigger')
                   or (c.relkind = 'S' and has_sequence_privilege(%(role)s, c.oid, 'usage')))
            union all
            select p.proname from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'growthpulse' and has_function_privilege(%(role)s, p.oid, 'execute')
            """,
            {"role": role},
        ).fetchall()
    assert granted == []


def test_nothing_in_the_schemas_is_granted_to_public():
    with admin() as connection:
        public_grants = connection.execute(
            """
            select n.nspname from pg_namespace n, aclexplode(n.nspacl) a
            where n.nspname in ('growthpulse', 'growthpulse_read') and a.grantee = 0
            union all
            select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace,
                aclexplode(c.relacl) a
            where n.nspname in ('growthpulse', 'growthpulse_read') and a.grantee = 0
            union all
            select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace,
                aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
            where n.nspname = 'growthpulse' and a.grantee = 0
            """
        ).fetchall()
    assert public_grants == []


def test_future_tables_stay_unreachable_to_api_roles():
    # A later migration that forgets its revokes still cannot expose a table, because the
    # API roles have no USAGE on the schema.
    with owner() as connection:
        connection.execute("create table growthpulse.future_table (id int)")
    try:
        assert scalar("select has_table_privilege('anon', 'growthpulse.future_table', 'select')")
        with admin() as connection:
            connection.execute("set role anon")
            with pytest.raises(pg_errors.InsufficientPrivilege):
                connection.execute("select * from growthpulse.future_table")
    finally:
        with owner() as connection:
            connection.execute("drop table growthpulse.future_table")


def test_app_roles_are_members_of_nothing():
    assert (
        scalar(
            "select count(*) from pg_auth_members where member in (%s::regrole, %s::regrole)",
            (BACKEND_ROLE, DASHBOARD_ROLE),
        )
        == 0
    )


@pytest.mark.parametrize("role", [BACKEND_ROLE, DASHBOARD_ROLE])
@pytest.mark.parametrize(
    "statement",
    [
        "create role escalated",
        "alter role {me} bypassrls",
        "alter role {me} createrole",
        "alter role {other} password 'x'",
        "grant {other} to {me}",
        "set role {other}",
        f"set role {OWNER}",
        "create schema escalated",
        "create table public.escalated (id int)",
    ],
)
def test_app_roles_cannot_escalate(role: str, statement: str):
    other = DASHBOARD_ROLE if role == BACKEND_ROLE else BACKEND_ROLE
    with as_role(role) as connection:
        connection.execute("set default_transaction_read_only = off")
        with pytest.raises(pg_errors.InsufficientPrivilege):
            connection.execute(statement.format(me=role, other=other))


def test_owner_can_enable_login_but_not_grant_elevated_attributes():
    with owner() as connection:
        connection.execute("alter role growthpulse_dashboard nologin")
        connection.execute("alter role growthpulse_dashboard login")
        for attribute in ("superuser", "bypassrls", "replication"):
            with pytest.raises(pg_errors.InsufficientPrivilege):
                connection.execute(f"alter role growthpulse_dashboard {attribute}")


def test_post_migration_checks_run_read_only_and_pass_here():
    path = PROJECT_ROOT / "supabase" / "verification" / "post_migration_checks.sql"
    statements = [s for s in path.read_text().split(";\n") if "select" in s]
    results = {}
    with admin() as connection, connection.transaction():
        connection.execute("set transaction read only")
        for index, statement in enumerate(statements, start=1):
            # Supabase-only catalogs (history table, authenticator role) are skipped here.
            if "supabase_migrations" in statement:
                continue
            results[index] = connection.execute(statement).fetchall()
    assert results[2] == [(OWNER,)]
    assert len(results[3]) == 7 and all(enabled for _, enabled in results[3])
    assert all(not any(flags[:-1]) for _, *flags in results[4])
    # The creator holds ADMIN on each app role; no app role is a member of anything.
    assert {member for _, member, *_ in results[5]} == {OWNER}
    assert len(results[6]) == 6 and not any(usage for *_, usage in results[6])
    assert {(role, schema) for role, schema, _ in results[8]} >= {
        (BACKEND_ROLE, "growthpulse"),
        (DASHBOARD_ROLE, "growthpulse_read"),
    }
    assert (BACKEND_ROLE, "growthpulse_read") not in {(r, s) for r, s, _ in results[8]}
    assert (DASHBOARD_ROLE, "growthpulse") not in {(r, s) for r, s, _ in results[8]}
    assert results[9] == []


# Rollback. Kept last because it drops and recreates everything.


def test_rollback_refuses_to_drop_objects_others_depend_on():
    with admin() as connection:
        connection.execute(
            "create view public.depends_on_growthpulse as select * from growthpulse.findings"
        )
    try:
        with pytest.raises(pg_errors.DependentObjectsStillExist):
            run_rollback()
        assert scalar("select count(*) from pg_namespace where nspname = 'growthpulse'") == 1
        assert scalar("select count(*) from pg_roles where rolname = %s", (BACKEND_ROLE,)) == 1
    finally:
        with admin() as connection:
            connection.execute("drop view public.depends_on_growthpulse")


def test_rollback_removes_everything_and_migrations_reapply():
    run()
    run_rollback()
    run_rollback()  # Safe to repeat on an already rolled-back database.
    remaining = scalar(
        """
        select (select count(*) from pg_namespace where nspname like 'growthpulse%%')
             + (select count(*) from pg_roles where rolname in (%s, %s))
        """,
        (BACKEND_ROLE, DASHBOARD_ROLE),
    )
    assert remaining == 0
    apply_migrations()
    assert scalar("select count(*) from growthpulse.pipeline_runs") == 0
