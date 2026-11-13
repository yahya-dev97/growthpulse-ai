"""Static checks on the SQL migrations. No database is involved.

These catch drift between Python and SQL, and grants that would widen access, before
anything is applied. tests/test_postgres_integration.py runs the same SQL on a server.
"""

import re
from enum import StrEnum
from pathlib import Path

import pytest

from growthpulse_ai.accounts.legacy import (
    LEGACY_CLIENT_ID,
    LEGACY_EXTERNAL_ACCOUNT_IDS,
    LEGACY_GOOGLE_ACCOUNT_ID,
    LEGACY_META_ACCOUNT_ID,
    LEGACY_SYNTHETIC_CLIENT,
    LEGACY_WORKSPACE_ID,
)
from growthpulse_ai.accounts.models import AccountStatus, ClientStatus, ConnectionStatus
from growthpulse_ai.accounts.scheduling import SyncFrequency, SyncStatus
from growthpulse_ai.analytics.anomalies import Direction, RuleId, Severity
from growthpulse_ai.analytics.noise import NoiseGuardOutcome
from growthpulse_ai.analytics.periods import ComparisonPurpose
from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.credentials.crypto import TokenType
from growthpulse_ai.persistence import store
from growthpulse_ai.schema import (
    AttributionModel,
    CampaignType,
    ConversionStage,
    DataSource,
    Platform,
    ReportTimeBasis,
)

MIGRATIONS_DIR = PROJECT_ROOT / "supabase" / "migrations"
ROLLBACK_DIR = PROJECT_ROOT / "supabase" / "rollback"
DROP_ALL = ROLLBACK_DIR / "20261111100000_drop_growthpulse.sql"
REVERT_M5 = ROLLBACK_DIR / "20261111200000_revert_milestone_5.sql"
REVERT_M7A = ROLLBACK_DIR / "20261114100000_revert_m7a2_tenant_security.sql"
MIGRATIONS = sorted(MIGRATIONS_DIR.glob("*.sql"))
SQL = "\n".join(path.read_text() for path in MIGRATIONS)
TABLES_SQL = MIGRATIONS[0].read_text()
ACCESS_SQL = MIGRATIONS[1].read_text()
VIEWS_SQL = MIGRATIONS[2].read_text()
(TENANCY_SQL, SCOPE_SQL, TOKENS_SQL, OWNER_SQL, GRANTS_SQL) = (
    path.read_text() for path in MIGRATIONS[3:8]
)
(IDENTITY_SQL, CONTEXT_SQL, HARDEN_SQL, POLICIES_SQL) = (
    path.read_text() for path in MIGRATIONS[8:]
)
M5_SQL = "\n".join(path.read_text() for path in MIGRATIONS[3:8])
M7A_SQL = "\n".join(path.read_text() for path in MIGRATIONS[8:])
API_ROLES = ("public", "anon", "authenticated", "service_role")
APPEND_ONLY = (
    "analytics_reports",
    "experiment_proposals",
    "experiment_status_events",
    "analytics_report_breakdowns",
    "owner_action_log",
)


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def table_bodies(sql: str = SQL) -> dict[str, str]:
    return dict(
        re.findall(r"create table growthpulse\.(\w+) \((.*?)\n\);", strip_comments(sql), re.DOTALL)
    )


def table_columns() -> dict[str, set[str]]:
    """Columns of every growthpulse table, including those added by later migrations."""
    tables = {}
    for name, body in table_bodies().items():
        columns = set()
        for line in body.splitlines():
            match = re.match(r"\s{4}(\w+) ", line)
            if match and match.group(1) not in {"constraint", "check", "primary"}:
                columns.add(match.group(1))
        tables[name] = columns
    for name, body in re.findall(
        r"alter table growthpulse\.(\w+)\s(.*?);", strip_comments(SQL), re.DOTALL
    ):
        tables[name] |= set(re.findall(r"add column (\w+)", body))
    return tables


def check_values(column: str, sql: str = SQL) -> list[set[str]]:
    """Every allowed-value list for a column, across all tables."""
    pattern = rf"\b{column} in \(([^)]*)\)"
    return [
        set(re.findall(r"'(\w+)'", match)) for match in re.findall(pattern, strip_comments(sql))
    ]


