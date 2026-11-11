"""Persistence errors and their sanitized codes.

Messages never include connection strings, SQL parameters, or row values, so they are safe
to log and to store in a pipeline run's failure record.
"""

import psycopg
from psycopg import errors as pg_errors


class PersistenceError(Exception):
    """A database operation failed. The message is safe to log."""


class DatabaseConfigurationError(PersistenceError):
    """A database setting is missing, malformed, insecure, or uses the wrong role."""


class DatabaseUnavailableError(PersistenceError):
    """A connection could not be opened."""


class RunStateError(PersistenceError):
    """A pipeline run was missing or not in the state an operation required."""


def classify_database_error(error: BaseException) -> str | None:
    """Return a fixed error code for a database failure, or None for other errors."""
    if isinstance(error, DatabaseConfigurationError):
        return "database_misconfigured"
    if isinstance(error, DatabaseUnavailableError):
        return "database_unavailable"
    if isinstance(error, RunStateError):
        return "run_state_conflict"
    # Checked before OperationalError: privilege and RLS failures are configuration problems.
    if isinstance(error, pg_errors.InsufficientPrivilege):
        return "database_permission_denied"
    if isinstance(error, psycopg.OperationalError):
        return "database_unavailable"
    if isinstance(error, (psycopg.IntegrityError, psycopg.DataError)):
        return "database_rejected_data"
    if isinstance(error, psycopg.Error):
        return "database_error"
    return None
