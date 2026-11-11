"""Read client, account and sync context from the database, as the pipeline's writer role.

The pipeline acts on explicit contexts loaded here, never on a database default. Every read
runs in a read-only transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from growthpulse_ai.accounts.models import (
    AccountContext,
    AccountStatus,
    ClientContext,
    ClientStatus,
)
from growthpulse_ai.accounts.scheduling import DateWindow
from growthpulse_ai.persistence.errors import ClientNotFoundError
from growthpulse_ai.schema import DataSource, Platform

SELECT_CLIENT = """
select client_id, name, data_source, status from growthpulse.clients
where client_id = %(client_id)s
"""

SELECT_ACCOUNTS = """
select ad_account_id, client_id, data_source, platform, external_account_id, display_name,
    currency, timezone, status, sync_connection_id
from growthpulse.ad_accounts
where client_id = %(client_id)s
order by platform, external_account_id
"""

# Every successful sync of these accounts whose window overlaps the analysis window, with
# the quality evidence it stored. Ordered, so callers see the same list every time.
SELECT_SYNC_EVIDENCE = """
select ad_account_id, window_start, window_end, data_quality
from growthpulse.pipeline_runs
where run_kind = 'account_sync' and status = 'succeeded'
    and ad_account_id = any(%(ad_account_ids)s)
    and window_start <= %(end)s and window_end >= %(start)s
order by ad_account_id, window_start, window_end, run_id
"""

SELECT_SYNC_INPUTS = """
select s.synced_through, c.lookback_days, c.backfill_start_date
from growthpulse.account_sync_state as s
join growthpulse.account_sync_config as c on c.ad_account_id = s.ad_account_id
where s.ad_account_id = %(ad_account_id)s
"""

SELECT_WATERMARKS = """
select ad_account_id, synced_through from growthpulse.account_sync_state
where ad_account_id = any(%(ad_account_ids)s)
"""

# Accounts whose next sync is due: syncing enabled, account and client active, not paused
# or running, and (for live accounts) an active connection.
SELECT_DUE_ACCOUNTS = """
select a.ad_account_id
from growthpulse.account_sync_state as s
join growthpulse.account_sync_config as c on c.ad_account_id = s.ad_account_id
join growthpulse.ad_accounts as a on a.ad_account_id = s.ad_account_id
join growthpulse.clients as cl on cl.client_id = a.client_id
left join growthpulse.provider_connections as p on p.connection_id = a.sync_connection_id
where c.enabled and a.status in ('onboarding', 'active') and cl.status = 'active'
    and s.status in ('idle', 'retry_wait') and s.next_due_at <= %(now)s
    and (a.data_source = 'synthetic' or p.status = 'active')
order by s.next_due_at, a.ad_account_id
"""


def _account(row: dict[str, Any]) -> AccountContext:
    return AccountContext(
        ad_account_id=row["ad_account_id"],
        client_id=row["client_id"],
        data_source=DataSource(row["data_source"]),
        platform=Platform(row["platform"]),
        external_account_id=row["external_account_id"],
        currency=row["currency"],
        timezone=row["timezone"],
        display_name=row["display_name"],
        status=AccountStatus(row["status"]),
        connection_id=row["sync_connection_id"],
    )


def load_client(connection: psycopg.Connection, client_id: UUID) -> ClientContext:
    """The client and every account assigned to it, in any status."""
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("set transaction read only")
        client = cursor.execute(SELECT_CLIENT, {"client_id": client_id}).fetchone()
        if client is None:
            raise ClientNotFoundError("No client with this ID")
        accounts = cursor.execute(SELECT_ACCOUNTS, {"client_id": client_id}).fetchall()
    return ClientContext(
        client_id=client["client_id"],
        name=client["name"],
        data_source=DataSource(client["data_source"]),
        accounts=tuple(_account(row) for row in accounts),
        status=ClientStatus(client["status"]),
    )


@dataclass(frozen=True, slots=True)
class SyncEvidence:
    ad_account_id: UUID
    window: DateWindow
    data_quality: dict[str, Any]


def sync_evidence(
    connection: psycopg.Connection, ad_account_ids: Sequence[UUID], window: DateWindow
) -> list[SyncEvidence]:
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set transaction read only")
        rows = cursor.execute(
            SELECT_SYNC_EVIDENCE,
            {"ad_account_ids": list(ad_account_ids), "start": window.start, "end": window.end},
        ).fetchall()
    return [
        SyncEvidence(account_id, DateWindow(start, end), data_quality or {})
        for account_id, start, end, data_quality in rows
    ]


def watermarks(connection: psycopg.Connection, ad_account_ids: Sequence[UUID]) -> dict[UUID, Any]:
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set transaction read only")
        rows = cursor.execute(SELECT_WATERMARKS, {"ad_account_ids": list(ad_account_ids)})
        return dict(rows.fetchall())


def due_accounts(connection: psycopg.Connection, now: datetime) -> list[UUID]:
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set transaction read only")
        return [row[0] for row in cursor.execute(SELECT_DUE_ACCOUNTS, {"now": now}).fetchall()]


def sync_inputs(connection: psycopg.Connection, ad_account_id: UUID) -> tuple[Any, int, Any] | None:
    """(synced_through, lookback_days, backfill_start_date), or None if hidden or unknown."""
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set transaction read only")
        return cursor.execute(SELECT_SYNC_INPUTS, {"ad_account_id": ad_account_id}).fetchone()
