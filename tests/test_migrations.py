"""Static checks on the SQL migrations. No database is involved.

These catch drift between Python and SQL, and grants that would widen access, before
anything is applied. tests/test_postgres_integration.py runs the same SQL on a server.
"""

import re
from enum import StrEnum
from pathlib import Path

import pytest

from growthpulse_ai.analytics.anomalies import Direction, RuleId, Severity
from growthpulse_ai.analytics.noise import NoiseGuardOutcome
from growthpulse_ai.analytics.periods import ComparisonPurpose
from growthpulse_ai.config import PROJECT_ROOT
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
MIGRATIONS = sorted(MIGRATIONS_DIR.glob("*.sql"))
SQL = "\n".join(path.read_text() for path in MIGRATIONS)
TABLES_SQL = MIGRATIONS[0].read_text()
ACCESS_SQL = MIGRATIONS[1].read_text()
VIEWS_SQL = MIGRATIONS[2].read_text()
API_ROLES = ("public", "anon", "authenticated", "service_role")
APPEND_ONLY = ("analytics_reports", "experiment_proposals", "experiment_status_events")


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def table_columns() -> dict[str, set[str]]:
    tables = {}
    for name, body in re.findall(
        r"create table growthpulse\.(\w+) \((.*?)\n\);", strip_comments(TABLES_SQL), re.DOTALL
    ):
        columns = set()
        for line in body.splitlines():
            match = re.match(r"\s{4}(\w+) ", line)
            if match and match.group(1) not in {"constraint", "check"}:
                columns.add(match.group(1))
        tables[name] = columns
    return tables


def check_values(column: str) -> list[set[str]]:
    """Every allowed-value list for a column, across all tables."""
    pattern = rf"\b{column} in \(([^)]*)\)"
    return [set(re.findall(r"'(\w+)'", match)) for match in re.findall(pattern, TABLES_SQL)]


def writer_update_grants() -> dict[str, set[str]]:
    grants = {}
    for columns, table in re.findall(
        r"grant update \((.*?)\)\s+on growthpulse\.(\w+) to growthpulse_writer",
        ACCESS_SQL,
        re.DOTALL,
    ):
        grants[table] = {column.strip() for column in columns.split(",")}
    return grants


def test_migrations_are_ordered_and_named():
    assert [path.name[:14] for path in MIGRATIONS] == sorted(path.name[:14] for path in MIGRATIONS)
    assert all(re.fullmatch(r"\d{14}_[a-z_]+\.sql", path.name) for path in MIGRATIONS)
    assert len(MIGRATIONS) == 3


def test_every_expected_table_exists():
    assert set(table_columns()) == {
        "pipeline_runs",
        "ad_performance_records",
        "quarantined_records",
        "analytics_reports",
        "findings",
        "experiment_proposals",
        "experiment_status_events",
    }


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
        }, table


# Access control


def test_every_table_has_row_level_security():
    enabled = set(re.findall(r"alter table growthpulse\.(\w+) enable row level security", SQL))
    assert enabled == set(table_columns())


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
    ]


def test_writer_never_gets_delete_truncate_or_schema_rights():
    for grant in re.findall(r"grant [^;]*to growthpulse_writer;", SQL):
        assert not re.search(r"\b(delete|truncate|all|create|references|trigger)\b", grant), grant


def test_history_tables_cannot_be_updated_by_the_writer():
    assert not set(writer_update_grants()) & set(APPEND_ONLY)
    for table in APPEND_ONLY:
        assert f"before update on growthpulse.{table}" in TABLES_SQL


def test_pipeline_can_only_propose_experiments():
    policy = re.search(r"create policy writer_propose_only.*?;", ACCESS_SQL, re.DOTALL).group(0)
    assert "status = 'proposed'" in policy
    assert not re.search(r"for update to growthpulse_writer[^;]*experiment", ACCESS_SQL)


def test_migrations_hold_no_secrets_or_privileged_attributes():
    lowered = strip_comments(SQL).lower()
    for forbidden in ("password", "superuser", "bypassrls", "createrole", "security definer"):
        assert forbidden not in lowered, forbidden
    assert not re.search(r"\blogin\b", lowered.replace("nologin", ""))


