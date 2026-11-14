"""Static checks for Milestone 7a.2 (tenant security foundation). No database is involved.

tests/test_postgres_tenancy.py runs the same SQL on PostgreSQL 17 with two organizations.
"""

import re
from pathlib import Path

from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.persistence import context, load, store, tenancy

MIGRATIONS = sorted((PROJECT_ROOT / "supabase" / "migrations").glob("*.sql"))
M7A = MIGRATIONS[8:]
IDENTITY_SQL, CONTEXT_SQL, HARDEN_SQL, POLICIES_SQL = (path.read_text() for path in M7A)
M7A_SQL = "\n".join(path.read_text() for path in M7A)
REVERT = PROJECT_ROOT / "supabase" / "rollback" / "20261114100000_revert_m7a2_tenant_security.sql"
SRC = PROJECT_ROOT / "src" / "growthpulse_ai"
DESIGN = PROJECT_ROOT / "docs" / "M7A_TENANT_SECURITY.md"


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_the_four_migrations_are_named_and_ordered():
    assert [path.name for path in M7A] == [
        "20261114100000_create_identity_tables.sql",
        "20261114100100_create_request_context.sql",
        "20261114100200_harden_definer_functions.sql",
        "20261114100300_tenant_policies.sql",
    ]


def test_out_of_scope_features_are_not_created():
    """Amendment 5: no invitations, billing, BYOK or live OAuth state in this phase."""
    code = strip_comments(M7A_SQL).lower()
    for name in ("invitation", "billing", "ai_credentials", "oauth_states", "connection_requests"):
        assert name not in code, name


def test_context_is_only_ever_transaction_local_in_application_code():
    """Through a transaction pooler a session-level setting leaks into other clients."""
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"set_config\([^)]*,\s*false\s*\)", text, re.I), path
        assert not re.search(r"""["']\s*set\s+(session\s+)?growthpulse\.""", text, re.I), path
        assert not re.search(r"""["']\s*set\s+(local\s+)?role\b""", text, re.I), path
    for code in (strip_comments(CONTEXT_SQL), strip_comments(HARDEN_SQL)):
        for call in re.findall(r"set_config\((.*?)\);", code, re.S):
            assert call.rstrip().endswith("true"), call


def test_every_pipeline_transaction_starts_with_a_job_scope():
    for module in (store, load, tenancy):
        source = Path(module.__file__).read_text()
        blocks = re.findall(r"with connection\.transaction\(\).*?:\n((?:.*\n){1,4})", source)
        assert blocks, module.__name__
        for block in blocks:
            if "SELECT_DUE_ACCOUNTS" in block:  # the one cross-organization read, IDs only
                continue
            assert "begin_job(" in block, (module.__name__, block)


def test_the_upgrade_compatibility_check_is_documented_for_removal():
    assert "Remove this check once migrations 9-12 are applied on Supabase" in (
        context.begin_job.__doc__ or ""
    )
    # And the G4 checklist makes it a step, so it can't persist unnoticed.
    g4 = (PROJECT_ROOT / "G4_MIGRATION_CHECKLIST.md").read_text()
    section = g4.split("## 7. Remove the pre-migration fallback")[1].split("\n## ")[0]
    for name in ("context_installed", "CONTEXT_INSTALLED", "SELECT_DUE_ACCOUNTS_M5"):
        assert name in section, name


def test_the_g4_checklist_applies_only_the_approved_four_and_checks_isolation():
    g4 = (PROJECT_ROOT / "G4_MIGRATION_CHECKLIST.md").read_text()
    for path in M7A:
        assert path.stem.split("_")[0] in g4
    for required in (
        "--dry-run",
        "m7a_checks.sql",
        "m7a_pooler_check.sql",
        "port 6543",
        "Exposed schemas",
        "milestone_4_fingerprints.sql",
    ):
        assert required in g4, required


