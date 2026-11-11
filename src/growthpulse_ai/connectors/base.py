"""The interface every advertising data source implements.

A connector only reads. A new source implements ``_fetch_raw`` to return rows shaped like
``AdPerformanceRecord``. Validation and quality checks are inherited, and analytics depends
only on the normalized records, so adding a source requires no analytics changes.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date
from typing import Any, ClassVar

from growthpulse_ai.data_quality import ValidationResult, validate_rows
from growthpulse_ai.schema import DataSource

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """A data source could not be read at all, as opposed to returning bad rows."""

    def __init__(self, connector: str, message: str) -> None:
        super().__init__(f"{connector}: {message}")
        self.connector = connector
        self.message = message


class AdDataConnector(ABC):
    name: ClassVar[str]
    data_source: ClassVar[DataSource]

    @abstractmethod
    def _fetch_raw(self, start_date: date, end_date: date) -> Iterable[Any]:
        """Return raw rows for the inclusive date range."""

    def fetch(self, start_date: date, end_date: date) -> ValidationResult:
        """Read, validate, and return records for the inclusive date range."""
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        try:
            raw_rows = list(self._fetch_raw(start_date, end_date))
        except Exception as error:
            # API errors can embed tokens in URLs, so only the exception type is surfaced.
            raise ConnectorError(
                self.name, f"Could not read source ({type(error).__name__})"
            ) from None

        result = validate_rows(raw_rows, expected_source=self.data_source)
        summary = result.summary
        logger.info(
            "connector=%s total_rows=%d valid_records=%d quarantined_rows=%d warnings=%d",
            self.name,
            summary["total_rows"],
            summary["valid_records"],
            summary["quarantined_rows"],
            summary["warnings"],
        )
        for code, count in sorted(summary["errors_by_code"].items()):
            logger.warning("connector=%s rejected code=%s count=%d", self.name, code, count)
        return result