def writer_update_grants() -> dict[str, set[str]]:
    grants: dict[str, set[str]] = {}
    for columns, table in re.findall(
        r"grant update \((.*?)\)\s+on growthpulse\.(\w+) to growthpulse_writer",
        SQL,
        re.DOTALL,
    ):
        grants.setdefault(table, set()).update(column.strip() for column in columns.split(","))
    return grants


def test_migrations_are_ordered_and_named():
    assert [path.name[:14] for path in MIGRATIONS] == sorted(path.name[:14] for path in MIGRATIONS)
    assert all(re.fullmatch(r"\d{14}_[a-z_]+\.sql", path.name) for path in MIGRATIONS)
    assert len(MIGRATIONS) == 12


def test_milestone_4_migrations_are_unchanged_in_name():
    assert [path.name for path in MIGRATIONS[:3]] == [
        "20261111100000_create_growthpulse_tables.sql",
        "20261111100100_create_access_roles.sql",
        "20261111100200_create_dashboard_views.sql",
    ]


def test_every_expected_table_exists():
    assert set(table_columns()) == {
        "pipeline_runs",
        "ad_performance_records",
        "quarantined_records",
        "analytics_reports",
        "findings",
        "experiment_proposals",
        "experiment_status_events",
        "workspaces",
        "clients",
        "provider_connections",
        "discovered_ad_accounts",
        "ad_accounts",
        "account_sync_config",
        "account_sync_state",
        "owner_action_log",
        "analytics_report_breakdowns",
    }
    assert re.findall(r"create table (growthpulse_secrets\.\w+)", SQL) == [
        "growthpulse_secrets.oauth_credentials"
    ]


# Allowed values in SQL match the Python enums.


@pytest.mark.parametrize(
    ("column", "enum"),
    [
        ("data_source", DataSource),
        ("platform", Platform),
        ("campaign_type", CampaignType),
        ("conversion_stage", ConversionStage),
        ("attribution_model", AttributionModel),
        ("report_time_basis", ReportTimeBasis),
        ("rule_id", RuleId),
        ("severity", Severity),
        ("pre_guard_severity", Severity),
        ("direction", Direction),
        ("guard_outcome", NoiseGuardOutcome),
        ("comparison_purpose", ComparisonPurpose),
    ],
)
def test_check_constraints_match_python_enums(column: str, enum: type[StrEnum]):
    lists = check_values(column)
    assert lists, f"no CHECK list for {column}"
    assert all(values == {member.value for member in enum} for values in lists)


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    [
        ("clients", "status", ClientStatus),
        ("clients", "data_source", DataSource),
        ("provider_connections", "status", ConnectionStatus),
        ("provider_connections", "provider", Platform),
        ("discovered_ad_accounts", "platform", Platform),
        ("ad_accounts", "status", AccountStatus),
        ("ad_accounts", "platform", Platform),
        ("account_sync_config", "frequency", SyncFrequency),
        ("account_sync_state", "status", SyncStatus),
    ],
)
def test_milestone_5_check_lists_match_python_enums(table: str, column: str, enum):
    lists = check_values(column, table_bodies()[table])
    assert lists == [{member.value for member in enum}], (table, column)


def test_token_types_match_python():
    assert check_values("token_type", TOKENS_SQL) == [{member.value for member in TokenType}]


# The SQL the store runs fits the schema and the grants.


def statement_columns(query: str) -> tuple[str, set[str], set[str]]:
    """Table, inserted columns, and columns changed by ON CONFLICT DO UPDATE."""
    table, columns = re.search(
        r"insert into growthpulse\.(\w+)(?: as existing)? \((.*?)\)", query, re.DOTALL
    ).groups()
    inserted = {column.strip() for column in columns.split(",")}
    updated = set(re.findall(r"^\s+(\w+) = excluded\.", query, re.MULTILINE))
    updated |= set(re.findall(r"^\s+(\w+) = now\(\)", query, re.MULTILINE))
    return table, inserted, updated


