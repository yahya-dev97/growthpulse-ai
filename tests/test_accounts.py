"""Client and account models, lifecycles, assignment and discovery planning."""

from itertools import count
from uuid import UUID

import pytest
from pydantic import SecretStr

from fake_oauth import FakeDiscovery
from growthpulse_ai.accounts.legacy import LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.accounts.models import (
    ACCOUNT_TRANSITIONS,
    CONNECTION_TRANSITIONS,
    AccountContext,
    AccountError,
    AccountMismatchError,
    AccountStatus,
    ClientContext,
    ClientStatus,
    ConnectionStatus,
    DiscoveredAccount,
    check_account_transition,
    check_connection_transition,
    check_unique_assignment,
    plan_account_additions,
)
from growthpulse_ai.schema import DataSource, Platform
from multi_client import CLIENTS, stable_uuid

CLIENT_ID = stable_uuid("live-client")
CONNECTION = stable_uuid("connection-1")


def account(**overrides) -> AccountContext:
    fields = {
        "ad_account_id": stable_uuid("acct", str(overrides.get("external_account_id", "1"))),
        "client_id": CLIENT_ID,
        "data_source": DataSource.LIVE,
        "platform": Platform.GOOGLE_ADS,
        "external_account_id": "1234567890",
        "currency": "GBP",
        "timezone": "Europe/London",
        "connection_id": CONNECTION,
    }
    return AccountContext(**{**fields, **overrides})


def live_client(*accounts: AccountContext, status=ClientStatus.ACTIVE) -> ClientContext:
    return ClientContext(CLIENT_ID, "Live client", DataSource.LIVE, accounts, status)


def ids() -> "callable":
    counter = count(1)
    return lambda: UUID(int=next(counter))


# Account context


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"external_account_id": ""}, "external_account_id"),
        ({"external_account_id": " 123"}, "external_account_id"),
        ({"external_account_id": "x" * 256}, "external_account_id"),
        ({"currency": "gbp"}, "currency"),
        ({"timezone": "London"}, "timezone"),
        ({"connection_id": None}, "connection"),
        ({"data_source": DataSource.SYNTHETIC}, "connection"),
    ],
)
def test_account_context_rejects_bad_values(override: dict, message: str):
    with pytest.raises(AccountError, match=message):
        account(**override)


def test_account_identity_is_source_platform_and_external_id():
    assert account().identity == (DataSource.LIVE, Platform.GOOGLE_ADS, "1234567890")


@pytest.mark.parametrize("status", list(AccountStatus))
def test_only_onboarding_and_active_accounts_sync(status: AccountStatus):
    assert account(status=status).syncable == (
        status in {AccountStatus.ONBOARDING, AccountStatus.ACTIVE}
    )


# Lifecycles


def test_archived_accounts_and_revoked_connections_are_final():
    assert ACCOUNT_TRANSITIONS[AccountStatus.ARCHIVED] == frozenset()
    assert CONNECTION_TRANSITIONS[ConnectionStatus.REVOKED] == frozenset()


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (AccountStatus.ONBOARDING, AccountStatus.ACTIVE),
        (AccountStatus.ACTIVE, AccountStatus.PAUSED),
        (AccountStatus.PAUSED, AccountStatus.ACTIVE),
        (AccountStatus.ACTIVE, AccountStatus.NEEDS_ATTENTION),
        (AccountStatus.NEEDS_ATTENTION, AccountStatus.ACTIVE),
        (AccountStatus.PAUSED, AccountStatus.ARCHIVED),
    ],
)
def test_allowed_account_transitions(current, new):
    check_account_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (AccountStatus.ARCHIVED, AccountStatus.ACTIVE),
        (AccountStatus.PAUSED, AccountStatus.NEEDS_ATTENTION),
        (AccountStatus.ACTIVE, AccountStatus.ONBOARDING),
        (AccountStatus.ACTIVE, AccountStatus.ACTIVE),
    ],
)
def test_forbidden_account_transitions(current, new):
    with pytest.raises(AccountError):
        check_account_transition(current, new)


def test_connection_transitions():
    check_connection_transition(ConnectionStatus.PENDING, ConnectionStatus.ACTIVE)
    check_connection_transition(ConnectionStatus.ACTIVE, ConnectionStatus.NEEDS_REAUTH)
    check_connection_transition(ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ACTIVE)
    with pytest.raises(AccountError):
        check_connection_transition(ConnectionStatus.REVOKED, ConnectionStatus.ACTIVE)
    with pytest.raises(AccountError):
        check_connection_transition(ConnectionStatus.PENDING, ConnectionStatus.NEEDS_REAUTH)


# Client context


def test_client_rejects_accounts_of_another_client_or_source():
    with pytest.raises(AccountError, match="belong"):
        live_client(account(client_id=stable_uuid("other")))
    synthetic = CLIENTS[1].accounts[0]
    with pytest.raises(AccountError):
        ClientContext(synthetic.client_id, "Mixed", DataSource.LIVE, (synthetic,))


