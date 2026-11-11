"""Read-only connection test for the backend and dashboard credentials in .env.

Usage: uv run python -m growthpulse_ai.persistence.connection_check

Each role connects through the normal application path, which already refuses the wrong
login role and anything weaker than sslmode=require. Every query runs inside a
transaction marked READ ONLY, so nothing can be written. Output lists only check names
and PASS or FAIL, never a connection string, host or value.
"""

import sys
from collections.abc import Callable

import psycopg
from psycopg import errors as pg_errors
from psycopg import sql

from growthpulse_ai.config import get_settings
from growthpulse_ai.persistence.connection import connect_backend, connect_dashboard
from growthpulse_ai.persistence.errors import PersistenceError

# Tables the writer reads. Milestone 5 tables need its migrations applied first.
TABLES = (
    "pipeline_runs",
    "ad_performance_records",
    "quarantined_records",
    "analytics_reports",
    "findings",
    "experiment_proposals",
    "experiment_status_events",
    "workspaces",
    "clients",
    "provider_connections",
    "discovered_ad_accounts",
    "ad_accounts",
    "account_sync_config",
    "account_sync_state",
    "analytics_report_breakdowns",
)
# Owner-only: the writer must be refused.
WRITER_REFUSED_TABLES = ("owner_action_log",)
VIEWS = (
    "pipeline_runs",
    "campaign_daily_conversions",
    "campaign_daily_delivery",
    "analytics_reports",
    "latest_reports",
    "findings",
    "data_quality_issues",
    "experiment_proposals",
    "experiment_status_history",
    "latest_client_reports",
    "report_breakdowns",
    "clients",
    "ad_accounts",
    "provider_connections",
    "discovered_ad_accounts",
    "account_sync_status",
    "owner_actions",
)
# Executable SECURITY DEFINER functions in a schema, read from the catalog so no schema
# USAGE is needed. The count must be positive, so a database without them cannot pass.
DEFINER_ACCESS = """
select count(*) > 0 and not coalesce(bool_or(has_function_privilege(p.oid, 'EXECUTE')), false)
from pg_catalog.pg_proc as p
join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
where n.nspname = %s and p.prosecdef
"""

Result = tuple[str, bool]


def check_tls(connection: psycopg.Connection) -> list[Result]:
    """TLS between this machine and the server or pooler it connected to.

    psycopg reports whether TLS is in use, but not the negotiated version. The check
    therefore reads the minimum version this client enforces (libpq's
    ssl_min_protocol_version, TLS 1.2 unless overridden). Only that one setting is read
    from the connection options, which also contain the password.
    """
    pgconn = connection.pgconn
    minimum = ""
    for option in pgconn.info:
        if option.keyword == b"ssl_min_protocol_version":
            minimum = (option.val or option.compiled or b"").decode()
    return [
        ("TLS in use", bool(pgconn.ssl_in_use)),
        ("client requires TLS 1.2 or newer", minimum in {"TLSv1.2", "TLSv1.3"}),
    ]


def _scalar(cursor: psycopg.Cursor, query: str) -> object:
    return cursor.execute(query).fetchone()[0]


def _can_select(connection: psycopg.Connection, schema: str, name: str) -> bool:
    """Run a zero-row SELECT inside a savepoint, so a refusal does not end the test."""
    try:
        with connection.transaction():
            connection.execute(
                sql.SQL("select from {} limit 0").format(sql.Identifier(schema, name))
            )
    except pg_errors.InsufficientPrivilege:
        return False
    return True


def _run(connection: psycopg.Connection, checks: Callable) -> list[Result]:
    """Run checks in a READ ONLY transaction that always ends in ROLLBACK."""
    with (
        connection.transaction(force_rollback=True),
        connection.cursor() as cursor,
    ):
        cursor.execute("set transaction read only")
        results = checks(connection, cursor)
        results.append(
            (
                "test transaction stayed read-only",
                _scalar(cursor, "show transaction_read_only") == "on",
            )
        )
    return results


