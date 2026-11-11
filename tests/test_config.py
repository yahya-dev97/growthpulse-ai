from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from growthpulse_ai.config import LIVE_DATA_REQUIRED_FIELDS, PROJECT_ROOT, Settings, get_settings

FAKE_SECRET = "fake-value-for-tests-only"


def load(**overrides) -> Settings:
    """Build settings without reading any .env file on the developer's machine."""
    return Settings(_env_file=None, **overrides)


def parse_env_file(path: Path) -> dict[str, str]:
    lines = (line.strip() for line in path.read_text().splitlines())
    pairs = (line.split("=", 1) for line in lines if line and not line.startswith("#"))
    return {key: value for key, value in pairs}


def test_defaults_need_no_credentials():
    settings = load()
    assert settings.data_source == "synthetic"
    assert settings.anthropic_api_key is None
    assert settings.database_url is None


def test_reads_environment_variables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_SECRET)
    settings = load()
    assert settings.log_level == "DEBUG"
    assert settings.anthropic_api_key.get_secret_value() == FAKE_SECRET


def test_reads_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=WARNING\n")
    assert Settings(_env_file=env_file).log_level == "WARNING"


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert Settings(_env_file=env_file).log_level == "ERROR"


def test_blank_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert load().anthropic_api_key is None


def test_secrets_are_masked_in_output():
    settings = load(anthropic_api_key=FAKE_SECRET, database_url=FAKE_SECRET)
    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in str(settings.model_dump())
    assert FAKE_SECRET not in settings.model_dump_json()


def test_rejects_unknown_data_source():
    with pytest.raises(ValidationError):
        load(data_source="production_db")


def test_live_mode_names_missing_credentials():
    with pytest.raises(ValidationError) as error:
        load(data_source="live")
    for name in LIVE_DATA_REQUIRED_FIELDS:
        assert name.upper() in str(error.value)


def test_live_mode_accepts_complete_credentials():
    credentials = dict.fromkeys(LIVE_DATA_REQUIRED_FIELDS, FAKE_SECRET)
    assert load(data_source="live", **credentials).data_source == "live"


def test_validation_errors_do_not_echo_rejected_values():
    # A secret pasted into the wrong .env line must not end up in logs.
    with pytest.raises(ValidationError) as error:
        load(data_source=FAKE_SECRET)
    assert FAKE_SECRET not in str(error.value)


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_env_example_matches_settings():
    example = parse_env_file(PROJECT_ROOT / ".env.example")
    assert set(example) == {name.upper() for name in Settings.model_fields}


def test_env_example_contains_no_secret_values():
    example = parse_env_file(PROJECT_ROOT / ".env.example")
    for name, field in Settings.model_fields.items():
        if field.annotation == SecretStr | None:
            assert example[name.upper()] == "", f"{name.upper()} must be blank in .env.example"