@pytest.mark.parametrize(
    "query",
    [
        store.INSERT_RUN,
        store.UPSERT_RECORD,
        store.UPSERT_QUARANTINE,
        store.INSERT_REPORT,
        store.UPSERT_FINDING,
        store.INSERT_PROPOSAL,
        store.INSERT_PROPOSED_STATUS,
    ],
)
def test_store_statements_use_existing_and_granted_columns(query: str):
    table, inserted, updated = statement_columns(query)
    assert inserted <= table_columns()[table]
    assert updated <= writer_update_grants().get(table, set())


@pytest.mark.parametrize("query", [store.FINISH_RUN, store.FAIL_RUN])
def test_run_updates_use_granted_columns(query: str):
    assigned = re.search(r"set (.*?)\nwhere", query, re.DOTALL).group(1)
    columns = set(re.findall(r"(\w+) = ", assigned))
    assert columns <= writer_update_grants()["pipeline_runs"]


def test_update_grants_name_real_columns():
    columns = table_columns()
    for table, granted in writer_update_grants().items():
        assert granted <= columns[table], table


def test_update_grants_exclude_identity_and_first_seen_columns():
    for table, granted in writer_update_grants().items():
        assert not {c for c in granted if c.startswith("first_") or c.endswith("_id")} - {
            "last_changed_run_id",
            "last_seen_run_id",
            "latest_report_id",
            "report_id",
            "lease_run_id",
        }, table


def test_scope_columns_are_never_updatable_by_the_writer():
    for table, granted in writer_update_grants().items():
        assert not granted & {"client_id", "ad_account_id", "run_kind", "data_source"}, table


# Access control


def test_every_table_has_row_level_security():
    enabled = set(re.findall(r"alter table growthpulse\.(\w+) enable row level security", SQL))
    assert enabled == set(table_columns())
    assert "alter table growthpulse_secrets.oauth_credentials enable row level security" in SQL
    assert "create policy" not in strip_comments(TOKENS_SQL)  # owner-only, through functions


def test_nothing_is_granted_to_public_or_supabase_api_roles():
    grants = re.findall(r"^\s*grant [^;]*;", strip_comments(SQL), re.MULTILINE)
    assert grants
    for grant in grants:
        assert not re.search(rf"\bto ({'|'.join(API_ROLES)})\b", grant), grant


def test_dashboard_gets_select_on_read_views_only():
    grants = [
        " ".join(grant.split())
        for grant in re.findall(r"grant [^;]*to growthpulse_dashboard;", SQL)
    ]
    assert grants == [
        "grant usage on schema growthpulse_read to growthpulse_dashboard;",
        "grant select on all tables in schema growthpulse_read to growthpulse_dashboard;",
        "grant select on all tables in schema growthpulse_read to growthpulse_dashboard;",
        # Milestone 7a.2: the filters its views call, and the new audit view.
        "grant execute on function growthpulse_auth.request_client_ids() to growthpulse_dashboard;",
        "grant execute on function growthpulse_auth.request_workspace_if(text) "
        "to growthpulse_dashboard;",
        "grant execute on function growthpulse_auth.request_owner_action_ids() "
        "to growthpulse_dashboard;",
        "grant select on growthpulse_read.audit_events to growthpulse_dashboard;",
    ]


def test_writer_never_gets_delete_truncate_or_schema_rights():
    for grant in re.findall(r"grant [^;]*to growthpulse_writer;", SQL):
        assert not re.search(r"\b(delete|truncate|all|create|references|trigger)\b", grant), grant


def test_history_tables_cannot_be_updated_by_the_writer():
    assert not set(writer_update_grants()) & set(APPEND_ONLY)
    for table in APPEND_ONLY:
        assert re.search(
            rf"before update on growthpulse\.{table}\s+for each row execute function "
            r"growthpulse\.reject_update\(\)",
            SQL,
        ), table


def test_pipeline_can_only_propose_experiments():
    policy = re.search(r"create policy writer_propose_only.*?;", ACCESS_SQL, re.DOTALL).group(0)
    assert "status = 'proposed'" in policy
    assert not re.search(r"for update to growthpulse_writer[^;]*experiment", ACCESS_SQL)


