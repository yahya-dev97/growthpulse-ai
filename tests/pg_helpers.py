"""Shared helpers for tests that run migrations on a disposable PostgreSQL server."""

import os
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg import conninfo as pg_conninfo
from psycopg import sql

from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.persistence.connection import open_connection

TEST_URL = os.environ.get("GROWTHPULSE_TEST_DATABASE_URL", "")
MIGRATIONS = sorted((PROJECT_ROOT / "supabase" / "migrations").glob("*.sql"))
M4_MIGRATIONS = MIGRATIONS[:3]
M5_MIGRATIONS = MIGRATIONS[3:8]
M7A_MIGRATIONS = MIGRATIONS[8:]
ROLLBACK_DIR = PROJECT_ROOT / "supabase" / "rollback"
DROP_ALL = ROLLBACK_DIR / "20261111100000_drop_growthpulse.sql"
REVERT_M5 = ROLLBACK_DIR / "20261111200000_revert_milestone_5.sql"
REVERT_M7A = ROLLBACK_DIR / "20261114100000_revert_m7a2_tenant_security.sql"
OWNER = "growthpulse_test_owner"
API_ROLES = ("anon", "authenticated", "service_role")
WRITER = "growthpulse_writer"
DASHBOARD = "growthpulse_dashboard"
MANAGER = "growthpulse_manager"
TOKEN_MANAGER = "growthpulse_token_manager"
AUTHENTICATOR = "growthpulse_authenticator"
APP_ROLES = (WRITER, DASHBOARD, MANAGER, TOKEN_MANAGER, AUTHENTICATOR)
SCHEMAS = ("growthpulse", "growthpulse_read", "growthpulse_secrets")  # Milestone 5
ALL_SCHEMAS = (*SCHEMAS, "growthpulse_auth")


def refuse_hosted_database() -> None:
    host = str(pg_conninfo.conninfo_to_dict(TEST_URL).get("host", ""))
    if "supabase" in host or "pooler" in host:
        raise RuntimeError("Integration tests drop schemas and must never target Supabase")


def admin() -> psycopg.Connection:
    return psycopg.connect(TEST_URL, autocommit=True)


def owner() -> psycopg.Connection:
    """The migration owner: like Supabase's postgres, not a superuser."""
    connection = admin()
    connection.execute(sql.SQL("set role {}").format(sql.Identifier(OWNER)))
    return connection


def as_role(role: str) -> psycopg.Connection:
    params = pg_conninfo.conninfo_to_dict(TEST_URL)
    params["user"] = role
    params.pop("password", None)
    return open_connection(
        pg_conninfo.make_conninfo("", **params),
        expected_role=role,
        application_name="growthpulse-tests",
        read_only=role == DASHBOARD,
    )


def prepare_server() -> None:
    """Roles and worst-case global default privileges, as the M4 integration tests do."""
    with admin() as connection:
        for role, attributes in [
            *((r, "nologin") for r in API_ROLES),
            (OWNER, "nologin createrole"),
        ]:
            if not connection.execute(
                "select 1 from pg_roles where rolname = %s", (role,)
            ).fetchone():
                connection.execute(
                    sql.SQL("create role {} " + attributes).format(sql.Identifier(role))  # noqa: S608
                )
        database = connection.execute("select current_database()").fetchone()[0]
        connection.execute(
            sql.SQL("grant create on database {} to {}").format(
                sql.Identifier(database), sql.Identifier(OWNER)
            )
        )
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


def run_sql_file(path: Path) -> None:
    with owner() as connection:
        connection.execute(path.read_text())


def drop_everything() -> None:
    run_sql_file(DROP_ALL)


def apply(paths: Sequence[Path]) -> None:
    """One transaction per file, as the Supabase CLI applies them."""
    with owner() as connection:
        for path in paths:
            with connection.transaction():
                connection.execute(path.read_text())


def enable_test_logins() -> None:
    """Disposable test database only: trust authentication, no passwords."""
    with owner() as connection:
        for role in APP_ROLES:
            exists = connection.execute(
                "select 1 from pg_roles where rolname = %s", (role,)
            ).fetchone()
            if exists:
                connection.execute(sql.SQL("alter role {} login").format(sql.Identifier(role)))


def scalar(query: str, params: tuple = ()) -> object:
    with admin() as connection:
        return connection.execute(query, params).fetchone()[0]


def rows(query: str, params: tuple | dict = ()) -> list[tuple]:
    with admin() as connection:
        return connection.execute(query, params).fetchall()


# Milestone 7a.2: signed-in requests and job scopes.


def sign_in(
    workspace_id,
    role: str = "owner",
    *,
    clients: Sequence = (),
    access: str = "view",
):
    """A user, an active membership and a live session, created as the owner would.

    Returns a SignedInRequest for growthpulse_auth.begin_request. Analysts and viewers get
    a client grant for each of ``clients``. Test database only.
    """
    import secrets
    from uuid import uuid4

    from growthpulse_ai.persistence.context import SignedInRequest, session_token_hash

    user_id, token = uuid4(), secrets.token_bytes(32)
    with admin() as connection:
        connection.execute("insert into growthpulse_auth.users (user_id) values (%s)", (user_id,))
        membership_id = connection.execute(
            "insert into growthpulse_auth.memberships (workspace_id, user_id, role) "
            "values (%s, %s, %s) returning membership_id",
            (workspace_id, user_id, role),
        ).fetchone()[0]
        for client_id in clients:
            connection.execute(
                "insert into growthpulse_auth.client_grants "
                "(membership_id, workspace_id, client_id, access) values (%s, %s, %s, %s)",
                (membership_id, workspace_id, client_id, access),
            )
        connection.execute(
            "insert into growthpulse_auth.sessions (user_id, token_hash, expires_at) "
            "values (%s, %s, now() + interval '1 hour')",
            (user_id, session_token_hash(token)),
        )
    return SignedInRequest(session_token=token, workspace_id=workspace_id, request_id=uuid4())


def request_rows(request, query: str, params: tuple | dict = (), *, role: str = DASHBOARD):
    """Rows of one query in a transaction that starts with begin_request, as ``role``."""
    from growthpulse_ai.persistence.context import begin_request

    with as_role(role) as connection, connection.transaction(), connection.cursor() as cursor:
        begin_request(cursor, request)
        return cursor.execute(query, params or None).fetchall()


def job_rows(kind: str, scope_id, query: str, params: tuple | dict = (), *, role: str = WRITER):
    """Rows of one statement in a transaction that starts with begin_job, as ``role``."""
    from growthpulse_ai.persistence.context import begin_job

    with as_role(role) as connection, connection.transaction(), connection.cursor() as cursor:
        begin_job(cursor, kind, scope_id)
        cursor.execute(query, params or None)
        return cursor.fetchall() if cursor.description else []
