"""Read stored advertising records back for a client analysis.

Account sync and client analysis are independent: syncs store records, and an analysis
later rebuilds ``AdPerformanceRecord`` objects from those rows. The rebuild must give the
same report as the records had in memory, so the two values the database can present
differently are normalised:

- ``ingested_at`` comes back in the session's timezone. It is converted to UTC, which is
  how connectors set it, so ``data_as_of`` is identical whatever the session timezone.
- ``numeric(20, 6)`` comes back padded to six decimal places. Amounts are restored to a
  canonical form, trailing zeros removed but at least two decimal places kept, which is
  how connectors produce them. No value ever changes; only the padding does.

Reads go through the writer role, inside a read-only transaction.
"""

from collections.abc import Sequence
from datetime import UTC, date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from growthpulse_ai.accounts.models import AccountContext
from growthpulse_ai.persistence.context import begin_job
from growthpulse_ai.schema import AdPerformanceRecord, DataSource

LOAD_RECORDS = """
select r.data_source, r.reporting_date, r.platform, r.account_id, r.campaign_id,
    r.campaign_name, r.campaign_type, r.impressions, r.clicks, r.spend, r.conversions,
    r.conversion_value, r.currency, r.account_timezone, r.conversion_event_id,
    r.conversion_event_name, r.conversion_stage, r.attribution_model, r.click_window_days,
    r.view_window_days, r.report_time_basis, r.ingested_at
from growthpulse.ad_performance_records as r
join unnest(%(platforms)s::text[], %(account_ids)s::text[]) as a (platform, account_id)
    on r.platform = a.platform and r.account_id = a.account_id
where r.data_source = %(data_source)s
    and r.reporting_date between %(start)s and %(end)s
order by r.record_id
"""


_CENTS = Decimal("0.01")


def canonical_amount(value: Decimal) -> Decimal:
    """Drop database padding: 237.340000 -> 237.34, 100.000000 -> 100.00. Value unchanged."""
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("Amounts must be finite")
    return value.quantize(_CENTS) if exponent > -2 else normalized


def row_to_record(row: dict[str, Any]) -> AdPerformanceRecord:
    attribution = None
    if row["attribution_model"] is not None:
        attribution = {
            "attribution_model": row["attribution_model"],
            "click_window_days": row["click_window_days"],
            "view_window_days": row["view_window_days"],
            "report_time_basis": row["report_time_basis"],
        }
    return AdPerformanceRecord.model_validate(
        {
            "data_source": row["data_source"],
            "reporting_date": row["reporting_date"].isoformat(),
            "platform": row["platform"],
            "account_id": row["account_id"],
            "campaign_id": row["campaign_id"],
            "campaign_name": row["campaign_name"],
            "campaign_type": row["campaign_type"],
            "impressions": row["impressions"],
            "clicks": row["clicks"],
            "spend": canonical_amount(row["spend"]),
            "conversions": canonical_amount(row["conversions"]),
            "conversion_value": canonical_amount(row["conversion_value"]),
            "currency": row["currency"],
            "account_timezone": row["account_timezone"],
            "conversion_definition": {
                "event_id": row["conversion_event_id"],
                "event_name": row["conversion_event_name"],
                "stage": row["conversion_stage"],
            },
            "attribution": attribution,
            "ingested_at": row["ingested_at"].astimezone(UTC),
        }
    )


def load_records(
    connection: psycopg.Connection,
    *,
    data_source: DataSource,
    accounts: Sequence[AccountContext],
    start: date,
    end: date,
) -> list[AdPerformanceRecord]:
    """Stored records for the given accounts and inclusive dates, ordered by record_id."""
    if start > end:
        raise ValueError("start must not be after end")
    if any(account.data_source is not data_source for account in accounts):
        raise ValueError("Every account must match the requested data source")
    if not accounts:
        return []
    params = {
        "platforms": [account.platform.value for account in accounts],
        "account_ids": [account.external_account_id for account in accounts],
        "data_source": data_source.value,
        "start": start,
        "end": end,
    }
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        # One client's accounts: the scope makes every other client's records invisible.
        begin_job(cursor, "client", accounts[0].client_id)
        cursor.execute("set transaction read only")
        rows = cursor.execute(LOAD_RECORDS, params).fetchall()
    return [row_to_record(row) for row in rows]
