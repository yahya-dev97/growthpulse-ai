"""Clients, advertising accounts and their lifecycles.

An advertising account belongs to exactly one client for its whole life (no reassignment
in version 1). Its identity on the platform is ``(data_source, platform,
external_account_id)``, which is also how stored records name it, so every existing record
maps to at most one account. These rules are mirrored by database constraints in Phase B.

Nothing here holds a credential. A connection is referred to by ID only.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from growthpulse_ai.schema import IANA_TIMEZONES, ISO_4217_CODES, DataSource, Platform

type AccountIdentity = tuple[DataSource, Platform, str]
"""(data_source, platform, external_account_id): how records name their account."""


class AccountError(ValueError):
    """An account, client or assignment rule was broken."""


class AccountMismatchError(AccountError):
    """Data names an account outside the scope it was supplied for."""


class AccountStatus(StrEnum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    ARCHIVED = "archived"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


class ClientStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


# Archived is final: data is kept, read-only, and the account is never synced again.
ACCOUNT_TRANSITIONS: Mapping[AccountStatus, frozenset[AccountStatus]] = {
    AccountStatus.ONBOARDING: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.NEEDS_ATTENTION, AccountStatus.ARCHIVED}
    ),
    AccountStatus.ACTIVE: frozenset(
        {AccountStatus.PAUSED, AccountStatus.NEEDS_ATTENTION, AccountStatus.ARCHIVED}
    ),
    AccountStatus.PAUSED: frozenset({AccountStatus.ACTIVE, AccountStatus.ARCHIVED}),
    AccountStatus.NEEDS_ATTENTION: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.PAUSED, AccountStatus.ARCHIVED}
    ),
    AccountStatus.ARCHIVED: frozenset(),
}

# Revoked is final: reconnecting creates a new connection.
CONNECTION_TRANSITIONS: Mapping[ConnectionStatus, frozenset[ConnectionStatus]] = {
    ConnectionStatus.PENDING: frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.REVOKED}),
    ConnectionStatus.ACTIVE: frozenset({ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.REVOKED}),
    ConnectionStatus.NEEDS_REAUTH: frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.REVOKED}),
    ConnectionStatus.REVOKED: frozenset(),
}

SYNCABLE_ACCOUNT_STATUSES = frozenset({AccountStatus.ONBOARDING, AccountStatus.ACTIVE})


def check_account_transition(current: AccountStatus, new: AccountStatus) -> None:
    if new not in ACCOUNT_TRANSITIONS[current]:
        raise AccountError(f"An account cannot move from {current} to {new}")


def check_connection_transition(current: ConnectionStatus, new: ConnectionStatus) -> None:
    if new not in CONNECTION_TRANSITIONS[current]:
        raise AccountError(f"A connection cannot move from {current} to {new}")


@dataclass(frozen=True, slots=True)
class AccountContext:
    """One advertising account, as a connector or analysis is allowed to see it."""

    ad_account_id: UUID
    client_id: UUID
    data_source: DataSource
    platform: Platform
    external_account_id: str
    currency: str
    timezone: str
    display_name: str = ""
    status: AccountStatus = AccountStatus.ACTIVE
    connection_id: UUID | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.external_account_id) <= 255 or self.external_account_id.strip() != (
            self.external_account_id
        ):
            raise AccountError("external_account_id must be 1-255 characters, no outer spaces")
        if self.currency not in ISO_4217_CODES:
            raise AccountError("currency must be an ISO 4217 code")
        if self.timezone not in IANA_TIMEZONES:
            raise AccountError("timezone must be an IANA timezone name")
        if (self.data_source is DataSource.LIVE) != (self.connection_id is not None):
            raise AccountError("Live accounts need a connection; synthetic accounts have none")

    @property
    def identity(self) -> AccountIdentity:
        return (self.data_source, self.platform, self.external_account_id)

    @property
    def syncable(self) -> bool:
        return self.status in SYNCABLE_ACCOUNT_STATUSES


@dataclass(frozen=True, slots=True)
class ClientContext:
    """A client (brand) and every advertising account assigned to it."""

    client_id: UUID
    name: str
    data_source: DataSource
    accounts: tuple[AccountContext, ...]
    status: ClientStatus = ClientStatus.ACTIVE
    _by_identity: dict[AccountIdentity, AccountContext] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not 1 <= len(self.name.strip()) <= 200:
            raise AccountError("Client name must be 1-200 characters")
        by_identity: dict[AccountIdentity, AccountContext] = {}
        ids = set()
        for account in self.accounts:
            if account.client_id != self.client_id:
                raise AccountError("Every account must belong to this client")
            if account.data_source is not self.data_source:
                raise AccountError("A client's accounts must share its data source")
            if account.identity in by_identity or account.ad_account_id in ids:
                raise AccountError("An account appears twice in one client")
            by_identity[account.identity] = account
            ids.add(account.ad_account_id)
        object.__setattr__(self, "_by_identity", by_identity)

    def account_for(self, platform: Platform | str, external_account_id: str) -> AccountContext:
        """The account a record or finding names. Raises if it is not this client's."""
        key = (self.data_source, Platform(platform), external_account_id)
        try:
            return self._by_identity[key]
        except KeyError:
            raise AccountMismatchError(
                "Data names an advertising account that is not assigned to this client"
            ) from None

    @property
    def syncable_accounts(self) -> tuple[AccountContext, ...]:
        return tuple(account for account in self.accounts if account.syncable)


