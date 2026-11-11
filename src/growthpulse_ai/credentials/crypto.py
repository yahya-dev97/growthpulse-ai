"""Application-level AES-256-GCM encryption for stored OAuth tokens.

- The key never enters PostgreSQL. It comes from the environment (GitHub and Streamlit
  secrets), as a versioned key ring, so the database alone never yields a usable token.
- Every encryption uses a fresh random 96-bit nonce. With random nonces, one key should
  encrypt well under 2**32 tokens; rotation (below) keeps each key far from that.
- The authenticated context binds each ciphertext to its connection, token type and key
  version, so a ciphertext copied onto another connection or type fails to decrypt.
- Errors never include a token, a key or the ciphertext.

Rotation: add a new key version, make it active, and re-encrypt stored tokens with
``rotate``. Old versions stay in the ring until nothing uses them. A lost key cannot be
recovered: tokens under it are unusable and their connections must be reauthorised.
"""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

FORMAT_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_KEY_VERSION = 32767  # fits a PostgreSQL smallint
MAX_TOKEN_BYTES = 8192


class TokenCryptoError(Exception):
    """A key ring or token could not be used. Never carries secret material."""


class TokenDecryptionError(TokenCryptoError):
    """The ciphertext is not valid for this connection, token type and key ring."""


class TokenType(StrEnum):
    """Tokens that are ever stored. Short-lived access tokens are kept in memory only."""

    REFRESH = "refresh"
    LONG_LIVED_ACCESS = "long_lived_access"


@dataclass(frozen=True, slots=True)
class EncryptedToken:
    format_version: int
    key_version: int
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


class TokenKeyring:
    """Versioned 256-bit keys. Its repr shows versions only."""

    __slots__ = ("_active", "_keys")

    def __init__(self, keys: Mapping[int, bytes], active_version: int) -> None:
        if not keys:
            raise TokenCryptoError("The key ring is empty")
        for version, key in keys.items():
            if not 1 <= version <= MAX_KEY_VERSION:
                raise TokenCryptoError(f"Key versions must be 1-{MAX_KEY_VERSION}")
            if len(key) != KEY_BYTES:
                raise TokenCryptoError("Every key must be exactly 32 bytes")
        if active_version not in keys:
            raise TokenCryptoError("The active key version is not in the key ring")
        if len(set(keys.values())) != len(keys):
            raise TokenCryptoError("Two key versions share the same key")
        self._keys = dict(keys)
        self._active = active_version

    @classmethod
    def from_secret(cls, value: SecretStr, active_version: int) -> "TokenKeyring":
        """Parse ``"1:<base64url key>,2:<base64url key>"`` without echoing any of it."""
        keys: dict[int, bytes] = {}
        for entry in value.get_secret_value().split(","):
            version_text, separator, encoded = entry.strip().partition(":")
            if not separator or not version_text.isdigit():
                raise TokenCryptoError("TOKEN_ENCRYPTION_KEYS must look like 1:<key>,2:<key>")
            version = int(version_text)
            if version in keys:
                raise TokenCryptoError("A key version appears twice")
            try:
                keys[version] = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except (binascii.Error, ValueError):
                raise TokenCryptoError("A key is not valid base64url") from None
        return cls(keys, active_version)

    @property
    def active_version(self) -> int:
        return self._active

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise TokenDecryptionError("The token's key version is not in the key ring") from None

    def __repr__(self) -> str:
        return f"TokenKeyring(versions={self.versions}, active={self._active})"


def _context(connection_id: UUID, token_type: TokenType, key_version: int) -> bytes:
    return json.dumps(
        ["growthpulse-token", FORMAT_VERSION, str(connection_id), token_type.value, key_version],
        separators=(",", ":"),
    ).encode()


class TokenCipher:
    def __init__(self, keyring: TokenKeyring) -> None:
        self._keyring = keyring

    def encrypt(
        self, token: SecretStr, *, connection_id: UUID, token_type: TokenType
    ) -> EncryptedToken:
        plaintext = token.get_secret_value().encode()
        if not plaintext:
            raise TokenCryptoError("Refusing to encrypt an empty token")
        if len(plaintext) > MAX_TOKEN_BYTES:
            raise TokenCryptoError("Token is longer than expected")
        version = self._keyring.active_version
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._keyring.key(version)).encrypt(
            nonce, plaintext, _context(connection_id, token_type, version)
        )
        return EncryptedToken(FORMAT_VERSION, version, nonce, ciphertext)

    def decrypt(
        self, encrypted: EncryptedToken, *, connection_id: UUID, token_type: TokenType
    ) -> SecretStr:
        if encrypted.format_version != FORMAT_VERSION or len(encrypted.nonce) != NONCE_BYTES:
            raise TokenDecryptionError("Unsupported token format")
        key = self._keyring.key(encrypted.key_version)
        try:
            plaintext = AESGCM(key).decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _context(connection_id, token_type, encrypted.key_version),
            )
        except InvalidTag:
            raise TokenDecryptionError("Token could not be decrypted") from None
        return SecretStr(plaintext.decode())

    def needs_rotation(self, encrypted: EncryptedToken) -> bool:
        return encrypted.key_version != self._keyring.active_version

    def rotate(
        self, encrypted: EncryptedToken, *, connection_id: UUID, token_type: TokenType
    ) -> EncryptedToken:
        """Re-encrypt under the active key. Unchanged if it already uses that key."""
        if not self.needs_rotation(encrypted):
            return encrypted
        token = self.decrypt(encrypted, connection_id=connection_id, token_type=token_type)
        return self.encrypt(token, connection_id=connection_id, token_type=token_type)
