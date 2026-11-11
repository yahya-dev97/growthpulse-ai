from uuid import uuid4

import psycopg
import pytest

from factories import make_row
from fake_db import FakeDatabase, scope_answers
from growthpulse_ai.accounts.legacy import LEGACY_CLIENT_ID, LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import DEFAULT_START_DATE
from growthpulse_ai.data_quality import validate_rows
from growthpulse_ai.persistence import store
from growthpulse_ai.persistence.errors import RunStateError
from growthpulse_ai.persistence.store import (
    ExperimentProposal,
    mark_run_failed,
    record_experiment_proposal,
    save_run_results,
    start_run,
)
from growthpulse_ai.pipeline.sample_report import build_sample_report
from growthpulse_ai.schema import DataSource

SYNTHETIC = DataSource.SYNTHETIC


@pytest.fixture(scope="module")
def run_inputs() -> dict:
    result = SyntheticConnector().fetch(DEFAULT_START_DATE, DEFAULT_START_DATE.replace(day=30))
    return {
        "records": result.records,
        "quarantined": validate_rows([make_row(spend="-1.00")]).quarantined,
        "report": build_sample_report(),
    }


def save(database: FakeDatabase, run_inputs: dict, run_id=None) -> dict[str, int]:
    scope_answers(database, LEGACY_CLIENT_ID)
    with database.connect() as connection:
        return save_run_results(
            connection,
            run_id or uuid4(),
            client=LEGACY_SYNTHETIC_CLIENT,
            data_source=SYNTHETIC,
            connector="synthetic",
            **run_inputs,
        )


# Starting a run


def test_start_run_commits_a_running_row_on_its_own():
    database = FakeDatabase()
    run_id = uuid4()
    start_run(
        database.connect(),
        run_id,
        data_source=SYNTHETIC,
        triggered_by="test",
        client_id=LEGACY_CLIENT_ID,
    )
    (statement,) = database.statements
    assert statement.query == store.INSERT_RUN
    assert statement.params["run_id"] == run_id
    assert database.committed == [statement.transaction]


def test_start_run_rejects_a_malformed_code_version():
    database = FakeDatabase()
    with pytest.raises(ValueError, match="code_version"):
        start_run(
            database.connect(),
            uuid4(),
            data_source=SYNTHETIC,
            triggered_by="test",
            client_id=LEGACY_CLIENT_ID,
            code_version="main; drop table",
        )
    assert database.statements == []


# Saving results


def test_results_are_written_in_one_committed_transaction(run_inputs: dict):
    database = FakeDatabase()
    save(database, run_inputs)
    transactions = {statement.transaction for statement in database.statements}
    assert len(transactions) == 1
    assert database.committed == list(transactions)
    assert database.rolled_back == []


def test_results_are_written_in_dependency_order(run_inputs: dict):
    database = FakeDatabase()
    save(database, run_inputs)
    assert [statement.query for statement in database.statements] == [
        store.UPSERT_RECORD,
        store.UPSERT_QUARANTINE,
        store.INSERT_REPORT,
        store.SELECT_REPORT_CLIENT,
        store.SELECT_FOREIGN_FINDINGS,
        store.UPSERT_FINDING,
        store.FINISH_RUN,
    ]


def test_counts_report_received_and_written_rows(run_inputs: dict):
    database = FakeDatabase(rowcounts={"ad_performance_records": 7, "analytics_reports": 0})
    counts = save(database, run_inputs)
    assert counts["records_received"] == len(run_inputs["records"])
    assert counts["records_written"] == 7
    assert counts["reports_written"] == 0
    assert counts["quarantined_rows"] == 1
    finish = database.matching("set status = 'succeeded'")[0]
    assert finish.params["counts"].obj == counts
    assert finish.params["report_id"] == run_inputs["report"]["report_id"]


def test_run_that_is_no_longer_running_rolls_everything_back(run_inputs: dict):
    database = FakeDatabase(rowcounts={"set status = 'succeeded'": 0})
    with pytest.raises(RunStateError):
        save(database, run_inputs)
    assert database.committed == []
    assert len(database.rolled_back) == 1


