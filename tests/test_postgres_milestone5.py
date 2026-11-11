"""Milestone 5 migrations on a disposable PostgreSQL server: upgrade, isolation and roles.

Skipped unless GROWTHPULSE_TEST_DATABASE_URL is set (see test_postgres_integration.py).
Every test builds its own state: usually the exact Milestone 4 state (migrations 1-3 plus
two persisted synthetic runs, as on Supabase), then the five Milestone 5 migrations.
Logins for the app roles are enabled through trust authentication on the throwaway
server only; no password is set anywhere.
"""

import hashlib
import json
import os
from datetime import date
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.types.json import Jsonb

from growthpulse_ai.accounts.assignment import report_findings
from growthpulse_ai.accounts.legacy import (
    LEGACY_CLIENT_ID,
    LEGACY_GOOGLE_ACCOUNT_ID,
    LEGACY_META_ACCOUNT_ID,
    LEGACY_SYNTHETIC_CLIENT,
    LEGACY_WORKSPACE_ID,
)
from growthpulse_ai.accounts.models import (
    ACCOUNT_TRANSITIONS,
    CONNECTION_TRANSITIONS,
    AccountStatus,
    ConnectionStatus,
)
from growthpulse_ai.analytics.breakdown import build_account_breakdown
from growthpulse_ai.analytics.report import build_report, dumps_report
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import SYNTHETIC_CLIENTS, generate_synthetic_rows
from growthpulse_ai.persistence import store
from growthpulse_ai.persistence.load import load_records
from growthpulse_ai.persistence.store import ExperimentProposal, record_experiment_proposal
from growthpulse_ai.persistence.tenancy import load_client
from growthpulse_ai.pipeline.client_analysis import analyse_client
from growthpulse_ai.pipeline.persisted_run import run_and_persist
from growthpulse_ai.pipeline.sample_report import build_sample_report
from growthpulse_ai.schema import DataSource
from m4_frozen import run_milestone_4_pipeline
from multi_client import CLIENTS, END, START, sync_client
from pg_helpers import (
    API_ROLES,
    DASHBOARD,
    M4_MIGRATIONS,
    M5_MIGRATIONS,
    MANAGER,
    MIGRATIONS,
    OWNER,
    REVERT_M5,
    SCHEMAS,
    TEST_URL,
    TOKEN_MANAGER,
    WRITER,
    admin,
    apply,
    as_role,
    drop_everything,
    enable_test_logins,
    owner,
    prepare_server,
    refuse_hosted_database,
    rows,
    run_sql_file,
    scalar,
)
from pg_seed import seed_synthetic_client, start_client_run

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_URL, reason="GROWTHPULSE_TEST_DATABASE_URL is not set"),
]

M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"
M4_TABLES = {
    "pipeline_runs": "run_id",
    "ad_performance_records": "record_id",
    "quarantined_records": "quarantine_id",
    "analytics_reports": "report_id",
    "findings": "finding_id",
    "experiment_proposals": "proposal_id",
    "experiment_status_events": "event_id",
}
IRELAND = CLIENTS[1]

MANAGER_FUNCTIONS = {
    "create_client(uuid, text, text)",
    "rename_client(uuid, text)",
    "archive_client(uuid)",
    "create_connection(uuid, text, text)",
    "revoke_connection(uuid)",
    "add_ad_account(uuid, uuid, text, date)",
    "add_synthetic_ad_account(uuid, text, text, text, text, date)",
    "set_account_status(uuid, text)",
    "set_sync_config(uuid, boolean, text, smallint, smallint, smallint)",
    "request_sync(uuid)",
    "decide_experiment(uuid, text, text)",
}
TOKEN_FUNCTIONS = {
    "store_credential(uuid, text, smallint, smallint, bytea, bytea, timestamp with time zone)",
    "read_credentials(uuid)",
    "delete_credentials(uuid)",
    "mark_connection_needs_reauth(uuid)",
    "credentials_needing_rotation(smallint)",
}


@pytest.fixture(scope="module", autouse=True)
def server():
    refuse_hosted_database()
    prepare_server()
    drop_everything()
    yield
    drop_everything()


# State builders


def legacy_client():
    with as_role(WRITER) as connection:
        return load_client(connection, LEGACY_CLIENT_ID)


def run_m4_pipeline():
    """A combined synthetic run of the legacy client, with its scope passed explicitly."""
    return run_and_persist(
        SyntheticConnector(),
        lambda: as_role(WRITER),
        START,
        END,
        client=legacy_client(),
        triggered_by="test",
    )


def milestone_4_state() -> None:
    """Migrations 1-3 and two persisted synthetic runs: the state on Supabase today."""
    drop_everything()
    apply(M4_MIGRATIONS)
    enable_test_logins()
    # The frozen Milestone 4 store, run twice, exactly as on Supabase.
    first = run_milestone_4_pipeline(lambda: as_role(WRITER), START, END)
    second = run_milestone_4_pipeline(lambda: as_role(WRITER), START, END)
    assert first["records_written"] == 300 and second["records_written"] == 0


def upgrade() -> None:
    apply(M5_MIGRATIONS)
    enable_test_logins()


@pytest.fixture
def upgraded() -> None:
    milestone_4_state()
    upgrade()


def m4_columns() -> dict[str, list[str]]:
    with admin() as connection:
        return {
            table: [
                name
                for (name,) in connection.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'growthpulse' and table_name = %s "
                    "order by ordinal_position",
                    (table,),
                ).fetchall()
            ]
            for table in M4_TABLES
        }


def data_snapshot(columns: dict[str, list[str]]) -> dict[str, list[str]]:
    """Every Milestone 4 row as JSON text, limited to the Milestone 4 columns."""
    snapshot = {}
    with admin() as connection:
        for table, key in M4_TABLES.items():
            query = sql.SQL(
                "select row_to_json(t)::text from (select {} from {} order by {}) as t"
            ).format(
                sql.SQL(", ").join(sql.Identifier(c) for c in columns[table]),
                sql.Identifier("growthpulse", table),
                sql.Identifier(key),
            )
            snapshot[table] = [row for (row,) in connection.execute(query).fetchall()]
    return snapshot


def call(role: str, function: str, *args):
    with as_role(role) as connection:
        if role == DASHBOARD:
            connection.execute("set default_transaction_read_only = off")
        placeholders = ", ".join(["%s"] * len(args))
        return connection.execute(f"select {function}({placeholders})", args).fetchone()[0]  # noqa: S608


def failing(statement: str, params: tuple = (), *, role: str | None = None):
    """Run one statement and return the PostgreSQL error class it raised."""
    connection = as_role(role) if role else admin()
    with connection:
        if role == DASHBOARD:
            connection.execute("set default_transaction_read_only = off")
        with pytest.raises(psycopg.Error) as caught:
            connection.execute(statement, params or None)
    return type(caught.value)


# 1. Upgrade from the exact Milestone 4 state


def test_new_roles_are_created_without_login(upgraded):
    # enable_test_logins ran after the upgrade, so check the migration text and attributes.
    text = "".join(path.read_text() for path in M5_MIGRATIONS)
    assert f"create role {MANAGER} nologin noinherit;" in text
    assert f"create role {TOKEN_MANAGER} nologin noinherit;" in text
    milestone_4_state()
    apply(M5_MIGRATIONS)
    logins = dict(
        rows(
            "select rolname, rolcanlogin from pg_roles where rolname in (%s, %s)",
            (MANAGER, TOKEN_MANAGER),
        )
    )
    assert logins == {MANAGER: False, TOKEN_MANAGER: False}


def test_upgrade_keeps_every_milestone_4_row_byte_for_byte():
    milestone_4_state()
    columns = m4_columns()
    before = data_snapshot(columns)
    upgrade()
    assert data_snapshot(columns) == before
    counts = {table: len(values) for table, values in before.items()}
    assert counts == {
        "pipeline_runs": 2,
        "ad_performance_records": 300,
        "quarantined_records": 0,
        "analytics_reports": 1,
        "findings": 19,
        "experiment_proposals": 0,
        "experiment_status_events": 0,
    }


