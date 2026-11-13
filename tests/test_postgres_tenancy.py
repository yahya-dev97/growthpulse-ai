"""Milestone 7a.2 on a disposable PostgreSQL server: tenant isolation between organizations.

Skipped unless GROWTHPULSE_TEST_DATABASE_URL is set (see test_postgres_integration.py).

Two organizations are built through the real pipeline: the internal one holding the
fictional legacy client, and a customer organization holding another fictional client.
Each also gets a provider connection, discovered accounts, fake encrypted bytes standing in
for a stored credential, an experiment proposal, an owner action and audit events. Then
every view, table, token function and owner action is exercised as each role, inside and
outside each organization's context. No credential, token or real account is involved:
the "credentials" are random bytes that were never a token.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg import sql

from growthpulse_ai.accounts.legacy import LEGACY_CLIENT_ID, LEGACY_WORKSPACE_ID
from growthpulse_ai.connectors.factory import SyntheticConnectorFactory
from growthpulse_ai.persistence import read
from growthpulse_ai.persistence.context import (
    SignedInRequest,
    begin_job,
    begin_request,
    session_token_hash,
)
from growthpulse_ai.persistence.errors import ClientNotFoundError, TenantScopeError
from growthpulse_ai.persistence.store import ExperimentProposal, record_experiment_proposal
from growthpulse_ai.persistence.tenancy import due_accounts, load_client
from growthpulse_ai.pipeline.persisted_run import SYNTHETIC_WINDOW, run_client
from growthpulse_ai.schema import DataSource
from m4_frozen import run_milestone_4_pipeline
from multi_client import CLIENTS, END, START
from pg_helpers import (
    ALL_SCHEMAS,
    API_ROLES,
    AUTHENTICATOR,
    DASHBOARD,
    M4_MIGRATIONS,
    M5_MIGRATIONS,
    M7A_MIGRATIONS,
    MANAGER,
    OWNER,
    REVERT_M5,
    REVERT_M7A,
    TEST_URL,
    TOKEN_MANAGER,
    WRITER,
    admin,
    apply,
    as_role,
    drop_everything,
    enable_test_logins,
    owner,
    prepare_server,
    refuse_hosted_database,
    request_rows,
    rows,
    run_sql_file,
    scalar,
    sign_in,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_URL, reason="GROWTHPULSE_TEST_DATABASE_URL is not set"),
]

M4_REPORT_ID = "feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601"
WORKSPACE_B = UUID("0b7a4c1e-3f52-4e8a-9a51-6a2b7f4c9d10")
CLIENT_B = CLIENTS[1]  # exampleclinic-ie, a fictional client
APP_ROLES = (WRITER, DASHBOARD, MANAGER, TOKEN_MANAGER, AUTHENTICATOR)
NOT_PERMITTED = pg_errors.InsufficientPrivilege


@pytest.fixture(scope="module", autouse=True)
def server():
    refuse_hosted_database()
    prepare_server()
    drop_everything()
    yield
    drop_everything()


# State builders


def writer() -> psycopg.Connection:
    return as_role(WRITER)


def milestone_4_state() -> None:
    """Migrations 1-3 and two frozen Milestone 4 runs: the state on Supabase today."""
    drop_everything()
    apply(M4_MIGRATIONS)
    enable_test_logins()
    for _ in range(2):
        run_milestone_4_pipeline(writer, START, END)


def full_upgrade() -> None:
    apply(M5_MIGRATIONS)
    apply(M7A_MIGRATIONS)
    enable_test_logins()


def seed_customer_organization() -> None:
    """Workspace B (a customer organization) with one fictional client and its accounts."""
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.workspaces (workspace_id, name) values (%s, 'Org B')",
            (WORKSPACE_B,),
        )
        connection.execute(
            "insert into growthpulse.clients (client_id, workspace_id, name, data_source) "
            "values (%s, %s, %s, 'synthetic')",
            (CLIENT_B.client_id, WORKSPACE_B, CLIENT_B.name),
        )
        for account in CLIENT_B.accounts:
            connection.execute(
                "insert into growthpulse.ad_accounts (ad_account_id, client_id, data_source, "
                "platform, external_account_id, currency, timezone, status) "
                "values (%s, %s, 'synthetic', %s, %s, %s, %s, 'active')",
                (account.ad_account_id, CLIENT_B.client_id, account.platform.value,
                 account.external_account_id, account.currency, account.timezone),
            )  # fmt: skip
            connection.execute(
                "insert into growthpulse.account_sync_config (ad_account_id, frequency, "
                "backfill_start_date) values (%s, 'manual', date '2026-10-01')",
                (account.ad_account_id,),
            )
            connection.execute(
                "insert into growthpulse.account_sync_state (ad_account_id, next_due_at) "
                "values (%s, now())",
                (account.ad_account_id,),
            )


def add_connection(workspace_id: UUID) -> UUID:
    """A pending connection with one discovered account, as discovery would leave it."""
    with admin() as connection:
        connection_id = connection.execute(
            "insert into growthpulse.provider_connections (workspace_id, provider, "
            "provider_subject_id) values (%s, 'google_ads', %s) returning connection_id",
            (workspace_id, f"subject-{uuid4().hex[:8]}"),
        ).fetchone()[0]
        connection.execute(
            "insert into growthpulse.discovered_ad_accounts (connection_id, platform, "
            "external_account_id, currency, timezone) "
            "values (%s, 'google_ads', %s, 'GBP', 'Europe/London')",
            (connection_id, f"discovered-{uuid4().hex[:8]}"),
        )
    return connection_id


def add_quarantined_row(client_id: UUID, ad_account_id: UUID | None) -> None:
    """One quarantined row: of an account sync, or (no account) of a combined run."""
    with admin() as connection:
        run_id = connection.execute(
            "select run_id from growthpulse.pipeline_runs where client_id = %s "
            "order by started_at limit 1",
            (client_id,),
        ).fetchone()[0]
        connection.execute(
            "insert into growthpulse.quarantined_records (quarantine_id, data_source, "
            "connector, row_index, issue_codes, issues, payload_sha256, first_seen_run_id, "
            "last_seen_run_id, ad_account_id) values (%s, 'synthetic', 'synthetic', 0, "
            "array['missing_field'], '[]', %s, %s, %s, %s)",
            (secrets.token_hex(32), secrets.token_hex(32), run_id, run_id, ad_account_id),
        )


def fake_credential() -> tuple:
    """Random bytes in the stored shape. Never a token: nothing can decrypt them."""
    return ("refresh", 1, 1, secrets.token_bytes(12), secrets.token_bytes(48), None)


def store_fake_credential(kind: str, scope_id: UUID, connection_id: UUID) -> None:
    with as_role(TOKEN_MANAGER) as connection, connection.transaction(), connection.cursor() as cur:
        begin_job(cur, kind, scope_id)
        cur.execute(
            "select growthpulse_secrets.store_credential(%s, %s, %s::smallint, "
            "%s::smallint, %s, %s, %s)",
            (connection_id, *fake_credential()),
        )


def propose(client_id: UUID) -> UUID:
    with admin() as connection:
        report_id, finding_id = connection.execute(
            "select f.latest_report_id, f.finding_id from growthpulse.findings as f "
            "where f.client_id = %s order by f.finding_id limit 1",
            (client_id,),
        ).fetchone()
    proposal = ExperimentProposal(
        data_source=DataSource.SYNTHETIC, client_id=client_id, report_id=report_id,
        finding_id=finding_id, title="Test a narrower audience",
        hypothesis="A narrower audience lowers cost per platform-attributed conversion.",
        proposed_change="A person runs a split test manually.",
        primary_metric="cost_per_platform_attributed_conversion",
    )  # fmt: skip
    with writer() as connection:
        return record_experiment_proposal(connection, proposal)


def manager_call(request: SignedInRequest | None, function: str, *args):
    """Call an owner-action function as the manager, in a request context if given."""
    with as_role(MANAGER) as connection, connection.transaction(), connection.cursor() as cursor:
        if request is not None:
            begin_request(cursor, request)
        placeholders = ", ".join(["%s"] * len(args))
        return cursor.execute(f"select {function}({placeholders})", args).fetchone()[0]  # noqa: S608


def build_two_organizations() -> dict:
    milestone_4_state()
    full_upgrade()
    seed_customer_organization()
    for client_id in (LEGACY_CLIENT_ID, CLIENT_B.client_id):
        run_client(client_id, writer, factory=SyntheticConnectorFactory(),
                   window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    add_quarantined_row(LEGACY_CLIENT_ID, None)
    add_quarantined_row(CLIENT_B.client_id, CLIENT_B.accounts[0].ad_account_id)
    connection_a, connection_b = add_connection(LEGACY_WORKSPACE_ID), add_connection(WORKSPACE_B)
    store_fake_credential("connection", connection_a, connection_a)
    store_fake_credential("connection", connection_b, connection_b)
    proposal_a, proposal_b = propose(LEGACY_CLIENT_ID), propose(CLIENT_B.client_id)
    owner_a, owner_b = sign_in(LEGACY_WORKSPACE_ID), sign_in(WORKSPACE_B)
    manager_call(owner_a, "growthpulse.decide_experiment", proposal_a, "approved", "ok")
    manager_call(owner_b, "growthpulse.decide_experiment", proposal_b, "rejected", "no")
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "connection_a": connection_a,
        "connection_b": connection_b,
        "proposal_a": proposal_a,
        "proposal_b": proposal_b,
    }


@pytest.fixture(scope="module")
def orgs() -> dict:
    return build_two_organizations()


def clients_of(workspace_id: UUID) -> set[UUID]:
    return {
        row[0]
        for row in rows(
            "select client_id from growthpulse.clients where workspace_id = %s", (workspace_id,)
        )
    }


def views() -> list[str]:
    return [
        row[0]
        for row in rows(
            "select viewname from pg_views where schemaname = 'growthpulse_read' order by 1"
        )
    ]


def view_rows(request: SignedInRequest, view: str) -> list[tuple]:
    return request_rows(request, f"select * from growthpulse_read.{view}")  # noqa: S608


def view_columns(view: str) -> list[str]:
    return [
        row[0]
        for row in rows(
            "select column_name from information_schema.columns "
            "where table_schema = 'growthpulse_read' and table_name = %s order by ordinal_position",
            (view,),
        )
    ]


# 1. Historical report IDs and fingerprints


FINGERPRINTS = M4_MIGRATIONS[0].parents[1] / "verification" / "milestone_4_fingerprints.sql"


def fingerprints() -> dict[str, tuple[str, str]]:
    results = {}
    with admin() as connection:
        for statement in FINGERPRINTS.read_text().split(";\n"):
            if statement.strip():
                cursor = connection.execute(statement)
                if cursor.description:
                    results.update({name: (n, digest) for name, n, digest in cursor.fetchall()})
    return results


def without_runs(prints: dict) -> dict:
    return {k: v for k, v in prints.items() if k != "01 pipeline_runs"}


def test_milestone_4_report_and_fingerprints_survive_the_upgrade_a_run_and_both_reverts():
    milestone_4_state()
    before = fingerprints()
    assert before["08 report id"] == ("1", M4_REPORT_ID)
    assert before["02 ad_performance_records"][0] == "300"
    assert before["05 findings"][0] == "19"

    full_upgrade()
    assert fingerprints() == before
    # A synthetic run under the new tenant policies adds runs and changes nothing else.
    run_client(LEGACY_CLIENT_ID, writer, factory=SyntheticConnectorFactory(),
               window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    after_run = fingerprints()
    assert after_run["08 report id"] == ("1", M4_REPORT_ID)
    assert without_runs(after_run) == without_runs(before)

    run_sql_file(REVERT_M7A)
    assert fingerprints() == after_run
    run_sql_file(REVERT_M5)
    assert without_runs(fingerprints()) == without_runs(before)


def test_the_m4_report_is_visible_to_its_own_organization_only(orgs):
    query = "select report_id from growthpulse_read.analytics_reports"
    assert (M4_REPORT_ID,) in request_rows(orgs["owner_a"], query)
    assert (M4_REPORT_ID,) not in request_rows(orgs["owner_b"], query)


# 2. Dashboard views


def test_every_view_is_a_tenant_filtered_security_barrier(orgs):
    found = rows(
        "select c.relname, c.reloptions::text, pg_get_viewdef(c.oid) from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'growthpulse_read' and c.relkind = 'v' order by 1"
    )
    assert len(found) == 18  # the 17 Milestone 5 views and audit_events
    for name, options, definition in found:
        assert "security_barrier=true" in options, name
        assert "security_invoker=false" in options, name
        assert "growthpulse_auth.request_" in definition, name


@pytest.mark.parametrize("role", [DASHBOARD, "admin"])
def test_no_view_returns_a_row_without_a_request_context(orgs, role):
    connection = as_role(DASHBOARD) if role == DASHBOARD else admin()
    with connection:
        for view in views():
            count = connection.execute(
                sql.SQL("select count(*) from {}").format(sql.Identifier("growthpulse_read", view))
            ).fetchone()[0]
            assert count == 0, view


def test_each_organization_sees_only_its_own_rows_in_every_view(orgs):
    a_clients, b_clients = clients_of(LEGACY_WORKSPACE_ID), clients_of(WORKSPACE_B)
    for view in views():
        seen_a, seen_b = view_rows(orgs["owner_a"], view), view_rows(orgs["owner_b"], view)
        assert seen_a and seen_b, view  # both organizations have data in every view
        assert not set(map(repr, seen_a)) & set(map(repr, seen_b)), view
        columns = view_columns(view)
        if "client_id" in columns:
            index = columns.index("client_id")
            assert {r[index] for r in seen_a} <= a_clients, view
            assert {r[index] for r in seen_b} <= b_clients, view
        if "workspace_id" in columns:
            index = columns.index("workspace_id")
            assert {r[index] for r in seen_a} == {LEGACY_WORKSPACE_ID}, view
            assert {r[index] for r in seen_b} == {WORKSPACE_B}, view


def test_views_without_scope_columns_are_still_scoped(orgs):
    history = "select proposal_id from growthpulse_read.experiment_status_history"
    assert {r[0] for r in request_rows(orgs["owner_a"], history)} == {orgs["proposal_a"]}
    assert {r[0] for r in request_rows(orgs["owner_b"], history)} == {orgs["proposal_b"]}
    discovered = "select connection_id from growthpulse_read.discovered_ad_accounts"
    assert {r[0] for r in request_rows(orgs["owner_a"], discovered)} == {orgs["connection_a"]}
    actions = "select target_id from growthpulse_read.owner_actions"
    assert {r[0] for r in request_rows(orgs["owner_b"], actions)} == {str(orgs["proposal_b"])}


def test_viewers_and_analysts_see_only_their_granted_clients(orgs):
    viewer_none = sign_in(LEGACY_WORKSPACE_ID, "viewer")
    viewer_one = sign_in(WORKSPACE_B, "viewer", clients=[CLIENT_B.client_id])
    analyst = sign_in(WORKSPACE_B, "analyst", clients=[CLIENT_B.client_id], access="analyse")
    for view in ("clients", "findings", "analytics_reports", "campaign_daily_delivery"):
        assert view_rows(viewer_none, view) == [], view
        assert view_rows(viewer_one, view) == view_rows(orgs["owner_b"], view), view
        assert view_rows(analyst, view) == view_rows(orgs["owner_b"], view), view
    # Organization-level views are for owners and admins only.
    for view in ("provider_connections", "discovered_ad_accounts", "owner_actions", "audit_events"):
        assert view_rows(viewer_one, view) == [], view
        assert view_rows(analyst, view) == [], view
    admin_b = sign_in(WORKSPACE_B, "admin")
    assert view_rows(admin_b, "provider_connections") == view_rows(
        orgs["owner_b"], "provider_connections"
    )


def test_a_grant_can_only_name_a_client_of_the_same_organization(orgs):
    with pytest.raises(pg_errors.ForeignKeyViolation):
        sign_in(WORKSPACE_B, "viewer", clients=[LEGACY_CLIENT_ID])
    with pytest.raises(pg_errors.CheckViolation):
        sign_in(WORKSPACE_B, "admin", clients=[CLIENT_B.client_id])


def test_dashboard_read_helpers_need_a_signed_in_request(orgs):
    with as_role(DASHBOARD) as connection:
        report = read.latest_report(connection, orgs["owner_b"], "synthetic")
        runs = read.recent_runs(connection, orgs["owner_b"], limit=500)
    assert report["report_id"] != M4_REPORT_ID
    b_runs = scalar(
        "select count(*) from growthpulse.pipeline_runs where client_id = %s",
        (CLIENT_B.client_id,),
    )
    assert len(runs) == b_runs


# 3. Sessions: the request boundary


def refused(request: SignedInRequest, role: str = DASHBOARD) -> bool:
    try:
        request_rows(request, "select 1", role=role)
    except TenantScopeError:
        return True
    return False


def test_begin_request_refuses_every_invalid_session_the_same_way(orgs):
    good = sign_in(WORKSPACE_B)
    assert not refused(good)
    assert refused(SignedInRequest(secrets.token_bytes(32), WORKSPACE_B, uuid4()))  # unknown
    assert refused(SignedInRequest(good.session_token, LEGACY_WORKSPACE_ID, uuid4()))  # not member
    assert refused(SignedInRequest(good.session_token, uuid4(), uuid4()))  # no organization

    expired, revoked, disabled, removed = (sign_in(WORKSPACE_B, "viewer") for _ in range(4))
    with admin() as connection:
        connection.execute(
            "update growthpulse_auth.sessions set created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' where token_hash = %s",
            (session_token_hash(expired.session_token),),
        )
        connection.execute(
            "update growthpulse_auth.sessions set revoked_at = now() where token_hash = %s",
            (session_token_hash(revoked.session_token),),
        )
        for request, statement in (
            (disabled, "update growthpulse_auth.users set status = 'disabled'"),
            (removed, "update growthpulse_auth.memberships set status = 'removed', "
                      "removed_at = now()"),
        ):  # fmt: skip
            target = "user_id" if "users" in statement else "user_id"
            connection.execute(
                statement + f" where {target} = (select user_id from growthpulse_auth.sessions "  # noqa: S608
                "where token_hash = %s)",
                (session_token_hash(request.session_token),),
            )
    for request in (expired, revoked, disabled, removed):
        assert refused(request)


def test_a_suspended_organization_refuses_requests_and_jobs(orgs):
    request = sign_in(WORKSPACE_B)
    with admin() as connection:
        connection.execute(
            "update growthpulse_auth.organizations set status = 'suspended' "
            "where workspace_id = %s",
            (WORKSPACE_B,),
        )
    try:
        assert refused(request)
        with writer() as connection, pytest.raises(ClientNotFoundError):
            load_client(connection, CLIENT_B.client_id)
    finally:
        with admin() as connection:
            connection.execute(
                "update growthpulse_auth.organizations set status = 'active' "
                "where workspace_id = %s",
                (WORKSPACE_B,),
            )


def test_removing_a_membership_takes_effect_at_the_next_statement(orgs):
    request = sign_in(WORKSPACE_B)
    with as_role(DASHBOARD) as connection, connection.transaction(), connection.cursor() as cur:
        begin_request(cur, request)
        assert cur.execute("select count(*) from growthpulse_read.clients").fetchone()[0] == 1
        with admin() as other:
            other.execute(
                "update growthpulse_auth.memberships set status = 'removed', removed_at = now() "
                "where user_id = (select user_id from growthpulse_auth.sessions "
                "where token_hash = %s)",
                (session_token_hash(request.session_token),),
            )
        assert cur.execute("select count(*) from growthpulse_read.clients").fetchone()[0] == 0


def test_session_functions_belong_to_the_authenticator_only(orgs):
    user_id, token = uuid4(), secrets.token_bytes(32)
    with admin() as connection:
        connection.execute("insert into growthpulse_auth.users (user_id) values (%s)", (user_id,))
        connection.execute(
            "insert into growthpulse_auth.memberships (workspace_id, user_id, role) "
            "values (%s, %s, 'viewer')",
            (WORKSPACE_B, user_id),
        )
    expires = datetime.now(UTC) + timedelta(hours=1)
    for role in (WRITER, DASHBOARD, MANAGER, TOKEN_MANAGER):
        with as_role(role) as connection, pytest.raises(NOT_PERMITTED):
            connection.execute(
                "select growthpulse_auth.create_session(%s, %s, %s)",
                (user_id, session_token_hash(token), expires),
            )
    with as_role(AUTHENTICATOR) as connection:
        connection.execute(
            "select growthpulse_auth.create_session(%s, %s, %s)",
            (user_id, session_token_hash(token), expires),
        )
        with pytest.raises(pg_errors.CheckViolation):  # at most 12 hours
            connection.execute(
                "select growthpulse_auth.create_session(%s, %s, now() + interval '13 hours')",
                (user_id, session_token_hash(secrets.token_bytes(32))),
            )
    request = SignedInRequest(token, WORKSPACE_B, uuid4())
    assert not refused(request)
    with as_role(AUTHENTICATOR) as connection:
        assert connection.execute(
            "select growthpulse_auth.revoke_session(%s)", (session_token_hash(token),)
        ).fetchone()[0]
        # The authenticator can create and revoke sessions but read no tenant data.
        with pytest.raises(NOT_PERMITTED):
            connection.execute("select * from growthpulse_read.clients")
    assert refused(request)


# 4. Context forgery, replay and transaction pooling


def captured_context(request: SignedInRequest) -> tuple[str, str]:
    return request_rows(
        request,
        "select current_setting('growthpulse.context'), current_setting('growthpulse.context_mac')",
    )[0]


@pytest.mark.parametrize("role", [DASHBOARD, WRITER, TOKEN_MANAGER, MANAGER])
def test_a_replayed_context_from_another_transaction_is_refused(orgs, role):
    context, mac = captured_context(orgs["owner_b"])
    with as_role(role) as connection, connection.transaction():
        connection.execute("select set_config('growthpulse.context', %s, true)", (context,))
        connection.execute("select set_config('growthpulse.context_mac', %s, true)", (mac,))
        with pytest.raises(NOT_PERMITTED):
            connection.execute("select growthpulse_auth.context_workspace()" if role != MANAGER
                               else "select growthpulse.rename_client(%s, 'x')",
                               () if role != MANAGER else (CLIENT_B.client_id,))  # fmt: skip


def test_a_forged_context_without_a_valid_mac_is_refused(orgs):
    forged = f"v1;request;{uuid4()};{WORKSPACE_B};;owner;{uuid4()};{uuid4()}"
    with as_role(DASHBOARD) as connection, connection.transaction():
        connection.execute("select set_config('growthpulse.context', %s, true)", (forged,))
        connection.execute("select set_config('growthpulse.context_mac', repeat('0', 64), true)")
        with pytest.raises(NOT_PERMITTED):
            connection.execute("select * from growthpulse_read.clients")
    with as_role(DASHBOARD) as connection, connection.transaction():
        connection.execute("select set_config('growthpulse.context', %s, true)", (forged,))
        with pytest.raises(NOT_PERMITTED):  # no MAC at all
            connection.execute("select * from growthpulse_read.clients")


def test_an_edited_context_is_refused(orgs):
    with as_role(DASHBOARD) as connection, connection.transaction(), connection.cursor() as cur:
        begin_request(cur, orgs["owner_a"])
        context = cur.execute("select current_setting('growthpulse.context')").fetchone()[0]
        edited = context.replace(str(LEGACY_WORKSPACE_ID), str(WORKSPACE_B))
        cur.execute("select set_config('growthpulse.context', %s, true)", (edited,))
        with pytest.raises(NOT_PERMITTED):
            cur.execute("select * from growthpulse_read.clients")


def test_a_session_level_setting_never_carries_a_context_into_later_transactions(orgs):
    context, mac = captured_context(orgs["owner_b"])
    with as_role(DASHBOARD) as connection:
        connection.execute("select set_config('growthpulse.context', %s, false)", (context,))
        connection.execute("select set_config('growthpulse.context_mac', %s, false)", (mac,))
        with pytest.raises(NOT_PERMITTED):
            connection.execute("select * from growthpulse_read.clients")


def test_one_pooled_connection_never_leaks_a_context_between_transactions(orgs):
    """A transaction pooler hands one server connection to many clients in turn."""
    with as_role(DASHBOARD) as pooled:
        with pooled.transaction(), pooled.cursor() as cursor:
            begin_request(cursor, orgs["owner_a"])
            first = cursor.execute("select client_id from growthpulse_read.clients").fetchall()
        assert first == [(LEGACY_CLIENT_ID,)]
        # The next client of the pool forgets begin_request: it must see nothing.
        assert pooled.execute("select count(*) from growthpulse_read.clients").fetchone()[0] == 0
        assert pooled.execute("select current_setting('growthpulse.context', true)").fetchone()[
            0
        ] in ("", None)
        with pooled.transaction(), pooled.cursor() as cursor:
            begin_request(cursor, orgs["owner_b"])
            second = cursor.execute("select client_id from growthpulse_read.clients").fetchall()
        assert second == [(CLIENT_B.client_id,)]
        # A failed transaction also leaves nothing behind.
        with (
            pytest.raises(pg_errors.DivisionByZero),
            pooled.transaction(),
            pooled.cursor() as cursor,
        ):
            begin_request(cursor, orgs["owner_a"])
            cursor.execute("select 1 / 0")
        assert pooled.execute("select count(*) from growthpulse_read.clients").fetchone()[0] == 0


def test_one_transaction_holds_one_context_only(orgs):
    with as_role(DASHBOARD) as connection, connection.transaction(), connection.cursor() as cur:
        begin_request(cur, orgs["owner_a"])
        with pytest.raises(NOT_PERMITTED):
            cur.execute(
                "select growthpulse_auth.begin_request(%s, %s, %s)",
                (orgs["owner_b"].session_token, WORKSPACE_B, uuid4()),
            )


def test_the_context_key_is_unreadable_by_every_app_role(orgs):
    for role in APP_ROLES:
        with as_role(role) as connection, pytest.raises(NOT_PERMITTED):
            connection.execute("select * from growthpulse_auth.context_keys")
        with as_role(role) as connection, pytest.raises(NOT_PERMITTED):
            connection.execute("select growthpulse_auth.context_mac('x')")


# 5. The writer (pipeline)


def writer_tables() -> list[str]:
    return [
        row[0]
        for row in rows(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'growthpulse' and c.relkind = 'r' "
            "and has_table_privilege(%s, c.oid, 'select') order by 1",
            (WRITER,),
        )
    ]


def test_the_writer_sees_nothing_without_a_job_context(orgs):
    tables = writer_tables()
    assert len(tables) == 15
    with writer() as connection:
        for table in tables:
            count = connection.execute(
                sql.SQL("select count(*) from {}").format(sql.Identifier("growthpulse", table))
            ).fetchone()[0]
            assert count == 0, table


SCOPED_COUNT = {
    "workspaces": "workspace_id = any(%(ws)s)",
    "clients": "client_id = any(%(c)s)",
    "ad_accounts": "client_id = any(%(c)s)",
    "pipeline_runs": "client_id = any(%(c)s)",
    "analytics_reports": "client_id = any(%(c)s)",
    "findings": "client_id = any(%(c)s)",
    "experiment_proposals": "client_id = any(%(c)s)",
    "analytics_report_breakdowns": "client_id = any(%(c)s)",
    "provider_connections": "workspace_id = any(%(ws)s)",
    "ad_performance_records": "ad_account_id in (select ad_account_id from "
    "growthpulse.ad_accounts where client_id = any(%(c)s))",
    "account_sync_config": "ad_account_id in (select ad_account_id from "
    "growthpulse.ad_accounts where client_id = any(%(c)s))",
    "account_sync_state": "ad_account_id in (select ad_account_id from "
    "growthpulse.ad_accounts where client_id = any(%(c)s))",
    "discovered_ad_accounts": "connection_id in (select connection_id from "
    "growthpulse.provider_connections where workspace_id = any(%(ws)s))",
    "experiment_status_events": "proposal_id in (select proposal_id from "
    "growthpulse.experiment_proposals where client_id = any(%(c)s))",
    "quarantined_records": "coalesce((select client_id from growthpulse.ad_accounts a where "
    "a.ad_account_id = t.ad_account_id), (select client_id from growthpulse.pipeline_runs r "
    "where r.run_id = t.last_seen_run_id)) = any(%(c)s)",
}


@pytest.mark.parametrize(
    ("kind", "workspace"), [("workspace", "a"), ("workspace", "b"), ("client", "b")]
)
def test_a_job_sees_exactly_its_own_rows_in_every_table(orgs, kind, workspace):
    ws = LEGACY_WORKSPACE_ID if workspace == "a" else WORKSPACE_B
    scope_id = ws if kind == "workspace" else CLIENT_B.client_id
    params = {"ws": [ws], "c": list(clients_of(ws))}
    for table in writer_tables():
        with writer() as connection, connection.transaction(), connection.cursor() as cursor:
            begin_job(cursor, kind, scope_id)
            seen = cursor.execute(
                sql.SQL("select count(*) from {} as t").format(sql.Identifier("growthpulse", table))
            ).fetchone()[0]
        expected = scalar(
            f"select count(*) from growthpulse.{table} as t where {SCOPED_COUNT[table]}",  # noqa: S608
            params,
        )
        assert seen == expected, table


def test_a_job_cannot_write_another_organizations_rows(orgs):
    b_account = CLIENT_B.accounts[0].ad_account_id
    with writer() as connection, connection.transaction(), connection.cursor() as cursor:
        begin_job(cursor, "workspace", LEGACY_WORKSPACE_ID)
        cursor.execute(
            "update growthpulse.account_sync_state set last_error_code = 'x' "
            "where ad_account_id = %s",
            (b_account,),
        )
        assert cursor.rowcount == 0
        cursor.execute(
            "update growthpulse.findings set last_seen_at = now() where client_id = %s",
            (CLIENT_B.client_id,),
        )
        assert cursor.rowcount == 0
    for statement, params in (
        (
            "insert into growthpulse.pipeline_runs (run_id, data_source, triggered_by, "
            "client_id) values (gen_random_uuid(), 'synthetic', 'test', %s)",
            (CLIENT_B.client_id,),
        ),
        (
            "insert into growthpulse.discovered_ad_accounts (connection_id, platform, "
            "external_account_id, currency, timezone) values (%s, 'google_ads', 'x', 'GBP', "
            "'Europe/London')",
            (orgs["connection_b"],),
        ),
    ):
        with (
            writer() as connection,
            pytest.raises(NOT_PERMITTED),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            begin_job(cursor, "workspace", LEGACY_WORKSPACE_ID)
            cursor.execute(statement, params)


def test_a_client_scoped_job_cannot_reach_a_sibling_client(orgs):
    """Even inside one organization, a client-scoped job sees only its client."""
    with writer() as connection, connection.transaction(), connection.cursor() as cursor:
        begin_job(cursor, "run", scalar(
            "select run_id from growthpulse.pipeline_runs where client_id = %s limit 1",
            (LEGACY_CLIENT_ID,),
        ))  # fmt: skip
        assert cursor.execute("select client_id from growthpulse.clients").fetchall() == [
            (LEGACY_CLIENT_ID,)
        ]


def test_pipeline_helpers_refuse_unknown_scopes(orgs):
    with writer() as connection:
        with pytest.raises(ClientNotFoundError):
            load_client(connection, uuid4())
        with (
            pytest.raises(TenantScopeError),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            begin_job(cursor, "client", uuid4())
        with (
            pytest.raises(pg_errors.InvalidParameterValue),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            cursor.execute("select growthpulse_auth.begin_job('everything', null, null)")


def test_the_dispatcher_lists_due_accounts_across_organizations_by_id_only(orgs):
    b_account = CLIENT_B.accounts[0].ad_account_id
    with admin() as connection:
        connection.execute(
            "update growthpulse.account_sync_state set next_due_at = now() "
            "where ad_account_id = %s",
            (b_account,),
        )
    try:
        with writer() as connection:
            due = due_accounts(connection, datetime(2030, 1, 1, tzinfo=UTC))
            # Without a job context the same connection still reads no account itself.
            assert (
                connection.execute("select count(*) from growthpulse.ad_accounts").fetchone()[0]
                == 0
            )
    finally:
        with admin() as connection:
            connection.execute(
                "update growthpulse.account_sync_state set next_due_at = null "
                "where ad_account_id = %s",
                (b_account,),
            )
    assert b_account in due
    columns = rows(
        "select pg_get_function_result(p.oid) from pg_proc p where p.proname = 'due_account_syncs'"
    )
    assert columns == [("SETOF uuid",)]


# 6. Token functions


def token_call(kind: str | None, scope_id, statement: str, params: tuple):
    with as_role(TOKEN_MANAGER) as connection, connection.transaction(), connection.cursor() as cur:
        if kind is not None:
            begin_job(cur, kind, scope_id)
        cur.execute(statement, params)
        return cur.fetchall()


READ = "select * from growthpulse_secrets.read_credentials(%s)"
DELETE = "select growthpulse_secrets.delete_credentials(%s)"
MARK = "select growthpulse_secrets.mark_connection_needs_reauth(%s)"
ROTATION = "select connection_id from growthpulse_secrets.credentials_needing_rotation(2::smallint)"
STORE = (
    "select growthpulse_secrets.store_credential(%s, %s, %s::smallint, %s::smallint, %s, %s, %s)"
)


def test_the_token_manager_reaches_only_its_contexts_organization(orgs):
    a, b = orgs["connection_a"], orgs["connection_b"]
    # Own organization: works.
    assert len(token_call("workspace", WORKSPACE_B, READ, (b,))) == 1
    assert token_call("workspace", WORKSPACE_B, ROTATION, ()) == [(b,)]
    # Another organization: exactly like an unknown connection.
    assert token_call("workspace", LEGACY_WORKSPACE_ID, READ, (b,)) == []
    assert token_call("workspace", LEGACY_WORKSPACE_ID, ROTATION, ()) == [(a,)]
    assert token_call("workspace", LEGACY_WORKSPACE_ID, DELETE, (b,)) == [(0,)]
    assert token_call("workspace", LEGACY_WORKSPACE_ID, MARK, (b,)) == [(False,)]
    with pytest.raises(pg_errors.ForeignKeyViolation, match="Unknown provider connection"):
        token_call("workspace", LEGACY_WORKSPACE_ID, STORE, (b, *fake_credential()))
    # No context at all: nothing.
    assert token_call(None, None, READ, (b,)) == []
    assert token_call(None, None, ROTATION, ()) == []
    assert token_call(None, None, DELETE, (b,)) == [(0,)]
    with pytest.raises(pg_errors.ForeignKeyViolation):
        token_call(None, None, STORE, (b, *fake_credential()))
    # Nothing above changed B's stored bytes or status.
    assert (
        scalar(
            "select count(*) from growthpulse_secrets.oauth_credentials where connection_id = %s",
            (b,),
        )
        == 1
    )
    assert (
        scalar("select status from growthpulse.provider_connections where connection_id = %s", (b,))
        == "active"
    )


def test_a_signed_in_admin_request_can_also_scope_the_token_manager(orgs):
    admin_b = sign_in(WORKSPACE_B, "admin")
    found = request_rows(admin_b, READ, (orgs["connection_b"],), role=TOKEN_MANAGER)
    assert len(found) == 1
    assert request_rows(admin_b, READ, (orgs["connection_a"],), role=TOKEN_MANAGER) == []


def test_token_writes_are_audited_without_secret_material(orgs):
    events = rows(
        "select action, target_type, target_id, actor_service from growthpulse_auth.audit_events "
        "where action like 'credential.%%' and workspace_id = %s",
        (WORKSPACE_B,),
    )
    assert ("credential.store", "connection", str(orgs["connection_b"]), "token_manager") in events


# 7. Owner actions (manager)


def test_owner_actions_need_a_signed_in_request_with_the_permission(orgs):
    query = "select name from growthpulse.clients where client_id = %s"
    name = scalar(query, (CLIENT_B.client_id,))
    with pytest.raises(NOT_PERMITTED):  # no context
        manager_call(None, "growthpulse.rename_client", CLIENT_B.client_id, "Renamed")
    with pytest.raises(NOT_PERMITTED):  # another organization's owner
        manager_call(orgs["owner_a"], "growthpulse.rename_client", CLIENT_B.client_id, "Renamed")
    for role in ("viewer", "analyst"):  # own organization, without client.manage
        request = sign_in(WORKSPACE_B, role, clients=[CLIENT_B.client_id])
        with pytest.raises(NOT_PERMITTED):
            manager_call(request, "growthpulse.rename_client", CLIENT_B.client_id, "Renamed")
    assert (
        scalar("select name from growthpulse.clients where client_id = %s", (CLIENT_B.client_id,))
        == name
    )

    admin_b = sign_in(WORKSPACE_B, "admin")
    manager_call(admin_b, "growthpulse.rename_client", CLIENT_B.client_id, name + " 2")
    manager_call(admin_b, "growthpulse.rename_client", CLIENT_B.client_id, name)
    events = rows(
        "select e.actor_user_id, e.request_id from growthpulse_auth.audit_events as e "
        "where e.action = 'rename_client' and e.workspace_id = %s",
        (WORKSPACE_B,),
    )
    admin_user = scalar(
        "select user_id from growthpulse_auth.sessions where token_hash = %s",
        (session_token_hash(admin_b.session_token),),
    )
    assert events == [(admin_user, admin_b.request_id)] * 2


def test_owner_actions_cannot_create_rows_in_another_organization(orgs):
    count = scalar("select count(*) from growthpulse.clients")
    with pytest.raises(NOT_PERMITTED):
        manager_call(orgs["owner_a"], "growthpulse.create_client", WORKSPACE_B, "Intruder",
                     "synthetic")  # fmt: skip
    with pytest.raises(NOT_PERMITTED):
        manager_call(orgs["owner_a"], "growthpulse.create_connection", WORKSPACE_B,
                     "google_ads", "subject-x")  # fmt: skip
    with pytest.raises(NOT_PERMITTED):
        manager_call(orgs["owner_a"], "growthpulse.set_account_status",
                     CLIENT_B.accounts[0].ad_account_id, "paused")  # fmt: skip
    assert (
        scalar(
            "select status from growthpulse.ad_accounts where ad_account_id = %s",
            (CLIENT_B.accounts[0].ad_account_id,),
        )
        == "active"
    )
    assert scalar("select count(*) from growthpulse.clients") == count


def test_every_manager_function_ends_in_the_checked_log(orgs):
    functions = rows(
        "select p.proname, p.prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'growthpulse' and has_function_privilege(%s, p.oid, 'execute')",
        (MANAGER,),
    )
    assert len(functions) == 11
    for name, source in functions:
        assert "growthpulse.log_owner_action(" in source, name


# 8. Organization rules


def test_the_internal_organization_holds_synthetic_clients_only(orgs):
    assert (
        scalar(
            "select kind from growthpulse_auth.organizations where workspace_id = %s",
            (LEGACY_WORKSPACE_ID,),
        )
        == "internal"
    )
    with pytest.raises(pg_errors.CheckViolation):
        manager_call(orgs["owner_a"], "growthpulse.create_client", LEGACY_WORKSPACE_ID,
                     "Real brand", "live")  # fmt: skip
    live = manager_call(orgs["owner_b"], "growthpulse.create_client", WORKSPACE_B,
                        "Real brand (fictional test)", "live")  # fmt: skip
    manager_call(orgs["owner_b"], "growthpulse.archive_client", live)


def test_an_organization_keeps_at_least_one_active_owner(orgs):
    with admin() as connection:
        owners = connection.execute(
            "select membership_id from growthpulse_auth.memberships "
            "where workspace_id = %s and role = 'owner' and status = 'active'",
            (LEGACY_WORKSPACE_ID,),
        ).fetchall()
        assert len(owners) == 1
        with pytest.raises(pg_errors.CheckViolation):
            connection.execute(
                "update growthpulse_auth.memberships set role = 'admin' where membership_id = %s",
                owners[0],
            )
        with pytest.raises(pg_errors.CheckViolation):
            connection.execute(
                "update growthpulse_auth.memberships set status = 'suspended' "
                "where membership_id = %s",
                owners[0],
            )


def test_an_ad_account_still_belongs_to_one_client_in_the_pilot(orgs):
    """Amendment 3: connecting one platform account to two organizations is not enabled.

    The global identity constraint from migration 4 is unchanged. Allowing it later means
    replacing that constraint with one per organization; nothing added in 7a.2 keys on the
    platform account ID, so no other table has to change.
    """
    account = CLIENT_B.accounts[0]
    with admin() as connection, pytest.raises(pg_errors.UniqueViolation):
        connection.execute(
            "insert into growthpulse.ad_accounts (client_id, data_source, platform, "
            "external_account_id, currency, timezone) values (%s, 'synthetic', %s, %s, %s, %s)",
            (LEGACY_CLIENT_ID, account.platform.value, account.external_account_id,
             account.currency, account.timezone),
        )  # fmt: skip


# 9. Privileges (catalog)


def test_no_function_is_executable_by_public_or_an_api_role(orgs):
    for grantee in ("public", *API_ROLES):
        found = rows(
            "select n.nspname || '.' || p.proname from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = any(%s) and has_function_privilege(%s, p.oid, 'execute')",
            (list(ALL_SCHEMAS), grantee),
        )
        assert found == [], grantee


EXPECTED_AUTH_EXECUTE = {
    WRITER: {"begin_job", "job_workspace", "job_client_ids", "job_account_ids",
             "due_account_syncs"},
    DASHBOARD: {"begin_request", "request_client_ids", "request_workspace_if",
                "request_owner_action_ids"},
    MANAGER: {"begin_request"},
    TOKEN_MANAGER: {"begin_request", "begin_job"},
    AUTHENTICATOR: {"create_session", "revoke_session"},
}  # fmt: skip


def test_each_role_executes_exactly_its_growthpulse_auth_functions(orgs):
    for role, expected in EXPECTED_AUTH_EXECUTE.items():
        found = {
            row[0]
            for row in rows(
                "select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                "where n.nspname = 'growthpulse_auth' and has_function_privilege(%s, p.oid, "
                "'execute')",
                (role,),
            )
        }
        assert found == expected, role


def test_definer_functions_pin_an_empty_search_path(orgs):
    unpinned = rows(
        "select n.nspname || '.' || p.proname from pg_proc p "
        "join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = any(%s) and p.prosecdef "
        "and not coalesce(p.proconfig, '{}') @> array['search_path=\"\"']",
        (list(ALL_SCHEMAS),),
    )
    assert unpinned == []


def test_no_app_role_reads_identity_tables_or_deletes_anything(orgs):
    for role in APP_ROLES:
        readable = rows(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'growthpulse_auth' and c.relkind = 'r' "
            "and has_table_privilege(%s, c.oid, 'select, insert, update, delete')",
            (role,),
        )
        assert readable == [], role
        deletable = rows(
            "select n.nspname, c.relname from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = any(%s) and c.relkind = 'r' "
            "and has_table_privilege(%s, c.oid, 'delete, truncate')",
            (list(ALL_SCHEMAS), role),
        )
        assert deletable == [], role


def test_new_roles_are_created_without_login():
    text = "".join(path.read_text() for path in M7A_MIGRATIONS)
    assert f"create role {AUTHENTICATOR} nologin noinherit;" in text


def test_every_writer_policy_requires_a_job_context(orgs):
    policies = rows(
        "select tablename, policyname, coalesce(qual, '') || ' ' || coalesce(with_check, '') "
        "from pg_policies where schemaname = 'growthpulse' and %s = any(roles)",
        (WRITER,),
    )
    assert len(policies) == 30
    for table, name, expressions in policies:
        uses_job = "growthpulse_auth.job_" in expressions
        # Policies scoped through another table's policy (status events, discovered accounts,
        # combined-run quarantine rows) reach the job context through that table.
        through = "experiment_proposals" in expressions or "provider_connections" in expressions
        assert uses_job or through, (table, name)
        assert expressions.strip() not in {"true", "true true"}, (table, name)


# 10. Revert


def extended_snapshot() -> dict[str, list]:
    schemas = ["growthpulse", "growthpulse_read", "growthpulse_secrets", "growthpulse_auth"]
    queries = {
        "columns": "select table_schema, table_name, column_name, data_type, is_nullable, "
        "column_default from information_schema.columns where table_schema = any(%s) "
        "order by 1, 2, 3",
        "constraints": "select conrelid::regclass::text, conname, pg_get_constraintdef(c.oid) "
        "from pg_constraint c join pg_namespace n on n.oid = c.connamespace "
        "where n.nspname = any(%s) order by 1, 2",
        "indexes": "select schemaname, indexname, indexdef from pg_indexes "
        "where schemaname = any(%s) order by 1, 2",
        "triggers": "select tgrelid::regclass::text, tgname, pg_get_triggerdef(t.oid) "
        "from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n "
        "on n.oid = c.relnamespace where n.nspname = any(%s) and not t.tgisinternal "
        "order by 1, 2",
        "policies": "select schemaname, tablename, policyname, cmd, roles::text, qual, "
        "with_check from pg_policies where schemaname = any(%s) order by 1, 2, 3",
        "relations": "select n.nspname, c.relname, c.relkind::text, c.reloptions::text, "
        "c.relacl::text, c.relrowsecurity from pg_class c join pg_namespace n "
        "on n.oid = c.relnamespace where n.nspname = any(%s) order by 1, 2",
        "views": "select schemaname, viewname, definition from pg_views "
        "where schemaname = any(%s) order by 1, 2",
        "functions": "select n.nspname, p.proname, pg_get_functiondef(p.oid), p.proacl::text "
        "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = any(%s) order by 1, 2, 3",
        "column_grants": "select c.relname, t.attname, t.attacl::text from pg_attribute t "
        "join pg_class c on c.oid = t.attrelid join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = any(%s) and t.attacl is not null order by 1, 2",
        "schemas": "select nspname, nspacl::text from pg_namespace "
        "where nspname = any(%s) order by 1",
        "default_acl": "select coalesce(n.nspname, ''), d.defaclobjtype::text, "
        "d.defaclacl::text from pg_default_acl d left join pg_namespace n "
        "on n.oid = d.defaclnamespace where n.nspname is null or n.nspname = any(%s) "
        "order by 1, 2",
    }
    snapshot = {name: rows(query, (schemas,)) for name, query in queries.items()}
    snapshot["roles"] = rows(
        "select rolname, rolcanlogin, rolconfig::text from pg_roles "
        "where rolname like 'growthpulse%%' and rolname <> %s order by 1",
        (OWNER,),
    )
    return snapshot


def m5_state() -> None:
    milestone_4_state()
    apply(M5_MIGRATIONS)
    enable_test_logins()


def test_the_revert_restores_the_exact_milestone_5_schema_and_keeps_the_data():
    m5_state()
    before = extended_snapshot()
    apply(M7A_MIGRATIONS)
    enable_test_logins()
    run_client(LEGACY_CLIENT_ID, writer, factory=SyntheticConnectorFactory(),
               window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    runs_after = scalar("select count(*) from growthpulse.pipeline_runs")
    run_sql_file(REVERT_M7A)
    assert extended_snapshot() == before
    assert scalar("select count(*) from growthpulse.pipeline_runs") == runs_after
    # The pipeline still works on the reverted (Milestone 5) schema, and the upgrade can be
    # applied again.
    run_client(LEGACY_CLIENT_ID, writer, factory=SyntheticConnectorFactory(),
               window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    apply(M7A_MIGRATIONS)


def test_the_revert_refuses_to_drop_identity_rows_unless_told_to():
    m5_state()
    apply(M7A_MIGRATIONS)
    enable_test_logins()
    sign_in(LEGACY_WORKSPACE_ID)
    with pytest.raises(pg_errors.CheckViolation, match="identity rows exist"):
        run_sql_file(REVERT_M7A)
    assert scalar("select count(*) from growthpulse_auth.memberships") == 1  # nothing undone
    with owner() as connection:
        connection.execute("set growthpulse.allow_identity_data_loss = 'yes'")
        connection.execute(REVERT_M7A.read_text())
    assert scalar("select to_regnamespace('growthpulse_auth') is null") is True


def test_migration_9_refuses_a_live_client_in_the_internal_workspace():
    m5_state()
    with admin() as connection:
        connection.execute(
            "insert into growthpulse.clients (workspace_id, name, data_source) "
            "values (%s, 'A live brand', 'live')",
            (LEGACY_WORKSPACE_ID,),
        )
    with pytest.raises(pg_errors.CheckViolation, match="internal organization holds a live"):
        apply(M7A_MIGRATIONS[:1])
    assert scalar("select to_regnamespace('growthpulse_auth') is null") is True


def test_the_pipeline_runs_unchanged_on_a_milestone_5_database():
    """Before migrations 9-12 the job scope is skipped (see context.begin_job)."""
    m5_state()
    outcome = run_client(LEGACY_CLIENT_ID, writer, factory=SyntheticConnectorFactory(),
                         window=SYNTHETIC_WINDOW, triggered_by="test")  # fmt: skip
    assert outcome.analysis.report_id