def test_migrations_hold_no_secrets_or_privileged_attributes():
    lowered = strip_comments(SQL).lower()
    for forbidden in ("password", "superuser", "bypassrls", "createrole", "replication"):
        assert forbidden not in lowered, forbidden
    assert not re.search(r"\blogin\b", lowered.replace("nologin", ""))
    for role in re.findall(r"create role (\w+)([^;]*);", lowered):
        assert role[1].strip() == "nologin noinherit", role


def definer_functions(sql: str | None = None) -> dict[str, str]:
    """name -> full text, for every SECURITY DEFINER function (Milestones 4 and 5 by default)."""
    sql = "\n".join(path.read_text() for path in MIGRATIONS[:8]) if sql is None else sql
    functions = re.findall(r"create function ([\w.]+)\((.*?)\$\$;", strip_comments(sql), re.DOTALL)
    return {name: body for name, body in functions if "security definer" in body}


def test_security_definer_functions_exist_only_in_the_approved_migrations():
    for path in [*MIGRATIONS[:5], MIGRATIONS[7]]:
        assert "security definer" not in strip_comments(path.read_text()), path.name
    names = set(definer_functions())
    assert len(names) == 16
    assert {n for n in names if n.startswith("growthpulse_secrets.")} == {
        f"growthpulse_secrets.{n}"
        for n in ("store_credential", "read_credentials", "delete_credentials",
                  "mark_connection_needs_reauth", "credentials_needing_rotation")
    }  # fmt: skip


def test_each_definer_function_is_granted_to_exactly_one_restricted_role():
    grants = re.findall(
        r"grant execute on function ([\w.]+)\(.*?\)\s+to (\w+);", strip_comments(M5_SQL), re.DOTALL
    )
    granted: dict[str, set[str]] = {}
    for name, role in grants:
        granted.setdefault(name, set()).add(role)
    assert set(granted) == set(definer_functions())
    for name, roles in granted.items():
        expected = "growthpulse_token_manager" if "secrets" in name else "growthpulse_manager"
        assert roles == {expected}, name


def test_functions_pin_their_search_path():
    functions = re.findall(r"create function .*?\$\$;", strip_comments(SQL), re.DOTALL)
    assert len(functions) >= 20
    assert all("set search_path = ''" in function for function in functions)


def test_views_never_expose_raw_payloads_or_secret_material():
    for sql in (VIEWS_SQL, GRANTS_SQL):
        code = strip_comments(sql)
        for column in ("raw_payload", "ciphertext", "nonce", "key_version", "provider_subject_id"):
            assert column not in code, column


def test_every_view_states_its_security_mode():
    for sql in (VIEWS_SQL, GRANTS_SQL):
        views = re.findall(
            r"create (?:or replace )?view growthpulse_read\.\w+\s+with \((.*?)\)", sql
        )
        assert len(views) == len(re.findall(r"create (?:or replace )?view", sql))
        assert set(views) == {"security_invoker = false"}


def test_rollback_script_is_kept_out_of_the_migrations_folder():
    rollbacks = list(ROLLBACK_DIR.glob("*.sql"))
    assert {path.name for path in rollbacks} == {DROP_ALL.name, REVERT_M5.name, REVERT_M7A.name}
    names = {path.name for path in MIGRATIONS}
    assert not names & {path.name for path in rollbacks}


def rollback_statements(path: Path = DROP_ALL) -> list[str]:
    sql = strip_comments(path.read_text())
    return [" ".join(s.split()) for s in sql.split(";") if s.strip()]


def drop_targets(targets: str) -> list[str]:
    """Split a drop list on top-level commas, keeping function argument lists whole."""
    return [t.strip() for t in re.split(r",\s*(?![^()]*\))", targets)]


def test_rollback_only_drops_growthpulse_objects_by_name():
    statements = rollback_statements()
    assert statements[0] == "begin" and statements[-1] == "commit"
    for statement in statements[1:-1]:
        kind, targets = re.fullmatch(
            r"drop (view|schema|table|function|role) if exists (.+?)(?: restrict)?", statement
        ).groups()
        for target in drop_targets(targets):
            assert re.fullmatch(
                r"growthpulse(_read|_secrets|_auth)?(\.\w+(\([\w ,]*\))?)?|growthpulse_\w+", target
            ), (kind, target)


