"""Tenant context for database transactions (migration 10, growthpulse_auth).

Every tenant-scoped transaction starts with exactly one call:

- ``begin_job`` for the pipeline and token manager. It scopes the transaction to one
  organization, and to one client when the scope is a client, account or run. The pipeline
  scopes each transaction to the client or account it is about to touch, so a wrong ID can
  never read or write another organization's rows.
- ``begin_request`` for a signed-in person. The database checks the session token against a
  live session and an active membership, and limits every view to that person's clients.

The database signs the context with a key only it holds and binds it to the current
transaction, so it cannot be forged by setting the values directly or replayed later.
Context is transaction-local: it ends at commit or rollback, which is what makes it safe
through a transaction pooler. Never use a session-level ``SET`` for it; a test enforces this.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

from growthpulse_ai.persistence.errors import TenantScopeError

JobScope = Literal["workspace", "connection", "client", "ad_account", "run"]

BEGIN_JOB = "select growthpulse_auth.begin_job(%(kind)s, %(scope_id)s, %(job_id)s)"
# Null until migration 10 is applied. See begin_job.
CONTEXT_INSTALLED = "select to_regprocedure('growthpulse_auth.begin_job(text,uuid,uuid)')"
BEGIN_REQUEST = "select growthpulse_auth.begin_request(%(token)s, %(workspace_id)s, %(request_id)s)"

SESSION_TOKEN_BYTES = 32


def context_installed(connection: psycopg.Connection) -> bool:
    """Whether migration 10 (growthpulse_auth.begin_job) exists in this database."""
    row = connection.execute(CONTEXT_INSTALLED).fetchone()
    return row is not None and row[0] is not None


def begin_job(
    cursor: psycopg.Cursor, kind: JobScope, scope_id: UUID, job_id: UUID | None = None
) -> None:
    """Scope the current transaction to a job. Must run inside an explicit transaction.

    Raises ``TenantScopeError`` when the scope is unknown or its organization is not active.
    The caller's ``transaction()`` block then rolls back.

    Upgrade compatibility: on a database still at Milestone 5 (migrations 1-8, the state
    Phase D creates on Supabase) the function does not exist and this is a no-op. That
    weakens nothing: Milestone 5 writer policies admit every row anyway. Once migration 10
    exists the call is always made, and from migration 12 a transaction without it sees and
    writes no rows. Remove this check once migrations 9-12 are applied on Supabase.
    """
    if not context_installed(cursor.connection):
        return
    try:
        cursor.execute(BEGIN_JOB, {"kind": kind, "scope_id": scope_id, "job_id": job_id})
    except pg_errors.InsufficientPrivilege:
        raise TenantScopeError("No active organization for this job scope") from None


@dataclass(frozen=True, slots=True)
class SignedInRequest:
    """One request by a signed-in person. The token is never logged or shown."""

    session_token: bytes = field(repr=False)
    workspace_id: UUID
    request_id: UUID

    def __post_init__(self) -> None:
        if len(self.session_token) != SESSION_TOKEN_BYTES:
            raise ValueError("A session token is 32 bytes")


def session_token_hash(token: bytes) -> bytes:
    """What the database stores for a session: SHA-256 of the token, never the token."""
    return hashlib.sha256(token).digest()


def begin_request(cursor: psycopg.Cursor, request: SignedInRequest) -> None:
    """Scope the current transaction to a signed-in person's request."""
    try:
        cursor.execute(
            BEGIN_REQUEST,
            {
                "token": request.session_token,
                "workspace_id": request.workspace_id,
                "request_id": request.request_id,
            },
        )
    except pg_errors.InsufficientPrivilege:
        raise TenantScopeError("Not signed in to this organization") from None


@contextmanager
def request_transaction(
    connection: psycopg.Connection, request: SignedInRequest
) -> Iterator[psycopg.Cursor]:
    """A transaction that starts with ``begin_request``, yielding its cursor."""
    with connection.transaction(), connection.cursor() as cursor:
        begin_request(cursor, request)
        yield cursor
