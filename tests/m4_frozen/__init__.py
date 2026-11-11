"""A frozen copy of the Milestone 4 store and row builders (commit 0eb3ca4), for tests only.

The current store is Milestone 5-aware and cannot write to a Milestone 4 schema. Upgrade
tests use this copy to build the exact Milestone 4 database state, the same way the
Milestone 4 pipeline built it on Supabase, before applying the Milestone 5 migrations.
store.py and rows.py are verbatim apart from one import line; a test checks that.
"""

from collections.abc import Callable
from datetime import date
from uuid import uuid4

import psycopg

from growthpulse_ai.analytics.report import build_report
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.schema import DataSource
from m4_frozen import store


def run_milestone_4_pipeline(
    connect: Callable[[], psycopg.Connection], start: date, end: date
) -> dict[str, int]:
    """One Milestone 4 combined run: start, fetch, report, save. Returns its counts."""
    run_id = uuid4()
    with connect() as connection:
        store.start_run(connection, run_id, data_source=DataSource.SYNTHETIC, triggered_by="test")
    result = SyntheticConnector().fetch(start, end)
    report = build_report(result.records, data_quality=result.summary)
    with connect() as connection:
        return store.save_run_results(
            connection, run_id, data_source=DataSource.SYNTHETIC, connector="synthetic",
            records=result.records, quarantined=result.quarantined, report=report,
        )  # fmt: skip
