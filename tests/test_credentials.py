"""Token encryption, encrypted storage, credential refresh and OAuth helpers.

Every key here is generated at test time and discarded. Every token is a fake string.
"""

import base64
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from fake_oauth import Clock, FakeOAuthProvider, InMemoryCiphertextBackend
from growthpulse_ai.credentials.crypto import (
    NONCE_BYTES,
    EncryptedToken,
    TokenCipher,
    TokenCryptoError,
    TokenDecryptionError,
    TokenKeyring,
    TokenType,
)
from growthpulse_ai.credentials.oauth import (
    EncryptingTokenStore,
    ReauthorizationRequired,
    RefreshingCredentialProvider,
    TokenSet,
    new_oauth_state,
    new_pkce_verifier,
    pkce_challenge,
)
from growthpulse_ai.schema import Platform
from multi_client import stable_uuid

TOKEN = "fake-refresh-token-not-real"
CONNECTION = stable_uuid("connection")
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def keyring(*versions: int, active: int | None = None) -> TokenKeyring:
    versions = versions or (1,)
    return TokenKeyring({v: os.urandom(32) for v in versions}, active or max(versions))


def encoded(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode()


# Key ring


def test_key_ring_parses_versioned_keys():
    one, two = os.urandom(32), os.urandom(32)
    ring = TokenKeyring.from_secret(SecretStr(f"1:{encoded(one)}, 2:{encoded(two)}"), 2)
    assert ring.versions == (1, 2)
    assert ring.active_version == 2
    assert ring.key(1) == one


@pytest.mark.parametrize(
    "value",
    ["", "nonsense", "x:abc", "1:" + "!" * 43, "1:{a},1:{b}", "1:{short}", "0:{a}", "40000:{a}"],
)
def test_bad_key_rings_are_refused_without_echoing_them(value: str):
    key_a, key_b = encoded(os.urandom(32)), encoded(os.urandom(32))
    text = value.format(a=key_a, b=key_b, short=encoded(os.urandom(16)))
    with pytest.raises(TokenCryptoError) as caught:
        TokenKeyring.from_secret(SecretStr(text), 1)
    for secret in (key_a, key_b):
        assert secret not in str(caught.value)


def test_active_version_must_exist_and_keys_must_differ():
    key = os.urandom(32)
    with pytest.raises(TokenCryptoError, match="active"):
        TokenKeyring({1: key}, 2)
    with pytest.raises(TokenCryptoError, match="same key"):
        TokenKeyring({1: key, 2: key}, 2)
    with pytest.raises(TokenCryptoError, match="empty"):
        TokenKeyring({}, 1)


def test_key_ring_repr_shows_versions_only():
    key = os.urandom(32)
    ring = TokenKeyring({3: key}, 3)
    assert repr(ring) == "TokenKeyring(versions=(3,), active=3)"
    assert key.hex() not in repr(ring) and encoded(key) not in repr(ring)


# Encryption


def cipher(ring: TokenKeyring | None = None) -> TokenCipher:
    return TokenCipher(ring or keyring())


def seal(c: TokenCipher, token: str = TOKEN, **context) -> EncryptedToken:
    context = {"connection_id": CONNECTION, "token_type": TokenType.REFRESH, **context}
    return c.encrypt(SecretStr(token), **context)


def test_round_trip_returns_the_token():
    c = cipher()
    encrypted = seal(c)
    assert TOKEN.encode() not in encrypted.ciphertext
    plain = c.decrypt(encrypted, connection_id=CONNECTION, token_type=TokenType.REFRESH)
    assert plain.get_secret_value() == TOKEN


def test_every_encryption_uses_a_fresh_nonce():
    c = cipher()
    sealed = [seal(c) for _ in range(2000)]
    assert len({s.nonce for s in sealed}) == 2000
    assert len({s.ciphertext for s in sealed}) == 2000
    assert all(len(s.nonce) == NONCE_BYTES for s in sealed)


def test_ciphertext_is_bound_to_its_connection_and_token_type():
    c = cipher()
    encrypted = seal(c)
    with pytest.raises(TokenDecryptionError):
        c.decrypt(encrypted, connection_id=stable_uuid("other"), token_type=TokenType.REFRESH)
    with pytest.raises(TokenDecryptionError):
        c.decrypt(encrypted, connection_id=CONNECTION, token_type=TokenType.LONG_LIVED_ACCESS)


def test_ciphertext_is_bound_to_its_key_version():
    ring = keyring(1, 2, active=2)
    encrypted = seal(TokenCipher(ring))
    relabelled = replace(encrypted, key_version=1)
    with pytest.raises(TokenDecryptionError):
        TokenCipher(ring).decrypt(
            relabelled, connection_id=CONNECTION, token_type=TokenType.REFRESH
        )


@pytest.mark.parametrize("part", ["ciphertext", "nonce"])
def test_tampering_is_detected(part: str):
    c = cipher()
    encrypted = seal(c)
    value = bytearray(getattr(encrypted, part))
    value[0] ^= 1
    with pytest.raises(TokenDecryptionError):
        c.decrypt(
            replace(encrypted, **{part: bytes(value)}),
            connection_id=CONNECTION,
            token_type=TokenType.REFRESH,
        )


def test_a_different_key_cannot_decrypt():
    encrypted = seal(cipher())
    with pytest.raises(TokenDecryptionError):
        cipher().decrypt(encrypted, connection_id=CONNECTION, token_type=TokenType.REFRESH)


def test_unknown_key_version_and_format_are_refused():
    c = cipher()
    encrypted = seal(c)
    with pytest.raises(TokenDecryptionError, match="key version"):
        c.decrypt(
            replace(encrypted, key_version=9),
            connection_id=CONNECTION,
            token_type=TokenType.REFRESH,
        )
    with pytest.raises(TokenDecryptionError, match="format"):
        c.decrypt(
            replace(encrypted, format_version=2),
            connection_id=CONNECTION,
            token_type=TokenType.REFRESH,
        )


def test_errors_and_reprs_never_show_the_token():
    c = cipher()
    encrypted = seal(c)
    assert TOKEN not in repr(encrypted)
    assert encrypted.ciphertext.hex() not in repr(encrypted)
    with pytest.raises(TokenDecryptionError) as caught:
        c.decrypt(encrypted, connection_id=stable_uuid("x"), token_type=TokenType.REFRESH)
    assert TOKEN not in str(caught.value)


def test_empty_and_oversized_tokens_are_refused():
    with pytest.raises(TokenCryptoError, match="empty"):
        seal(cipher(), token="")
    with pytest.raises(TokenCryptoError, match="longer"):
        seal(cipher(), token="x" * 8193)


def test_rotation_moves_tokens_to_the_active_key():
    old_ring = keyring(1)
    encrypted = seal(TokenCipher(old_ring))
    new_ring = TokenKeyring({1: old_ring.key(1), 2: os.urandom(32)}, 2)
    rotating = TokenCipher(new_ring)
    assert rotating.needs_rotation(encrypted)
    rotated = rotating.rotate(encrypted, connection_id=CONNECTION, token_type=TokenType.REFRESH)
    assert rotated.key_version == 2 and not rotating.needs_rotation(rotated)
    # After rotation the old key can be retired.
    retired = TokenCipher(TokenKeyring({2: new_ring.key(2)}, 2))
    plain = retired.decrypt(rotated, connection_id=CONNECTION, token_type=TokenType.REFRESH)
    assert plain.get_secret_value() == TOKEN
    assert rotating.rotate(rotated, connection_id=CONNECTION, token_type=TokenType.REFRESH) is (
        rotated
    )


# Encrypted token storage


def test_the_backend_only_ever_holds_ciphertext():
    backend = InMemoryCiphertextBackend()
    store = EncryptingTokenStore(backend, cipher())
    store.save(
        CONNECTION,
        TokenSet(
            access_token=SecretStr("fake-short-lived-access"),
            access_expires_at=NOW + timedelta(hours=1),
            refresh_token=SecretStr(TOKEN),
        ),
    )
    assert set(backend.rows[CONNECTION]) == {TokenType.REFRESH}
    raw = backend.all_bytes()
    assert TOKEN.encode() not in raw and b"fake-short-lived-access" not in raw


def test_stored_tokens_load_back():
    store = EncryptingTokenStore(InMemoryCiphertextBackend(), cipher())
    expiry = NOW + timedelta(days=60)
    store.save(
        CONNECTION,
        TokenSet(access_token=SecretStr("fake-long-lived"), access_expires_at=expiry,
                 long_lived_access=True),
    )  # fmt: skip
    loaded = store.load(CONNECTION)
    assert loaded.access_token.get_secret_value() == "fake-long-lived"
    assert loaded.access_expires_at == expiry and loaded.long_lived_access
    store.delete(CONNECTION)
    assert store.load(CONNECTION) is None


def test_a_set_with_nothing_storable_is_refused():
    store = EncryptingTokenStore(InMemoryCiphertextBackend(), cipher())
    with pytest.raises(ValueError, match="stored"):
        store.save(CONNECTION, TokenSet(access_token=SecretStr("fake-short-lived")))


# Token sets


def test_token_set_validation_and_masking():
    with pytest.raises(ValueError):
        TokenSet()
    with pytest.raises(ValueError, match="aware"):
        TokenSet(refresh_token=SecretStr(TOKEN), refresh_expires_at=datetime(2026, 1, 1))
    tokens = TokenSet(refresh_token=SecretStr(TOKEN))
    assert TOKEN not in repr(tokens)


def test_expiry_warnings():
    meta = TokenSet(
        access_token=SecretStr("x"), access_expires_at=NOW + timedelta(days=5),
        long_lived_access=True,
    )  # fmt: skip
    assert meta.expiring_soon(NOW)
    assert not meta.expiring_soon(NOW - timedelta(days=10))
    google = TokenSet(refresh_token=SecretStr("x"))
    assert not google.expiring_soon(NOW)
    testing_app = TokenSet(refresh_token=SecretStr("x"), refresh_expires_at=NOW + timedelta(days=7))
    assert testing_app.expiring_soon(NOW)


# Credential provider


def provider_setup(**fake_options):
    clock = Clock(NOW)
    store = EncryptingTokenStore(InMemoryCiphertextBackend(), cipher())
    fake = FakeOAuthProvider(Platform.GOOGLE_ADS, clock, **fake_options)
    credentials = RefreshingCredentialProvider(
        store, {Platform.GOOGLE_ADS: fake}, lambda _: Platform.GOOGLE_ADS, clock
    )
    return clock, store, fake, credentials


def test_access_tokens_are_refreshed_and_cached_in_memory():
    clock, store, fake, credentials = provider_setup()
    store.save(CONNECTION, TokenSet(refresh_token=SecretStr(TOKEN)))
    first = credentials.access_token(CONNECTION)
    assert first.get_secret_value() == "fake-access-1"
    assert credentials.access_token(CONNECTION) == first
    assert fake.refreshes == [TOKEN]
    clock.advance(timedelta(minutes=56))  # inside the 5-minute margin
    assert credentials.access_token(CONNECTION).get_secret_value() == "fake-access-2"
    assert store.load(CONNECTION).access_token is None  # short-lived tokens never stored


def test_a_rotated_refresh_token_is_saved():
    _, store, fake, credentials = provider_setup(rotate_refresh=True)
    store.save(CONNECTION, TokenSet(refresh_token=SecretStr(TOKEN)))
    credentials.access_token(CONNECTION)
    assert store.load(CONNECTION).refresh_token.get_secret_value() == "fake-refresh-1"


def test_missing_or_expired_credentials_need_reauthorisation():
    clock, store, fake, credentials = provider_setup()
    with pytest.raises(ReauthorizationRequired):
        credentials.access_token(CONNECTION)
    store.save(
        CONNECTION,
        TokenSet(refresh_token=SecretStr(TOKEN), refresh_expires_at=NOW + timedelta(days=1)),
    )
    clock.advance(timedelta(days=2))
    with pytest.raises(ReauthorizationRequired):
        credentials.access_token(CONNECTION)
    assert fake.refreshes == []


def test_long_lived_tokens_are_used_until_they_expire():
    clock, store, _, credentials = provider_setup()
    store.save(
        CONNECTION,
        TokenSet(access_token=SecretStr("fake-meta-long-lived"),
                 access_expires_at=NOW + timedelta(days=60), long_lived_access=True),
    )  # fmt: skip
    assert credentials.access_token(CONNECTION).get_secret_value() == "fake-meta-long-lived"
    clock.advance(timedelta(days=61))
    with pytest.raises(ReauthorizationRequired):
        credentials.access_token(CONNECTION)


# OAuth helpers


def test_pkce_challenge_matches_rfc_7636():
    verifier = SecretStr("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_state_and_verifier_are_random_and_masked():
    states = {new_oauth_state().get_secret_value() for _ in range(100)}
    assert len(states) == 100
    verifier = new_pkce_verifier()
    assert 43 <= len(verifier.get_secret_value()) <= 128
    assert verifier.get_secret_value() not in repr(verifier)
