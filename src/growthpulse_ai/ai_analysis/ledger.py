"""A local, durable record of estimated Anthropic API spend, with a hard budget cap.

The ledger is an append-only JSON Lines file. Every live request follows one path:

1. **Reserve.** Before the call, its maximum possible cost is reserved. A reservation that
   would take committed spend past the budget is refused, so the call never happens.
2. **Resolve.** Afterwards the reservation ends in exactly one way:
   - *settle*: the response reported its usage, which is priced and recorded;
   - *release*: the API rejected the request before doing any work, so nothing was billed;
   - *hold*: the cost cannot be known (a timeout, a dropped connection, a server error, an
     unpriced model or price modifier). The full reservation stays counted as spent.

Committed spend is settled costs plus every reservation that is open or held. A crashed
process leaves its reservation open, so it stays counted: the ledger fails closed.

All reads and writes happen under an exclusive ``flock`` on the ledger file, so concurrent
processes and threads see each other's reservations. Any file the ledger cannot read
completely blocks every new reservation rather than being skipped.

This is GrowthPulse's own estimate at list prices. It is never the Anthropic account
balance, which only the Console shows.
"""

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

from ..config import AI_BUDGET_CEILING_USD as BUDGET_CEILING_USD
from .pricing import PRICING_VERSION, TokenUsage

LEDGER_VERSION = 1

EventKind = Literal["reserve", "settle", "release", "hold"]
RequestState = Literal["open", "settled", "released", "held"]


class LedgerError(RuntimeError):
    """The ledger cannot be used safely, so no live request may be made."""


class LedgerUnavailableError(LedgerError):
    """The ledger file cannot be locked, read or parsed."""


class BudgetExceededError(LedgerError):
    """Reserving this request would take committed spend past the budget."""


class LedgerReviewRequiredError(LedgerError):
    """An earlier request cost more than its reservation; a person must review it."""


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    model: str
    reserved_usd: Decimal
    state: RequestState
    cost_usd: Decimal | None = None
    exceeded_reservation: bool = False
    reason: str | None = None

    @property
    def committed_usd(self) -> Decimal:
        if self.state == "settled":
            return self.cost_usd or Decimal(0)
        if self.state == "released":
            return Decimal(0)
        return self.reserved_usd  # open or held: assume the worst


@dataclass(frozen=True)
class LedgerSummary:
    budget_usd: Decimal
    settled_usd: Decimal
    held_usd: Decimal
    open_usd: Decimal
    request_counts: dict[str, int]
    needs_review: bool

    @property
    def committed_usd(self) -> Decimal:
        return self.settled_usd + self.held_usd + self.open_usd

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.budget_usd - self.committed_usd, Decimal(0))

    @property
    def live_requests(self) -> int:
        return sum(self.request_counts.values())

    def as_dict(self) -> dict:
        return {
            "budget_usd": str(self.budget_usd),
            "settled_usd": str(self.settled_usd),
            "held_usd": str(self.held_usd),
            "open_reserved_usd": str(self.open_usd),
            "committed_usd": str(self.committed_usd),
            "remaining_usd": str(self.remaining_usd),
            "live_requests": self.live_requests,
            "requests_by_state": dict(sorted(self.request_counts.items())),
            "needs_review": self.needs_review,
            "pricing_version": PRICING_VERSION,
            "note": (
                "GrowthPulse's local estimate at list prices. It is not the Anthropic "
                "account balance."
            ),
        }


