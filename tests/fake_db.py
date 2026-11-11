"""A recording stand-in for a psycopg connection. No database is involved.

It records every statement with the transaction it ran in, and can be told to fail on a
statement or to report a row count, so tests can check transaction boundaries and
failure handling without a server.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Statement:
    query: str
    params: Any
    transaction: int | None
    many: bool = False


@dataclass
class FakeDatabase:
    """Shared across every connection a test opens, like a real server would be."""

    statements: list[Statement] = field(default_factory=list)
    committed: list[int] = field(default_factory=list)
    rolled_back: list[int] = field(default_factory=list)
    connections: list["FakeConnection"] = field(default_factory=list)
    # query substring -> exception raised when a matching statement runs
    fail_on: dict[str, Exception] = field(default_factory=dict)
    # query substring -> rowcount reported for each matching statement
    rowcounts: dict[str, int] = field(default_factory=dict)
    # query substring -> row returned by fetchone()
    results: dict[str, Any] = field(default_factory=dict)
    connect_error: Exception | None = None
    _next_transaction: int = 0

    def connect(self) -> "FakeConnection":
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def matching(self, fragment: str) -> list[Statement]:
        return [s for s in self.statements if fragment in s.query]

    def _lookup(self, table: dict[str, Any], query: str, default: Any) -> Any:
        for fragment, value in table.items():
            if fragment in query:
                return value
        return default


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self.rowcount = -1
        self._row: Any = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _run(self, query: str, params: Any, many: bool) -> None:
        database = self._connection.database
        database.statements.append(Statement(query, params, self._connection.transaction_id, many))
        error = database._lookup(database.fail_on, query, None)
        if error is not None:
            raise error
        count = len(params) if many else 1
        self.rowcount = database._lookup(database.rowcounts, query, count)
        self._row = database._lookup(database.results, query, None)

    def execute(self, query: str, params: Any = None) -> "FakeCursor":
        self._run(query, params, many=False)
        return self

    def executemany(self, query: str, params: list[Any]) -> None:
        self._run(query, list(params), many=True)

    def fetchone(self) -> Any:
        return self._row


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.transaction_id: int | None = None
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, query: str, params: Any = None) -> FakeCursor:
        return self.cursor().execute(query, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        assert self.transaction_id is None, "nested transactions are not used"
        database = self.database
        self.transaction_id = database._next_transaction
        database._next_transaction += 1
        try:
            yield
        except BaseException:
            database.rolled_back.append(self.transaction_id)
            raise
        else:
            database.committed.append(self.transaction_id)
        finally:
            self.transaction_id = None


Connect = Callable[[], FakeConnection]


def scope_answers(database: FakeDatabase, client_id) -> FakeDatabase:
    """Answer the store's scope checks as a database holding only this client's rows would."""
    database.results.setdefault("select client_id from growthpulse.analytics_reports", (client_id,))
    database.results.setdefault("select count(*) from growthpulse.findings", (0,))
    return database
