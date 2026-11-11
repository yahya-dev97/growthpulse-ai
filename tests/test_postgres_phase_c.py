"""Phase C: scoped account syncs and client analyses on a disposable PostgreSQL server.

Skipped unless GROWTHPULSE_TEST_DATABASE_URL is set. Each test starts from the exact
Milestone 4 state (built by the frozen Milestone 4 store) upgraded through the five
Milestone 5 migrations. Tests marked with the ``strict`` fixture run twice: as migrated,
and with the proposed migration that removes the database's scope fallbacks, to show the
application no longer relies on them. Only the writer role connects; no new role logs in.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors

from growthpulse_ai.accounts.legacy import (
    LEGACY_CLIENT_ID,
    LEGACY_GOOGLE_ACCOUNT_ID,
    LEGACY_META_ACCOUNT_ID,
)
from growthpulse_ai.accounts.models import AccountContext, ClientContext
from growthpulse_ai.accounts.scheduling import DateWindow
from growthpulse_ai.analytics.report import dumps_report
from growthpulse_ai.connectors.factory import SyntheticConnectorFactory
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import SYNTHETIC_CLIENTS, generate_synthetic_rows
from growthpulse_ai.persistence import store
from growthpulse_ai.persistence.tenancy import due_accounts, load_client
from growthpulse_ai.pipeline.account_sync import AccountSyncError, sync_account
from growthpulse_ai.pipeline.client_analysis import ClientAnalysisError, run_client_analysis
from growthpulse_ai.pipeline.persisted_run import SYNTHETIC_WINDOW, run_client
from growthpulse_ai.pipeline.sample_report import build_sample_report
from growthpulse_ai.schema import DataSource
from m4_frozen import run_milestone_4_pipeline
from multi_client import CLIENTS, END, START
from pg_helpers import (
    M4_MIGRATIONS,
    M5_MIGRATIONS,
    MIGRATIONS,
    TEST_URL,
    WRITER,
    admin,
    apply,
    as_role,
    drop_everything,
    owner,
    prepare_server,
    refuse_hosted_database,
    rows,
    run_sql_file,
    scalar,
)
from pg_seed import seed_synthetic_client
from test_postgres_milestone5 import data_snapshot, m4_columns, schema_snapshot

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_URL, reason="GROWTHPULSE_TEST_DATABASE_URL is not set"),
]

M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"
PROPOSED = MIGRATIONS[0].parents[1] / "proposed" / "20261111200500_remove_scope_fallbacks.sql"
REVERT_M5 = MIGRATIONS[0].parents[1] / "rollback" / "20261111200000_revert_milestone_5.sql"
IRELAND, EUROPE = CLIENTS[1], CLIENTS[2]
NOW = datetime(2026, 11, 11, 9, tzinfo=UTC)


def writer() -> psycopg.Connection:
    return as_role(WRITER)


def logins_for_existing_roles_only() -> None:
    """Writer and dashboard only: Phase C never logs in as the two new roles."""
    with admin() as connection:
        for role in ("growthpulse_writer", "growthpulse_dashboard"):
            connection.execute(f"alter role {role} login")  # noqa: S608 - fixed role names


@pytest.fixture(scope="module", autouse=True)
def server():
    refuse_hosted_database()
    prepare_server()
    drop_everything()
    yield
    drop_everything()


def build_upgraded(*, strict: bool) -> None:
    drop_everything()
    apply(M4_MIGRATIONS)
    logins_for_existing_roles_only()
    for _ in range(2):  # exactly as the Milestone 4 pipeline ran on Supabase
        run_milestone_4_pipeline(writer, START, END)
    apply(M5_MIGRATIONS)
    if strict:
        apply([PROPOSED])
    logins_for_existing_roles_only()


@pytest.fixture
def upgraded() -> None:
    build_upgraded(strict=False)


@pytest.fixture(params=[False, True], ids=["as-migrated", "fallbacks-removed"])
def strict(request) -> bool:
    build_upgraded(strict=request.param)
    return request.param


def legacy() -> ClientContext:
    with writer() as connection:
        return load_client(connection, LEGACY_CLIENT_ID)


def account(account_id) -> AccountContext:
    return next(a for a in legacy().accounts if a.ad_account_id == account_id)


def counts(*tables: str) -> tuple[int, ...]:
    return tuple(scalar(f"select count(*) from growthpulse.{t}") for t in tables)  # noqa: S608


def state(account_id) -> dict:
    with admin() as connection:
        cursor = connection.execute(
            "select * from growthpulse.account_sync_state where ad_account_id = %s", (account_id,)
        )
        names = [c.name for c in cursor.description]
        return dict(zip(names, cursor.fetchone(), strict=True))


def seed(*clients: ClientContext) -> None:
    with admin() as connection:
        for client in clients:
            seed_synthetic_client(connection, client, with_sync=True)


class ScriptedFactory:
    """Returns connectors that serve given raw rows, bound to the account or not."""

    def __init__(self, rows: Iterable[dict], *, bind: bool = True, fail: bool = False):
        self.rows, self.bind, self.fail = list(rows), bind, fail

    def for_account(self, account_context: AccountContext):
        if self.fail:
            return FailingConnector(account=account_context)
        connector = SyntheticConnector(rows=self.rows)
        if self.bind:
            connector._bind_account(account_context)
        return connector


class FailingConnector(SyntheticConnector):
    def _fetch_raw(self, start_date, end_date):
        raise RuntimeError("upstream outage")


def sync(account_context, *, factory=None, window=SYNTHETIC_WINDOW, now=NOW, **kwargs):
    return sync_account(
        account_context,
        factory or SyntheticConnectorFactory(),
        writer,
        triggered_by="test",
        window=window,
        now=lambda: now,
        random=lambda: 0.5,
        **kwargs,
    )


# 1. Backward compatibility


def test_upgraded_milestone_4_data_reproduces_its_report_through_syncs_and_analysis(strict):
    columns = m4_columns()
    before = data_snapshot(columns)
    outcome = run_client(
        LEGACY_CLIENT_ID,
        writer,
        factory=SyntheticConnectorFactory(),
        window=SYNTHETIC_WINDOW,
        triggered_by="test",
    )
    assert [(s.counts["records_received"], s.counts["records_written"]) for s in outcome.syncs] == [
        (210, 0),
        (90, 0),
    ]
    analysis = outcome.analysis
    assert analysis.report_id == M4_REPORT_ID
    assert analysis.counts == {
        "records_analysed": 300,
        "reports_written": 0,
        "findings_received": 19,
        "findings_written": 0,
        "breakdowns_written": 1,
    }
    after = data_snapshot(columns)
    for table in ("ad_performance_records", "analytics_reports", "findings", "quarantined_records"):
        assert after[table] == before[table], table
    stored = rows("select report from growthpulse.analytics_reports")[0][0]
    assert dumps_report(stored) == dumps_report(build_sample_report())
    assert counts("pipeline_runs", "analytics_report_breakdowns") == (5, 1)

    again = run_client(
        LEGACY_CLIENT_ID,
        writer,
        factory=SyntheticConnectorFactory(),
        window=SYNTHETIC_WINDOW,
        triggered_by="test",
    )
    assert again.analysis.report_id == M4_REPORT_ID
    assert again.analysis.counts["breakdowns_written"] == 0
    assert data_snapshot(columns)["findings"] == before["findings"]


def test_fresh_install_writes_once_then_nothing(strict):
    drop_everything()
    apply(MIGRATIONS)
    if strict:
        apply([PROPOSED])
    logins_for_existing_roles_only()
    first = run_client(
        LEGACY_CLIENT_ID,
        writer,
        factory=SyntheticConnectorFactory(),
        window=SYNTHETIC_WINDOW,
        triggered_by="test",
    )
    assert [s.counts["records_written"] for s in first.syncs] == [210, 90]
    assert first.analysis.report_id == M4_REPORT_ID
    assert (
        first.analysis.counts["reports_written"],
        first.analysis.counts["findings_written"],
        first.analysis.counts["breakdowns_written"],
    ) == (1, 19, 1)
    second = run_client(
        LEGACY_CLIENT_ID,
        writer,
        factory=SyntheticConnectorFactory(),
        window=SYNTHETIC_WINDOW,
        triggered_by="test",
    )
    assert [s.counts["records_written"] for s in second.syncs] == [0, 0]
    assert (
        second.analysis.counts["reports_written"],
        second.analysis.counts["findings_written"],
        second.analysis.counts["breakdowns_written"],
    ) == (0, 0, 0)
    assert counts("ad_performance_records", "analytics_reports", "findings") == (300, 1, 19)


def test_upgrade_through_phase_b_still_preserves_milestone_4(upgraded):
    assert counts("ad_performance_records", "findings", "analytics_reports") == (300, 19, 1)
    assert scalar("select report_id from growthpulse.analytics_reports") == M4_REPORT_ID


def test_the_frozen_milestone_4_store_needs_the_fallbacks_and_the_new_code_does_not(strict):
    if not strict:
        run_milestone_4_pipeline(writer, START, END)  # still works while fallbacks exist
        return
    with pytest.raises(pg_errors.NotNullViolation):
        run_milestone_4_pipeline(writer, START, END)
    run_client(
        LEGACY_CLIENT_ID,
        writer,
        factory=SyntheticConnectorFactory(),
        window=SYNTHETIC_WINDOW,
        triggered_by="test",
    )


# 2. Account syncs: history, watermark, status


def test_a_sync_records_its_run_summary_and_watermark(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    outcome = sync(google)
    assert outcome.attempt == 1
    run = rows(
        "select run_kind, status, client_id, ad_account_id, window_start, window_end, attempt, "
        "retry_of_run_id, report_id, data_quality from growthpulse.pipeline_runs "
        "where run_id = %s",
        (outcome.run_id,),
    )[0]
    assert run[:9] == (
        "account_sync",
        "succeeded",
        LEGACY_CLIENT_ID,
        LEGACY_GOOGLE_ACCOUNT_ID,
        START,
        END,
        1,
        None,
        None,
    )
    evidence = run[9]
    assert evidence["evidence_version"] == 1 and evidence["quarantine"] == []
    assert evidence["summary"] == (
        SyntheticConnectorFactory().for_account(google).fetch(START, END).summary
    )
    s = state(LEGACY_GOOGLE_ACCOUNT_ID)
    assert (
        s["status"],
        s["synced_through"],
        s["consecutive_failures"],
        s["lease_run_id"],
        s["next_due_at"],
        s["last_success_at"],
    ) == ("idle", END, 0, None, None, NOW)


def test_the_watermark_never_moves_backwards(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    sync(google, window=DateWindow(date(2026, 10, 20), date(2026, 10, 25)))
    assert state(LEGACY_GOOGLE_ACCOUNT_ID)["synced_through"] == END  # backfilled through Oct 30


def test_an_empty_window_is_a_successful_sync_of_nothing(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    outcome = sync(google, window=DateWindow(date(2026, 11, 1), date(2026, 11, 5)))
    assert outcome.counts == {
        "records_received": 0,
        "records_written": 0,
        "quarantined_rows": 0,
        "quarantine_entries_written": 0,
    }
    assert state(LEGACY_GOOGLE_ACCOUNT_ID)["synced_through"] == date(2026, 11, 5)


def test_a_sync_before_its_backfill_start_starts_no_run(upgraded):
    # Yesterday (local) is before the backfill start, so there is nothing to fetch yet.
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_config set backfill_start_date = %s "
            "where ad_account_id = %s",
            (date(2026, 11, 11), LEGACY_GOOGLE_ACCOUNT_ID),
        )
    before = counts("pipeline_runs")
    outcome = sync(account(LEGACY_GOOGLE_ACCOUNT_ID), window=None)
    assert outcome.run_id is None and outcome.window is None
    assert counts("pipeline_runs") == before


def test_a_scheduled_sync_refetches_its_lookback_window(upgraded):
    outcome = sync(account(LEGACY_GOOGLE_ACCOUNT_ID), window=None)
    # Watermark Oct 30, lookback 7 days, today Nov 11 in London: Oct 31 to Nov 10.
    assert outcome.window == DateWindow(date(2026, 10, 31), date(2026, 11, 10))
    assert state(LEGACY_GOOGLE_ACCOUNT_ID)["synced_through"] == date(2026, 11, 10)


def test_scheduled_accounts_get_their_next_due_time(upgraded):
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_config set frequency = 'daily', "
            "preferred_local_hour = 6 where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        )
    sync(account(LEGACY_GOOGLE_ACCOUNT_ID))
    assert state(LEGACY_GOOGLE_ACCOUNT_ID)["next_due_at"] == datetime(2026, 11, 12, 6, tzinfo=UTC)


# 3. Failures, retries and duplicate execution


def test_a_failed_sync_writes_nothing_keeps_the_watermark_and_schedules_a_retry(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    before = (counts("ad_performance_records"), state(LEGACY_GOOGLE_ACCOUNT_ID)["synced_through"])
    with pytest.raises(AccountSyncError) as caught:
        sync(google, factory=ScriptedFactory([], fail=True))
    error = caught.value
    assert (error.error_code, error.recorded) == ("connector_unavailable", True)
    assert "outage" not in str(error)
    s = state(LEGACY_GOOGLE_ACCOUNT_ID)
    assert (s["status"], s["consecutive_failures"], s["last_error_code"], s["lease_run_id"]) == (
        "retry_wait",
        1,
        "connector_unavailable",
        None,
    )
    assert s["next_due_at"] == NOW + timedelta(minutes=15)
    assert (counts("ad_performance_records"), s["synced_through"]) == before
    assert rows(
        "select status, error_code from growthpulse.pipeline_runs where run_id = %s",
        (error.run_id,),
    ) == [("failed", "connector_unavailable")]

    retried = sync(google)
    assert retried.attempt == 2
    assert rows(
        "select retry_of_run_id from growthpulse.pipeline_runs where run_id = %s", (retried.run_id,)
    ) == [(error.run_id,)]
    s = state(LEGACY_GOOGLE_ACCOUNT_ID)
    assert (s["status"], s["consecutive_failures"], s["last_error_code"]) == ("idle", 0, None)


def test_a_database_failure_mid_save_rolls_back_every_row(upgraded, monkeypatch):
    original = store.record_row
    seen = {"n": 0}

    def corrupt(record, run_id, **scope):
        row = original(record, run_id, **scope)
        seen["n"] += 1
        if seen["n"] == 50:
            row["campaign_type"] = "not_a_campaign_type"
        return row

    monkeypatch.setattr(store, "record_row", corrupt)
    drop_everything()
    apply(MIGRATIONS)
    logins_for_existing_roles_only()
    with pytest.raises(AccountSyncError) as caught:
        sync(account(LEGACY_GOOGLE_ACCOUNT_ID))
    assert caught.value.error_code == "database_rejected_data"
    assert counts("ad_performance_records") == (0,)
    s = state(LEGACY_GOOGLE_ACCOUNT_ID)
    assert (s["synced_through"], s["status"], s["lease_run_id"]) == (None, "needs_attention", None)


def test_repeated_failures_stop_the_account_until_the_owner_resumes_it(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_config set "
            "max_consecutive_failures = 2 where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        )
    for expected in ("retry_wait", "needs_attention"):
        with pytest.raises(AccountSyncError):
            sync(google, factory=ScriptedFactory([], fail=True))
        assert state(LEGACY_GOOGLE_ACCOUNT_ID)["status"] == expected
    with pytest.raises(AccountSyncError) as caught:
        sync(google)
    assert (caught.value.error_code, caught.value.recorded) == ("account_not_syncable", False)


def test_a_second_sync_is_refused_while_the_first_holds_the_lease(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    first = uuid4()
    with writer() as connection:
        store.claim_account_sync(
            connection, first, account=google, window=SYNTHETIC_WINDOW, triggered_by="test", now=NOW
        )
    with pytest.raises(AccountSyncError) as caught:
        sync(google)
    assert (caught.value.error_code, caught.value.recorded) == ("account_busy", False)
    assert rows("select status from growthpulse.pipeline_runs where run_id = %s", (first,)) == [
        ("running",)
    ]


def test_an_expired_lease_is_taken_over_and_its_run_marked_abandoned(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    stale = uuid4()
    with writer() as connection:
        store.claim_account_sync(
            connection,
            stale,
            account=google,
            window=SYNTHETIC_WINDOW,
            triggered_by="test",
            now=NOW - timedelta(hours=2),
        )
    outcome = sync(google)
    assert rows(
        "select status, error_code from growthpulse.pipeline_runs where run_id = %s", (stale,)
    ) == [("failed", "abandoned")]
    assert state(LEGACY_GOOGLE_ACCOUNT_ID)["status"] == "idle" and outcome.run_id != stale


def test_a_lost_lease_cannot_finish_the_run(upgraded):
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    first, second = uuid4(), uuid4()
    with writer() as connection:
        store.claim_account_sync(
            connection,
            first,
            account=google,
            window=SYNTHETIC_WINDOW,
            triggered_by="test",
            now=NOW - timedelta(hours=2),
        )
        store.claim_account_sync(
            connection,
            second,
            account=google,
            window=SYNTHETIC_WINDOW,
            triggered_by="test",
            now=NOW,
        )
    result = SyntheticConnectorFactory().for_account(google).fetch(START, END)
    with writer() as connection, pytest.raises(store.RunStateError, match="lease"):
        store.save_account_sync(
            connection,
            first,
            account=google,
            connector="synthetic",
            window=SYNTHETIC_WINDOW,
            result=result,
            now=NOW,
            next_due_at=None,
        )


@pytest.mark.parametrize("setup", ["paused", "archived", "client_archived", "disabled"])
def test_paused_archived_and_disabled_accounts_are_not_synced(upgraded, setup):
    statements = {
        "paused": [
            "update growthpulse.ad_accounts set status = 'paused' where ad_account_id = %(a)s",
            "update growthpulse.account_sync_state set status = 'paused' "
            "where ad_account_id = %(a)s",
        ],
        "archived": [
            "update growthpulse.ad_accounts set status = 'archived', archived_at = now() "
            "where ad_account_id = %(a)s"
        ],
        "client_archived": [
            "update growthpulse.clients set status = 'archived', "
            "archived_at = now() where client_id = %(c)s"
        ],
        "disabled": [
            "update growthpulse.account_sync_config set enabled = false where ad_account_id = %(a)s"
        ],
    }
    google = account(LEGACY_GOOGLE_ACCOUNT_ID)
    with admin() as connection:
        for statement in statements[setup]:
            connection.execute(statement, {"a": LEGACY_GOOGLE_ACCOUNT_ID, "c": LEGACY_CLIENT_ID})
    with pytest.raises(AccountSyncError) as caught:
        sync(google)
    assert caught.value.error_code == "account_not_syncable"


def test_due_accounts_lists_only_due_syncable_accounts(upgraded):
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_state set next_due_at = %s where ad_account_id = %s",
            (NOW - timedelta(minutes=1), LEGACY_GOOGLE_ACCOUNT_ID),
        )
        connection.execute(
            "update growthpulse.account_sync_state set next_due_at = %s where ad_account_id = %s",
            (NOW + timedelta(hours=1), LEGACY_META_ACCOUNT_ID),
        )
    with writer() as connection:
        assert due_accounts(connection, NOW) == [LEGACY_GOOGLE_ACCOUNT_ID]
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_config set enabled = false where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        )
    with writer() as connection:
        assert due_accounts(connection, NOW) == []


# 4. Account ownership: no record is ever re-filed


def test_rows_from_another_account_are_quarantined_not_remapped(upgraded):
    meta = account(LEGACY_META_ACCOUNT_ID)
    own = [r for r in generate_synthetic_rows() if r["platform"] == "meta_ads"]
    foreign = generate_synthetic_rows(client=SYNTHETIC_CLIENTS[1])[:5]
    outcome = sync(meta, factory=ScriptedFactory([*own, *foreign]))
    assert outcome.counts["records_received"] == 90
    assert outcome.counts["quarantined_rows"] == 5
    assert rows(
        "select distinct ad_account_id, issue_codes from growthpulse.quarantined_records"
    ) == [(LEGACY_META_ACCOUNT_ID, ["account_mismatch"])]
    assert (
        scalar(
            "select count(*) from growthpulse.ad_performance_records where account_id like '%%-002'"
        )
        == 0
    )


def test_a_foreign_record_that_bypasses_the_guard_fails_the_sync(upgraded):
    meta = account(LEGACY_META_ACCOUNT_ID)
    foreign = generate_synthetic_rows(client=SYNTHETIC_CLIENTS[1])[:5]
    before = counts("ad_performance_records")
    with pytest.raises(AccountSyncError) as caught:
        sync(meta, factory=ScriptedFactory(foreign, bind=False))
    assert caught.value.error_code == "account_mismatch"
    assert counts("ad_performance_records") == before
    assert state(LEGACY_META_ACCOUNT_ID)["status"] == "needs_attention"


def test_a_combined_run_refuses_records_of_unassigned_accounts(upgraded):
    from growthpulse_ai.pipeline.persisted_run import PipelineRunError, run_and_persist

    rows_ = [*generate_synthetic_rows(), *generate_synthetic_rows(client=SYNTHETIC_CLIENTS[1])]
    with pytest.raises(PipelineRunError) as caught:
        run_and_persist(
            SyntheticConnector(rows=rows_), writer, START, END, client=legacy(), triggered_by="test"
        )
    assert (caught.value.error_code, caught.value.recorded) == ("account_mismatch", True)
    assert counts("ad_performance_records") == (300,)


# 5. Several clients and accounts


def test_clients_sync_and_analyse_independently_and_stay_apart(strict):
    seed(IRELAND, EUROPE)
    outcomes = {
        client.client_id: run_client(
            client.client_id,
            writer,
            factory=SyntheticConnectorFactory(),
            window=SYNTHETIC_WINDOW,
            triggered_by="test",
        )
        for client in (CLIENTS[0], IRELAND, EUROPE)
    }
    report_ids = {cid: o.analysis.report_id for cid, o in outcomes.items()}
    assert report_ids[LEGACY_CLIENT_ID] == M4_REPORT_ID
    assert len(set(report_ids.values())) == 3
    for client in (CLIENTS[0], IRELAND, EUROPE):
        own = {a.ad_account_id for a in client.accounts}
        assert set(outcomes[client.client_id].analysis.ad_account_ids) == own
        assert rows(
            "select distinct client_id from growthpulse.findings where latest_report_id = %s",
            (report_ids[client.client_id],),
        ) == [(client.client_id,)]
    assert (
        scalar(
            "select count(*) from growthpulse.findings f join growthpulse.ad_accounts a "
            "on a.ad_account_id = f.ad_account_id where a.client_id <> f.client_id"
        )
        == 0
    )
    breakdown = rows(
        "select breakdown from growthpulse.analytics_report_breakdowns where client_id = %s",
        (EUROPE.client_id,),
    )[0][0]
    assert breakdown["compatibility"]["mixed_currencies"] is True
    assert breakdown["compatibility"]["currencies"] == ["EUR", "GBP", "USD"]
    europe_report = rows(
        "select report from growthpulse.analytics_reports where client_id = %s", (EUROPE.client_id,)
    )[0][0]
    assert sorted(p["currency"] for p in europe_report["full_period"]["portfolios"]) == [
        "EUR",
        "GBP",
        "USD",
    ]


def test_one_accounts_failure_never_touches_another(upgraded):
    seed(EUROPE)
    first, second, third = EUROPE.accounts
    with pytest.raises(AccountSyncError):
        sync(second, factory=ScriptedFactory([], fail=True))
    sync(first)
    sync(third)
    assert [state(a.ad_account_id)["status"] for a in EUROPE.accounts] == [
        "idle",
        "retry_wait",
        "idle",
    ]
    # The analysis waits until every active account has synced through the window.
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(EUROPE.client_id, writer, triggered_by="test", end=END)
    assert (caught.value.error_code, caught.value.recorded) == ("no_valid_records", False)
    assert (
        scalar(
            "select count(*) from growthpulse.analytics_reports where client_id = %s",
            (EUROPE.client_id,),
        )
        == 0
    )


# 6. Client analysis reads only stored records of active accounts


def test_analysis_uses_only_active_accounts_and_never_a_connector(upgraded, monkeypatch):
    for a in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        sync(account(a))

    def no_fetch(*_, **__):
        raise AssertionError("analysis must not fetch")

    monkeypatch.setattr(SyntheticConnector, "fetch", no_fetch)
    with admin() as connection:
        connection.execute(
            "update growthpulse.ad_accounts set status = 'paused' where ad_account_id = %s",
            (LEGACY_META_ACCOUNT_ID,),
        )
    outcome = run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test")
    assert outcome.ad_account_ids == (LEGACY_GOOGLE_ACCOUNT_ID,)
    assert outcome.counts["records_analysed"] == 210
    assert outcome.report_id != M4_REPORT_ID
    report = rows(
        "select report from growthpulse.analytics_reports where report_id = %s",
        (outcome.report_id,),
    )[0][0]
    assert {c["platform"] for c in report["full_period"]["campaigns"]} == {"google_ads"}


def test_analysis_refuses_a_window_past_an_accounts_watermark(upgraded):
    for a in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        sync(account(a))
    before = counts("pipeline_runs")
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test", end=date(2026, 11, 2))
    assert (caught.value.error_code, caught.value.recorded) == ("no_valid_records", False)
    assert counts("pipeline_runs") == before


def test_analysis_refuses_records_whose_currency_differs_from_their_account(upgraded):
    for a in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        sync(account(a))
    with admin() as connection:
        connection.execute(
            "update growthpulse.ad_performance_records set currency = 'EUR' "
            "where ad_account_id = %s",
            (LEGACY_META_ACCOUNT_ID,),
        )
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test")
    assert (caught.value.error_code, caught.value.recorded) == ("account_mismatch", True)
    assert counts("analytics_report_breakdowns") == (0,)


def test_analysis_without_records_in_the_window_records_a_failure(upgraded):
    google, meta = account(LEGACY_GOOGLE_ACCOUNT_ID), account(LEGACY_META_ACCOUNT_ID)
    empty = DateWindow(date(2026, 11, 1), date(2026, 11, 5))
    sync(google, window=empty)
    sync(meta, window=empty)
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test", days=5)
    assert (caught.value.error_code, caught.value.recorded) == ("no_valid_records", True)
    assert counts("analytics_reports") == (1,)


def test_a_conflicting_breakdown_fails_the_analysis_without_writing(upgraded):
    for a in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        sync(account(a))
    fake = {"breakdown_id": "e" * 64, "report_id": M4_REPORT_ID, "breakdown_schema_version": "1.0"}
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.analytics_report_breakdowns (report_id, client_id, "
            "breakdown_id, breakdown_schema_version, breakdown) values (%s, %s, %s, '1.0', %s)",
            (M4_REPORT_ID, LEGACY_CLIENT_ID, "e" * 64, psycopg.types.json.Jsonb(fake)),
        )
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test")
    assert caught.value.error_code == "run_state_conflict"
    assert rows(
        "select status from growthpulse.pipeline_runs where run_kind = 'client_analysis'"
    ) == [("failed",)]


def test_a_report_id_held_by_another_client_is_refused(strict):
    seed(IRELAND)
    ireland_run = uuid4()
    body = build_sample_report()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, client_id) "
            "values (%s, 'synthetic', 'test', %s)",
            (ireland_run, IRELAND.client_id),
        )
        connection.execute("delete from growthpulse.findings")
        connection.execute(
            "update growthpulse.pipeline_runs set report_id = null, "
            "status = 'failed', error_code = 'x' where report_id is not null"
        )
        connection.execute("alter table growthpulse.analytics_reports disable trigger user")
        connection.execute("delete from growthpulse.analytics_reports")
        connection.execute("alter table growthpulse.analytics_reports enable trigger user")
        connection.execute(
            "insert into growthpulse.analytics_reports (report_id, run_id, data_source, "
            "report_schema_version, data_as_of, period_start, period_end, record_count, report, "
            "client_id) values (%s, %s, 'synthetic', '1.1', now(), %s, %s, 300, %s, %s)",
            (
                M4_REPORT_ID,
                ireland_run,
                START,
                END,
                psycopg.types.json.Jsonb(body),
                IRELAND.client_id,
            ),
        )
    for a in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        sync(account(a))
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test")
    assert caught.value.error_code == "run_state_conflict"
    assert counts("findings") == (0,)


# 7. The proposed migration that removes the fallbacks


def test_without_fallbacks_a_writer_that_omits_scope_is_refused():
    build_upgraded(strict=True)
    with pytest.raises(pg_errors.NotNullViolation), writer() as connection:
        connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, "
            "triggered_by) values (gen_random_uuid(), 'synthetic', 'test')"
        )
    assert not scalar("select exists (select from pg_proc where proname = 'derive_scope')")
    assert scalar("select exists (select from pg_proc where proname = 'check_record_run_scope')")


def test_the_milestone_5_revert_still_works_after_the_proposed_migration():
    drop_everything()
    apply(M4_MIGRATIONS)
    logins_for_existing_roles_only()
    for _ in range(2):
        run_milestone_4_pipeline(writer, START, END)
    columns = m4_columns()
    pristine_schema, pristine_data = schema_snapshot(), data_snapshot(columns)
    apply(M5_MIGRATIONS)
    apply([PROPOSED])
    run_sql_file(REVERT_M5)
    assert schema_snapshot() == pristine_schema
    assert data_snapshot(columns) == pristine_data


def test_milestone_5_scope_is_never_derived_in_application_sql():
    """The store names every scope column it writes, so no write depends on a default."""
    for query in (
        store.INSERT_RUN,
        store.UPSERT_RECORD,
        store.UPSERT_QUARANTINE,
        store.INSERT_REPORT,
        store.UPSERT_FINDING,
        store.INSERT_PROPOSAL,
    ):
        assert "client_id" in query or "ad_account_id" in query, query[:40]
    assert DataSource.SYNTHETIC  # synthetic data only


# 8. Data quality from sync evidence: re-fetches, overlap and order (approved decision 1)

LOOKBACK = DateWindow(date(2026, 10, 24), date(2026, 10, 30))
FIRST_HALF = DateWindow(date(2026, 10, 1), date(2026, 10, 15))
SECOND_HALF = DateWindow(date(2026, 10, 16), date(2026, 10, 30))


def fresh() -> None:
    drop_everything()
    apply(MIGRATIONS)
    logins_for_existing_roles_only()


def sync_both(windows: list[DateWindow], factory=None) -> None:
    for window in windows:
        for account_id in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
            sync(account(account_id), window=window, factory=factory)


def analyse(**kwargs):
    return run_client_analysis(LEGACY_CLIENT_ID, writer, triggered_by="test", **kwargs)


def stored_quality(report_id: str) -> dict:
    return rows("select report -> 'data_quality' from growthpulse.analytics_reports "
                "where report_id = %s", (report_id,))[0][0]  # fmt: skip


@pytest.mark.parametrize(
    "windows",
    [
        [SYNTHETIC_WINDOW, LOOKBACK],
        [LOOKBACK, SYNTHETIC_WINDOW],
        [FIRST_HALF, SECOND_HALF, LOOKBACK],
        [SECOND_HALF, LOOKBACK, FIRST_HALF, SYNTHETIC_WINDOW],
    ],
    ids=["full-then-lookback", "lookback-then-full", "halves-then-lookback", "shuffled"],
)
def test_refetches_and_sync_order_never_change_the_report(windows):
    fresh()
    sync_both(windows)
    outcome = analyse(end=END)
    assert outcome.report_id == M4_REPORT_ID
    assert stored_quality(M4_REPORT_ID) == build_sample_report()["data_quality"]


BAD_META_ROW = {
    **[r for r in generate_synthetic_rows() if r["platform"] == "meta_ads"][0],
    "reporting_date": "2026-10-25",
    "campaign_id": "synthetic-broken-row",
    "spend": "-5.00",
}


def meta_rows_with_one_bad_row() -> list[dict]:
    return [*generate_synthetic_rows(), BAD_META_ROW]


def sync_with_bad_row(windows: list[DateWindow]) -> None:
    """Google syncs normally; Meta's source also returns one bad row dated 25 October."""
    meta_rows = [r for r in meta_rows_with_one_bad_row() if r["platform"] == "meta_ads"]
    for window in windows:
        sync(account(LEGACY_GOOGLE_ACCOUNT_ID), window=window)
        sync(account(LEGACY_META_ACCOUNT_ID), window=window, factory=ScriptedFactory(meta_rows))


