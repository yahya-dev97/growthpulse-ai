import os

import pytest

from growthpulse_ai.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's shell environment and cached settings out of every test."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_configure(config: pytest.Config) -> None:
    """In CI, the PostgreSQL tests must run: a missing database URL is an error, not a skip."""
    required = os.environ.get("GROWTHPULSE_REQUIRE_POSTGRES") == "1"
    if required and not os.environ.get("GROWTHPULSE_TEST_DATABASE_URL"):
        raise pytest.UsageError(
            "GROWTHPULSE_REQUIRE_POSTGRES=1 but GROWTHPULSE_TEST_DATABASE_URL is not set"
        )
