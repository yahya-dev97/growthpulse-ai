import re

import pytest

from growthpulse_ai.config import PROJECT_ROOT, Settings
from growthpulse_ai.persistence import connection_check
from growthpulse_ai.persistence.connection_check import TABLES, VIEWS, check_tls

MIGRATIONS = sorted((PROJECT_ROOT / "supabase" / "migrations").glob("*.sql"))


class FakeOption:
    def __init__(self, keyword: bytes, val: bytes | None, compiled: bytes | None) -> None:
        self.keyword, self.val, self.compiled = keyword, val, compiled


class FakePgconn:
    """Mirrors only the attributes the real psycopg PGconn has."""

    def __init__(self, ssl: bool, minimum: bytes | None, compiled: bytes | None) -> None:
        self.ssl_in_use = ssl
        self.info = [
            FakeOption(b"password", b"must-not-be-read", None),
            FakeOption(b"ssl_min_protocol_version", minimum, compiled),
        ]


class FakeConnection:
    def __init__(self, ssl: bool, minimum: bytes | None = None, compiled=b"TLSv1.2") -> None:
        self.pgconn = FakePgconn(ssl, minimum, compiled)


def test_fake_matches_the_real_psycopg_api():
    # The real PGconn has no per-connection TLS version attribute, which an earlier
    # version of the check wrongly relied on. Keep the fake honest.
    from psycopg import pq

    assert hasattr(pq.PGconn, "ssl_in_use")
    assert hasattr(pq.PGconn, "info")
    assert not hasattr(pq.PGconn, "ssl_attribute")


@pytest.mark.parametrize(
    ("ssl", "minimum", "compiled", "expected"),
    [
        (True, None, b"TLSv1.2", [True, True]),  # libpq's compiled default
        (True, b"TLSv1.3", b"TLSv1.2", [True, True]),
        (True, b"TLSv1", b"TLSv1.2", [True, False]),  # weakened by configuration
        (False, None, b"TLSv1.2", [False, True]),
    ],
)
def test_tls_is_judged_on_the_client_side(ssl, minimum, compiled, expected):
    assert [ok for _, ok in check_tls(FakeConnection(ssl, minimum, compiled))] == expected


def test_checked_tables_and_views_match_the_migrations():
    sql = "\n".join(path.read_text() for path in MIGRATIONS)
    assert set(TABLES) == set(re.findall(r"create table growthpulse\.(\w+)", sql))
    assert set(VIEWS) == set(re.findall(r"create view growthpulse_read\.(\w+)", sql))


def test_check_never_writes():
    source = (PROJECT_ROOT / "src/growthpulse_ai/persistence/connection_check.py").read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    assert "force_rollback=True" in code
    assert "set transaction read only" in code
    # SQL statement shapes, so check labels such as "cannot update reports" don't match.
    writes = re.findall(
        r"\b(insert\s+into|update\s+[\w.]+\s+set|delete\s+from|truncate\s+\w|"
        r"(?:alter|create|drop|grant|revoke)\s+(?:table|role|schema|view|function|index|on))\b",
        code.lower(),
    )
    assert writes == []


def test_cli_without_urls_fails_cleanly(monkeypatch, capsys):
    # Ignore any local .env, so this test can never reach a real database.
    monkeypatch.setattr(connection_check, "get_settings", lambda: Settings(_env_file=None))
    assert connection_check.main() == 1
    output = capsys.readouterr().out
    assert "FAIL  writer: connect and run checks (DatabaseConfigurationError)" in output
    assert "FAIL  dashboard: connect and run checks (DatabaseConfigurationError)" in output
    assert "0 passed, 2 failed" in output