def test_a_bad_row_refetched_by_overlapping_syncs_is_counted_once():
    fresh()
    sync_with_bad_row([SYNTHETIC_WINDOW, LOOKBACK, LOOKBACK])
    quality = stored_quality(analyse(end=END).report_id)
    assert (quality["quarantined_rows"], quality["errors_by_code"]) == (1, {"negative_value": 1})
    assert quality["valid_records"] == 300
    assert counts("quarantined_records") == (1,)


def test_quality_evidence_outside_the_window_is_not_counted():
    fresh()
    sync_with_bad_row([SYNTHETIC_WINDOW])
    quality = stored_quality(analyse(end=date(2026, 10, 24), days=24).report_id)
    assert quality["quarantined_rows"] == 0


def test_the_same_evidence_in_any_sync_order_gives_the_same_report():
    report_ids = []
    for windows in ([SYNTHETIC_WINDOW, LOOKBACK], [LOOKBACK, FIRST_HALF, SECOND_HALF]):
        fresh()
        sync_with_bad_row(windows)
        report_ids.append(analyse(end=END).report_id)
    assert report_ids[0] == report_ids[1] != M4_REPORT_ID


def test_analysis_refuses_a_window_its_syncs_do_not_cover():
    fresh()
    sync_both([SECOND_HALF])
    with pytest.raises(ClientAnalysisError) as caught:
        analyse(end=END)  # watermarks reach 30 October, but 1-15 October was never synced
    assert (caught.value.error_code, caught.value.recorded) == ("quality_evidence_incomplete", True)
    assert counts("analytics_reports") == (0,)