def definer_functions(sql: str) -> dict[str, str]:
    found = re.findall(
        r"create (?:or replace )?function ([\w.]+)\((.*?)\$\$;", strip_comments(sql), re.S
    )
    return {name: body for name, body in found if "security definer" in body}


def test_every_new_function_pins_an_empty_search_path():
    pattern = r"create (?:or replace )?function .*?\$\$;"
    functions = re.findall(pattern, strip_comments(M7A_SQL), re.S)
    assert len(functions) >= 30
    assert all("set search_path = ''" in function for function in functions)


def test_internal_functions_are_granted_to_no_role():
    code = strip_comments(M7A_SQL)
    granted = set(re.findall(r"grant execute on function ([\w.]+)\(", code))
    for internal in (
        "growthpulse_auth.context_mac", "growthpulse_auth.set_context",
        "growthpulse_auth.current_context", "growthpulse_auth.target_workspace",
        "growthpulse_auth.action_permission", "growthpulse_auth.role_permits",
        "growthpulse_auth.credential_permits",
        "growthpulse_auth.require_credential_permission", "growthpulse.log_owner_action",
    ):  # fmt: skip
        assert internal not in granted, internal


def test_the_unverified_session_functions_have_no_application_caller_yet():
    """Session creation trusts its caller to have verified the Supabase login, which
    nothing does until M7b. Until then no application code may reach it, so no endpoint
    can. The M7b sign-in service verifies the login token first, and then removes this test.
    """
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for name in ("create_session", "revoke_session", "growthpulse_authenticator"):
            assert name not in text, (path, name)
    code = strip_comments(CONTEXT_SQL)
    for function in ("create_session", "revoke_session"):
        pattern = rf"grant execute on function growthpulse_auth\.{function}\(.*?;"
        grants = re.findall(pattern, code, re.S)
        assert grants, function
        assert all(g.rstrip(";").endswith("to growthpulse_authenticator") for g in grants)
    assert "UNVERIFIED" in CONTEXT_SQL and "session.create_unverified" in CONTEXT_SQL


def test_every_token_function_checks_authorization_before_anything_else():
    bodies = definer_functions(HARDEN_SQL)
    for name, permission in (
        ("store_credential", "connection.create"), ("read_credentials", "connection.read"),
        ("delete_credentials", "connection.revoke"),
        ("mark_connection_needs_reauth", "connection.revoke"),
        ("credentials_needing_rotation", "connection.read"),
    ):  # fmt: skip
        body = bodies[f"growthpulse_secrets.{name}"]
        first = re.search(r"\bbegin\s+(.*?);", body, re.S).group(1)
        assert first.startswith("perform growthpulse_auth.require_credential_permission("), name
        assert f"'{permission}')" in first, name


def test_token_functions_check_the_context_organization():
    bodies = definer_functions(HARDEN_SQL)
    for name in (
        "store_credential", "read_credentials", "delete_credentials",
        "mark_connection_needs_reauth", "credentials_needing_rotation",
    ):  # fmt: skip
        assert "growthpulse_auth.context_workspace()" in bodies[f"growthpulse_secrets.{name}"]


def test_every_dashboard_view_is_recreated_with_a_tenant_filter():
    earlier = "\n".join(path.read_text() for path in MIGRATIONS[:8])
    views = set(re.findall(r"create (?:or replace )?view (growthpulse_read\.\w+)", earlier))
    assert len(views) == 17
    recreated = {
        name: (options, body)
        for name, options, body in re.findall(
            r"create (?:or replace )?view (growthpulse_read\.\w+)\s+with \((.*?)\) as(.*?);\n",
            POLICIES_SQL,
            re.S,
        )
    }
    assert views <= set(recreated)
    for name, (options, body) in recreated.items():
        assert options == "security_invoker = false, security_barrier = true", name
        assert "growthpulse_auth.request_" in body, name