def _decimal(value: object, what: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise LedgerUnavailableError(f"ledger entry has an invalid {what}") from None
    if not result.is_finite() or result < 0:
        raise LedgerUnavailableError(f"ledger entry has an invalid {what}")
    return result


def replay(lines: list[str]) -> dict[str, RequestRecord]:
    """Rebuild each request's state from the event log, rejecting anything inconsistent."""
    records: dict[str, RequestRecord] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise LedgerUnavailableError(f"ledger line {number} is not valid JSON") from None
        if not isinstance(event, dict) or event.get("v") != LEDGER_VERSION:
            raise LedgerUnavailableError(f"ledger line {number} has an unknown format")
        kind, request_id = event.get("event"), event.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise LedgerUnavailableError(f"ledger line {number} has no request ID")
        current = records.get(request_id)
        if kind == "reserve":
            if current is not None:
                raise LedgerUnavailableError(f"ledger line {number} reserves a request twice")
            records[request_id] = RequestRecord(
                request_id=request_id,
                model=str(event.get("model")),
                reserved_usd=_decimal(event.get("reserved_usd"), "reservation"),
                state="open",
            )
            continue
        if current is None or current.state != "open":
            raise LedgerUnavailableError(
                f"ledger line {number} resolves a request that is not open"
            )
        if kind == "settle":
            records[request_id] = RequestRecord(
                request_id=request_id,
                model=current.model,
                reserved_usd=current.reserved_usd,
                state="settled",
                cost_usd=_decimal(event.get("cost_usd"), "cost"),
                exceeded_reservation=bool(event.get("exceeded_reservation")),
            )
        elif kind in ("release", "hold"):
            records[request_id] = RequestRecord(
                request_id=request_id,
                model=current.model,
                reserved_usd=current.reserved_usd,
                state="released" if kind == "release" else "held",
                reason=str(event.get("reason")),
            )
        else:
            raise LedgerUnavailableError(f"ledger line {number} has an unknown event")
    return records


def summarise(records: dict[str, RequestRecord], budget: Decimal) -> LedgerSummary:
    totals = {"settled": Decimal(0), "held": Decimal(0), "open": Decimal(0)}
    counts: dict[str, int] = {}
    for record in records.values():
        counts[record.state] = counts.get(record.state, 0) + 1
        if record.state in totals:
            totals[record.state] += record.committed_usd
    return LedgerSummary(
        budget_usd=budget,
        settled_usd=totals["settled"],
        held_usd=totals["held"],
        open_usd=totals["open"],
        request_counts=counts,
        needs_review=any(record.exceeded_reservation for record in records.values()),
    )


class SpendLedger:
    """The budget gate for live requests. One instance per ledger file is not required:
    the file lock coordinates every process and thread that opens the same path."""

    def __init__(self, path: Path, budget_usd: Decimal = BUDGET_CEILING_USD):
        if budget_usd > BUDGET_CEILING_USD or budget_usd < 0:
            raise LedgerError(f"budget must be between $0 and ${BUDGET_CEILING_USD}")
        self.path = Path(path)
        self.budget_usd = budget_usd
        self._thread_lock = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[tuple[object, dict[str, RequestRecord]]]:
        if fcntl is None:
            raise LedgerUnavailableError("file locking is unavailable on this platform")
        with self._thread_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
            except OSError as error:
                raise LedgerUnavailableError(
                    f"cannot open the ledger ({type(error).__name__})"
                ) from None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.seek(0)
                content = handle.read()
                if content and not content.endswith("\n"):
                    raise LedgerUnavailableError("the ledger's last entry is incomplete")
                yield handle, replay(content.splitlines())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @staticmethod
    def _append(handle, event: dict) -> None:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _event(kind: EventKind, request_id: str, **fields) -> dict:
        return {
            "v": LEDGER_VERSION,
            "event": kind,
            "request_id": request_id,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            **fields,
        }

    def summary(self) -> LedgerSummary:
        with self._locked() as (_, records):
            return summarise(records, self.budget_usd)

    def reserve(self, request_id: str, model: str, max_cost_usd: Decimal) -> LedgerSummary:
        """Reserve a request's maximum cost, or raise without recording anything."""
        if max_cost_usd <= 0:
            raise LedgerError("a reservation must be positive")
        with self._locked() as (handle, records):
            if request_id in records:
                raise LedgerError("request ID already used")
            current = summarise(records, self.budget_usd)
            if current.needs_review:
                raise LedgerReviewRequiredError(
                    "an earlier request cost more than its reservation; review the ledger"
                )
            if current.committed_usd + max_cost_usd > self.budget_usd:
                raise BudgetExceededError(
                    f"reserving ${max_cost_usd} would exceed the ${self.budget_usd} budget "
                    f"(${current.committed_usd} already committed)"
                )
            self._append(
                handle,
                self._event(
                    "reserve",
                    request_id,
                    model=model,
                    reserved_usd=str(max_cost_usd),
                    pricing_version=PRICING_VERSION,
                ),
            )
        return self.summary()

    def settle(self, request_id: str, cost_usd: Decimal, usage: TokenUsage) -> RequestRecord:
        with self._locked() as (handle, records):
            record = self._open(records, request_id)
            exceeded = cost_usd > record.reserved_usd
            self._append(
                handle,
                self._event(
                    "settle",
                    request_id,
                    cost_usd=str(cost_usd),
                    exceeded_reservation=exceeded,
                    pricing_version=PRICING_VERSION,
                    **usage.as_dict(),
                ),
            )
        return replace_state(record, "settled", cost_usd=cost_usd, exceeded=exceeded)

    def release(self, request_id: str, reason: str) -> RequestRecord:
        """Record that the request was rejected before any work, so nothing was billed."""
        return self._resolve("release", request_id, reason)

    def hold(self, request_id: str, reason: str) -> RequestRecord:
        """Record that the cost is unknown; the full reservation stays counted."""
        return self._resolve("hold", request_id, reason)

    def _resolve(self, kind: EventKind, request_id: str, reason: str) -> RequestRecord:
        with self._locked() as (handle, records):
            record = self._open(records, request_id)
            self._append(handle, self._event(kind, request_id, reason=reason))
        return replace_state(record, "released" if kind == "release" else "held", reason=reason)

    @staticmethod
    def _open(records: dict[str, RequestRecord], request_id: str) -> RequestRecord:
        record = records.get(request_id)
        if record is None or record.state != "open":
            raise LedgerError("no open reservation for this request")
        return record


def replace_state(
    record: RequestRecord,
    state: RequestState,
    *,
    cost_usd: Decimal | None = None,
    exceeded: bool = False,
    reason: str | None = None,
) -> RequestRecord:
    return RequestRecord(
        request_id=record.request_id,
        model=record.model,
        reserved_usd=record.reserved_usd,
        state=state,
        cost_usd=cost_usd,
        exceeded_reservation=exceeded,
        reason=reason,
    )