def test_upgraded_data_is_not_analysed_until_its_accounts_have_synced(upgraded):
    # The Milestone 4 rows came from combined runs, which stored no sync evidence.
    with pytest.raises(ClientAnalysisError) as caught:
        analyse(end=END)
    assert caught.value.error_code == "quality_evidence_incomplete"
    assert counts("analytics_report_breakdowns") == (0,)


# 9. Complete responses only (approved decision 3)


class UnconfirmedFactory(ScriptedFactory):
    def for_account(self, account_context: AccountContext):
        connector = UnconfirmedConnector(rows=self.rows)
        connector._bind_account(account_context)
        return connector


class UnconfirmedConnector(SyntheticConnector):
    def _response_complete(self) -> bool:
        return False  # e.g. pagination not finished


@pytest.mark.parametrize("returned", ["some rows", "no rows"])
def test_an_unconfirmed_response_never_advances_the_watermark(upgraded, returned):
    meta = account(LEGACY_META_ACCOUNT_ID)
    raw = [r for r in generate_synthetic_rows() if r["platform"] == "meta_ads"]
    window = DateWindow(date(2026, 11, 1), date(2026, 11, 5))
    before = state(LEGACY_META_ACCOUNT_ID)["synced_through"]
    with pytest.raises(AccountSyncError) as caught:
        sync(
            meta, window=window, factory=UnconfirmedFactory(raw if returned == "some rows" else [])
        )
    assert caught.value.error_code == "incomplete_response"
    s = state(LEGACY_META_ACCOUNT_ID)
    assert (s["synced_through"], s["status"]) == (before, "retry_wait")


