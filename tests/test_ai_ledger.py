"""The spend ledger: durable, budget-capped, concurrency-safe, and fail-closed."""

import json
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from growthpulse_ai.ai_analysis.ledger import (
    BUDGET_CEILING_USD,
    BudgetExceededError,
    LedgerError,
    LedgerReviewRequiredError,
    LedgerUnavailableError,
    SpendLedger,
)
from growthpulse_ai.ai_analysis.pricing import TokenUsage
from growthpulse_ai.config import PROJECT_ROOT, Settings

USAGE = TokenUsage(input_tokens=1000, output_tokens=500)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


def test_budget_ceiling_is_one_dollar_and_cannot_be_raised(path):
    assert Decimal("1.00") == BUDGET_CEILING_USD
    with pytest.raises(LedgerError):
        SpendLedger(path, Decimal("1.01"))
    with pytest.raises(ValueError):
        Settings(_env_file=None, ai_budget_usd="5.00")
    assert Settings(_env_file=None).ai_budget_usd == Decimal("1.00")


def test_an_empty_ledger_has_the_full_budget(path):
    summary = SpendLedger(path).summary()
    assert summary.committed_usd == 0 and summary.remaining_usd == Decimal("1.00")
    assert summary.live_requests == 0 and not summary.needs_review


def test_reservations_count_until_resolved(path):
    ledger = SpendLedger(path)
    ledger.reserve("a", "claude-opus-5-5", Decimal("0.40"))
    assert ledger.summary().open_usd == Decimal("0.40")
    ledger.settle("a", Decimal("0.10"), USAGE)
    summary = ledger.summary()
    assert summary.settled_usd == Decimal("0.10") and summary.open_usd == 0
    assert summary.remaining_usd == Decimal("0.90")


def test_released_requests_cost_nothing_and_held_ones_cost_their_reservation(path):
    ledger = SpendLedger(path)
    ledger.reserve("released", "claude-opus-5-5", Decimal("0.30"))
    ledger.release("released", "rate_limited")
    ledger.reserve("held", "claude-opus-5-5", Decimal("0.30"))
    ledger.hold("held", "timeout")
    summary = ledger.summary()
    assert summary.held_usd == Decimal("0.30")
    assert summary.committed_usd == Decimal("0.30")
    assert summary.request_counts == {"released": 1, "held": 1}


def test_the_ledger_persists_across_instances(path):
    SpendLedger(path).reserve("a", "claude-opus-5-5", Decimal("0.25"))
    SpendLedger(path).settle("a", Decimal("0.05"), USAGE)
    SpendLedger(path).reserve("crashed", "claude-opus-5-5", Decimal("0.40"))  # never resolved
    summary = SpendLedger(path).summary()
    assert summary.settled_usd == Decimal("0.05")
    assert summary.open_usd == Decimal("0.40")  # a crashed process stays counted
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["event"] for e in entries] == ["reserve", "settle", "reserve"]
    assert entries[1]["input_tokens"] == 1000 and entries[1]["cost_usd"] == "0.05"


def test_budget_exhaustion_refuses_without_recording(path):
    ledger = SpendLedger(path)
    ledger.reserve("a", "claude-opus-5-5", Decimal("0.60"))
    ledger.settle("a", Decimal("0.60"), USAGE)
    before = path.read_text()
    with pytest.raises(BudgetExceededError):
        ledger.reserve("b", "claude-opus-5-5", Decimal("0.41"))
    assert path.read_text() == before
    ledger.reserve("c", "claude-opus-5-5", Decimal("0.40"))  # exactly reaches the cap
    with pytest.raises(BudgetExceededError):
        ledger.reserve("d", "claude-opus-5-5", Decimal("0.000001"))


def test_a_lower_configured_budget_is_enforced(path):
    with pytest.raises(BudgetExceededError):
        SpendLedger(path, Decimal("0.10")).reserve("a", "claude-opus-5-5", Decimal("0.11"))


def test_a_cost_above_its_reservation_blocks_further_calls(path):
    ledger = SpendLedger(path)
    ledger.reserve("a", "claude-opus-5-5", Decimal("0.10"))
    record = ledger.settle("a", Decimal("0.12"), USAGE)
    assert record.exceeded_reservation
    assert ledger.summary().needs_review
    with pytest.raises(LedgerReviewRequiredError):
        ledger.reserve("b", "claude-opus-5-5", Decimal("0.01"))


@pytest.mark.parametrize(
    "content",
    [
        "not json\n",
        '{"v": 99, "event": "reserve", "request_id": "a"}\n',
        '{"v": 1, "event": "settle", "request_id": "a", "cost_usd": "0.1"}\n',
        '{"v": 1, "event": "reserve", "request_id": "a", "reserved_usd": "-1"}\n',
        '{"v": 1, "event": "reserve", "request_id": "a", "reserved_usd": "NaN"}\n',
        '{"v": 1, "event": "reserve", "request_id": "a", "reserved_usd": "0.1"}\n'
        '{"v": 1, "event": "reserve", "request_id": "a", "reserved_usd": "0.1"}\n',
        '{"v": 1, "event": "reserve", "request_id": "a", "reserved_usd": "0.1"}',  # truncated
        '{"v": 1, "event": "mystery", "request_id": "a"}\n',
    ],
)
def test_an_unreadable_ledger_blocks_every_reservation(path, content):
    path.write_text(content)
    ledger = SpendLedger(path)
    with pytest.raises(LedgerUnavailableError):
        ledger.reserve("new", "claude-opus-5-5", Decimal("0.01"))
    with pytest.raises(LedgerUnavailableError):
        ledger.summary()


def test_requests_resolve_exactly_once(path):
    ledger = SpendLedger(path)
    with pytest.raises(LedgerError):
        ledger.settle("never-reserved", Decimal("0.01"), USAGE)
    ledger.reserve("a", "claude-opus-5-5", Decimal("0.10"))
    ledger.release("a", "bad_request")
    with pytest.raises(LedgerError):
        ledger.hold("a", "timeout")
    with pytest.raises(LedgerError):
        ledger.reserve("a", "claude-opus-5-5", Decimal("0.10"))


def test_concurrent_threads_never_overspend(path):
    results: list[bool] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            SpendLedger(path).reserve(f"t{index}", "claude-opus-5-5", Decimal("0.30"))
            outcome = True
        except BudgetExceededError:
            outcome = False
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 3
    assert SpendLedger(path).summary().open_usd == Decimal("0.90")


def _reserve_in_process(args: tuple[str, int]) -> bool:
    ledger_path, index = args
    try:
        SpendLedger(Path(ledger_path)).reserve(f"p{index}", "claude-opus-5-5", Decimal("0.30"))
        return True
    except BudgetExceededError:
        return False


def test_concurrent_processes_never_overspend(path):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=6, mp_context=context) as pool:
        outcomes = list(pool.map(_reserve_in_process, [(str(path), i) for i in range(10)]))
    assert outcomes.count(True) == 3
    assert len(path.read_text().splitlines()) == 3


def test_the_default_ledger_path_is_gitignored():
    default = Settings(_env_file=None).ai_ledger_path
    assert default.relative_to(PROJECT_ROOT).parts[0] == "data"
    assert "/data/" in (PROJECT_ROOT / ".gitignore").read_text().splitlines()


def test_the_summary_never_claims_to_be_the_account_balance(path):
    view = SpendLedger(path).summary().as_dict()
    assert "not the Anthropic account balance" in view["note"]
    assert "balance" not in {key for key in view if key != "note"}