def test_historical_report_bytes_hash_and_ids_are_unchanged(upgraded):
    report_id, body, body_text = rows(
        "select report_id, report, report::text from growthpulse.analytics_reports"
    )[0]
    assert report_id == M4_REPORT_ID == body["report_id"]
    unsigned = {key: value for key, value in body.items() if key != "report_id"}
    assert hashlib.sha256(dumps_report(unsigned).encode()).hexdigest() == M4_REPORT_ID
    assert body == build_sample_report()
    assert json.loads(body_text) == body
    stored_records = {
        r for (r,) in rows("select record_id from growthpulse.ad_performance_records")
    }
    assert stored_records == {r.record_id for r in SyntheticConnector().fetch(START, END).records}
    stored_findings = {f for (f,) in rows("select finding_id from growthpulse.findings")}
    assert stored_findings == {f["finding_id"] for f in report_findings(build_sample_report())}


def test_backfill_maps_210_google_and_90_meta_records_by_stored_identity(upgraded):
    mapped = rows(
        """
        select a.ad_account_id, a.platform, a.external_account_id, count(*),
            bool_and(r.platform = a.platform and r.account_id = a.external_account_id
                     and r.data_source = a.data_source)
        from growthpulse.ad_performance_records as r
        join growthpulse.ad_accounts as a on a.ad_account_id = r.ad_account_id
        group by a.ad_account_id, a.platform, a.external_account_id
        order by a.platform
        """
    )
    assert mapped == [
        (LEGACY_GOOGLE_ACCOUNT_ID, "google_ads", "synthetic-google-account-001", 210, True),
        (LEGACY_META_ACCOUNT_ID, "meta_ads", "synthetic-meta-account-001", 90, True),
    ]
    assert (
        scalar(
            "select count(*) from growthpulse.ad_performance_records where ad_account_id is null"
        )
        == 0
    )


def test_backfill_scopes_runs_reports_and_findings_to_the_legacy_client(upgraded):
    assert rows(
        "select distinct run_kind, client_id, ad_account_id from growthpulse.pipeline_runs"
    ) == [("combined", LEGACY_CLIENT_ID, None)]
    assert rows("select client_id from growthpulse.analytics_reports") == [(LEGACY_CLIENT_ID,)]
    by_account = dict(
        rows(
            "select ad_account_id, count(*) from growthpulse.findings "
            "where client_id = %s group by ad_account_id",
            (LEGACY_CLIENT_ID,),
        )
    )
    assert sum(by_account.values()) == 19
    assert set(by_account) <= {LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID}
    assert (
        scalar(
            "select count(*) from growthpulse.findings f join growthpulse.ad_accounts a "
            "on a.ad_account_id = f.ad_account_id where a.platform <> f.platform "
            "or a.external_account_id <> f.account_id"
        )
        == 0
    )


def test_backfill_seeds_the_legacy_client_and_watermarks(upgraded):
    assert rows("select workspace_id, name from growthpulse.workspaces") == [
        (LEGACY_WORKSPACE_ID, "GrowthPulse workspace")
    ]
    assert rows("select client_id, name, data_source, status from growthpulse.clients") == [
        (LEGACY_CLIENT_ID, LEGACY_SYNTHETIC_CLIENT.name, "synthetic", "active")
    ]
    state = rows(
        "select s.ad_account_id, s.status, s.synced_through, c.frequency, c.backfill_start_date "
        "from growthpulse.account_sync_state s join growthpulse.account_sync_config c "
        "using (ad_account_id) order by s.ad_account_id"
    )
    assert state == sorted(
        (account_id, "idle", date(2026, 10, 30), "manual", date(2026, 10, 1))
        for account_id in (LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID)
    )


def test_combined_run_after_upgrade_creates_no_duplicates(upgraded):
    outcome = run_m4_pipeline()
    assert outcome.report_id == M4_REPORT_ID
    assert (
        outcome.counts["records_written"],
        outcome.counts["reports_written"],
        outcome.counts["findings_written"],
    ) == (0, 0, 0)
    assert rows(
        "select (select count(*) from growthpulse.ad_performance_records), "
        "(select count(*) from growthpulse.analytics_reports), "
        "(select count(*) from growthpulse.findings), "
        "(select count(*) from growthpulse.pipeline_runs)"
    ) == [(300, 1, 19, 3)]
    assert rows(
        "select run_kind, client_id, status from growthpulse.pipeline_runs where run_id = %s",
        (outcome.run_id,),
    ) == [("combined", LEGACY_CLIENT_ID, "succeeded")]


def test_combined_run_after_upgrade_still_writes_real_changes_in_scope(upgraded):
    raw = generate_synthetic_rows()
    raw[0] = {**raw[0], "spend": "999.99"}
    outcome = run_and_persist(
        SyntheticConnector(rows=raw),
        lambda: as_role(WRITER),
        START,
        END,
        client=legacy_client(),
        triggered_by="test",
    )
    assert outcome.counts["records_written"] == 1 and outcome.counts["reports_written"] == 1
    assert rows(
        "select client_id from growthpulse.analytics_reports where report_id = %s",
        (outcome.report_id,),
    ) == [(LEGACY_CLIENT_ID,)]
    assert (
        scalar(
            "select count(*) from growthpulse.findings where client_id <> %s", (LEGACY_CLIENT_ID,)
        )
        == 0
    )


def test_fresh_install_runs_the_combined_pipeline_idempotently():
    drop_everything()
    apply(MIGRATIONS)
    enable_test_logins()
    first, second = run_m4_pipeline(), run_m4_pipeline()
    assert first.report_id == second.report_id == M4_REPORT_ID
    assert (first.counts["records_written"], first.counts["findings_written"]) == (300, 19)
    assert (
        second.counts["records_written"],
        second.counts["reports_written"],
        second.counts["findings_written"],
    ) == (0, 0, 0)
    assert scalar("select count(*) from growthpulse.ad_accounts") == 2


# 2. The backfill fails closed and leaves Milestone 4 untouched


def insert_live_run_and_record(connection: psycopg.Connection) -> None:
    run_id = uuid4()
    connection.execute(
        "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by) "
        "values (%s, 'live', 'test')",
        (run_id,),
    )


def insert_unknown_account_record(connection: psycopg.Connection) -> None:
    connection.execute(
        "insert into growthpulse.ad_performance_records "
        "select md5('x') || md5('y'), data_source, reporting_date, platform, "
        "'synthetic-google-account-999', campaign_id, campaign_name, campaign_type, "
        "impressions, clicks, spend, conversions, conversion_value, currency, "
        "account_timezone, conversion_event_id, conversion_event_name, conversion_stage, "
        "attribution_model, click_window_days, view_window_days, report_time_basis, "
        "ingested_at, first_seen_run_id, last_changed_run_id, first_seen_at, updated_at "
        "from growthpulse.ad_performance_records limit 1"
    )


def change_one_record_currency(connection: psycopg.Connection) -> None:
    connection.execute(
        "update growthpulse.ad_performance_records set currency = 'EUR' "
        "where record_id = (select min(record_id) from growthpulse.ad_performance_records)"
    )


def duplicate_one_record_identity(connection: psycopg.Connection) -> None:
    connection.execute(
        "insert into growthpulse.ad_performance_records "
        "select md5('dup') || md5('dup'), data_source, reporting_date, platform, account_id, "
        "campaign_id, campaign_name, campaign_type, impressions, clicks, spend, conversions, "
        "conversion_value, currency, account_timezone, conversion_event_id, "
        "conversion_event_name, conversion_stage, attribution_model, click_window_days, "
        "view_window_days, report_time_basis, ingested_at, first_seen_run_id, "
        "last_changed_run_id, first_seen_at, updated_at "
        "from growthpulse.ad_performance_records limit 1"
    )


def point_a_finding_at_an_unknown_account(connection: psycopg.Connection) -> None:
    connection.execute(
        "update growthpulse.findings set account_id = 'synthetic-meta-account-404' "
        "where finding_id = (select min(finding_id) from growthpulse.findings)"
    )


ANOMALIES = {
    "non-synthetic run": (insert_live_run_and_record, "non-synthetic"),
    "unknown account": (insert_unknown_account_record, "does not match a legacy account"),
    "inconsistent currency": (change_one_record_currency, "does not match a legacy account"),
    "duplicate identity": (duplicate_one_record_identity, "duplicate record identities"),
    "unknown finding account": (point_a_finding_at_an_unknown_account, "finding"),
}


def assert_milestone_4_untouched(columns, before) -> None:
    assert data_snapshot(columns) == before
    assert not scalar(
        "select exists (select from information_schema.columns where table_schema = "
        "'growthpulse' and table_name = 'analytics_reports' and column_name = 'client_id')"
    )
    assert not scalar("select exists (select from pg_roles where rolname = %s)", (MANAGER,))