def check_unique_assignment(clients: Iterable[ClientContext]) -> None:
    """No account identity may belong to two clients. Version 1 has no reassignment."""
    seen: dict[AccountIdentity, UUID] = {}
    for client in clients:
        for account in client.accounts:
            owner = seen.setdefault(account.identity, client.client_id)
            if owner != client.client_id:
                raise AccountError("An advertising account is assigned to more than one client")


@dataclass(frozen=True, slots=True)
class DiscoveredAccount:
    """An account a provider connection can see. Discovery never assigns it to a client."""

    connection_id: UUID
    platform: Platform
    external_account_id: str
    display_name: str
    currency: str
    timezone: str
    is_manager: bool = False
    accessible: bool = True


def plan_account_additions(
    client: ClientContext,
    discovered: Iterable[DiscoveredAccount],
    selected: Iterable[str],
    *,
    assigned_elsewhere: frozenset[AccountIdentity] = frozenset(),
    new_id: Callable[[], UUID],
) -> tuple[AccountContext, ...]:
    """Turn the owner's selection from one connection's discovery into new client accounts.

    One connection may expose many accounts. Only selected, accessible, non-manager
    accounts not already assigned to any client are added; anything else is refused.
    """
    if client.data_source is not DataSource.LIVE:
        raise AccountError("Discovered accounts can only be added to a live client")
    if client.status is not ClientStatus.ACTIVE:
        raise AccountError("Accounts cannot be added to an archived client")
    available = {account.external_account_id: account for account in discovered}
    additions = []
    for external_id in dict.fromkeys(selected):
        found = available.get(external_id)
        if found is None:
            raise AccountError("A selected account was not found by this connection")
        if found.is_manager:
            raise AccountError("Manager accounts hold no campaigns and cannot be added")
        if not found.accessible:
            raise AccountError("A selected account is no longer accessible")
        identity = (DataSource.LIVE, found.platform, found.external_account_id)
        if identity in assigned_elsewhere or identity in client._by_identity:
            raise AccountError("A selected account is already assigned to a client")
        additions.append(
            AccountContext(
                ad_account_id=new_id(),
                client_id=client.client_id,
                data_source=DataSource.LIVE,
                platform=found.platform,
                external_account_id=found.external_account_id,
                currency=found.currency,
                timezone=found.timezone,
                display_name=found.display_name,
                status=AccountStatus.ONBOARDING,
                connection_id=found.connection_id,
            )
        )
    return tuple(additions)
