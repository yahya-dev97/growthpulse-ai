"""Connector that serves the deterministic synthetic dataset."""

from collections.abc import Iterable
from datetime import date
from typing import Any, ClassVar

from growthpulse_ai.connectors.base import AdDataConnector
from growthpulse_ai.connectors.synthetic_data import DEFAULT_SEED, generate_synthetic_rows
from growthpulse_ai.schema import DataSource


def _in_range(row: Any, start_date: date, end_date: date) -> bool:
    try:
        reporting_date = date.fromisoformat(str(row["reporting_date"]))
    except (KeyError, TypeError, ValueError, IndexError):
        # Rows without a usable date stay in, so validation can quarantine them.
        return True
    return start_date <= reporting_date <= end_date


class SyntheticConnector(AdDataConnector):
    """Reads generated rows, or the supplied rows when given."""

    name: ClassVar[str] = "synthetic"
    data_source: ClassVar[DataSource] = DataSource.SYNTHETIC

    def __init__(self, rows: Iterable[Any] | None = None, seed: int = DEFAULT_SEED) -> None:
        self._rows = None if rows is None else list(rows)
        self._seed = seed

    def _fetch_raw(self, start_date: date, end_date: date) -> list[Any]:
        rows = generate_synthetic_rows(self._seed) if self._rows is None else self._rows
        return [row for row in rows if _in_range(row, start_date, end_date)]
