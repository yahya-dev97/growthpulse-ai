"""Build the right connector for one advertising account.

The pipeline asks the factory for a connector per account and never configures a
connector from global settings, so no account ID is ever hardcoded. Live factories arrive
with Milestone 6; they will receive an ``AccountContext`` and a credential provider, never
a raw token or setting.
"""

from collections.abc import Iterable
from typing import Protocol

from growthpulse_ai.accounts.models import AccountContext
from growthpulse_ai.connectors.base import AdDataConnector
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import SYNTHETIC_CLIENTS, SyntheticClientSpec
from growthpulse_ai.schema import DataSource, Platform


class ConnectorFactory(Protocol):
    def for_account(self, account: AccountContext) -> AdDataConnector: ...


class SyntheticConnectorFactory:
    """Connectors for the fictional synthetic accounts. Refuses live accounts."""

    def __init__(self, clients: Iterable[SyntheticClientSpec] = SYNTHETIC_CLIENTS) -> None:
        self._accounts: dict[tuple[Platform, str], SyntheticClientSpec] = {}
        for client in clients:
            for account in client.accounts:
                key = (account.platform, account.account_id)
                if key in self._accounts:
                    raise ValueError("A synthetic account belongs to two synthetic clients")
                self._accounts[key] = client

    def for_account(self, account: AccountContext) -> SyntheticConnector:
        if account.data_source is not DataSource.SYNTHETIC:
            raise ValueError("Live accounts need a live connector, which Milestone 6 adds")
        client = self._accounts.get((account.platform, account.external_account_id))
        if client is None:
            raise ValueError("No synthetic data exists for this account")
        spec = next(
            a
            for a in client.accounts
            if (a.platform, a.account_id) == (account.platform, account.external_account_id)
        )
        if (spec.currency, spec.timezone) != (account.currency, account.timezone):
            raise ValueError("The account's currency or timezone differs from its data")
        return SyntheticConnector(client=client, account=account)
