"""Interfaces for OAuth connections, account discovery and token storage.

Milestone 6 implements ``OAuthProvider`` and ``AccountDiscovery`` for Google Ads and Meta,
read-only. Milestone 5 defines them and tests them with fakes; nothing here makes a network
call. Every token is a ``SecretStr``, so reprs, logs and errors show it masked.

Which tokens are stored:

- Refresh tokens, and Meta's long-lived access tokens, are encrypted and stored.
- Short-lived access tokens are cached in memory for one process only, never stored.
"""

import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from growthpulse_ai.accounts.models import DiscoveredAccount
from growthpulse_ai.credentials.crypto import EncryptedToken, TokenCipher, TokenType
from growthpulse_ai.schema import Platform

ACCESS_REFRESH_MARGIN = timedelta(minutes=5)
EXPIRY_WARNING = timedelta(days=7)


class ReauthorizationRequired(Exception):
    """The connection has no usable credential. The owner must sign in again."""


def _require_aware(moment: datetime | None) -> None:
    if moment is not None and moment.tzinfo is None:
        raise ValueError("Token expiry times must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TokenSet:
    """Tokens from one grant or refresh. Expiry times are timezone-aware."""

    access_token: SecretStr | None = None
    access_expires_at: datetime | None = None
    refresh_token: SecretStr | None = None
    refresh_expires_at: datetime | None = None
    long_lived_access: bool = False
    """True for Meta's long-lived token, which is stored; False for short-lived tokens."""
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.access_token is None and self.refresh_token is None:
            raise ValueError("A token set needs an access or refresh token")
        _require_aware(self.access_expires_at)
        _require_aware(self.refresh_expires_at)
        if self.long_lived_access and self.access_token is None:
            raise ValueError("A long-lived token set needs its access token")

    def access_usable(self, now: datetime, margin: timedelta = ACCESS_REFRESH_MARGIN) -> bool:
        if self.access_token is None:
            return False
        return self.access_expires_at is None or self.access_expires_at > now + margin

    def refresh_usable(self, now: datetime) -> bool:
        if self.refresh_token is None:
            return False
        return self.refresh_expires_at is None or self.refresh_expires_at > now

    def expiring_soon(self, now: datetime, warning: timedelta = EXPIRY_WARNING) -> bool:
        """True when the stored credential will need reauthorisation within ``warning``."""
        if self.refresh_token is not None:
            ends = self.refresh_expires_at
        else:
            ends = self.access_expires_at if self.long_lived_access else None
        return ends is not None and ends <= now + warning


class OAuthProvider(Protocol):
    platform: Platform

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    def exchange_code(
        self, *, code: SecretStr, code_verifier: SecretStr, redirect_uri: str
    ) -> TokenSet: ...

    def refresh(self, refresh_token: SecretStr) -> TokenSet: ...

    def revoke(self, token: SecretStr) -> None: ...


class AccountDiscovery(Protocol):
    """Read-only listing of every advertising account one connection can see."""

    def list_accounts(
        self, connection_id: UUID, access_token: SecretStr
    ) -> list[DiscoveredAccount]: ...


class TokenStore(Protocol):
    def save(self, connection_id: UUID, tokens: TokenSet) -> None: ...

    def load(self, connection_id: UUID) -> TokenSet | None: ...

    def delete(self, connection_id: UUID) -> None: ...


class CredentialProvider(Protocol):
    """What a live connector receives: a way to get an access token, nothing more."""

    def access_token(self, connection_id: UUID) -> SecretStr: ...


@dataclass(frozen=True, slots=True)
class StoredCredential:
    encrypted: EncryptedToken
    expires_at: datetime | None


class CiphertextBackend(Protocol):
    """Storage for encrypted tokens only. Phase C backs it with growthpulse_secrets."""

    def put(self, connection_id: UUID, token_type: TokenType, credential: StoredCredential) -> None:
        """Replace this connection's stored token of this type."""
        ...

    def get(self, connection_id: UUID) -> Mapping[TokenType, StoredCredential]: ...

    def remove(self, connection_id: UUID) -> None: ...


class EncryptingTokenStore:
    """A ``TokenStore`` that encrypts before anything reaches the backend.

    Only refresh tokens and long-lived access tokens are stored. A short-lived access token
    in the set is ignored here; the credential provider keeps it in memory.
    """

    def __init__(self, backend: CiphertextBackend, cipher: TokenCipher) -> None:
        self._backend = backend
        self._cipher = cipher

    def save(self, connection_id: UUID, tokens: TokenSet) -> None:
        stored = []
        if tokens.refresh_token is not None:
            stored.append((TokenType.REFRESH, tokens.refresh_token, tokens.refresh_expires_at))
        if tokens.long_lived_access and tokens.access_token is not None:
            stored.append(
                (TokenType.LONG_LIVED_ACCESS, tokens.access_token, tokens.access_expires_at)
            )
        if not stored:
            raise ValueError("Nothing in this token set is meant to be stored")
        for token_type, token, expires_at in stored:
            encrypted = self._cipher.encrypt(
                token, connection_id=connection_id, token_type=token_type
            )
            self._backend.put(connection_id, token_type, StoredCredential(encrypted, expires_at))

    def load(self, connection_id: UUID) -> TokenSet | None:
        stored = self._backend.get(connection_id)
        if not stored:
            return None
        values = {
            token_type: self._cipher.decrypt(
                credential.encrypted, connection_id=connection_id, token_type=token_type
            )
            for token_type, credential in stored.items()
        }
        refresh = stored.get(TokenType.REFRESH)
        long_lived = stored.get(TokenType.LONG_LIVED_ACCESS)
        return TokenSet(
            access_token=values.get(TokenType.LONG_LIVED_ACCESS),
            access_expires_at=long_lived.expires_at if long_lived else None,
            refresh_token=values.get(TokenType.REFRESH),
            refresh_expires_at=refresh.expires_at if refresh else None,
            long_lived_access=long_lived is not None,
        )

    def delete(self, connection_id: UUID) -> None:
        self._backend.remove(connection_id)


class RefreshingCredentialProvider:
    """Hands out access tokens, refreshing them shortly before they expire.

    A refresh token the provider rotates is saved straight away. A connection with no usable
    credential raises ``ReauthorizationRequired``; the scheduler then marks it needs_reauth.
    """

    def __init__(
        self,
        store: TokenStore,
        providers: Mapping[Platform, OAuthProvider],
        platform_of: Callable[[UUID], Platform],
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._providers = providers
        self._platform_of = platform_of
        self._clock = clock
        self._cache: dict[UUID, TokenSet] = {}

    def access_token(self, connection_id: UUID) -> SecretStr:
        now = self._clock()
        cached = self._cache.get(connection_id)
        if cached is not None and cached.access_usable(now):
            return cached.access_token  # type: ignore[return-value]
        stored = self._store.load(connection_id)
        if stored is None:
            raise ReauthorizationRequired("No stored credential for this connection")
        if stored.access_usable(now):
            return stored.access_token  # type: ignore[return-value]
        if not stored.refresh_usable(now):
            raise ReauthorizationRequired("The stored credential has expired")
        provider = self._providers[self._platform_of(connection_id)]
        fresh = provider.refresh(stored.refresh_token)  # type: ignore[arg-type]
        if fresh.access_token is None:
            raise ReauthorizationRequired("The provider returned no access token")
        if fresh.refresh_token is not None:
            self._store.save(connection_id, fresh)
        self._cache[connection_id] = fresh
        return fresh.access_token

    def forget(self, connection_id: UUID) -> None:
        self._cache.pop(connection_id, None)


def new_oauth_state() -> SecretStr:
    """A single-use, unguessable value tying the callback to the request that started it."""
    return SecretStr(secrets.token_urlsafe(32))


def new_pkce_verifier() -> SecretStr:
    """RFC 7636 code verifier: 43 characters from the unreserved set."""
    return SecretStr(secrets.token_urlsafe(32))


def pkce_challenge(verifier: SecretStr) -> str:
    """RFC 7636 S256 challenge for a verifier."""
    digest = hashlib.sha256(verifier.get_secret_value().encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
