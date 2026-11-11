"""Test doubles for OAuth providers, discovery and ciphertext storage. No network."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import SecretStr

from growthpulse_ai.accounts.models import DiscoveredAccount
from growthpulse_ai.credentials.crypto import TokenType
from growthpulse_ai.credentials.oauth import StoredCredential, TokenSet
from growthpulse_ai.schema import Platform


@dataclass
class FakeOAuthProvider:
    platform: Platform
    clock: object
    lifetime: timedelta = timedelta(hours=1)
    rotate_refresh: bool = False
    refreshes: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return f"https://auth.invalid/authorize?state={state}&challenge={code_challenge}"

    def exchange_code(
        self, *, code: SecretStr, code_verifier: SecretStr, redirect_uri: str
    ) -> TokenSet:
        return TokenSet(
            access_token=SecretStr("fake-access-0"),
            access_expires_at=self.clock() + self.lifetime,
            refresh_token=SecretStr("fake-refresh-0"),
        )

    def refresh(self, refresh_token: SecretStr) -> TokenSet:
        self.refreshes.append(refresh_token.get_secret_value())
        n = len(self.refreshes)
        return TokenSet(
            access_token=SecretStr(f"fake-access-{n}"),
            access_expires_at=self.clock() + self.lifetime,
            refresh_token=SecretStr(f"fake-refresh-{n}") if self.rotate_refresh else None,
        )

    def revoke(self, token: SecretStr) -> None:
        self.revoked.append(token.get_secret_value())


@dataclass
class FakeDiscovery:
    accounts: list[DiscoveredAccount]

    def list_accounts(
        self, connection_id: UUID, access_token: SecretStr
    ) -> list[DiscoveredAccount]:
        return [a for a in self.accounts if a.connection_id == connection_id]


@dataclass
class InMemoryCiphertextBackend:
    rows: dict[UUID, dict[TokenType, StoredCredential]] = field(default_factory=dict)

    def put(self, connection_id: UUID, token_type: TokenType, credential: StoredCredential) -> None:
        self.rows.setdefault(connection_id, {})[token_type] = credential

    def get(self, connection_id: UUID) -> Mapping[TokenType, StoredCredential]:
        return dict(self.rows.get(connection_id, {}))

    def remove(self, connection_id: UUID) -> None:
        self.rows.pop(connection_id, None)

    def all_bytes(self) -> bytes:
        return b"".join(
            c.encrypted.nonce + c.encrypted.ciphertext
            for creds in self.rows.values()
            for c in creds.values()
        )


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta
