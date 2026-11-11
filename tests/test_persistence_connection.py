import psycopg
import pytest
from pydantic import SecretStr

from fake_db import FakeDatabase
from growthpulse_ai.config import Settings
from growthpulse_ai.persistence import connection as connection_module
from growthpulse_ai.persistence.connection import (
    connect_backend,
    connect_dashboard,
    open_connection,
    secure_conninfo,
)
from growthpulse_ai.persistence.errors import DatabaseConfigurationError, DatabaseUnavailableError

SECRET = "hunter2-not-a-real-password"
URL = f"postgresql://growthpulse_writer.ref:{SECRET}@pooler.example.com:5432/postgres"


def url(**query: str) -> SecretStr:
    suffix = "&".join(f"{key}={value}" for key, value in query.items())
    return SecretStr(f"{URL}?{suffix}" if suffix else URL)


# TLS


def test_tls_is_required_when_the_url_does_not_say():
    assert "sslmode=require" in secure_conninfo(url(), "DATABASE_URL")


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_secure_ssl_modes_are_kept(mode: str):
    assert f"sslmode={mode}" in secure_conninfo(url(sslmode=mode), "DATABASE_URL")


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_insecure_ssl_modes_are_refused(mode: str):
    with pytest.raises(DatabaseConfigurationError, match="sslmode") as caught:
        secure_conninfo(url(sslmode=mode), "DATABASE_URL")
    assert SECRET not in str(caught.value)


def test_missing_url_names_the_setting():
    with pytest.raises(DatabaseConfigurationError, match="DASHBOARD_DATABASE_URL is not set"):
        secure_conninfo(None, "DASHBOARD_DATABASE_URL")


def test_malformed_url_error_does_not_echo_the_value():
    with pytest.raises(DatabaseConfigurationError) as caught:
        secure_conninfo(SecretStr(f"host=x password={SECRET} =broken"), "DATABASE_URL")
    assert SECRET not in str(caught.value)
    assert caught.value.__suppress_context__ is True


# Opening connections


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace psycopg.connect, recording its arguments."""
    calls: dict = {"database": FakeDatabase(results={"current_user": ("growthpulse_writer",)})}

    def fake_connect(conninfo: str, **kwargs):
        calls["conninfo"], calls["kwargs"] = conninfo, kwargs
        return calls["database"].connect()

    monkeypatch.setattr(connection_module.psycopg, "connect", fake_connect)
    return calls


def test_connection_is_autocommit_and_pooler_safe(server: dict):
    open_connection("host=x", expected_role="growthpulse_writer", application_name="test")
    assert server["kwargs"]["autocommit"] is True
    assert server["kwargs"]["prepare_threshold"] is None
    assert server["kwargs"]["connect_timeout"] > 0


def test_connection_as_another_role_is_refused_and_closed(server: dict):
    server["database"].results = {"current_user": ("postgres",)}
    with pytest.raises(DatabaseConfigurationError, match="growthpulse_writer"):
        open_connection("host=x", expected_role="growthpulse_writer", application_name="test")
    assert server["database"].connections[0].closed


def test_connection_failure_does_not_leak_the_driver_message(monkeypatch: pytest.MonkeyPatch):
    def refuse(*_, **__):
        raise psycopg.OperationalError(f'password authentication failed "{SECRET}"')

    monkeypatch.setattr(connection_module.psycopg, "connect", refuse)
    with pytest.raises(DatabaseUnavailableError) as caught:
        open_connection("host=x", expected_role="growthpulse_writer", application_name="test")
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_backend_uses_database_url_and_writer_role(server: dict):
    settings = Settings(_env_file=None, database_url=URL)
    connect_backend(settings)
    assert "sslmode=require" in server["conninfo"]
    assert server["kwargs"]["application_name"] == "growthpulse-pipeline"
    assert server["database"].matching("default_transaction_read_only") == []


def test_dashboard_uses_its_own_url_role_and_read_only_session(server: dict):
    server["database"].results = {"current_user": ("growthpulse_dashboard",)}
    dashboard_url = URL.replace("growthpulse_writer", "growthpulse_dashboard")
    settings = Settings(_env_file=None, database_url=URL, dashboard_database_url=dashboard_url)
    connect_dashboard(settings)
    assert "growthpulse_dashboard" in server["conninfo"]
    assert server["database"].matching("set default_transaction_read_only = on")


def test_dashboard_refuses_the_writer_credential(server: dict):
    settings = Settings(_env_file=None, dashboard_database_url=URL)
    with pytest.raises(DatabaseConfigurationError, match="growthpulse_dashboard"):
        connect_dashboard(settings)


def test_dashboard_without_its_own_url_never_falls_back_to_the_backend(server: dict):
    settings = Settings(_env_file=None, database_url=URL)
    with pytest.raises(DatabaseConfigurationError, match="DASHBOARD_DATABASE_URL"):
        connect_dashboard(settings)
    assert "conninfo" not in server