@pytest.mark.parametrize("case", list(ANOMALIES))
def test_backfill_refuses_unexpected_data_and_changes_nothing(case: str):
    corrupt, message = ANOMALIES[case]
    milestone_4_state()
    with admin() as connection:
        corrupt(connection)
    columns = m4_columns()
    before = data_snapshot(columns)
    apply(M5_MIGRATIONS[:1])  # migration 4 only creates empty tables
    with pytest.raises(psycopg.Error, match=message):
        apply(M5_MIGRATIONS[1:2])
    assert_milestone_4_untouched(columns, before)
    assert scalar("select count(*) from growthpulse.workspaces") == 0


def without_preconditions() -> str:
    text = M5_MIGRATIONS[1].read_text()
    start, end = text.index("-- preconditions:begin"), text.index("-- preconditions:end")
    return text[:start] + text[end:]


@pytest.mark.parametrize(
    ("corrupt", "error"),
    [
        (insert_live_run_and_record, pg_errors.ForeignKeyViolation),
        (insert_unknown_account_record, pg_errors.NotNullViolation),
        (point_a_finding_at_an_unknown_account, pg_errors.NotNullViolation),
    ],
)
def test_constraints_still_fail_closed_if_the_preconditions_were_bypassed(corrupt, error):
    milestone_4_state()
    with admin() as connection:
        corrupt(connection)
    columns = m4_columns()
    before = data_snapshot(columns)
    apply(M5_MIGRATIONS[:1])
    with owner() as connection, pytest.raises(error), connection.transaction():
        connection.execute(without_preconditions())
    assert_milestone_4_untouched(columns, before)


# 3. Adversarial cross-client references, by direct SQL as a superuser


@pytest.fixture
def two_clients(upgraded) -> UUID:
    """The legacy client plus the fictional Ireland client, with stored data for both."""
    with admin() as connection:
        seed_synthetic_client(connection, IRELAND)
    run_id = uuid4()
    with admin() as connection:
        start_client_run(connection, run_id, IRELAND.client_id)
    result = SyntheticConnector(rows=generate_synthetic_rows(client=SYNTHETIC_CLIENTS[1])).fetch(
        START, END
    )
    with as_role(WRITER) as connection:
        store.save_run_results(
            connection,
            run_id,
            client=IRELAND,
            data_source=DataSource.SYNTHETIC,
            connector="synthetic",
            records=result.records,
            quarantined=result.quarantined,
            report=build_report(result.records, data_quality=result.summary),
        )
    return run_id


def ireland_account(index: int = 0) -> UUID:
    return IRELAND.accounts[index].ad_account_id


def legacy_run() -> UUID:
    return scalar(
        "select min(run_id::text)::uuid from growthpulse.pipeline_runs where client_id = %s",
        (LEGACY_CLIENT_ID,),
    )


def report_of(client_id: UUID) -> str:
    return scalar(
        "select report_id from growthpulse.analytics_reports where client_id = %s", (client_id,)
    )


def copy_record(**overrides: str) -> str:
    """INSERT ... SELECT copying one legacy Google record, with SQL overrides."""
    columns = [
        "record_id",
        "data_source",
        "reporting_date",
        "platform",
        "account_id",
        "campaign_id",
        "campaign_name",
        "campaign_type",
        "impressions",
        "clicks",
        "spend",
        "conversions",
        "conversion_value",
        "currency",
        "account_timezone",
        "conversion_event_id",
        "conversion_event_name",
        "conversion_stage",
        "attribution_model",
        "click_window_days",
        "view_window_days",
        "report_time_basis",
        "ingested_at",
        "first_seen_run_id",
        "last_changed_run_id",
        "ad_account_id",
    ]
    overrides.setdefault("record_id", "md5(random()::text) || md5(random()::text)")
    selected = ", ".join(overrides.get(c, c) for c in columns)
    return (
        f"insert into growthpulse.ad_performance_records ({', '.join(columns)}) "  # noqa: S608
        f"select {selected} from growthpulse.ad_performance_records "
        f"where platform = 'google_ads' and account_id = 'synthetic-google-account-001' limit 1"
    )


def test_two_clients_store_side_by_side_with_derived_scope(two_clients):
    assert dict(
        rows("select client_id, count(*) from growthpulse.analytics_reports group by 1")
    ) == {
        LEGACY_CLIENT_ID: 1,
        IRELAND.client_id: 1,
    }
    assert (
        scalar(
            "select count(*) from growthpulse.findings f join growthpulse.ad_accounts a "
            "on a.ad_account_id = f.ad_account_id where a.client_id <> f.client_id"
        )
        == 0
    )
    assert (
        scalar(
            "select count(*) from growthpulse.ad_performance_records r "
            "join growthpulse.ad_accounts a "
            "on a.ad_account_id = r.ad_account_id where a.client_id = %s",
            (IRELAND.client_id,),
        )
        == 150
    )


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        # A record filed under another client's account than the one it names.
        (
            lambda: copy_record(ad_account_id=f"'{ireland_account()}'::uuid"),
            pg_errors.ForeignKeyViolation,
        ),
        # A record written into another client's account by the legacy client's run.
        (
            lambda: copy_record(
                account_id="'synthetic-google-account-002'",
                currency="'EUR'",
                account_timezone="'Europe/Dublin'",
                ad_account_id=f"'{ireland_account()}'::uuid",
            ),
            pg_errors.ForeignKeyViolation,
        ),
        # A finding of one client pointing at another client's account.
        (
            lambda: (
                "update growthpulse.findings set client_id = "  # noqa: S608
                f"'{IRELAND.client_id}' where client_id = '{LEGACY_CLIENT_ID}'"
            ),
            pg_errors.ForeignKeyViolation,
        ),
        # A finding moved onto another client's report.
        (
            lambda: (
                "update growthpulse.findings set latest_report_id = "  # noqa: S608
                f"'{report_of(IRELAND.client_id)}' where client_id = '{LEGACY_CLIENT_ID}'"
            ),
            pg_errors.ForeignKeyViolation,
        ),
        # A run claiming another client's report.
        (
            lambda: (
                "update growthpulse.pipeline_runs set report_id = "  # noqa: S608
                f"'{report_of(IRELAND.client_id)}' where client_id = '{LEGACY_CLIENT_ID}'"
            ),
            pg_errors.ForeignKeyViolation,
        ),
        # A report re-labelled with another client.
        (
            lambda: (
                "update growthpulse.analytics_reports set client_id = "  # noqa: S608
                f"'{IRELAND.client_id}' where client_id = '{LEGACY_CLIENT_ID}'"
            ),
            pg_errors.InsufficientPrivilege,
        ),  # the append-only trigger refuses any update
        # The same platform account added to a second client.
        (
            lambda: (
                "insert into growthpulse.ad_accounts (client_id, data_source, platform, "  # noqa: S608
                "external_account_id, currency, timezone) values "
                f"('{IRELAND.client_id}', 'synthetic', 'google_ads', "
                "'synthetic-google-account-001', 'GBP', 'Europe/London')"
            ),
            pg_errors.UniqueViolation,
        ),
        # A synthetic account under a live client, and a live account with no connection.
        (
            lambda: (
                "insert into growthpulse.clients (workspace_id, name, data_source) values "  # noqa: S608
                f"('{LEGACY_WORKSPACE_ID}', 'Live client', 'live'); "
                "insert into growthpulse.ad_accounts (client_id, data_source, platform, "
                "external_account_id, currency, timezone) select client_id, 'synthetic', "
                "'meta_ads', 'synthetic-x', 'GBP', 'UTC' from growthpulse.clients "
                "where name = 'Live client'"
            ),
            pg_errors.ForeignKeyViolation,
        ),
        (
            lambda: (
                "insert into growthpulse.ad_accounts (client_id, data_source, platform, "  # noqa: S608
                "external_account_id, currency, timezone) values "
                f"('{LEGACY_CLIENT_ID}', 'live', 'meta_ads', 'act_1', 'GBP', 'UTC')"
            ),
            pg_errors.CheckViolation,
        ),  # a live account needs a connection
    ],
    ids=[
        "record-account-identity",
        "record-written-by-other-client-run",
        "finding-client",
        "finding-report",
        "run-report",
        "report-client",
        "duplicate-account",
        "synthetic-account-live-client",
        "live-account-synthetic-client",
    ],
)
def test_cross_client_references_are_refused(two_clients, statement, error):
    assert failing(statement()) is error


