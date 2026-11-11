"""Run the migrations and the persistence code against a real, throwaway PostgreSQL server.

Skipped unless GROWTHPULSE_TEST_DATABASE_URL is set. The tests drop and recreate the
growthpulse schemas and roles, so the URL must point to a disposable database that
accepts the test roles without a password (trust authentication), such as the CI service
container. Supabase hosts are refused outright.
"""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo
from psycopg import errors as pg_errors
from psycopg import sql
from pydantic import SecretStr

from factories import make_row
from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import generate_synthetic_rows
from growthpulse_ai.persistence import connection_check, provision_roles, read, store
from growthpulse_ai.persistence.connection import (
    BACKEND_ROLE,
    DASHBOARD_ROLE,
    open_connection,
    secure_conninfo,
)
from growthpulse_ai.persistence.errors import DatabaseUnavailableError
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


CHECKS = PROJECT_ROOT / "supabase" / "verification" / "post_migration_checks.sql"


def run_verification() -> list[tuple]:
    """Run the verification script statement by statement, as psql would."""
    with admin() as connection:
        # Test database only: stand in for the Supabase CLI's migration history table.
        connection.execute("create schema if not exists supabase_migrations")
        connection.execute(
            "create table if not exists supabase_migrations.schema_migrations "
            "(version text primary key, statements text[], name text)"
        )
        for path in MIGRATIONS:
            version, name = path.stem.split("_", 1)
            connection.execute(
                "insert into supabase_migrations.schema_migrations (version, name) "
                "values (%s, %s) on conflict do nothing",
                (version, name),
            )
    rows: list[tuple] = []
    with admin() as connection:
        for statement in CHECKS.read_text().split(";\n"):
            if not statement.strip():
                continue
            cursor = connection.execute(statement)
            if cursor.description:
                rows.extend(cursor.fetchall())
        assert connection.info.transaction_status.name == "IDLE"  # ended with ROLLBACK
    return rows


def test_post_migration_checks_pass_on_the_tested_schema():
    rows = run_verification()
    assert len({row[0] for row in rows}) >= 17
    for check, item, expected, actual, ok in rows:
        if check in {"02 schemas", "04 ownership"}:
            # The test owner stands in for Supabase's postgres.
            assert OWNER in actual, (check, item)
        elif check == "08 roles" and item in {BACKEND_ROLE, DASHBOARD_ROLE}:
            # Login is enabled here so the tests can connect; on Supabase it is still off.
            assert actual == expected.replace("login=f", "login=t"), item
        else:
            assert ok is not False, (check, item, expected, actual)


def test_post_migration_checks_run_in_a_read_only_transaction():
    statements = [s for s in CHECKS.read_text().split(";\n") if s.strip()]
    assert statements[0].strip().endswith("begin transaction read only")
    assert statements[-1].strip() == "rollback"
    with admin() as connection:
        connection.execute(statements[0])
        with pytest.raises(pg_errors.ReadOnlySqlTransaction):
            connection.execute("create table public.should_not_exist (id int)")


def test_post_migration_checks_detect_drift():
    with admin() as connection:
        connection.execute("grant select on growthpulse.findings to anon")
        connection.execute("create table growthpulse.extra_table (id int)")
    try:
        failing = {(check, item) for check, item, *_, ok in run_verification() if ok is False}
    finally:
        with admin() as connection:
            connection.execute("drop table growthpulse.extra_table")
            connection.execute("revoke select on growthpulse.findings from anon")
    assert ("09 grants", "grants that were not expected") in failing
    assert ("03 objects", "unexpected objects") in failing
    assert ("03 objects", "tables") in failing


# Connection check and role provisioning


def test_tls_check_runs_on_a_real_connection():
    # Uses the real psycopg connection object, so an API the driver lacks fails here.
    with writer() as connection:
        results = dict(connection_check.check_tls(connection))
    assert results["TLS in use"] is False  # the throwaway server has no TLS
    assert results["client requires TLS 1.2 or newer"] is True


def test_connection_check_passes_for_both_roles():
    with writer() as connection:
        backend = connection_check.check_backend(connection)
    with as_role(DASHBOARD_ROLE) as connection:
        dashboard = connection_check.check_dashboard(connection)
    failures = [name for name, ok in [*backend, *dashboard] if not ok]
    assert failures == []
    assert len(backend) == 6 + len(connection_check.TABLES) + 1
    assert len(dashboard) == 4 + len(connection_check.VIEWS) + 1


def test_connection_check_detects_privilege_drift():
    with admin() as connection:
        connection.execute(
            "grant delete on growthpulse.ad_performance_records to growthpulse_writer"
        )
    try:
        with writer() as connection:
            results = dict(connection_check.check_backend(connection))
    finally:
        with admin() as connection:
            connection.execute(
                "revoke delete on growthpulse.ad_performance_records from growthpulse_writer"
            )
    assert results["writer: cannot delete records"] is False