def test_client_rejects_the_same_account_twice():
    first = account()
    with pytest.raises(AccountError, match="twice"):
        live_client(first, account(ad_account_id=stable_uuid("another-row")))


def test_client_name_is_required():
    with pytest.raises(AccountError, match="name"):
        ClientContext(CLIENT_ID, "  ", DataSource.LIVE, ())


def test_account_for_resolves_or_refuses():
    client = live_client(account())
    assert client.account_for("google_ads", "1234567890").external_account_id == "1234567890"
    with pytest.raises(AccountMismatchError):
        client.account_for(Platform.META_ADS, "1234567890")
    with pytest.raises(AccountMismatchError):
        client.account_for(Platform.GOOGLE_ADS, "999")


def test_syncable_accounts_skip_paused_and_archived():
    client = live_client(
        account(),
        account(external_account_id="2", status=AccountStatus.PAUSED),
        account(external_account_id="3", status=AccountStatus.ARCHIVED),
        account(external_account_id="4", status=AccountStatus.ONBOARDING),
    )
    assert [a.external_account_id for a in client.syncable_accounts] == ["1234567890", "4"]


def test_an_account_cannot_belong_to_two_clients():
    first = live_client(account())
    other_id = stable_uuid("second-client")
    second = ClientContext(other_id, "Second", DataSource.LIVE, (account(client_id=other_id),))
    with pytest.raises(AccountError, match="more than one client"):
        check_unique_assignment([first, second])


def test_legacy_client_is_synthetic_with_two_accounts():
    assert LEGACY_SYNTHETIC_CLIENT.data_source is DataSource.SYNTHETIC
    assert [a.platform for a in LEGACY_SYNTHETIC_CLIENT.accounts] == [
        Platform.GOOGLE_ADS,
        Platform.META_ADS,
    ]
    assert all(a.connection_id is None for a in LEGACY_SYNTHETIC_CLIENT.accounts)


# Discovery: one connection, many accounts


def discovered(external_id: str, **overrides) -> DiscoveredAccount:
    fields = {
        "connection_id": CONNECTION,
        "platform": Platform.GOOGLE_ADS,
        "external_account_id": external_id,
        "display_name": f"Account {external_id}",
        "currency": "GBP",
        "timezone": "Europe/London",
    }
    return DiscoveredAccount(**{**fields, **overrides})


DISCOVERY = FakeDiscovery(
    [
        discovered("111"),
        discovered("222", currency="EUR", timezone="Europe/Dublin"),
        discovered("333"),
        discovered("900", is_manager=True),
        discovered("444", accessible=False),
        discovered("555", connection_id=stable_uuid("connection-2")),
    ]
)


def test_one_connection_discovers_several_accounts():
    found = DISCOVERY.list_accounts(CONNECTION, SecretStr("fake-access"))
    assert [a.external_account_id for a in found] == ["111", "222", "333", "900", "444"]


def test_selected_accounts_from_one_connection_join_the_client_as_onboarding():
    found = DISCOVERY.list_accounts(CONNECTION, SecretStr("fake-access"))
    added = plan_account_additions(live_client(), found, ["111", "222", "111"], new_id=ids())
    assert [(a.external_account_id, a.currency) for a in added] == [
        ("111", "GBP"),
        ("222", "EUR"),
    ]
    assert all(a.status is AccountStatus.ONBOARDING for a in added)
    assert all(a.connection_id == CONNECTION and a.client_id == CLIENT_ID for a in added)
    assert len({a.ad_account_id for a in added}) == 2


@pytest.mark.parametrize(
    ("selected", "message"),
    [
        (["900"], "Manager"),
        (["444"], "no longer accessible"),
        (["555"], "not found"),
        (["nope"], "not found"),
    ],
)
def test_unusable_discovered_accounts_are_refused(selected: list[str], message: str):
    found = DISCOVERY.list_accounts(CONNECTION, SecretStr("fake-access"))
    with pytest.raises(AccountError, match=message):
        plan_account_additions(live_client(), found, selected, new_id=ids())


def test_an_account_assigned_anywhere_cannot_be_added_again():
    found = DISCOVERY.list_accounts(CONNECTION, SecretStr("fake-access"))
    elsewhere = frozenset({(DataSource.LIVE, Platform.GOOGLE_ADS, "111")})
    with pytest.raises(AccountError, match="already assigned"):
        plan_account_additions(
            live_client(), found, ["111"], assigned_elsewhere=elsewhere, new_id=ids()
        )
    own = live_client(account(external_account_id="333"))
    with pytest.raises(AccountError, match="already assigned"):
        plan_account_additions(own, found, ["333"], new_id=ids())


def test_discovered_accounts_only_join_active_live_clients():
    found = DISCOVERY.list_accounts(CONNECTION, SecretStr("fake-access"))
    with pytest.raises(AccountError, match="archived"):
        plan_account_additions(
            live_client(status=ClientStatus.ARCHIVED), found, ["111"], new_id=ids()
        )
    with pytest.raises(AccountError, match="live client"):
        plan_account_additions(CLIENTS[1], found, ["111"], new_id=ids())