def test_an_account_sync_run_may_only_write_its_own_account(upgraded):
    run_id = uuid4()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, run_kind, "
            "client_id, ad_account_id, window_start, window_end) values "
            "(%s, 'synthetic', 'test', 'account_sync', %s, %s, '2026-10-01', '2026-10-30')",
            (run_id, LEGACY_CLIENT_ID, LEGACY_META_ACCOUNT_ID),
        )
    assert failing(
        copy_record(last_changed_run_id=f"'{run_id}'::uuid", first_seen_run_id=f"'{run_id}'::uuid")
    ) is (pg_errors.ForeignKeyViolation)


@pytest.mark.parametrize(
    ("columns", "values", "error"),
    [
        ("run_kind", "'account_sync'", pg_errors.CheckViolation),
        (
            "run_kind, ad_account_id",
            f"'client_analysis', '{LEGACY_META_ACCOUNT_ID}'",
            pg_errors.CheckViolation,
        ),
        (
            "run_kind, client_id, ad_account_id, window_start, window_end",
            f"'account_sync', '{LEGACY_CLIENT_ID}', '{CLIENTS[1].accounts[0].ad_account_id}', "
            "'2026-10-01', '2026-10-02'",
            pg_errors.ForeignKeyViolation,
        ),
        (
            "run_kind, ad_account_id",
            f"'account_sync', '{LEGACY_META_ACCOUNT_ID}'",
            pg_errors.CheckViolation,
        ),
        ("data_source", "'live'", pg_errors.ForeignKeyViolation),
    ],
    ids=[
        "sync-without-account",
        "analysis-with-account",
        "sync-other-client-account",
        "sync-without-window",
        "live-run-in-synthetic-client",
    ],
)
def test_run_scope_rules(two_clients, columns, values, error):
    statement = (
        f"insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, {columns}) "  # noqa: S608
        f"values (gen_random_uuid(), 'synthetic', 'test', {values})"
    )
    if columns == "data_source":
        statement = (
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by) "
            "values (gen_random_uuid(), 'live', 'test')"
        )
    assert failing(statement) is error


def test_sync_lease_must_belong_to_the_same_account(upgraded):
    run_id = uuid4()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, run_kind, "
            "ad_account_id, window_start, window_end) values "
            "(%s, 'synthetic', 'test', 'account_sync', %s, '2026-10-01', '2026-10-30')",
            (run_id, LEGACY_META_ACCOUNT_ID),
        )
    assert (
        failing(
            "update growthpulse.account_sync_state set status = 'running', lease_run_id = %s, "
            "lease_expires_at = now() + interval '30 minutes' where ad_account_id = %s",
            (run_id, LEGACY_GOOGLE_ACCOUNT_ID),
        )
        is pg_errors.ForeignKeyViolation
    )
    assert (
        failing(
            "update growthpulse.account_sync_state set status = 'running' where ad_account_id = %s",
            (LEGACY_META_ACCOUNT_ID,),
        )
        is pg_errors.CheckViolation
    )


# 4. Account breakdowns


def legacy_breakdown(**changes) -> dict:
    records = SyntheticConnector().fetch(START, END).records
    breakdown = build_account_breakdown(records, report_id=M4_REPORT_ID)
    return {**breakdown, **changes}


def insert_breakdown(breakdown: dict, client_id: UUID = LEGACY_CLIENT_ID, role=WRITER) -> None:
    with as_role(role) as connection:
        connection.execute(
            "insert into growthpulse.analytics_report_breakdowns (report_id, client_id, "
            "breakdown_id, breakdown_schema_version, breakdown) values (%s, %s, %s, %s, %s)",
            (
                breakdown["report_id"],
                client_id,
                breakdown["breakdown_id"],
                breakdown["breakdown_schema_version"],
                Jsonb(breakdown),
            ),
        )


def test_breakdown_is_stored_beside_the_unchanged_report(upgraded):
    before = scalar("select report::text from growthpulse.analytics_reports")
    insert_breakdown(legacy_breakdown())
    assert scalar("select report::text from growthpulse.analytics_reports") == before
    with as_role(DASHBOARD) as connection:
        stored = connection.execute(
            "select client_id, breakdown from growthpulse_read.report_breakdowns"
        ).fetchall()
    assert stored == [(LEGACY_CLIENT_ID, legacy_breakdown())]


def test_breakdowns_cannot_be_duplicated_mislinked_or_rewritten(two_clients):
    # Linked to another client's report, or labelled with another client.
    with pytest.raises(pg_errors.ForeignKeyViolation):
        insert_breakdown(
            legacy_breakdown(report_id=report_of(IRELAND.client_id)), client_id=LEGACY_CLIENT_ID
        )
    with pytest.raises(pg_errors.ForeignKeyViolation):
        insert_breakdown(legacy_breakdown(), client_id=IRELAND.client_id)
    # A stored ID that differs from the body's.
    body = legacy_breakdown()
    assert (
        failing(
            "insert into growthpulse.analytics_report_breakdowns (report_id, client_id, "
            "breakdown_id, breakdown_schema_version, breakdown) values (%s, %s, %s, %s, %s)",
            (M4_REPORT_ID, LEGACY_CLIENT_ID, "0" * 64, "1.0", Jsonb(body)),
        )
        is pg_errors.CheckViolation
    )
    # One per report, and never rewritten.
    insert_breakdown(body)
    with pytest.raises(pg_errors.UniqueViolation):
        insert_breakdown(legacy_breakdown(breakdown_id="f" * 64))
    assert (
        failing("update growthpulse.analytics_report_breakdowns set created_at = now()")
        is pg_errors.InsufficientPrivilege
    )  # the append-only trigger, even for a superuser
    assert failing("delete from growthpulse.analytics_report_breakdowns", role=WRITER) is (
        pg_errors.InsufficientPrivilege
    )


# 5. Identity and lifecycle rules


def test_account_identity_and_client_never_change(upgraded):
    for change in (
        f"client_id = '{uuid4()}'",
        "external_account_id = 'synthetic-google-account-777'",
        "platform = 'meta_ads'",
        "data_source = 'live'",
    ):
        assert failing(
            f"update growthpulse.ad_accounts set {change} where ad_account_id = %s",  # noqa: S608
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        ) in (pg_errors.CheckViolation, pg_errors.RaiseException)


def all_pairs(enum) -> list[tuple]:
    return [(a, b) for a in enum for b in enum if a is not b]


@pytest.mark.parametrize(("current", "new"), all_pairs(AccountStatus))
def test_database_account_lifecycle_matches_the_python_model(upgraded, current, new):
    with admin() as connection:
        connection.execute(
            "alter table growthpulse.ad_accounts disable trigger ad_accounts_check_update"
        )
        connection.execute(
            "update growthpulse.ad_accounts set status = %s, archived_at = case when %s = "
            "'archived' then now() end where ad_account_id = %s",
            (current.value, current.value, LEGACY_GOOGLE_ACCOUNT_ID),
        )
        connection.execute(
            "alter table growthpulse.ad_accounts enable trigger ad_accounts_check_update"
        )
    statement = (
        "update growthpulse.ad_accounts set status = %s, archived_at = case when %s = "
        "'archived' then now() end where ad_account_id = %s"
    )
    params = (new.value, new.value, LEGACY_GOOGLE_ACCOUNT_ID)
    if new in ACCOUNT_TRANSITIONS[current]:
        with admin() as connection:
            connection.execute(statement, params)
    else:
        assert failing(statement, params) is pg_errors.CheckViolation


@pytest.mark.parametrize(("current", "new"), all_pairs(ConnectionStatus))
def test_database_connection_lifecycle_matches_the_python_model(upgraded, current, new):
    connection_id = uuid4()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.provider_connections (connection_id, workspace_id, provider, "
            "provider_subject_id, status, revoked_at) values (%s, %s, 'meta_ads', 'subject', %s, "
            "case when %s = 'revoked' then now() end)",
            (connection_id, LEGACY_WORKSPACE_ID, current.value, current.value),
        )
    statement = (
        "update growthpulse.provider_connections set status = %s, revoked_at = case when %s = "
        "'revoked' then now() end where connection_id = %s"
    )
    params = (new.value, new.value, connection_id)
    if new in CONNECTION_TRANSITIONS[current]:
        with admin() as connection:
            connection.execute(statement, params)
    else:
        assert failing(statement, params) is pg_errors.CheckViolation