def test_rollback_never_cascades():
    statements = rollback_statements()
    assert not any("cascade" in statement for statement in statements)
    drops = [s for s in statements if s.startswith("drop ") and not s.startswith("drop role")]
    assert drops
    assert all(statement.endswith(" restrict") for statement in drops)


def test_rollback_drops_every_object_the_migrations_create():
    text = " ".join(rollback_statements())
    for table in table_columns():
        assert f"growthpulse.{table}," in text or f"growthpulse.{table} restrict" in text, table
    assert "growthpulse_secrets.oauth_credentials" in text
    for view in re.findall(r"create view (growthpulse_read\.\w+)", SQL):
        assert f"{view}," in text or f"{view} restrict" in text, view
    for function in re.findall(r"create function ([\w.]+)\(", SQL):
        assert f"{function}(" in text, function
    for schema in re.findall(r"create schema (\w+);", SQL):
        assert f"drop schema if exists {schema} restrict" in text
    for role in re.findall(r"create role (\w+)", SQL):
        assert f"drop role if exists {role}" in text


# The Milestone 5 revert script


def test_revert_script_never_cascades_and_touches_only_growthpulse_objects():
    statements = rollback_statements(REVERT_M5)
    assert statements[0] == "begin" and statements[-1] == "commit"
    assert not any("cascade" in s for s in statements)
    for statement in statements:
        if statement.startswith("drop ") and not statement.startswith("drop role"):
            assert statement.endswith(" restrict"), statement[:60]
        if statement.startswith(("drop table", "drop view", "drop function", "alter table")):
            assert "growthpulse" in statement


def test_revert_script_removes_everything_milestone_5_adds():
    text = " ".join(rollback_statements(REVERT_M5))
    m5 = M5_SQL
    for name in re.findall(r"create table growthpulse(?:_secrets)?\.(\w+)", m5):
        assert f".{name}" in text, name
    for function in re.findall(r"create function ([\w.]+)\(", m5):
        assert f"{function}(" in text, function
    for column in re.findall(r"add column (\w+)", strip_comments(SCOPE_SQL)):
        assert f"drop column if exists {column} restrict" in text, column
    for trigger in re.findall(r"create trigger (\w+)\s+before \w+(?: or \w+)? on growthpulse\.(?:"
                              r"ad_performance_records|analytics_reports|findings|"
                              r"experiment_proposals)", m5):  # fmt: skip
        assert f"drop trigger if exists {trigger}" in text, trigger
    for role in re.findall(r"create role (\w+)", m5):
        assert f"drop role if exists {role}" in text


def test_revert_script_recreates_the_milestone_4_views_verbatim():
    revert = REVERT_M5.read_text()
    replaced = re.findall(r"create or replace view (growthpulse_read\.\w+)", GRANTS_SQL)
    assert len(replaced) == 8
    for view in replaced:
        original = re.search(rf"create view {re.escape(view)} with .*?;\n", VIEWS_SQL, re.DOTALL)
        assert original and original.group(0) in revert, view


# The Milestone 5 backfill


def test_backfill_seeds_exactly_the_legacy_ids():
    for value in (
        LEGACY_WORKSPACE_ID,
        LEGACY_CLIENT_ID,
        LEGACY_GOOGLE_ACCOUNT_ID,
        LEGACY_META_ACCOUNT_ID,
    ):
        assert str(value) in SCOPE_SQL
    assert f"'{LEGACY_SYNTHETIC_CLIENT.name}'" in SCOPE_SQL
    for platform, external_id in LEGACY_EXTERNAL_ACCOUNT_IDS.items():
        assert f"'{platform.value}', '{external_id}', 'GBP', 'Europe/London'" in SCOPE_SQL
    uuids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", SQL))
    assert uuids == {
        str(v)
        for v in (
            LEGACY_WORKSPACE_ID, LEGACY_CLIENT_ID, LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID,
        )
    }  # fmt: skip


def test_backfill_never_deletes_or_rewrites_history():
    code = strip_comments(SCOPE_SQL)
    assert "delete " not in code and "truncate" not in code
    assert not re.search(r"update growthpulse\.(analytics_reports|experiment_proposals|"
                         r"experiment_status_events|pipeline_runs)\b", code)  # fmt: skip
    for statement in re.findall(
        r"update growthpulse\.\w+ as \w+\s+set (.*?)\s+from", code, re.DOTALL
    ):
        assert statement.strip() == "ad_account_id = a.ad_account_id", statement
    assert "disable trigger" not in code


