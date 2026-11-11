"""Database connections for the backend and the dashboard.

Each side has its own setting and its own database role:

- ``DATABASE_URL`` must log in as ``growthpulse_writer``, used only by the pipeline.
- ``DASHBOARD_DATABASE_URL`` must log in as ``growthpulse_dashboard``, which can only read
  the ``growthpulse_read`` views.

A connection that logs in as any other role, including ``postgres``, is refused, so an
owner credential pasted into the wrong setting fails instead of quietly working.

TLS is required, and so is SCRAM-SHA-256 authentication: libpq refuses a server or pooler
that asks for the password in clear text or MD5, or that skips authentication. TLS with
``sslmode=require`` does not verify the server certificate; use ``verify-full`` with the
provider's CA certificate for that. Connection strings stay inside ``SecretStr`` and are
never logged or put into error messages.
"""

from typing import Any

import psycopg
from psycopg import conninfo as pg_conninfo
from pydantic import SecretStr

from growthpulse_ai.config import Settings
from growthpulse_ai.persistence.errors import DatabaseConfigurationError, DatabaseUnavailableError

BACKEND_ROLE = "growthpulse_writer"
DASHBOARD_ROLE = "growthpulse_dashboard"
SECURE_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
REQUIRED_AUTH = "scram-sha-256"
# require_auth arrived in libpq 16. Older versions reject the option outright.
MIN_LIBPQ_VERSION = 160000
CONNECT_TIMEOUT_SECONDS = 10


def secure_conninfo(url: SecretStr | None, setting: str) -> str:
    """Return the connection string with TLS and SCRAM-SHA-256 enforced.

    sslmode defaults to require and require_auth to scram-sha-256. A URL that asks for a
    weaker TLS mode or any other authentication method is refused.
    """
    if url is None:
        raise DatabaseConfigurationError(f"{setting} is not set")
    try:
        params: dict[str, Any] = pg_conninfo.conninfo_to_dict(url.get_secret_value())
    except psycopg.ProgrammingError:
        raise DatabaseConfigurationError(
            f"{setting} is not a valid PostgreSQL connection string"
        ) from None
    sslmode = params.setdefault("sslmode", "require")
    if sslmode not in SECURE_SSL_MODES:
        raise DatabaseConfigurationError(
            f"{setting} must use sslmode=require, verify-ca, or verify-full"
        )
    if params.setdefault("require_auth", REQUIRED_AUTH) != REQUIRED_AUTH:
        raise DatabaseConfigurationError(f"{setting} must use require_auth={REQUIRED_AUTH}")
    if psycopg.pq.version() < MIN_LIBPQ_VERSION:
        raise DatabaseConfigurationError(
            "SCRAM-SHA-256 enforcement needs libpq 16 or newer; install psycopg[binary]"
        )
    return pg_conninfo.make_conninfo("", **params)


def open_connection(
    conninfo: str, *, expected_role: str, application_name: str, read_only: bool = False
) -> psycopg.Connection:
    """Open an autocommit connection and confirm it logged in as the expected role.

    Autocommit means every write happens inside an explicit ``transaction()`` block.
    Prepared statements are disabled so the connection also works through a transaction
    pooler.
    """
    try:
        connection = psycopg.connect(
            conninfo,
            autocommit=True,
            prepare_threshold=None,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            application_name=application_name,
        )
    except psycopg.Error as error:
        # libpq messages can include the host and user name, so only the type is kept.
        raise DatabaseUnavailableError(
            f"Could not connect to the database ({type(error).__name__})"
        ) from None
    try:
        row = connection.execute("select current_user").fetchone()
        if row is None or row[0] != expected_role:
            raise DatabaseConfigurationError(f"The connection must log in as {expected_role}")
        if read_only:
            connection.execute("set default_transaction_read_only = on")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_backend(settings: Settings) -> psycopg.Connection:
    """Connect as the pipeline's writer role."""
    return open_connection(
        secure_conninfo(settings.database_url, "DATABASE_URL"),
        expected_role=BACKEND_ROLE,
        application_name="growthpulse-pipeline",
    )


def connect_dashboard(settings: Settings) -> psycopg.Connection:
    """Connect as the dashboard's read-only role."""
    return open_connection(
        secure_conninfo(settings.dashboard_database_url, "DASHBOARD_DATABASE_URL"),
        expected_role=DASHBOARD_ROLE,
        application_name="growthpulse-dashboard",
        read_only=True,
    )
