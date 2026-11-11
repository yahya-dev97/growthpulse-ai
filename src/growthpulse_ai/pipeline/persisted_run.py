"""Run the synthetic pipeline for one client and save the results to Supabase PostgreSQL.

Usage: uv run python -m growthpulse_ai.pipeline.persisted_run
           [--trigger manual|scheduled] [--client-id UUID]

Requires DATABASE_URL for the growthpulse_writer role. Nothing runs this automatically.
Only synthetic data is supported until live connectors exist.

The command syncs each of the client's syncable accounts on its own, then analyses the
client from what the syncs stored. The client is always named explicitly: by default the
fictional legacy client that holds the Milestone 4 data, whose analysis reproduces
Milestone 4's report exactly. Nothing depends on a database default.

``run_and_persist`` keeps the Milestone 4 shape (fetch, report and store in one run) for
one explicit client. Both paths store a failure as a fixed error code on a fresh
connection; error messages, which can contain hosts, user names or data values, are
never stored or printed.
"""

import argparse
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

import psycopg

from growthpulse_ai.accounts.legacy import LEGACY_CLIENT_ID
from growthpulse_ai.accounts.models import ClientContext
from growthpulse_ai.accounts.scheduling import DateWindow
from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.config import get_settings
from growthpulse_ai.connectors.base import AdDataConnector
from growthpulse_ai.connectors.factory import ConnectorFactory, SyntheticConnectorFactory
from growthpulse_ai.connectors.synthetic_data import DEFAULT_DAYS, DEFAULT_START_DATE
from growthpulse_ai.persistence.connection import connect_backend
from growthpulse_ai.persistence.store import (
    CODE_VERSION_PATTERN,
    Trigger,
    mark_run_failed,
    save_run_results,
    start_run,
)
from growthpulse_ai.persistence.tenancy import load_client
from growthpulse_ai.pipeline.account_sync import AccountSyncError, SyncOutcome, sync_account
from growthpulse_ai.pipeline.client_analysis import (
    AnalysisOutcome,
    ClientAnalysisError,
    run_client_analysis,
)
from growthpulse_ai.pipeline.failures import NoValidRecordsError, classify_error

__all__ = ["NoValidRecordsError", "classify_error"]

logger = logging.getLogger(__name__)

Connect = Callable[[], psycopg.Connection]
SYNTHETIC_WINDOW = DateWindow(
    DEFAULT_START_DATE, DEFAULT_START_DATE + timedelta(days=DEFAULT_DAYS - 1)
)


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
    client: ClientContext,
    triggered_by: Trigger,
    code_version: str | None = None,
    run_id: UUID | None = None,
) -> RunOutcome:
    """Combined run for one explicit client: fetch, build the report and store it at once.

    A record or finding naming an account outside the client fails the run with
    ``account_mismatch``; nothing is re-filed under another account.
    """
    run_id = run_id or uuid4()
    try:
        with connect() as connection:
            start_run(
                connection,
                run_id,
                data_source=connector.data_source,
                triggered_by=triggered_by,
                client_id=client.client_id,
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
                client=client,
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


@dataclass(frozen=True, slots=True)
class ClientRunOutcome:
    syncs: tuple[SyncOutcome, ...]
    analysis: AnalysisOutcome


def run_client(
    client_id: UUID,
    connect: Connect,
    *,
    factory: ConnectorFactory,
    window: DateWindow | None,
    triggered_by: Trigger,
    code_version: str | None = None,
) -> ClientRunOutcome:
    """Sync each of the client's syncable accounts, then analyse the client.

    Syncs are independent: a failed sync is reported and the analysis does not run, so no
    report ever describes data an account failed to deliver.
    """
    with connect() as connection:
        client = load_client(connection, client_id)
    syncs = tuple(
        sync_account(
            account,
            factory,
            connect,
            triggered_by=triggered_by,
            window=window,
            code_version=code_version,
        )  # fmt: skip
        for account in client.syncable_accounts
    )
    analysis = run_client_analysis(
        client_id,
        connect,
        triggered_by=triggered_by,
        end=window.end if window else None,
        days=window.days if window else DEFAULT_DAYS,
        code_version=code_version,
    )
    return ClientRunOutcome(syncs, analysis)


def _code_version() -> str | None:
    sha = os.environ.get("GITHUB_SHA", "").lower()
    return sha if CODE_VERSION_PATTERN.fullmatch(sha) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trigger", choices=["manual", "scheduled"], default="manual")
    parser.add_argument("--client-id", type=UUID, default=LEGACY_CLIENT_ID)
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.data_source != "synthetic":
        print("Only DATA_SOURCE=synthetic is supported until live connectors exist.")
        return 2

    try:
        outcome = run_client(
            args.client_id,
            lambda: connect_backend(settings),
            factory=SyntheticConnectorFactory(),
            window=SYNTHETIC_WINDOW,
            triggered_by=args.trigger,
            code_version=_code_version(),
        )
    except (AccountSyncError, ClientAnalysisError) as error:
        print(error)
        return 1
    except Exception as error:
        code = classify_error(error)
        print(f"Run for client {args.client_id} failed: {code} (failure not recorded)")
        return 1
    for sync in outcome.syncs:
        print(f"Account {sync.ad_account_id} synced (run {sync.run_id}):")
        for name, value in sync.counts.items():
            print(f"  {name}: {value}")
    analysis = outcome.analysis
    print(
        f"Client {analysis.client_id} analysed (run {analysis.run_id}): report {analysis.report_id}"
    )
    for name, value in analysis.counts.items():
        print(f"  {name}: {value}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().log_level)
    raise SystemExit(main())