def test_archived_clients_stay_archived(upgraded):
    call(MANAGER, "growthpulse.archive_client", LEGACY_CLIENT_ID)
    assert (
        failing("update growthpulse.clients set status = 'active', archived_at = null")
        is pg_errors.CheckViolation
    )


# 6. Role boundaries


TABLES = [
    "workspaces",
    "clients",
    "provider_connections",
    "discovered_ad_accounts",
    "ad_accounts",
    "account_sync_config",
    "account_sync_state",
    "owner_action_log",
    "pipeline_runs",
    "ad_performance_records",
    "quarantined_records",
    "analytics_reports",
    "findings",
    "experiment_proposals",
    "experiment_status_events",
    "analytics_report_breakdowns",
]


def privileges(role: str) -> dict[str, set[str]]:
    granted: dict[str, set[str]] = {}
    for schema, table, privilege in rows(
        """
        select n.nspname, c.relname, p.privilege
        from pg_class c join pg_namespace n on n.oid = c.relnamespace,
            unnest(array['select', 'insert', 'update', 'delete', 'truncate', 'references',
                         'trigger']) as p (privilege)
        where n.nspname = any(%s) and c.relkind in ('r', 'v')
            and (has_table_privilege(%s, c.oid, p.privilege)
                 or (p.privilege in ('select', 'insert', 'update')
                     and has_any_column_privilege(%s, c.oid, p.privilege)))
        """,
        (list(SCHEMAS), role, role),
    ):
        granted.setdefault(f"{schema}.{table}", set()).add(privilege)
    return granted


def executable_functions(role: str) -> set[str]:
    return {
        f"{schema}.{signature}"
        for schema, signature in rows(
            """
            select n.nspname, p.proname || '(' || oidvectortypes(p.proargtypes) || ')'
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = any(%s) and has_function_privilege(%s, p.oid, 'execute')
            """,
            (list(SCHEMAS), role),
        )
    }


def test_manager_can_execute_exactly_its_functions_and_read_nothing(upgraded):
    assert privileges(MANAGER) == {}
    expected = {f"growthpulse.{f}" for f in MANAGER_FUNCTIONS}
    assert executable_functions(MANAGER) == expected


def test_token_manager_can_execute_exactly_its_functions_and_read_nothing(upgraded):
    assert privileges(TOKEN_MANAGER) == {}
    assert executable_functions(TOKEN_MANAGER) == {
        f"growthpulse_secrets.{f}" for f in TOKEN_FUNCTIONS
    }


@pytest.mark.parametrize("role", [WRITER, DASHBOARD])
def test_existing_roles_cannot_reach_tokens_or_owner_actions(upgraded, role):
    assert executable_functions(role) == set()
    assert not any(name.startswith("growthpulse_secrets.") for name in privileges(role))
    assert (
        scalar("select has_schema_privilege(%s, 'growthpulse_secrets', 'usage')", (role,)) is False
    )
    assert failing("select * from growthpulse_secrets.oauth_credentials", role=role) is (
        pg_errors.InsufficientPrivilege
    )
    assert (
        failing(
            "select growthpulse.create_client(%s, 'x', 'synthetic')",
            (LEGACY_WORKSPACE_ID,),
            role=role,
        )
        is pg_errors.InsufficientPrivilege
    )
    assert failing("select growthpulse_secrets.read_credentials(%s)", (uuid4(),), role=role) is (
        pg_errors.InsufficientPrivilege
    )


def test_writer_privileges_are_exactly_the_approved_set(upgraded):
    granted = privileges(WRITER)
    expected = {
        "growthpulse.pipeline_runs": {"select", "insert", "update"},
        "growthpulse.ad_performance_records": {"select", "insert", "update"},
        "growthpulse.quarantined_records": {"select", "insert", "update"},
        "growthpulse.analytics_reports": {"select", "insert"},
        "growthpulse.findings": {"select", "insert", "update"},
        "growthpulse.experiment_proposals": {"select", "insert"},
        "growthpulse.experiment_status_events": {"select", "insert"},
        "growthpulse.workspaces": {"select"},
        "growthpulse.clients": {"select"},
        "growthpulse.ad_accounts": {"select"},
        "growthpulse.provider_connections": {"select"},
        "growthpulse.account_sync_config": {"select"},
        "growthpulse.discovered_ad_accounts": {"select", "insert", "update"},
        "growthpulse.account_sync_state": {"select", "update"},
        "growthpulse.analytics_report_breakdowns": {"select", "insert"},
    }
    assert granted == expected
    updatable = {
        name
        for (name,) in rows(
            "select attname from pg_attribute "
            "where attrelid = 'growthpulse.pipeline_runs'::regclass and attnum > 0 "
            "and not attisdropped and has_column_privilege(%s, attrelid, attnum, 'update')",
            (WRITER,),
        )
    }
    assert updatable == {
        "finished_at",
        "status",
        "counts",
        "error_code",
        "report_id",
        "data_quality",
    }


def test_dashboard_reads_views_only(upgraded):
    granted = privileges(DASHBOARD)
    assert all(name.startswith("growthpulse_read.") for name in granted)
    assert all(values == {"select"} for values in granted.values())
    views = {
        name
        for (name,) in rows(
            "select table_name from information_schema.views "
            "where table_schema = 'growthpulse_read'"
        )
    }
    assert {name.split(".")[1] for name in granted} == views
    assert len(views) == 17


@pytest.mark.parametrize(
    "statement",
    [
        "insert into growthpulse.clients (workspace_id, name, data_source) "
        "values ('26f3f7ff-8015-413a-8029-06e04727974c', 'x', 'synthetic')",
        "update growthpulse.ad_accounts set status = 'paused'",
        "insert into growthpulse.account_sync_config (ad_account_id, backfill_start_date) "
        "values (gen_random_uuid(), '2026-01-01')",
        "update growthpulse.account_sync_config set enabled = false",
        "select * from growthpulse.owner_action_log",
        "insert into growthpulse.owner_action_log (action, target_type, target_id) "
        "values ('x', 'client', 'x')",
        "update growthpulse.provider_connections set status = 'revoked'",
        "update growthpulse.pipeline_runs set client_id = gen_random_uuid()",
        "update growthpulse.ad_performance_records set ad_account_id = gen_random_uuid()",
        "delete from growthpulse.analytics_report_breakdowns",
    ],
)
def test_writer_cannot_perform_owner_actions_directly(upgraded, statement):
    assert failing(statement, role=WRITER) is pg_errors.InsufficientPrivilege


def test_writer_cannot_approve_experiments_but_the_owner_can(upgraded):
    with as_role(WRITER) as connection:
        proposal_id = record_experiment_proposal(
            connection,
            ExperimentProposal(
                data_source=DataSource.SYNTHETIC,
                client_id=LEGACY_CLIENT_ID,
                report_id=M4_REPORT_ID,
                title="Test",
                hypothesis="h",
                proposed_change="c",
                primary_metric="m",
                finding_id=scalar("select min(finding_id) from growthpulse.findings"),
            ),
        )
    assert scalar("select client_id from growthpulse.experiment_proposals") == LEGACY_CLIENT_ID
    assert (
        failing(
            "insert into growthpulse.experiment_status_events (proposal_id, status, actor) "
            "values (%s, 'approved', 'pipeline')",
            (proposal_id,),
            role=WRITER,
        )
        is pg_errors.InsufficientPrivilege
    )
    call(MANAGER, "growthpulse.decide_experiment", proposal_id, "approved", "Looks safe")
    assert rows(
        "select status, actor, sequence from growthpulse.experiment_status_events "
        "where proposal_id = %s order by sequence",
        (proposal_id,),
    ) == [("proposed", "pipeline", 1), ("approved", "owner", 2)]
    with pytest.raises(pg_errors.CheckViolation):
        call(MANAGER, "growthpulse.decide_experiment", proposal_id, "proposed", None)
    with pytest.raises(pg_errors.CheckViolation):
        call(MANAGER, "growthpulse.decide_experiment", proposal_id, "completed", None)


def test_writer_cannot_resume_or_pause_an_account(upgraded):
    call(MANAGER, "growthpulse.set_account_status", LEGACY_META_ACCOUNT_ID, "paused")
    with as_role(WRITER) as connection:
        cursor = connection.execute(
            "update growthpulse.account_sync_state set status = 'idle' where ad_account_id = %s",
            (LEGACY_META_ACCOUNT_ID,),
        )
        assert cursor.rowcount == 0  # RLS hides paused accounts from the pipeline's updates
    assert (
        failing(
            "update growthpulse.account_sync_state set status = 'paused' where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
            role=WRITER,
        )
        is pg_errors.InsufficientPrivilege
    )