def test_functions_pin_their_search_path():
    functions = re.findall(r"create function .*?\$\$;", TABLES_SQL, re.DOTALL)
    assert functions
    assert all("set search_path = ''" in function for function in functions)


def test_views_never_expose_raw_payloads():
    assert "raw_payload" not in strip_comments(VIEWS_SQL)


def test_every_view_states_its_security_mode():
    views = re.findall(r"create view growthpulse_read\.\w+ with \((.*?)\)", VIEWS_SQL)
    assert len(views) == VIEWS_SQL.count("create view")
    assert set(views) == {"security_invoker = false"}


def test_rollback_script_is_kept_out_of_the_migrations_folder():
    rollbacks = list(ROLLBACK_DIR.glob("*.sql"))
    assert rollbacks
    names = {path.name for path in MIGRATIONS}
    assert not names & {path.name for path in rollbacks}


def rollback_statements() -> list[str]:
    sql = strip_comments(next(ROLLBACK_DIR.glob("*.sql")).read_text())
    return [" ".join(s.split()) for s in sql.split(";") if s.strip()]


def test_rollback_only_drops_growthpulse_objects_by_name():
    statements = rollback_statements()
    assert statements[0] == "begin" and statements[-1] == "commit"
    for statement in statements[1:-1]:
        kind, targets = re.fullmatch(
            r"drop (view|schema|table|function|role) if exists (.+?)(?: restrict)?", statement
        ).groups()
        for target in targets.split(", "):
            assert re.fullmatch(r"growthpulse(_read)?(\.\w+(\(\))?)?|growthpulse_\w+", target), (
                kind,
                target,
            )


def test_rollback_never_cascades():
    statements = rollback_statements()
    assert not any("cascade" in statement for statement in statements)
    drops = [s for s in statements if s.startswith("drop ") and not s.startswith("drop role")]
    assert drops
    assert all(statement.endswith(" restrict") for statement in drops)


def test_rollback_drops_every_object_the_migrations_create():
    text = " ".join(rollback_statements())
    for table in table_columns():
        assert f"growthpulse.{table}" in text
    for view in re.findall(r"create view (growthpulse_read\.\w+)", VIEWS_SQL):
        assert view in text
    for function in re.findall(r"create function (growthpulse\.\w+\(\))", TABLES_SQL):
        assert function in text
    for role in re.findall(r"create role (\w+)", ACCESS_SQL):
        assert f"drop role if exists {role}" in text


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
    keywords |= {"sequences", "functions", "latest"}  # latest is a PL/pgSQL variable
    references = re.findall(
        r"\b(?:table|view|from|join|references|on|into|function)\s+(?=([a-z_][\w.]*))", sql
    )
    assert references
    unqualified = {name for name in references if "." not in name and name not in keywords} - {
        "growthpulse",
        "growthpulse_read",
    }
    assert unqualified == set()
    for name in references:
        if "." in name:
            assert name.split(".")[0] in {"growthpulse", "growthpulse_read", "pg_catalog"}, name


def test_post_migration_checks_are_read_only():
    path = PROJECT_ROOT / "supabase" / "verification" / "post_migration_checks.sql"
    statements = [s.strip() for s in strip_comments(path.read_text()).split(";") if s.strip()]
    assert len(statements) == 11
    for statement in statements:
        assert statement.lower().startswith("select"), statement[:40]
        # Privilege names such as 'CREATE' appear only inside string literals.
        code = re.sub(r"'[^']*'", "''", statement.lower())
        assert not re.search(
            r"\b(insert|update|delete|alter|create|drop|grant|revoke|truncate|set|do)\b", code
        ), statement[:40]


def test_supabase_cli_files_hold_no_secrets():
    for path in [PROJECT_ROOT / "supabase" / "config.toml", *MIGRATIONS]:
        text = Path(path).read_text()
        assert not re.search(r"(sb_secret_|sbp_[0-9a-f]{20}|eyJhbGciOi|://[^:@\s]+:[^@\s]+@)", text)