# 10. Onboarding and manual activation (approved decision 2)


def onboarding_client() -> ClientContext:
    with admin() as connection:
        seed_synthetic_client(connection, IRELAND, with_sync=True, status="onboarding")
    with writer() as connection:
        return load_client(connection, IRELAND.client_id)


def test_onboarding_accounts_sync_but_are_never_analysed_or_activated_automatically(upgraded):
    client = onboarding_client()
    for context in client.syncable_accounts:
        sync(context)
    assert {a.status.value for a in load_client(writer(), IRELAND.client_id).accounts} == {
        "onboarding"
    }
    with pytest.raises(ClientAnalysisError) as caught:
        run_client_analysis(IRELAND.client_id, writer, triggered_by="test", end=END)
    assert caught.value.error_code == "client_not_found"  # no active account yet


def test_only_the_owner_action_activates_an_account(upgraded):
    client = onboarding_client()
    for context in client.syncable_accounts:
        sync(context)
    first = client.accounts[0].ad_account_id
    with writer() as connection, pytest.raises(pg_errors.InsufficientPrivilege):
        connection.execute("update growthpulse.ad_accounts set status = 'active' "
                           "where ad_account_id = %s", (first,))  # fmt: skip
    can_activate = scalar(
        "select has_function_privilege(%s, 'growthpulse.set_account_status(uuid, text)', "
        "'execute')",
        (WRITER,),
    )
    assert can_activate is False
    # The owner action itself, run here as the migration owner: no role's login changes.
    with owner() as connection:
        for context in client.accounts:
            connection.execute("select growthpulse.set_account_status(%s, 'active')",
                               (context.ad_account_id,))  # fmt: skip
    outcome = run_client_analysis(IRELAND.client_id, writer, triggered_by="test", end=END)
    assert set(outcome.ad_account_ids) == {a.ad_account_id for a in IRELAND.accounts}
    assert scalar("select count(*) from growthpulse.owner_action_log "
                  "where action = 'set_account_status_active'") == 2  # fmt: skip


