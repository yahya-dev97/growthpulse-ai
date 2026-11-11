from datetime import date
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors

from factories import make_row
from fake_db import FakeDatabase, scope_answers
from growthpulse_ai.accounts.legacy import LEGACY_CLIENT_ID, LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.analytics.errors import AnalyticsError
from growthpulse_ai.config import Settings
from growthpulse_ai.connectors.base import ConnectorError
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.persistence import store
from growthpulse_ai.persistence.errors import (
    DatabaseConfigurationError,
    DatabaseUnavailableError,
    RunStateError,
)
from growthpulse_ai.pipeline import persisted_run
from growthpulse_ai.pipeline.persisted_run import (
    NoValidRecordsError,
    PipelineRunError,
    classify_error,
    run_and_persist,
)

START = date(2026, 10, 1)
END = date(2026, 10, 30)
SECRET = "hunter2-not-a-real-password"


class BrokenConnector(SyntheticConnector):
    def _fetch_raw(self, start_date, end_date):
        raise RuntimeError(f"token={SECRET}")


def run(database: FakeDatabase, connector=None, **kwargs):
    scope_answers(database, LEGACY_CLIENT_ID)
    return run_and_persist(
        connector or SyntheticConnector(),
        database.connect,
        START,
        END,
        client=LEGACY_SYNTHETIC_CLIENT,
        triggered_by="test",
        **kwargs,
    )


class FlakyDatabase(FakeDatabase):
    """Connects a fixed number of times, then refuses every new connection."""

    def __init__(self, allowed: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.allowed = allowed

    def connect(self):
        if len(self.connections) >= self.allowed:
            raise DatabaseUnavailableError("Could not connect to the database (OperationalError)")
        return super().connect()


def test_successful_run_uses_two_committed_transactions():
    database = FakeDatabase()
    run_id = uuid4()
    outcome = run(database, run_id=run_id)
    assert outcome.run_id == run_id
    assert outcome.counts["records_received"] == 300
    assert len(database.committed) == 2
    assert database.statements[0].query == store.INSERT_RUN
    assert database.statements[-1].query == store.FINISH_RUN
    assert all(connection.closed for connection in database.connections)


def test_connector_failure_is_recorded_with_a_code_only():
    database = FakeDatabase()
    with pytest.raises(PipelineRunError) as caught:
        run(database, connector=BrokenConnector())
    assert caught.value.error_code == "connector_unavailable"
    assert caught.value.recorded is True
    failure = database.matching("set status = 'failed'")[0]
    assert failure.params["error_code"] == "connector_unavailable"
    assert SECRET not in str(caught.value)
    assert SECRET not in repr([s.params for s in database.statements])


def test_database_failure_rolls_back_data_and_records_failure_separately():
    error = psycopg.OperationalError(f"connection lost password={SECRET}")
    database = FakeDatabase(fail_on={"growthpulse.findings": error})
    with pytest.raises(PipelineRunError) as caught:
        run(database)
    assert caught.value.error_code == "database_unavailable"
    assert caught.value.recorded is True
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert SECRET not in str(caught.value)

    data_transaction = database.matching("ad_performance_records")[0].transaction
    failure = database.matching("set status = 'failed'")[0]
    assert data_transaction in database.rolled_back
    assert failure.transaction in database.committed
    assert failure.transaction != data_transaction
    assert len(database.connections) == 3


def test_failure_counts_describe_progress_without_values():
    database = FakeDatabase(fail_on={"growthpulse.findings": psycopg.OperationalError()})
    with pytest.raises(PipelineRunError):
        run(database)
    counts = database.matching("set status = 'failed'")[0].params["counts"].obj
    assert counts == {"rows_received": 300, "records_received": 300, "quarantined_rows": 0}


def test_failure_that_cannot_be_recorded_says_so():
    database = FlakyDatabase(
        allowed=2, fail_on={"growthpulse.findings": psycopg.OperationalError()}
    )
    with pytest.raises(PipelineRunError) as caught:
        run(database)
    assert caught.value.recorded is False
    assert "not recorded" in str(caught.value)


def test_run_that_cannot_start_writes_nothing():
    database = FakeDatabase(connect_error=DatabaseConfigurationError("DATABASE_URL is not set"))
    with pytest.raises(PipelineRunError) as caught:
        run(database)
    assert caught.value.error_code == "database_misconfigured"
    assert caught.value.recorded is False
    assert database.statements == []


def test_all_rows_quarantined_fails_the_run():
    database = FakeDatabase()
    with pytest.raises(PipelineRunError) as caught:
        run(database, connector=SyntheticConnector(rows=[make_row(spend="-1.00")]))
    assert caught.value.error_code == "no_valid_records"
    counts = database.matching("set status = 'failed'")[0].params["counts"].obj
    assert counts["quarantined_rows"] == 1
    assert database.matching("ad_performance_records") == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (DatabaseConfigurationError("x"), "database_misconfigured"),
        (DatabaseUnavailableError("x"), "database_unavailable"),
        (RunStateError("x"), "run_state_conflict"),
        (pg_errors.InsufficientPrivilege("x"), "database_permission_denied"),
        (psycopg.OperationalError("x"), "database_unavailable"),
        (psycopg.IntegrityError("x"), "database_rejected_data"),
        (psycopg.DataError("x"), "database_rejected_data"),
        (psycopg.ProgrammingError("x"), "database_error"),
        (ConnectorError("synthetic", "x"), "connector_unavailable"),
        (NoValidRecordsError(), "no_valid_records"),
        (AnalyticsError("x"), "analytics_error"),
        (KeyError("x"), "unexpected_error"),
    ],
)
def test_errors_map_to_fixed_codes(error: Exception, code: str):
    assert classify_error(error) == code
    assert store.ERROR_CODE_PATTERN.fullmatch(code)


def test_cli_without_database_url_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # Ignore any local .env, so this test can never reach a real database.
    monkeypatch.setattr(persisted_run, "get_settings", lambda: Settings(_env_file=None))
    assert persisted_run.main([]) == 1
    output = capsys.readouterr().out
    assert "database_misconfigured" in output
    assert "not recorded" in output


def test_code_version_comes_only_from_a_valid_commit_hash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_SHA", "FE070FA467670A51A0C43F6AE80339A22272A201")
    assert persisted_run._code_version() == "fe070fa467670a51a0c43f6ae80339a22272a201"
    monkeypatch.setenv("GITHUB_SHA", "not a sha")
    assert persisted_run._code_version() is None