def test_connection_check_leaves_nothing_behind():
    before = scalar("select count(*) from growthpulse.pipeline_runs")
    with writer() as connection:
        connection_check.check_backend(connection)
        assert connection.info.transaction_status.name == "IDLE"
    assert scalar("select count(*) from growthpulse.pipeline_runs") == before


def stored_verifier(role: str) -> str:
    return scalar("select rolpassword from pg_authid where rolname = %s", (role,))


PROVISION = {
    "project_ref": "abcdefghijklmnopqrst",
    "pooler_host": "aws-1-ap-southeast-2.pooler.supabase.com",
}


class StatementSpy:
    """Wraps a connection and records the exact SQL text sent to the server."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.pgconn = connection.pgconn
        self.sent: list[str] = []

    def transaction(self):
        return self.connection.transaction()

    def cursor(self):
        spy = self

        class Cursor:
            def __init__(self) -> None:
                self.inner = spy.connection.cursor()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def execute(self, query, params=None):
                text = query if isinstance(query, str) else query.as_string(spy.connection)
                spy.sent.append(text)
                return self.inner.execute(query, params)

        return Cursor()


def test_provisioning_sets_scram_hashes_and_writes_a_private_env(tmp_path):
    env = tmp_path / ".env"
    with owner() as connection:
        spy = StatementSpy(connection)
        roles = provision_roles.provision(
            spy, env_path=env, rotate=True, owner_role=OWNER, **PROVISION
        )
    alters = [text for text in spy.sent if text.startswith("alter role")]
    assert len(alters) == 2
    assert all("with login password 'SCRAM-SHA-256$" in text for text in alters)
    # Every other statement only reads, and the two writes touch only the two app roles.
    assert [text.split(" with ")[0] for text in alters] == [
        f'alter role "{BACKEND_ROLE}"',
        f'alter role "{DASHBOARD_ROLE}"',
    ]
    reads = [text for text in spy.sent if text not in alters]
    assert reads
    assert all(re.match(r"\s*(select|show)\b", text) for text in reads)
    assert roles == [BACKEND_ROLE, DASHBOARD_ROLE]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not (tmp_path / ".env.provisioning").exists()
    text = env.read_text()
    passwords = {}
    for role, key in provision_roles.ROLES.items():
        match = re.search(
            rf"^{key}=postgresql://{role}\.abcdefghijklmnopqrst:([A-Za-z0-9]{{40}})"
            r"@aws-1-ap-southeast-2\.pooler\.supabase\.com:5432/postgres\?sslmode=require$",
            text,
            re.MULTILINE,
        )
        assert match, key
        passwords[role] = match.group(1)
        verifier = stored_verifier(role)
        assert verifier.startswith("SCRAM-SHA-256$")
        assert passwords[role] not in verifier
        # The plaintext was never sent: only the hash computed on this machine was.
        assert not any(passwords[role] in text for text in spy.sent)
        assert any(verifier in text for text in alters)
    assert passwords[BACKEND_ROLE] != passwords[DASHBOARD_ROLE]
    assert "APP_ENV=local" in text  # the rest of .env.example is kept
    assert scalar(
        "select bool_and(rolcanlogin) from pg_roles where rolname in (%s, %s)",
        (BACKEND_ROLE, DASHBOARD_ROLE),
    )


def test_provisioning_refuses_roles_that_can_already_log_in(tmp_path):
    before = stored_verifier(BACKEND_ROLE)
    with owner() as connection, pytest.raises(provision_roles.ProvisioningError, match="rotate"):
        provision_roles.provision(
            connection, env_path=tmp_path / ".env", owner_role=OWNER, **PROVISION
        )
    assert stored_verifier(BACKEND_ROLE) == before
    assert not (tmp_path / ".env").exists()


def test_provisioning_failure_changes_no_role_and_leaves_no_file(tmp_path, monkeypatch):
    before = {role: stored_verifier(role) for role in provision_roles.ROLES}

    def broken_write(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(provision_roles, "write_private_file", broken_write)
    with owner() as connection, pytest.raises(OSError):
        provision_roles.provision(
            connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
        )
    assert {role: stored_verifier(role) for role in provision_roles.ROLES} == before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("write_completes", [False, True])
def test_failure_after_the_file_is_written_removes_it(tmp_path, monkeypatch, write_completes):
    """A write that fails partway, or fully, before COMMIT: roll back and delete the file."""
    before = {role: stored_verifier(role) for role in provision_roles.ROLES}
    real_write = provision_roles.write_private_file

    def write_then_fail(path, text):
        real_write(path, text if write_completes else text[:10])
        raise OSError("fsync failed")

    monkeypatch.setattr(provision_roles, "write_private_file", write_then_fail)
    with owner() as connection, pytest.raises(OSError, match="fsync"):
        provision_roles.provision(
            connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
        )
    assert {role: stored_verifier(role) for role in provision_roles.ROLES} == before
    assert list(tmp_path.iterdir()) == []


def test_provisioning_refuses_a_server_without_scram(tmp_path):
    before = stored_verifier(BACKEND_ROLE)
    with owner() as connection:
        connection.execute("set password_encryption = 'md5'")
        with pytest.raises(provision_roles.ProvisioningError, match="scram-sha-256"):
            provision_roles.provision(
                connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
            )
    assert stored_verifier(BACKEND_ROLE) == before


def test_provisioning_refuses_a_role_with_elevated_attributes(tmp_path):
    with admin() as connection:
        connection.execute("alter role growthpulse_dashboard createdb")
    try:
        with (
            owner() as connection,
            pytest.raises(provision_roles.ProvisioningError, match="elevated"),
        ):
            provision_roles.provision(
                connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
            )
    finally:
        with admin() as connection:
            connection.execute("alter role growthpulse_dashboard nocreatedb")


class FailingCommit(StatementSpy):
    """Rolls the transaction back and raises, as a failed COMMIT would."""

    @contextmanager
    def transaction(self):
        with self.connection.transaction():
            yield
            raise psycopg.OperationalError("commit failed")


def test_failed_commit_keeps_the_only_copy_and_says_the_outcome_is_unknown(tmp_path):
    before = {role: stored_verifier(role) for role in provision_roles.ROLES}
    with (
        owner() as connection,
        pytest.raises(
            provision_roles.ProvisioningError, match="commit outcome is unknown"
        ) as caught,
    ):
        provision_roles.provision(
            FailingCommit(connection),
            env_path=tmp_path / ".env",
            rotate=True,
            owner_role=OWNER,
            **PROVISION,
        )
    # A failed COMMIT may or may not have applied, so the new values are not thrown away.
    staged = tmp_path / ".env.provisioning"
    assert [path.name for path in tmp_path.iterdir()] == [staged.name]
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert not re.search(r"[A-Za-z0-9]{40}", str(caught.value))
    # Here the commit really rolled back, which the read-only verification would show.
    assert {role: stored_verifier(role) for role in provision_roles.ROLES} == before


def test_provisioning_refuses_app_roles_with_memberships(tmp_path):
    with admin() as connection:
        connection.execute("grant growthpulse_dashboard to growthpulse_writer")
    try:
        with (
            owner() as connection,
            pytest.raises(provision_roles.ProvisioningError, match="member of another role"),
        ):
            provision_roles.provision(
                connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
            )
    finally:
        with admin() as connection:
            connection.execute("revoke growthpulse_dashboard from growthpulse_writer")


def test_bundled_libpq_refuses_servers_that_skip_scram():
    # The throwaway server uses trust authentication, so it never asks for SCRAM. With
    # require_auth, libpq must refuse it rather than continue.
    conninfo = pg_conninfo.make_conninfo(TEST_URL, require_auth="scram-sha-256")
    with pytest.raises(psycopg.OperationalError, match="require_auth|authentication"):
        psycopg.connect(conninfo).close()


def test_application_conninfo_is_refused_by_a_server_that_skips_scram():
    # Build the URL exactly as the app does, then drop only TLS, because the throwaway
    # server has none. The require_auth that secure_conninfo added must stop the login.
    params = pg_conninfo.conninfo_to_dict(TEST_URL)
    params["user"] = BACKEND_ROLE
    params.pop("password", None)
    params.pop("sslmode", None)
    app_conninfo = secure_conninfo(
        SecretStr(pg_conninfo.make_conninfo("", **params)), "DATABASE_URL"
    )
    without_tls = pg_conninfo.make_conninfo(app_conninfo, sslmode="disable")
    assert pg_conninfo.conninfo_to_dict(without_tls)["require_auth"] == "scram-sha-256"
    with pytest.raises(DatabaseUnavailableError):
        open_connection(
            without_tls, expected_role=BACKEND_ROLE, application_name="growthpulse-tests"
        )


def test_connection_check_detects_dashboard_reaching_base_tables():
    with admin() as connection:
        connection.execute("grant usage on schema growthpulse to growthpulse_dashboard")
        connection.execute(
            "grant select on growthpulse.quarantined_records to growthpulse_dashboard"
        )
    try:
        with as_role(DASHBOARD_ROLE) as connection:
            results = dict(connection_check.check_dashboard(connection))
    finally:
        with admin() as connection:
            connection.execute(
                "revoke select on growthpulse.quarantined_records from growthpulse_dashboard"
            )
            connection.execute("revoke usage on schema growthpulse from growthpulse_dashboard")
    assert results["dashboard: refused when reading a base table"] is False
    assert results["dashboard: cannot use growthpulse"] is False


def test_provisioning_needs_the_owner(tmp_path):
    with writer() as connection, pytest.raises(provision_roles.ProvisioningError, match="owner"):
        provision_roles.provision(
            connection, env_path=tmp_path / ".env", rotate=True, owner_role=OWNER, **PROVISION
        )


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
