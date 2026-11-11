"""Connector that serves the deterministic synthetic dataset."""

from collections.abc import Iterable
from datetime import date
from typing import Any, ClassVar

from growthpulse_ai.accounts.models import AccountContext
from growthpulse_ai.connectors.base import AdDataConnector
from growthpulse_ai.connectors.synthetic_data import (
    DEFAULT_CLIENT,
    SyntheticClientSpec,
    generate_synthetic_rows,
)
from growthpulse_ai.schema import DataSource


def _in_range(row: Any, start_date: date, end_date: date) -> bool:
    try:
        reporting_date = date.fromisoformat(str(row["reporting_date"]))
    except (KeyError, TypeError, ValueError, IndexError):
        # Rows without a usable date stay in, so validation can quarantine them.
        return True
    return start_date <= reporting_date <= end_date


def _belongs_to(row: Any, account: AccountContext) -> bool:
    try:
        return (row["platform"], row["account_id"]) == (
            account.platform.value,
            account.external_account_id,
        )
    except (KeyError, TypeError, IndexError):
        return False


class SyntheticConnector(AdDataConnector):
    """Reads generated rows for one fictional client, or the supplied rows when given.

    With an account, generated rows are limited to that account, as a platform API would
    return them. Supplied rows are passed through untouched, so tests can feed a connector
    another account's rows and see the account guard quarantine them.
    """

    name: ClassVar[str] = "synthetic"
    data_source: ClassVar[DataSource] = DataSource.SYNTHETIC

    def __init__(
        self,
        rows: Iterable[Any] | None = None,
        seed: int | None = None,
        *,
        client: SyntheticClientSpec = DEFAULT_CLIENT,
        account: AccountContext | None = None,
    ) -> None:
        self._rows = None if rows is None else list(rows)
        self._seed = seed
        self._client = client
        self._bind_account(account)

    def _fetch_raw(self, start_date: date, end_date: date) -> list[Any]:
        if self._rows is not None:
            rows = self._rows
        else:
            rows = generate_synthetic_rows(self._seed, client=self._client)
            if self.account is not None:
                rows = [row for row in rows if _belongs_to(row, self.account)]
        return [row for row in rows if _in_range(row, start_date, end_date)]