def test_backfill_preconditions_run_before_any_change():
    code = strip_comments(SCOPE_SQL)
    first_change = min(code.index(k) for k in ("insert into", "alter table", "update "))
    assert code.index("raise exception 'Milestone 5 backfill refused") < first_change
    for refusal in (
        "non-synthetic rows",
        "does not match a legacy account",
        "duplicate record identities",
    ):
        assert refusal in code  # fmt: skip


def test_each_migration_closes_the_schemas_it_creates():
    for path in MIGRATIONS:
        sql = path.read_text()
        for schema in re.findall(r"create schema (\w+);", sql):
            assert f"revoke all on schema {schema} from public;" in sql, path.name
            assert f"revoke all on schema {schema} from %I" in sql, path.name
            assert f"revoke all on all tables in schema {schema} from %I" in sql, path.name


def test_object_references_are_schema_qualified():
    sql = re.sub(r"'[^']*'", "''", strip_comments(SQL))
    keywords = {"schema", "all", "public", "true", "lateral", "role", "function", "tables"}
    keywords |= {"sequences", "functions", "latest", "conflict", "deleted", "credentials"}
    keywords |= set(re.findall(r"\bas (\w+)", sql))  # table and subquery aliases
    keywords |= {name for name in re.findall(r"\b([pv]_\w+)", sql)}  # arguments, variables
    references = re.findall(
        r"\b(?:table|view|from|join|references|on|into|function)\s+(?=([a-z_][\w.]*))", sql
    )
    assert references
    unqualified = {name for name in references if "." not in name and name not in keywords} - {
        "growthpulse",
        "growthpulse_read",
        "growthpulse_secrets",
        "growthpulse_auth",
    }
    assert unqualified == set()
    for name in references:
        if "." in name:
            prefix = name.split(".")[0]
            allowed = {
                "growthpulse",
                "growthpulse_read",
                "growthpulse_secrets",
                "growthpulse_auth",
                "pg_catalog",
            }
            # Aliases (from "as x") and trigger records qualify columns, not schemas.
            assert prefix in allowed or prefix in {"new", "old", "excluded"} | keywords, name


VERIFICATION_DIR = PROJECT_ROOT / "supabase" / "verification"


@pytest.mark.parametrize(
    ("name", "statement_count"),
    [("post_migration_checks.sql", 21), ("milestone_5_checks.sql", 13)],
)
def test_post_migration_checks_are_read_only(name: str, statement_count: int):
    path = VERIFICATION_DIR / name
    # Privilege names such as 'CREATE' and separators such as '; ' appear only inside
    # string literals, so literals are blanked before splitting and checking.
    code = re.sub(r"'[^']*'", "''", strip_comments(path.read_text()).lower())
    statements = [" ".join(s.split()) for s in code.split(";") if s.strip()]
    assert statements[0] == "begin transaction read only"
    assert statements[-1] == "rollback"
    assert len(statements) == statement_count
    for statement in statements[1:-1]:
        assert statement.startswith(("select", "with")), statement[:40]
        assert not re.search(
            r"\b(insert|update|delete|merge|alter|create|drop|grant|revoke|truncate|copy|"
            r"set|reset|do|call|execute|lock|vacuum|analyze|refresh|notify|listen)\b",
            statement,
        ), statement[:60]
        # Functions with side effects, or that read files, settings or secrets.
        assert not re.search(
            r"\b(set_config|pg_read_file|pg_ls_dir|lo_\w+|dblink\w*|pg_terminate_backend|"
            r"pg_cancel_backend|nextval|setval|current_setting|pg_reload_conf)\s*\(",
            statement,
        ), statement[:60]
    # Password hashes, secret material and full setting values are never selected.
    assert "rolpassword" not in code and "pg_authid" not in code
    assert "setconfig" not in code.replace("unnest(d.setconfig)", "")
    assert not re.search(r"\b(ciphertext|nonce|read_credentials)\b", code)


