"""Client contexts and per-account fetches for the fictional synthetic clients."""

from datetime import date
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from growthpulse_ai.accounts.legacy import LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.accounts.models import AccountContext, ClientContext
from growthpulse_ai.connectors.factory import SyntheticConnectorFactory
from growthpulse_ai.connectors.synthetic_data import (
    DEFAULT_CLIENT,
    SYNTHETIC_CLIENTS,
    SyntheticClientSpec,
)
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

START, END = date(2026, 10, 1), date(2026, 10, 30)


def stable_uuid(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "growthpulse-test:" + ":".join(parts))


def client_context(spec: SyntheticClientSpec) -> ClientContext:
    """The legacy client for the default spec, otherwise stable test IDs."""
    if spec is DEFAULT_CLIENT:
        return LEGACY_SYNTHETIC_CLIENT
    client_id = stable_uuid("client", spec.key)
    return ClientContext(
        client_id=client_id,
        name=spec.name,
        data_source=DataSource.SYNTHETIC,
        accounts=tuple(
            AccountContext(
                ad_account_id=stable_uuid("account", account.platform.value, account.account_id),
                client_id=client_id,
                data_source=DataSource.SYNTHETIC,
                platform=account.platform,
                external_account_id=account.account_id,
                currency=account.currency,
                timezone=account.timezone,
            )
            for account in spec.accounts
        ),
    )


CLIENTS = tuple(client_context(spec) for spec in SYNTHETIC_CLIENTS)


def sync_client(
    client: ClientContext, start: date = START, end: date = END
) -> tuple[list[AdPerformanceRecord], dict[UUID, dict[str, Any]]]:
    """Fetch each account separately, as independent account syncs would."""
    factory = SyntheticConnectorFactory()
    records: list[AdPerformanceRecord] = []
    summaries: dict[UUID, dict[str, Any]] = {}
    for account in client.accounts:
        result = factory.for_account(account).fetch(start, end)
        records.extend(result.records)
        summaries[account.ad_account_id] = result.summary
    return records, summaries