def test_database_error_mid_write_rolls_everything_back(run_inputs: dict):
    database = FakeDatabase(fail_on={"growthpulse.findings": psycopg.OperationalError("down")})
    with pytest.raises(psycopg.OperationalError):
        save(database, run_inputs)
    assert database.committed == []
    assert database.rolled_back == [0]
    assert database.matching("set status = 'succeeded'") == []


def save_as_live(run_inputs: dict, message: str) -> None:
    database = FakeDatabase()
    with database.connect() as connection, pytest.raises(ValueError, match=message):
        save_run_results(
            connection,
            uuid4(),
            client=LEGACY_SYNTHETIC_CLIENT,
            data_source=DataSource.LIVE,
            connector="live",
            **run_inputs,
        )
    assert database.statements == []


def test_report_from_another_data_source_is_rejected(run_inputs: dict):
    save_as_live({**run_inputs, "records": ()}, "The report's data source")


def test_records_from_another_data_source_are_rejected(run_inputs: dict):
    live_report = {**run_inputs["report"], "data_source": "live"}
    save_as_live({**run_inputs, "report": live_report}, "Every record must match")


def test_empty_quarantine_skips_its_statement(run_inputs: dict):
    database = FakeDatabase()
    counts = save(database, {**run_inputs, "quarantined": ()})
    assert database.matching("quarantined_records") == []
    assert counts["quarantine_entries_written"] == 0


# Marking a run failed


def test_failure_stores_only_a_code_and_counts():
    database = FakeDatabase()
    assert mark_run_failed(database.connect(), uuid4(), "database_unavailable", {"rows": 3})
    (statement,) = database.statements
    assert statement.params["error_code"] == "database_unavailable"
    assert statement.params["counts"].obj == {"rows": 3}
    assert database.committed == [statement.transaction]


@pytest.mark.parametrize(
    "message",
    [
        "connection to server at db.example failed",
        "postgresql://user:secret@host/db",
        "Database_Unavailable",
        "",
        "x" * 65,
    ],
)
def test_failure_rejects_anything_but_a_short_code(message: str):
    database = FakeDatabase()
    with pytest.raises(ValueError, match="error_code"):
        mark_run_failed(database.connect(), uuid4(), message)
    assert database.statements == []


def test_failure_reports_when_the_run_was_not_running():
    database = FakeDatabase(rowcounts={"set status = 'failed'": 0})
    assert mark_run_failed(database.connect(), uuid4(), "unexpected_error") is False


# Experiment proposals


def proposal(**overrides) -> ExperimentProposal:
    values = {
        "data_source": SYNTHETIC,
        "client_id": LEGACY_CLIENT_ID,
        "report_id": "a" * 64,
        "finding_id": "b" * 64,
        "title": "Test a tighter keyword set",
        "hypothesis": "Narrower matching lowers cost per platform-attributed conversion.",
        "proposed_change": "Run a split test. A person applies any change manually.",
        "primary_metric": "cost_per_platform_attributed_conversion",
    }
    return ExperimentProposal(**{**values, **overrides})


def test_proposal_needs_a_finding_or_group():
    with pytest.raises(ValueError, match="finding"):
        proposal(finding_id=None, group_id=None)


def test_proposal_key_is_stable_and_depends_on_the_subject():
    assert proposal().idempotency_key == proposal().idempotency_key
    assert proposal().idempotency_key != proposal(finding_id="c" * 64).idempotency_key


def test_new_proposal_starts_as_proposed_in_the_same_transaction():
    proposal_id = uuid4()
    database = FakeDatabase(
        results={"insert into growthpulse.experiment_proposals": (proposal_id,)}
    )
    assert record_experiment_proposal(database.connect(), proposal()) == proposal_id
    insert, status = database.statements
    assert status.query == store.INSERT_PROPOSED_STATUS
    assert status.params == {"proposal_id": proposal_id}
    assert insert.transaction == status.transaction
    assert database.committed == [insert.transaction]


def test_existing_proposal_is_returned_without_a_new_status():
    proposal_id = uuid4()
    database = FakeDatabase(results={"select proposal_id": (proposal_id,)})
    assert record_experiment_proposal(database.connect(), proposal()) == proposal_id
    assert database.matching("experiment_status_events") == []