@pytest.mark.parametrize(
    ("name", "count"), [("post_migration_checks.sql", 19), ("milestone_5_checks.sql", 11)]
)
def test_post_migration_checks_share_one_result_shape(name: str, count: int):
    text = strip_comments((VERIFICATION_DIR / name).read_text())
    checks = re.findall(r"select '(\d\d) [a-z ]+' as check_name", text)
    assert checks == [f"{n:02d}" for n in range(1, count + 1)]


def test_milestone_5_checks_expect_exactly_the_legacy_backfill():
    text = (VERIFICATION_DIR / "milestone_5_checks.sql").read_text()
    for value in (LEGACY_CLIENT_ID, LEGACY_GOOGLE_ACCOUNT_ID, LEGACY_META_ACCOUNT_ID):
        assert str(value) in text
    for path in MIGRATIONS[:8]:
        assert f"('{path.name[:14]}')" in text


def test_supabase_cli_files_hold_no_secrets():
    for path in [PROJECT_ROOT / "supabase" / "config.toml", *MIGRATIONS]:
        text = Path(path).read_text()
        assert not re.search(r"(sb_secret_|sbp_[0-9a-f]{20}|eyJhbGciOi|://[^:@\s]+:[^@\s]+@)", text)


# The proposed migration that removes the Phase B scope fallbacks (not yet approved)

PROPOSED_DIR = PROJECT_ROOT / "supabase" / "proposed"
REMOVE_FALLBACKS = PROPOSED_DIR / "20261111200500_remove_scope_fallbacks.sql"


def test_proposed_migration_is_kept_out_of_the_migrations_folder():
    assert REMOVE_FALLBACKS.exists()
    assert REMOVE_FALLBACKS.name not in {path.name for path in MIGRATIONS}
    # It sorts after Milestone 5. Milestone 7a.2 migrations now sort after it, so if it is
    # ever approved it must be renamed to the next free number before it moves.
    assert MIGRATIONS[7].name[:14] < REMOVE_FALLBACKS.name[:14] < MIGRATIONS[8].name[:14]


def test_proposed_migration_only_removes_the_fallbacks():
    code = strip_comments(REMOVE_FALLBACKS.read_text())
    statements = [" ".join(s.split()) for s in code.split(";") if s.strip()]
    assert statements == [
        "alter table growthpulse.pipeline_runs alter column client_id drop default",
        "drop trigger ad_performance_records_1_derive_scope "
        "on growthpulse.ad_performance_records restrict",
        "drop trigger findings_derive_scope on growthpulse.findings restrict",
        "drop trigger analytics_reports_derive_scope on growthpulse.analytics_reports restrict",
        "drop trigger experiment_proposals_derive_scope "
        "on growthpulse.experiment_proposals restrict",
        "drop function growthpulse.derive_scope() restrict",
    ]


def test_every_fallback_trigger_is_removed_and_the_run_scope_check_is_kept():
    created = set(re.findall(r"create trigger (\w+_derive_scope)", SCOPE_SQL))
    proposed = REMOVE_FALLBACKS.read_text()
    assert created and all(f"drop trigger {name}" in proposed for name in created)
    assert "check_record_run_scope" not in strip_comments(proposed)


def test_milestone_4_fingerprints_are_read_only_and_cover_every_milestone_4_table():
    path = VERIFICATION_DIR / "milestone_4_fingerprints.sql"
    code = re.sub(r"'[^']*'", "''", strip_comments(path.read_text()).lower())
    statements = [" ".join(s.split()) for s in code.split(";") if s.strip()]
    assert statements[0] == "begin transaction read only"
    assert statements[1:3] == ["set local timezone = ''", "set local datestyle = ''"]
    assert statements[-1] == "rollback"
    assert len(statements) == 5 and statements[3].startswith("select")
    assert not re.search(r"\b(insert|update|delete|alter|create|drop|grant|truncate)\b", code)
    text = path.read_text()
    for table in table_bodies(TABLES_SQL):
        assert f"from growthpulse.{table})" in text, table
    for column in ("client_id", "ad_account_id", "run_kind", "data_quality"):
        assert column not in text  # Milestone 5 columns never enter a fingerprint