# 11. The Phase D fingerprints


FINGERPRINTS = MIGRATIONS[0].parents[1] / "verification" / "milestone_4_fingerprints.sql"


def fingerprints() -> dict[str, tuple[str, str]]:
    results = {}
    with admin() as connection:
        connection.execute("set timezone = 'Pacific/Chatham'")  # must not matter
        for statement in FINGERPRINTS.read_text().split(";\n"):
            if statement.strip():
                cursor = connection.execute(statement)
                if cursor.description:
                    results.update({name: (n, digest) for name, n, digest in cursor.fetchall()})
        assert connection.info.transaction_status.name == "IDLE"
    return results


def test_fingerprints_are_unchanged_by_the_upgrade_the_phase_d_run_and_a_revert():
    drop_everything()
    apply(M4_MIGRATIONS)
    logins_for_existing_roles_only()
    for _ in range(2):
        run_milestone_4_pipeline(writer, START, END)
    before = fingerprints()
    assert before["08 report id"] == ("1", M4_REPORT_ID)
    assert before["02 ad_performance_records"][0] == "300"
    assert before["05 findings"][0] == "19"

    apply(M5_MIGRATIONS)
    logins_for_existing_roles_only()
    assert fingerprints() == before

    # The Phase D synthetic command adds runs and a breakdown, and changes nothing else.
    run_client(LEGACY_CLIENT_ID, writer, factory=SyntheticConnectorFactory(),
               window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    after_run = fingerprints()
    assert after_run["01 pipeline_runs"][0] == "5"
    assert {k: v for k, v in after_run.items() if k != "01 pipeline_runs"} == {
        k: v for k, v in before.items() if k != "01 pipeline_runs"
    }

    run_sql_file(REVERT_M5)
    reverted = fingerprints()
    assert {k: v for k, v in reverted.items() if k != "01 pipeline_runs"} == {
        k: v for k, v in before.items() if k != "01 pipeline_runs"
    }
