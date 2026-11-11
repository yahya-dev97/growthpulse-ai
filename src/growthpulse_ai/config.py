"""Application settings, loaded from environment variables and an optional .env file.

Real environment variables take precedence over .env, so GitHub Actions and hosted
deployments need no file on disk. Every credential is optional at load time, so the
synthetic-data path runs with no accounts at all.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LIVE_DATA_REQUIRED_FIELDS = (
    "google_ads_developer_token",
    "google_ads_client_id",
    "google_ads_client_secret",
    "google_ads_refresh_token",
    "google_ads_customer_id",
    "meta_access_token",
    "meta_ad_account_id",
)

LEGACY_ACCOUNT_SETTINGS = (
    "google_ads_refresh_token",
    "google_ads_customer_id",
    "google_ads_login_customer_id",
    "meta_access_token",
    "meta_ad_account_id",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # A copied .env.example leaves blank values; treat those as unset.
        env_ignore_empty=True,
        extra="ignore",
        # Keep rejected values, which may be misplaced secrets, out of error messages.
        hide_input_in_errors=True,
    )

    app_env: Literal["local", "test", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_source: Literal["synthetic", "live"] = "synthetic"

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"

    # Backend read/write connection, and a separate read-only one for the dashboard.
    database_url: SecretStr | None = None
    dashboard_database_url: SecretStr | None = None

    # Versioned AES-256-GCM key ring for stored OAuth tokens, "1:<base64url>,2:<...>".
    # Never stored in the database. Both settings are set together, or neither.
    token_encryption_keys: SecretStr | None = None
    token_encryption_active_version: int | None = None

    # App-level Google Ads credentials. These identify the app, not an advertiser.
    google_ads_developer_token: SecretStr | None = None
    google_ads_client_id: str | None = None
    google_ads_client_secret: SecretStr | None = None

    # Legacy single-account settings (Milestones 1-4). Accounts and tokens now come from
    # per-account OAuth connections (Milestone 6). Kept, unused by the multi-account path,
    # until that path is proven; then removed. See LEGACY_ACCOUNT_SETTINGS.
    google_ads_refresh_token: SecretStr | None = None
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None
    meta_access_token: SecretStr | None = None
    meta_ad_account_id: str | None = None

    @property
    def legacy_account_settings_in_use(self) -> tuple[str, ...]:
        """Names of legacy single-account settings that are set, for a deprecation notice."""
        return tuple(
            name.upper() for name in LEGACY_ACCOUNT_SETTINGS if getattr(self, name) is not None
        )

    @model_validator(mode="after")
    def _require_complete_key_ring(self) -> Self:
        keys_set = self.token_encryption_keys is not None
        if keys_set != (self.token_encryption_active_version is not None):
            raise ValueError(
                "TOKEN_ENCRYPTION_KEYS and TOKEN_ENCRYPTION_ACTIVE_VERSION must be set together"
            )
        return self

    @model_validator(mode="after")
    def _require_live_credentials(self) -> Self:
        if self.data_source == "live":
            missing = [
                name.upper() for name in LIVE_DATA_REQUIRED_FIELDS if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"DATA_SOURCE=live requires: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    return Settings()
