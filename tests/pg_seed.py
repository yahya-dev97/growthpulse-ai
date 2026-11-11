"""Re-create the fictional legacy client that migration 5 seeds, after a test truncates.

Values come from growthpulse_ai.accounts.legacy, which a static test ties to migration 5.
"""

import psycopg

from growthpulse_ai.accounts.legacy import (
    LEGACY_CLIENT_ID,
    LEGACY_SYNTHETIC_CLIENT,
    LEGACY_WORKSPACE_ID,
)


def seed_legacy_client(connection: psycopg.Connection) -> None:
    connection.execute(
        "insert into growthpulse.workspaces (workspace_id, name) values (%s, %s)",
        (LEGACY_WORKSPACE_ID, "GrowthPulse workspace"),
    )
    connection.execute(
        "insert into growthpulse.clients (client_id, workspace_id, name, data_source) "
        "values (%s, %s, %s, 'synthetic')",
        (LEGACY_CLIENT_ID, LEGACY_WORKSPACE_ID, LEGACY_SYNTHETIC_CLIENT.name),
    )
    for account in LEGACY_SYNTHETIC_CLIENT.accounts:
        connection.execute(
            "insert into growthpulse.ad_accounts (ad_account_id, client_id, data_source, "
            "platform, external_account_id, display_name, currency, timezone, status) "
            "values (%s, %s, 'synthetic', %s, %s, %s, %s, %s, 'active')",
            (
                account.ad_account_id,
                LEGACY_CLIENT_ID,
                account.platform.value,
                account.external_account_id,
                account.display_name,
                account.currency,
                account.timezone,
            ),
        )
        connection.execute(
            "insert into growthpulse.account_sync_config (ad_account_id, frequency, "
            "backfill_start_date) values (%s, 'manual', date '2026-10-01')",
            (account.ad_account_id,),
        )
        connection.execute(
            "insert into growthpulse.account_sync_state (ad_account_id) values (%s)",
            (account.ad_account_id,),
        )


def register_account(
    connection: psycopg.Connection,
    external_account_id: str,
    *,
    platform: str = "google_ads",
    currency: str = "GBP",
    timezone: str = "Europe/London",
    client_id=LEGACY_CLIENT_ID,
) -> None:
    """Register a synthetic account under a client, as add_synthetic_ad_account would."""
    connection.execute(
        "insert into growthpulse.ad_accounts (client_id, data_source, platform, "
        "external_account_id, currency, timezone, status) "
        "values (%s, 'synthetic', %s, %s, %s, %s, 'active')",
        (client_id, platform, external_account_id, currency, timezone),
    )


def seed_synthetic_client(
    connection: psycopg.Connection, client, *, with_sync: bool = False, status: str = "active"
) -> None:
    """Insert a synthetic ClientContext and its accounts, as owner actions would.

    ``with_sync`` also creates each account's sync settings (manual) and state.
    """
    connection.execute(
        "insert into growthpulse.clients (client_id, workspace_id, name, data_source) "
        "values (%s, %s, %s, 'synthetic')",
        (client.client_id, LEGACY_WORKSPACE_ID, client.name),
    )
    for account in client.accounts:
        connection.execute(
            "insert into growthpulse.ad_accounts (ad_account_id, client_id, data_source, "
            "platform, external_account_id, currency, timezone, status) "
            "values (%s, %s, 'synthetic', %s, %s, %s, %s, %s)",
            (
                account.ad_account_id,
                client.client_id,
                account.platform.value,
                account.external_account_id,
                account.currency,
                account.timezone,
                status,
            ),
        )
        if with_sync:
            connection.execute(
                "insert into growthpulse.account_sync_config (ad_account_id, frequency, "
                "backfill_start_date) values (%s, 'manual', date '2026-10-01')",
                (account.ad_account_id,),
            )
            connection.execute(
                "insert into growthpulse.account_sync_state (ad_account_id) values (%s)",
                (account.ad_account_id,),
            )


def start_client_run(connection: psycopg.Connection, run_id, client_id) -> None:
    """A running combined run for a client other than the legacy one.

    The Milestone 4 store cannot pass a client yet (Phase C), so tests create the run row
    themselves and let store.save_run_results write into it.
    """
    connection.execute(
        "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, client_id) "
        "values (%s, 'synthetic', 'test', %s)",
        (run_id, client_id),
    )