def test_writer_can_update_sync_state_and_record_discoveries(upgraded):
    with as_role(WRITER) as connection:
        connection.execute(
            "update growthpulse.account_sync_state set last_attempt_at = now(), "
            "consecutive_failures = 1, status = 'retry_wait', last_error_code = 'rate_limited' "
            "where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        )
    assert (
        scalar(
            "select status from growthpulse.account_sync_state where ad_account_id = %s",
            (LEGACY_GOOGLE_ACCOUNT_ID,),
        )
        == "retry_wait"
    )


@pytest.mark.parametrize("role", [MANAGER, TOKEN_MANAGER])
@pytest.mark.parametrize(
    "statement",
    [
        "select * from growthpulse.clients",
        "select * from growthpulse.ad_performance_records",
        "select * from growthpulse.owner_action_log",
        "select * from growthpulse_secrets.oauth_credentials",
        "select * from growthpulse_read.clients",
        "insert into growthpulse.clients (workspace_id, name, data_source) "
        "values ('26f3f7ff-8015-413a-8029-06e04727974c', 'x', 'synthetic')",
        "select growthpulse.log_owner_action('x', 'client', 'x')",
    ],
)
def test_new_roles_cannot_touch_tables_directly(upgraded, role, statement):
    assert failing(statement, role=role) is pg_errors.InsufficientPrivilege


def test_manager_and_token_manager_cannot_use_each_others_functions(upgraded):
    assert (
        failing("select growthpulse_secrets.read_credentials(%s)", (uuid4(),), role=MANAGER)
        is pg_errors.InsufficientPrivilege
    )
    assert (
        failing(
            "select growthpulse.create_client(%s, 'x', 'synthetic')",
            (LEGACY_WORKSPACE_ID,),
            role=TOKEN_MANAGER,
        )
        is pg_errors.InsufficientPrivilege
    )


def test_roles_have_no_elevated_attributes_or_memberships(upgraded):
    flags = rows(
        "select rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication, "
        "rolinherit from pg_roles where rolname = any(%s) order by rolname",
        ([WRITER, DASHBOARD, MANAGER, TOKEN_MANAGER],),
    )
    assert len(flags) == 4 and all(not any(values) for _, *values in flags)
    assert (
        scalar(
            "select count(*) from pg_auth_members where member = any(%s::regrole[])",
            ([WRITER, DASHBOARD, MANAGER, TOKEN_MANAGER],),
        )
        == 0
    )


@pytest.mark.parametrize("role", [*API_ROLES, "public"])
def test_api_roles_and_public_reach_nothing(upgraded, role):
    if role == "public":
        granted = rows(
            """
            select n.nspname from pg_namespace n, aclexplode(n.nspacl) a
            where n.nspname = any(%(s)s) and a.grantee = 0
            union all
            select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace,
                aclexplode(c.relacl) a where n.nspname = any(%(s)s) and a.grantee = 0
            union all
            select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace,
                aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
            where n.nspname = any(%(s)s) and a.grantee = 0
            """,
            {"s": list(SCHEMAS)},
        )
        assert granted == []
        return
    for schema in SCHEMAS:
        assert scalar("select has_schema_privilege(%s, %s, 'usage')", (role, schema)) is False
    assert executable_functions(role) == set()
    assert (
        rows(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = any(%s) and c.relkind in ('r','v','S') and has_table_privilege(%s, "
            "c.oid, 'select, insert, update, delete, truncate, references, trigger')",
            (list(SCHEMAS), role),
        )
        == []
    )


def test_definer_functions_are_pinned_owned_and_granted_to_one_role(upgraded):
    functions = rows(
        """
        select n.nspname || '.' || p.proname
                || '(' || oidvectortypes(p.proargtypes) || ')',
            p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner),
            array(select pg_get_userbyid(a.grantee) from aclexplode(p.proacl) a
                  where a.grantee <> p.proowner order by 1)
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = any(%s)
        """,
        (list(SCHEMAS),),
    )
    definers = {name: grantees for name, definer, _, _, grantees in functions if definer}
    assert set(definers) == {f"growthpulse.{f}" for f in MANAGER_FUNCTIONS} | {
        f"growthpulse_secrets.{f}" for f in TOKEN_FUNCTIONS
    }
    for name, grantees in definers.items():
        assert grantees == ([TOKEN_MANAGER] if "secrets" in name else [MANAGER]), name
    for name, _, config, function_owner, grantees in functions:
        assert config == ['search_path=""'], name
        assert function_owner == OWNER, name
        if name not in definers:
            assert grantees == [], name  # trigger and internal functions: nobody


# 7. Owner actions


def manager(function: str, *args):
    return call(MANAGER, f"growthpulse.{function}", *args)


def test_owner_actions_manage_clients_and_are_logged(upgraded):
    client_id = manager("create_client", LEGACY_WORKSPACE_ID, "  Example Live Brand  ", "live")
    assert scalar("select name from growthpulse.clients where client_id = %s", (client_id,)) == (
        "Example Live Brand"
    )
    with pytest.raises(pg_errors.UniqueViolation):
        manager("create_client", LEGACY_WORKSPACE_ID, "example live brand", "live")
    with pytest.raises(pg_errors.CheckViolation):
        manager("create_client", LEGACY_WORKSPACE_ID, "Bad", "production")
    manager("rename_client", client_id, "Renamed Brand")
    manager("archive_client", client_id)
    with pytest.raises(pg_errors.NoDataFound):
        manager("rename_client", client_id, "Again")
    assert [
        a for (a,) in rows("select action from growthpulse.owner_action_log order by action_id")
    ] == [
        "create_client",
        "rename_client",
        "archive_client",
    ]
    assert failing("update growthpulse.owner_action_log set action = 'x'") is (
        pg_errors.InsufficientPrivilege
    )


def discovered(connection_id: UUID, external_id: str, **flags) -> None:
    with as_role(WRITER) as connection:
        connection.execute(
            "insert into growthpulse.discovered_ad_accounts (connection_id, platform, "
            "external_account_id, display_name, currency, timezone, is_manager, accessible) "
            "values (%s, 'meta_ads', %s, %s, 'GBP', %s, %s, %s)",
            (
                connection_id,
                external_id,
                f"Account {external_id}",
                flags.get("timezone", "Europe/London"),
                flags.get("is_manager", False),
                flags.get("accessible", True),
            ),
        )


def fake_credential(connection_id: UUID, kind: str = "refresh", nonce: bytes | None = None):
    call(
        TOKEN_MANAGER,
        "growthpulse_secrets.store_credential",
        connection_id,
        kind,
        1,
        1,
        nonce or os.urandom(12),
        os.urandom(48),
        None,
    )


def test_one_connection_discovers_and_adds_several_accounts(upgraded):
    client_id = manager("create_client", LEGACY_WORKSPACE_ID, "Live Brand", "live")
    connection_id = manager("create_connection", LEGACY_WORKSPACE_ID, "meta_ads", "meta-user-1")
    for external_id in ("act_1", "act_2", "act_3"):
        discovered(connection_id, external_id)
    discovered(connection_id, "act_mgr", is_manager=True)
    discovered(connection_id, "act_gone", accessible=False)
    discovered(connection_id, "act_badtz", timezone="Mars/Olympus")

    with pytest.raises(pg_errors.CheckViolation, match="active"):
        manager("add_ad_account", client_id, connection_id, "act_1", date(2026, 8, 1))
    fake_credential(connection_id)  # storing a credential activates the connection
    added = [
        manager("add_ad_account", client_id, connection_id, a, date(2026, 8, 1))
        for a in ("act_1", "act_2", "act_3")
    ]
    assert len(set(added)) == 3
    assert rows(
        "select a.status, a.sync_connection_id, c.frequency, s.status "
        "from growthpulse.ad_accounts a "
        "join growthpulse.account_sync_config c using (ad_account_id) "
        "join growthpulse.account_sync_state s using (ad_account_id) where a.client_id = %s "
        "group by 1, 2, 3, 4",
        (client_id,),
    ) == [("onboarding", connection_id, "daily", "idle")]

    for external_id, error in [
        ("act_mgr", pg_errors.CheckViolation),
        ("act_gone", pg_errors.CheckViolation),
        ("act_badtz", pg_errors.CheckViolation),
        ("act_unknown", pg_errors.NoDataFound),
        ("act_1", pg_errors.UniqueViolation),
    ]:
        with pytest.raises(error):
            manager("add_ad_account", client_id, connection_id, external_id, date(2026, 8, 1))
    with pytest.raises(pg_errors.CheckViolation, match="live client"):
        manager("add_ad_account", LEGACY_CLIENT_ID, connection_id, "act_1", date(2026, 8, 1))


def test_revoking_a_connection_deletes_credentials_and_flags_accounts(upgraded):
    client_id = manager("create_client", LEGACY_WORKSPACE_ID, "Live Brand", "live")
    connection_id = manager("create_connection", LEGACY_WORKSPACE_ID, "meta_ads", "meta-user-1")
    discovered(connection_id, "act_1")
    fake_credential(connection_id)
    account_id = manager("add_ad_account", client_id, connection_id, "act_1", date(2026, 8, 1))
    manager("revoke_connection", connection_id)
    assert scalar("select count(*) from growthpulse_secrets.oauth_credentials") == 0
    assert (
        scalar("select status from growthpulse.ad_accounts where ad_account_id = %s", (account_id,))
        == "needs_attention"
    )
    with pytest.raises(pg_errors.CheckViolation, match="revoked"):
        fake_credential(connection_id)


def test_synthetic_accounts_and_account_status_actions(upgraded):
    client_id = manager(
        "create_client", LEGACY_WORKSPACE_ID, "Synthetic Two (fictional)", "synthetic"
    )
    with pytest.raises(pg_errors.CheckViolation, match="synthetic-"):
        manager(
            "add_synthetic_ad_account",
            client_id,
            "meta_ads",
            "act_9",
            "GBP",
            "Europe/London",
            date(2026, 10, 1),
        )
    account_id = manager(
        "add_synthetic_ad_account",
        client_id,
        "meta_ads",
        "synthetic-meta-account-900",
        "EUR",
        "Europe/Paris",
        date(2026, 10, 1),
    )
    manager("set_account_status", account_id, "active")
    manager("set_account_status", account_id, "paused")
    assert (
        scalar(
            "select status from growthpulse.account_sync_state where ad_account_id = %s",
            (account_id,),
        )
        == "paused"
    )
    with pytest.raises(pg_errors.CheckViolation):
        manager("request_sync", account_id)
    manager("set_account_status", account_id, "active")
    manager("request_sync", account_id)
    manager("set_sync_config", account_id, True, "daily", 5, 14, 3)
    with pytest.raises(pg_errors.CheckViolation):
        manager("set_sync_config", account_id, True, "hourly", 5, 14, 3)
    with pytest.raises(pg_errors.CheckViolation):
        manager("set_account_status", account_id, "needs_attention")
    manager("set_account_status", account_id, "archived")
    with pytest.raises(pg_errors.CheckViolation):
        manager("set_account_status", account_id, "active")
    with pytest.raises(pg_errors.CheckViolation):
        manager("set_sync_config", account_id, True, "daily", 5, 14, 3)
    assert (
        scalar(
            "select enabled from growthpulse.account_sync_config where ad_account_id = %s",
            (account_id,),
        )
        is False
    )


def test_status_changes_wait_for_a_running_sync(upgraded):
    run_id = uuid4()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, run_kind, "
            "ad_account_id, window_start, window_end) values "
            "(%s, 'synthetic', 'test', 'account_sync', %s, '2026-10-01', '2026-10-30')",
            (run_id, LEGACY_META_ACCOUNT_ID),
        )
        connection.execute(
            "update growthpulse.account_sync_state set status = 'running', lease_run_id = %s, "
            "lease_expires_at = now() + interval '30 minutes' where ad_account_id = %s",
            (run_id, LEGACY_META_ACCOUNT_ID),
        )
    with pytest.raises(pg_errors.LockNotAvailable):
        manager("set_account_status", LEGACY_META_ACCOUNT_ID, "paused")


# 8. Token storage


def test_token_manager_stores_reads_rotates_and_deletes_ciphertext_only(upgraded):
    connection_id = manager("create_connection", LEGACY_WORKSPACE_ID, "google_ads", "google-1")
    with as_role(TOKEN_MANAGER) as connection:
        assert (
            connection.execute(
                "select * from growthpulse_secrets.read_credentials(%s)", (connection_id,)
            ).fetchall()
            == []
        )  # pending: nothing
    nonce, ciphertext = os.urandom(12), os.urandom(64)
    call(
        TOKEN_MANAGER,
        "growthpulse_secrets.store_credential",
        connection_id,
        "refresh",
        1,
        1,
        nonce,
        ciphertext,
        None,
    )
    assert (
        scalar(
            "select status from growthpulse.provider_connections where connection_id = %s",
            (connection_id,),
        )
        == "active"
    )
    with as_role(TOKEN_MANAGER) as connection:
        stored = connection.execute(
            "select token_type, key_version, nonce, ciphertext from "
            "growthpulse_secrets.read_credentials(%s)",
            (connection_id,),
        ).fetchall()
        assert stored == [("refresh", 1, nonce, ciphertext)]
        assert connection.execute(
            "select * from growthpulse_secrets.credentials_needing_rotation(2::smallint)"
        ).fetchall() == [(connection_id, "refresh", 1)]
    with pytest.raises(pg_errors.UniqueViolation):
        fake_credential(connection_id, "long_lived_access", nonce=nonce)  # same key, same nonce
    with pytest.raises(pg_errors.CheckViolation):
        call(
            TOKEN_MANAGER,
            "growthpulse_secrets.store_credential",
            connection_id,
            "refresh",
            1,
            1,
            os.urandom(11),
            ciphertext,
            None,
        )
    assert call(TOKEN_MANAGER, "growthpulse_secrets.mark_connection_needs_reauth", connection_id)
    with as_role(TOKEN_MANAGER) as connection:
        assert (
            connection.execute(
                "select * from growthpulse_secrets.read_credentials(%s)", (connection_id,)
            ).fetchall()
            == []
        )
    assert call(TOKEN_MANAGER, "growthpulse_secrets.delete_credentials", connection_id) == 1


def test_dashboard_sees_credential_status_but_no_secret_material(upgraded):
    connection_id = manager("create_connection", LEGACY_WORKSPACE_ID, "google_ads", "google-1")
    fake_credential(connection_id)
    with as_role(DASHBOARD) as connection:
        cursor = connection.execute(
            "select * from growthpulse_read.provider_connections where connection_id = %s",
            (connection_id,),
        )
        columns = [c.name for c in cursor.description]
        row = dict(zip(columns, cursor.fetchone(), strict=True))
    assert row["stored_credentials"] == 1 and row["status"] == "active"
    assert not {"nonce", "ciphertext", "key_version", "provider_subject_id"} & set(columns)


# 9. Dashboard views


def test_existing_views_keep_their_columns_and_append_scope():
    milestone_4_state()

    def view_columns():
        return {
            view: [
                c
                for (c,) in rows(
                    "select column_name from information_schema.columns where table_schema = "
                    "'growthpulse_read' and table_name = %s order by ordinal_position",
                    (view,),
                )
            ]
            for (view,) in rows(
                "select table_name from information_schema.views "
                "where table_schema = 'growthpulse_read'"
            )
        }

    before = view_columns()
    upgrade()
    after = view_columns()
    for view, columns in before.items():
        assert after[view][: len(columns)] == columns, view
    assert set(after) - set(before) == {
        "latest_client_reports",
        "report_breakdowns",
        "clients",
        "ad_accounts",
        "provider_connections",
        "discovered_ad_accounts",
        "account_sync_status",
        "owner_actions",
    }


def test_dashboard_reads_each_clients_latest_report_separately(two_clients):
    with as_role(DASHBOARD) as connection:
        latest = dict(
            connection.execute(
                "select client_id, report_id from growthpulse_read.latest_client_reports"
            ).fetchall()
        )
        delivery = connection.execute(
            "select client_id, count(*) from growthpulse_read.campaign_daily_delivery group by 1"
        ).fetchall()
        sync = connection.execute(
            "select count(*) from growthpulse_read.account_sync_status where client_id = %s",
            (LEGACY_CLIENT_ID,),
        ).fetchone()[0]
    assert latest[LEGACY_CLIENT_ID] == M4_REPORT_ID
    assert latest[IRELAND.client_id] not in (None, M4_REPORT_ID)
    assert dict(delivery) == {LEGACY_CLIENT_ID: 300, IRELAND.client_id: 150}
    assert sync == 2


def test_each_clients_stored_records_rebuild_its_own_report(two_clients):
    for client in (LEGACY_SYNTHETIC_CLIENT, IRELAND):
        with as_role(WRITER) as connection:
            loaded = load_records(
                connection,
                data_source=DataSource.SYNTHETIC,
                accounts=client.accounts,
                start=START,
                end=END,
            )
        _, summaries = sync_client(client)
        analysis = analyse_client(client, loaded, summaries)
        assert analysis.report["report_id"] == report_of(client.client_id)


# 10. Reverting Milestone 5


def schema_snapshot() -> dict[str, list]:
    schemas = list(SCHEMAS)
    queries = {
        "columns": "select table_schema, table_name, column_name, data_type, is_nullable, "
        "column_default from information_schema.columns where table_schema = any(%s) "
        "order by 1, 2, column_name",
        "constraints": "select conrelid::regclass::text, conname, pg_get_constraintdef(c.oid) "
        "from pg_constraint c join pg_namespace n on n.oid = c.connamespace "
        "where n.nspname = any(%s) order by 1, 2",
        "indexes": "select indexname, indexdef from pg_indexes "
        "where schemaname = any(%s) order by 1",
        "triggers": "select tgrelid::regclass::text, tgname, pg_get_triggerdef(t.oid) "
        "from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n "
        "on n.oid = c.relnamespace where n.nspname = any(%s) and not t.tgisinternal order by 1, 2",
        "policies": "select tablename, policyname, cmd, roles::text, qual, with_check "
        "from pg_policies where schemaname = any(%s) order by 1, 2",
        "views": "select viewname, definition from pg_views where schemaname = any(%s) order by 1",
        "functions": "select p.proname, pg_get_functiondef(p.oid) from pg_proc p join "
        "pg_namespace n on n.oid = p.pronamespace where n.nspname = any(%s) order by 1",
        "relation_grants": "select c.relname, a.grantee::regrole::text, a.privilege_type "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "where n.nspname = any(%s) and a.grantee <> 0 order by 1, 2, 3",
        "column_grants": "select c.relname, t.attname, a.grantee::regrole::text, a.privilege_type "
        "from pg_attribute t join pg_class c on c.oid = t.attrelid join pg_namespace n "
        "on n.oid = c.relnamespace, aclexplode(t.attacl) a where n.nspname = any(%s) "
        "and t.attnum > 0 and not t.attisdropped "
        "order by 1, 2, 3, 4",
        "schemas": "select nspname from pg_namespace where nspname = any(%s) order by 1",
    }
    snapshot = {name: rows(query, (schemas,)) for name, query in queries.items()}
    snapshot["roles"] = rows(
        "select rolname from pg_roles where rolname like 'growthpulse%%' "
        "and rolname <> %s order by 1",
        (OWNER,),
    )
    return snapshot


def test_reverting_milestone_5_restores_the_exact_milestone_4_schema_and_data():
    milestone_4_state()
    columns = m4_columns()
    data_before = data_snapshot(columns)
    schema_before = schema_snapshot()
    upgrade()
    run_m4_pipeline()  # a run after the upgrade, which the revert must keep
    data_after_run = data_snapshot(columns)
    run_sql_file(REVERT_M5)
    assert schema_snapshot() == schema_before
    assert data_snapshot(columns) == data_after_run
    assert data_after_run["analytics_reports"] == data_before["analytics_reports"]
    # The upgrade can be applied again afterwards.
    upgrade()
    assert (
        scalar(
            "select count(*) from growthpulse.ad_performance_records "
            "where ad_account_id is not null"
        )
        == 300
    )


# 11. The read-only verification script for Supabase


M5_CHECKS = MIGRATIONS[0].parents[1] / "verification" / "milestone_5_checks.sql"


def run_m5_verification() -> list[tuple]:
    with admin() as connection:
        connection.execute("create schema if not exists supabase_migrations")
        connection.execute(
            "create table if not exists supabase_migrations.schema_migrations "
            "(version text primary key, statements text[], name text)"
        )
        connection.execute("truncate supabase_migrations.schema_migrations")
        for path in MIGRATIONS:
            version, name = path.stem.split("_", 1)
            connection.execute(
                "insert into supabase_migrations.schema_migrations (version, name) values (%s, %s)",
                (version, name),
            )
    results: list[tuple] = []
    with admin() as connection:
        for statement in M5_CHECKS.read_text().split(";\n"):
            if statement.strip():
                cursor = connection.execute(statement)
                if cursor.description:
                    results.extend(cursor.fetchall())
        assert connection.info.transaction_status.name == "IDLE"  # ended with ROLLBACK
    return results


def upgraded_as_on_supabase() -> None:
    """Writer and dashboard logins enabled (as in Milestone 4); the new roles left without."""
    milestone_4_state()
    apply(M5_MIGRATIONS)


def test_milestone_5_checks_pass_right_after_the_upgrade():
    upgraded_as_on_supabase()
    results = run_m5_verification()
    assert {row[0] for row in results} == {
        "01 migration history", "02 schemas", "03 objects", "04 ownership",
        "05 row level security", "06 functions", "07 roles", "08 grants", "09 api roles",
        "10 data api exposure", "11 backfill",
    }  # fmt: skip
    for check, item, expected, actual, ok in results:
        if check in {"02 schemas", "04 ownership"}:
            assert OWNER in actual, (check, item)  # the test owner stands in for postgres
        else:
            assert ok is True, (check, item, expected, actual)


def test_milestone_5_checks_detect_drift():
    upgraded_as_on_supabase()
    with owner() as connection:
        connection.execute("grant usage on schema growthpulse_secrets to growthpulse_writer")
        connection.execute("create table growthpulse_secrets.extra_table (id int)")
        connection.execute("alter role growthpulse_manager login")
    try:
        enable_test_logins()
        run_m4_pipeline()  # a third run: the backfill counts no longer match
        with owner() as connection:
            connection.execute("alter role growthpulse_manager nologin")
            connection.execute("alter role growthpulse_manager login")
        failing = {(c, item) for c, item, *_, ok in run_m5_verification() if ok is False}
    finally:
        with owner() as connection:
            connection.execute("drop table growthpulse_secrets.extra_table")
            connection.execute("revoke usage on schema growthpulse_secrets from growthpulse_writer")
    assert ("08 grants", "grants that were not expected") in failing
    assert ("03 objects", "unexpected objects") in failing
    assert ("07 roles", MANAGER) in failing
    assert ("11 backfill", "pipeline runs (combined, legacy client)") in failing
    assert ("05 row level security", "growthpulse_secrets.extra_table") in failing


def test_live_rows_can_never_be_assigned_to_the_fictional_client(upgraded):
    """The temporary legacy default applies to synthetic runs only, and records never default."""
    with admin() as connection:
        live_client = connection.execute(
            "insert into growthpulse.clients (workspace_id, name, data_source) "
            "values (%s, 'Live brand', 'live') returning client_id",
            (LEGACY_WORKSPACE_ID,),
        ).fetchone()[0]
        live_run = connection.execute(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, client_id) "
            "values (gen_random_uuid(), 'live', 'test', %s) returning run_id",
            (live_client,),
        ).fetchone()[0]
    overrides = dict(
        data_source="'live'",
        first_seen_run_id=f"'{live_run}'::uuid",
        last_changed_run_id=f"'{live_run}'::uuid",
    )
    # Same platform account ID as a legacy record, but live: no account is derived.
    assert failing(copy_record(**overrides, ad_account_id="null")) is pg_errors.ForeignKeyViolation
    # Forcing the legacy account explicitly fails its identity foreign key.
    assert (
        failing(copy_record(**overrides, ad_account_id=f"'{LEGACY_GOOGLE_ACCOUNT_ID}'::uuid"))
        is pg_errors.ForeignKeyViolation
    )
    # A live run left without a client cannot default into the synthetic legacy client.
    assert (
        failing(
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by) "
            "values (gen_random_uuid(), 'live', 'test')"
        )
        is pg_errors.ForeignKeyViolation
    )
    assert scalar("select count(*) from growthpulse.ad_performance_records "
                  "where data_source = 'live'") == 0  # fmt: skip
