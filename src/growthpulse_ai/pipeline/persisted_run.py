"""Run the synthetic pipeline and save the results to Supabase PostgreSQL.

Usage: uv run python -m growthpulse_ai.pipeline.persisted_run [--trigger manual|scheduled]

Requires DATABASE_URL for the growthpulse_writer role. Nothing runs this automatically.
Only synthetic data is supported until live connectors exist.

If anything fails after the run starts, the data transaction rolls back and the run is
marked failed with a fixed error code on a fresh connection. Error messages, which can
contain hosts, user names or data values, are never stored or printed.
"""

import argparse
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

import psycopg

from growthpulse_ai.analytics.errors import AnalyticsError
from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.config import get_settings
from growthpulse_ai.connectors.base import AdDataConnector, ConnectorError
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import DEFAULT_DAYS, DEFAULT_START_DATE
from growthpulse_ai.persistence.connection import connect_backend
from growthpulse_ai.persistence.errors import classify_database_error
from growthpulse_ai.persistence.store import (
    CODE_VERSION_PATTERN,
    Trigger,
    mark_run_failed,
    save_run_results,
    start_run,
)

logger = logging.getLogger(__name__)

Connect = Callable[[], psycopg.Connection]


class NoValidRecordsError(Exception):
    """Every row was quarantined, so there is nothing to analyse."""


class PipelineRunError(Exception):
    """A run failed. Carries only the run ID and a fixed error code."""

    def __init__(self, run_id: UUID, error_code: str, *, recorded: bool) -> None:
        state = "recorded" if recorded else "not recorded"
        super().__init__(f"Pipeline run {run_id} failed: {error_code} (failure {state})")
        self.run_id = run_id
        self.error_code = error_code
        self.recorded = recorded


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: UUID
    report_id: str
    counts: dict[str, int]


def classify_error(error: BaseException) -> str:
    """Map any exception to a fixed code that is safe to store."""
    database_code = classify_database_error(error)
    if database_code is not None:
        return database_code
    if isinstance(error, ConnectorError):
        return "connector_unavailable"
    if isinstance(error, NoValidRecordsError):
        return "no_valid_records"
    if isinstance(error, AnalyticsError):
        return "analytics_error"
    return "unexpected_error"


def _record_failure(connect: Connect, run_id: UUID, code: str, counts: dict[str, int]) -> bool:
    try:
        with connect() as connection:
            return mark_run_failed(connection, run_id, code, counts)
    except Exception as error:
        logger.error(
            "run=%s failure_not_recorded code=%s cause=%s",
            run_id,
            classify_error(error),
            type(error).__name__,
        )
        return False


def run_and_persist(
    connector: AdDataConnector,
    connect: Connect,
    start_date: date,
    end_date: date,
    *,
    triggered_by: Trigger,
    code_version: str | None = None,
    run_id: UUID | None = None,
) -> RunOutcome:
    run_id = run_id or uuid4()
    try:
        with connect() as connection:
            start_run(
                connection,
                run_id,
                data_source=connector.data_source,
                triggered_by=triggered_by,
                code_version=code_version,
            )
    except Exception as error:
        code = classify_error(error)
        logger.error("run=%s not_started code=%s cause=%s", run_id, code, type(error).__name__)
        raise PipelineRunError(run_id, code, recorded=False) from None

    progress: dict[str, int] = {}
    try:
        result = connector.fetch(start_date, end_date)
        progress = {
            "rows_received": len(result.records) + len(result.quarantined),
            "records_received": len(result.records),
            "quarantined_rows": len(result.quarantined),
        }
        if not result.records:
            raise NoValidRecordsError
        report = build_report(result.records, data_quality=result.summary)
        with connect() as connection:
            counts = save_run_results(
                connection,
                run_id,
                data_source=connector.data_source,
                connector=connector.name,
                records=result.records,
                quarantined=result.quarantined,
                report=report,
            )
    except Exception as error:
        code = classify_error(error)
        recorded = _record_failure(connect, run_id, code, progress)
        logger.error("run=%s failed code=%s cause=%s", run_id, code, type(error).__name__)
        raise PipelineRunError(run_id, code, recorded=recorded) from None

    logger.info("run=%s succeeded report=%s", run_id, report["report_id"])
    return RunOutcome(run_id=run_id, report_id=report["report_id"], counts=counts)


def _code_version() -> str | None:
    sha = os.environ.get("GITHUB_SHA", "").lower()
    return sha if CODE_VERSION_PATTERN.fullmatch(sha) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trigger", choices=["manual", "scheduled"], default="manual")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.data_source != "synthetic":
        print("Only DATA_SOURCE=synthetic is supported until live connectors exist.")
        return 2

    end_date = DEFAULT_START_DATE + timedelta(days=DEFAULT_DAYS - 1)
    try:
        outcome = run_and_persist(
            SyntheticConnector(),
            lambda: connect_backend(settings),
            DEFAULT_START_DATE,
            end_date,
            triggered_by=args.trigger,
            code_version=_code_version(),
        )
    except PipelineRunError as error:
        print(error)
        return 1
    print(f"Pipeline run {outcome.run_id} succeeded: report {outcome.report_id}")
    for name, value in outcome.counts.items():
        print(f"  {name}: {value}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().log_level)
    raise SystemExit(main())