def _backend_checks(connection: psycopg.Connection, cursor: psycopg.Cursor) -> list[Result]:
    results = [
        (
            "writer: logged in as growthpulse_writer",
            _scalar(cursor, "select current_user") == "growthpulse_writer",
        ),
        (
            "writer: can use growthpulse",
            _scalar(cursor, "select has_schema_privilege('growthpulse', 'USAGE')") is True,
        ),
        (
            "writer: cannot use growthpulse_read",
            _scalar(cursor, "select has_schema_privilege('growthpulse_read', 'USAGE')") is False,
        ),
        (
            "writer: can insert pipeline runs (checked, not done)",
            _scalar(cursor, "select has_table_privilege('growthpulse.pipeline_runs', 'INSERT')")
            is True,
        ),
        (
            "writer: cannot delete records",
            _scalar(
                cursor, "select has_table_privilege('growthpulse.ad_performance_records', 'DELETE')"
            )
            is False,
        ),
        (
            "writer: cannot update reports",
            _scalar(cursor, "select has_table_privilege('growthpulse.analytics_reports', 'UPDATE')")
            is False,
        ),
    ]
    results += [
        (f"writer: can read growthpulse.{table}", _can_select(connection, "growthpulse", table))
        for table in TABLES
    ]
    results += [
        (
            f"writer: refused when reading growthpulse.{table}",
            not _can_select(connection, "growthpulse", table),
        )
        for table in WRITER_REFUSED_TABLES
    ]
    results += _no_token_or_owner_access(cursor, "writer")
    return results


def _no_token_or_owner_access(cursor: psycopg.Cursor, label: str) -> list[Result]:
    """Neither app role may reach encrypted tokens or run an owner action."""
    return [
        (
            f"{label}: cannot use growthpulse_secrets",
            _scalar(cursor, "select has_schema_privilege('growthpulse_secrets', 'USAGE')") is False,
        ),
        (
            f"{label}: cannot run token functions",
            cursor.execute(DEFINER_ACCESS, ("growthpulse_secrets",)).fetchone()[0] is True,
        ),
        (
            f"{label}: cannot run owner actions",
            cursor.execute(DEFINER_ACCESS, ("growthpulse",)).fetchone()[0] is True,
        ),
    ]


def _dashboard_checks(connection: psycopg.Connection, cursor: psycopg.Cursor) -> list[Result]:
    results = [
        (
            "dashboard: logged in as growthpulse_dashboard",
            _scalar(cursor, "select current_user") == "growthpulse_dashboard",
        ),
        (
            "dashboard: session defaults to read-only",
            _scalar(cursor, "show default_transaction_read_only") == "on",
        ),
        (
            "dashboard: cannot use growthpulse",
            _scalar(cursor, "select has_schema_privilege('growthpulse', 'USAGE')") is False,
        ),
        (
            "dashboard: refused when reading a base table",
            not _can_select(connection, "growthpulse", "quarantined_records"),
        ),
    ]
    results += [
        (
            f"dashboard: can read growthpulse_read.{view}",
            _can_select(connection, "growthpulse_read", view),
        )
        for view in VIEWS
    ]
    results += _no_token_or_owner_access(cursor, "dashboard")
    return results


def check_backend(connection: psycopg.Connection) -> list[Result]:
    return _run(connection, _backend_checks)


def check_dashboard(connection: psycopg.Connection) -> list[Result]:
    return _run(connection, _dashboard_checks)


def main() -> int:
    settings = get_settings()
    all_results: list[Result] = []
    for label, connect, check in (
        ("writer", connect_backend, check_backend),
        ("dashboard", connect_dashboard, check_dashboard),
    ):
        try:
            with connect(settings) as connection:
                all_results += [(f"{label}: {name}", ok) for name, ok in check_tls(connection)]
                all_results += check(connection)
        except (PersistenceError, psycopg.Error) as error:
            all_results.append((f"{label}: connect and run checks ({type(error).__name__})", False))
    for name, ok in all_results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = sum(not ok for _, ok in all_results)
    print(f"{len(all_results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
