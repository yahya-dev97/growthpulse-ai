"""The read-only G4 verification script (supabase/verification/m7a_checks.sql).

Runs it on a disposable PostgreSQL server in the state gate G4 creates: the exact Milestone 4
data, upgraded through Milestone 5 (Phase D) and then migrations 9 to 12. Every check must
pass, and drift in each guarded property must make it fail. Never connects to Supabase.
"""

import pytest

from m4_frozen import run_milestone_4_pipeline
from multi_client import END, START
from pg_helpers import (
    AUTHENTICATOR,
    DASHBOARD,
    M4_MIGRATIONS,
    M5_MIGRATIONS,
    M7A_MIGRATIONS,
    MIGRATIONS,
    TEST_URL,
    WRITER,
    admin,
    apply,
    as_role,
    drop_everything,
    enable_test_logins,
    owner,
    prepare_server,
    refuse_hosted_database,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_URL, reason="GROWTHPULSE_TEST_DATABASE_URL is not set"),
]

M7A_CHECKS = MIGRATIONS[0].parents[1] / "verification" / "m7a_checks.sql"
ALL_CHECKS = {
    "01 migration history", "02 views fail closed", "03 view options", "04 context",
    "05 sign-in", "06 function privileges", "07 identity tables", "08 writer policies",
    "09 data api exposure", "10 identity state",
}  # fmt: skip


@pytest.fixture(autouse=True)
def g4_state():
    """The state right after G4 on Supabase: app logins as provisioned, none for sign-in."""
    refuse_hosted_database()
    prepare_server()
    drop_everything()
    apply(M4_MIGRATIONS)
    enable_test_logins()
    for _ in range(2):
        run_milestone_4_pipeline(lambda: as_role(WRITER), START, END)
    apply(M5_MIGRATIONS)
    apply(M7A_MIGRATIONS)
    enable_test_logins()
    with owner() as connection:
        connection.execute(f"alter role {AUTHENTICATOR} nologin")
    yield
    drop_everything()


def run_checks() -> list[tuple]:
    with admin() as connection:
        connection.execute("create schema if not exists supabase_migrations")
        connection.execute(
            "create table if not exists supabase_migrations.schema_migrations "
            "(version text primary key, statements text[], name text)"
        )
        connection.execute("truncate supabase_migrations.schema_migrations")
        for path in [*M4_MIGRATIONS, *M5_MIGRATIONS, *M7A_MIGRATIONS]:
            version, name = path.stem.split("_", 1)
            connection.execute(
                "insert into supabase_migrations.schema_migrations (version, name) values (%s, %s)",
                (version, name),
            )
    results: list[tuple] = []
    with admin() as connection:
        for statement in M7A_CHECKS.read_text().split(";\n"):
            if statement.strip():
                cursor = connection.execute(statement)
                if cursor.description:
                    results.extend(cursor.fetchall())
        assert connection.info.transaction_status.name == "IDLE"  # ended with ROLLBACK
    return results


def test_every_g4_check_passes_right_after_the_upgrade():
    results = run_checks()
    assert {row[0] for row in results} == ALL_CHECKS
    assert len([r for r in results if r[0] == "02 views fail closed"]) == 19  # 18 + total
    for check, item, expected, actual, ok in results:
        assert ok is True, (check, item, expected, actual)


def test_the_script_is_read_only_and_never_begins_a_context():
    text = M7A_CHECKS.read_text().lower()
    code = "\n".join(line.split("--")[0] for line in text.splitlines())
    assert code.strip().startswith("begin transaction read only;")
    assert code.strip().endswith("rollback;")
    for forbidden in (
        "begin_request",
        "begin_job",
        "set_config",
        "inner_pad",
        "outer_pad",
        "token_hash",
        "ciphertext",
        "insert ",
        "update ",
        "delete ",
        "grant ",
    ):
        assert forbidden not in code, forbidden


POOLER_CHECK = MIGRATIONS[0].parents[1] / "verification" / "m7a_pooler_check.sql"


def test_the_pooler_check_passes_on_a_reused_server_connection():
    """Without Supavisor, one reused connection stands in for the pooler's server side."""
    with admin() as connection:
        results = []
        for _ in range(3):
            for statement in POOLER_CHECK.read_text().split(";\n"):
                lines = statement.splitlines()
                code = "\n".join(line for line in lines if not line.startswith("--"))
                if code.strip():
                    cursor = connection.execute(code)
                    if cursor.description:
                        results.extend(cursor.fetchall())
        assert connection.info.transaction_status.name == "IDLE"
    # Two-column rows are checks or backend PIDs; begin_job's own row has one column.
    checks = [tuple(row) for row in results if len(row) == 2 and row[0] != "backend"]
    assert len(checks) == 3 * 4
    assert all(ok is True for _, ok in checks), checks


def test_the_pooler_check_writes_nothing():
    text = POOLER_CHECK.read_text().lower()
    code = "\n".join(line.split("--")[0] for line in text.splitlines())
    assert "begin transaction read only;" in code
    for forbidden in ("insert ", "update ", "delete ", "set_config", "grant ", "alter ", "create "):
        assert forbidden not in code, forbidden


def test_the_checks_fail_on_drift():
    with owner() as connection:
        connection.execute("alter view growthpulse_read.clients reset (security_barrier)")
        connection.execute(
            "create or replace view growthpulse_read.findings_unfiltered as "
            "select finding_id from growthpulse.findings"
        )
        connection.execute(
            f"grant execute on function growthpulse_auth.create_session(uuid, bytea, timestamptz) "
            f"to {DASHBOARD}"
        )
        connection.execute(f"alter role {AUTHENTICATOR} login")
        connection.execute("alter policy writer_select on growthpulse.clients using (true)")
        connection.execute(
            "insert into growthpulse_auth.users (user_id) values (gen_random_uuid())"
        )
    try:
        failing = {(check, item) for check, item, *_, ok in run_checks() if ok is False}
    finally:
        with owner() as connection:  # the drop script is restrict-only
            connection.execute("drop view growthpulse_read.findings_unfiltered")
    assert ("02 views fail closed", "findings_unfiltered") in failing
    assert ("02 views fail closed", "number of dashboard views") in failing
    assert ("03 view options", "clients") in failing
    assert ("03 view options", "findings_unfiltered") in failing
    assert ("05 sign-in", "growthpulse_authenticator") in failing
    assert ("05 sign-in", "create_session") in failing
    assert ("08 writer policies", "policies for growthpulse_writer") in failing
    assert ("10 identity state", "users") in failing
