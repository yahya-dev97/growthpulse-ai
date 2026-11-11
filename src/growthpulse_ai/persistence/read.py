"""Read-only queries for the dashboard. They use the growthpulse_read views only.

The dashboard role has no grant on the base tables, so these queries are the whole of what
it can see.
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

LATEST_REPORT = """
select report from growthpulse_read.latest_reports where data_source = %(data_source)s
"""

RECENT_RUNS = """
select run_id, started_at, finished_at, status, data_source, triggered_by, counts,
    error_code, report_id, appears_abandoned
from growthpulse_read.pipeline_runs
order by started_at desc
limit %(limit)s
"""


def latest_report(connection: psycopg.Connection, data_source: str) -> dict[str, Any] | None:
    row = connection.execute(LATEST_REPORT, {"data_source": data_source}).fetchone()
    return None if row is None else row[0]


def recent_runs(connection: psycopg.Connection, limit: int = 20) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(RECENT_RUNS, {"limit": limit}).fetchall()