def test_every_writer_policy_is_altered_to_check_the_job():
    earlier = "\n".join(path.read_text() for path in MIGRATIONS[:8])
    pattern = r"create policy (\w+) on (growthpulse\.\w+)\s+for \w+ to growthpulse_writer"
    created = set(re.findall(pattern, earlier))
    altered = set(re.findall(r"alter policy (\w+) on (growthpulse\.\w+)", POLICIES_SQL))
    assert created and created == altered


def test_the_revert_restores_the_milestone_5_text_verbatim():
    revert = REVERT.read_text()
    sources = {
        "20261111100200_create_dashboard_views.sql": ["experiment_status_history"],
        "20261111200400_update_grants_and_views.sql": None,
    }
    for file, only in sources.items():
        text = (PROJECT_ROOT / "supabase" / "migrations" / file).read_text()
        for match in re.finditer(r"^create (?:or replace )?view (\S+)\s.*?;\n", text, re.S | re.M):
            name = match.group(1).split(".")[1]
            if only is not None and name not in only:
                continue
            body = re.sub(r"^create (?:or replace )?view", "create or replace view", match.group(0))
            assert body in revert, name
            assert f"alter view {match.group(1)} reset (security_barrier);" in revert, name
    for file, names in {
        "20261111200200_create_token_storage.sql": [
            "store_credential", "read_credentials", "delete_credentials",
            "mark_connection_needs_reauth", "credentials_needing_rotation",
        ],
        "20261111200300_create_owner_actions.sql": ["log_owner_action"],
    }.items():  # fmt: skip
        text = (PROJECT_ROOT / "supabase" / "migrations" / file).read_text()
        for name in names:
            original = re.search(rf"^create function [\w.]+{name}\(.*?^\$\$;\n", text, re.S | re.M)
            assert original, name
            restored = original.group(0).replace("create function", "create or replace function", 1)
            assert restored in revert, name


def test_the_revert_drops_everything_the_migrations_create():
    revert = strip_comments(REVERT.read_text())
    assert "cascade" not in revert.lower()
    for function in re.findall(r"create function ([\w.]+)\(", M7A_SQL):
        assert f"drop function {function}(" in revert, function
    for table in re.findall(r"create table ([\w.]+)", M7A_SQL):
        assert f"drop table {table};" in revert, table
    pattern = r"create trigger (\w+)\s+\w+ \w+(?: or \w+)? on ([\w.]+)"
    for trigger, table in re.findall(pattern, M7A_SQL):
        if not table.startswith("growthpulse_auth."):  # dropped with their tables
            assert f"drop trigger {trigger} on {table};" in revert, trigger
    for role in re.findall(r"create role (\w+)", M7A_SQL):
        assert f"drop role {role};" in revert, role
    assert "drop view growthpulse_read.audit_events;" in revert
    assert "drop schema growthpulse_auth;" in revert


def test_the_revert_refuses_to_lose_identity_rows_by_default():
    revert = REVERT.read_text()
    assert "growthpulse.allow_identity_data_loss" in revert
    assert "Revert refused: identity rows exist" in revert
    code = strip_comments(revert).strip()
    assert code.startswith("begin;") and code.endswith("commit;")


def test_the_drop_all_script_covers_milestone_7a():
    drop_all = (
        PROJECT_ROOT / "supabase" / "rollback" / "20261111100000_drop_growthpulse.sql"
    ).read_text()
    for function in re.findall(r"create function ([\w.]+)\(", M7A_SQL):
        assert f"{function}(" in drop_all, function
    for table in re.findall(r"create table ([\w.]+)", M7A_SQL):
        assert table in drop_all, table
    assert "drop role if exists growthpulse_authenticator;" in drop_all


def test_the_design_document_exists_and_covers_the_amendments():
    text = DESIGN.read_text()
    for heading in (
        "Trust model",
        "SECURITY DEFINER review",
        "Recoverable OAuth revocation",
        "multiple organizations",
        "Unresolved risks",
        "first real Google OAuth test",
    ):
        assert heading in text, heading
