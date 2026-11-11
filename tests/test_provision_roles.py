import re
import stat
from pathlib import Path

import psycopg
import pytest

from fake_db import FakeDatabase
from growthpulse_ai.persistence import provision_roles
from growthpulse_ai.persistence.provision_roles import (
    ProvisioningError,
    database_url,
    env_has_database_urls,
    generate_password,
    provision,
    updated_env,
    write_private_file,
)

REF = "abcdefghijklmnopqrst"
HOST = "aws-1-ap-southeast-2.pooler.supabase.com"
SECRET = "hunter2-not-a-real-password"


def test_generated_passwords_are_long_random_and_url_safe():
    passwords = {generate_password() for _ in range(50)}
    assert len(passwords) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9]{40}", p) for p in passwords)


def test_database_url_uses_pooler_username_and_tls():
    url = database_url("growthpulse_writer", "abc123", REF, HOST)
    assert url == (
        f"postgresql://growthpulse_writer.{REF}:abc123@{HOST}:5432/postgres?sslmode=require"
    )


def test_env_update_replaces_blank_keys_and_keeps_everything_else():
    text = "# comment\nAPP_ENV=local\nDATABASE_URL=\nDASHBOARD_DATABASE_URL=\nOTHER=1\n"
    result = updated_env(text, {"DATABASE_URL": "a", "DASHBOARD_DATABASE_URL": "b"})
    assert result == "# comment\nAPP_ENV=local\nDATABASE_URL=a\nDASHBOARD_DATABASE_URL=b\nOTHER=1\n"


def test_env_update_appends_missing_keys():
    assert updated_env("APP_ENV=local\n", {"DATABASE_URL": "a"}) == (
        "APP_ENV=local\nDATABASE_URL=a\n"
    )


def test_env_update_treats_values_literally():
    # Backslashes or group references in a value must not be interpreted by re.sub.
    result = updated_env("DATABASE_URL=\n", {"DATABASE_URL": r"x\1\g<0>"})
    assert result == "DATABASE_URL=x\\1\\g<0>\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DATABASE_URL=\nDASHBOARD_DATABASE_URL=\n", False),
        ("DATABASE_URL=postgresql://x\n", True),
        ("DASHBOARD_DATABASE_URL=postgresql://x\n", True),
        ("# DATABASE_URL=postgresql://x\n", False),
    ],
)
def test_existing_urls_are_detected(text: str, expected: bool):
    assert env_has_database_urls(text) is expected


def test_private_file_is_owner_only_and_never_overwritten(tmp_path: Path):
    path = tmp_path / ".env.provisioning"
    write_private_file(path, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private_file(path, "y")


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        ("short", HOST),
        (REF.upper(), HOST),
        (REF, "db.abcdefghijklmnopqrst.supabase.co"),
        (REF, "evil.example.com"),
    ],
)
def test_bad_ref_or_host_is_refused_before_connecting(tmp_path: Path, ref: str, host: str):
    database = FakeDatabase()
    with pytest.raises(ProvisioningError):
        provision(database.connect(), project_ref=ref, pooler_host=host, env_path=tmp_path / "e")
    assert database.statements == []


def test_existing_urls_are_kept_unless_rotating(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql://kept\n")
    database = FakeDatabase()
    with pytest.raises(ProvisioningError, match="--rotate"):
        provision(database.connect(), project_ref=REF, pooler_host=HOST, env_path=env)
    assert env.read_text() == "DATABASE_URL=postgresql://kept\n"
    assert database.statements == []


def test_leftover_staged_file_stops_provisioning(tmp_path: Path):
    (tmp_path / ".env.provisioning").write_text("old")
    database = FakeDatabase()
    with pytest.raises(ProvisioningError, match="leftover"):
        provision(
            database.connect(),
            project_ref=REF,
            pooler_host=HOST,
            env_path=tmp_path / ".env",
            env_example_path=Path(provision_roles.ENV_EXAMPLE_PATH),
        )
    assert database.statements == []


def test_cli_rejects_bad_arguments_without_prompting(monkeypatch, capsys):
    monkeypatch.setattr(provision_roles.getpass, "getpass", pytest.fail)
    assert provision_roles.main(["--project-ref", "x", "--pooler-host", HOST]) == 2


def test_cli_hides_driver_messages(monkeypatch, capsys):
    monkeypatch.setattr(provision_roles, "read_owner_password", lambda: SECRET)

    def refuse(*_, **__):
        raise psycopg.OperationalError(f"password authentication failed {SECRET}")

    monkeypatch.setattr(provision_roles.psycopg, "connect", refuse)
    assert provision_roles.main(["--project-ref", REF, "--pooler-host", HOST]) == 1
    output = capsys.readouterr().out
    assert SECRET not in output
    assert "OperationalError" in output
    assert "No role was changed" in output


def test_cli_passes_only_validated_settings_to_the_driver(monkeypatch, capsys):
    monkeypatch.setattr(provision_roles, "read_owner_password", lambda: SECRET)
    seen = {}

    def capture(conninfo, **kwargs):
        seen.update(psycopg.conninfo.conninfo_to_dict(conninfo), **kwargs)
        raise psycopg.OperationalError("stop here")

    monkeypatch.setattr(provision_roles.psycopg, "connect", capture)
    provision_roles.main(["--project-ref", REF, "--pooler-host", HOST])
    assert seen["host"] == HOST
    assert seen["user"] == f"postgres.{REF}"
    assert seen["require_auth"] == "scram-sha-256"
    assert seen["sslmode"] == "require"
    assert seen["autocommit"] is True
    assert SECRET not in capsys.readouterr().out


# Owner password prompt


def test_prompt_refuses_without_a_terminal(monkeypatch):
    monkeypatch.setattr(provision_roles.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(provision_roles.getpass, "getpass", pytest.fail)
    with pytest.raises(ProvisioningError, match="interactive terminal"):
        provision_roles.read_owner_password()


def test_prompt_refuses_when_input_would_be_echoed(monkeypatch):
    class Terminal:
        def isatty(self):
            return True

    def echoing_getpass(prompt):
        import warnings

        warnings.warn("Can not control echo", provision_roles.getpass.GetPassWarning, stacklevel=1)
        return SECRET

    monkeypatch.setattr(provision_roles.sys, "stdin", Terminal())
    monkeypatch.setattr(provision_roles.getpass, "getpass", echoing_getpass)
    with pytest.raises(ProvisioningError, match="cannot hide input") as caught:
        provision_roles.read_owner_password()
    assert SECRET not in str(caught.value)


def test_prompt_refuses_an_empty_password(monkeypatch):
    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(provision_roles.sys, "stdin", Terminal())
    monkeypatch.setattr(provision_roles.getpass, "getpass", lambda prompt: "")
    with pytest.raises(ProvisioningError, match="No password"):
        provision_roles.read_owner_password()


def test_cli_stops_cleanly_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(provision_roles.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(provision_roles.psycopg, "connect", pytest.fail)
    assert provision_roles.main(["--project-ref", REF, "--pooler-host", HOST]) == 1
    assert "No role was changed" in capsys.readouterr().out


# Owner connection settings


def test_owner_connection_requires_scram_and_tls():
    params = psycopg.conninfo.conninfo_to_dict(
        provision_roles.owner_conninfo(REF, HOST, SECRET, None)
    )
    assert params["require_auth"] == "scram-sha-256"
    assert params["sslmode"] == "require"
    assert params["user"] == f"postgres.{REF}"
    assert params["port"] == "5432"
    assert "sslrootcert" not in params


def test_owner_connection_can_verify_the_server_certificate(tmp_path: Path):
    cert = tmp_path / "ca.crt"
    cert.write_text("placeholder")
    params = psycopg.conninfo.conninfo_to_dict(
        provision_roles.owner_conninfo(REF, HOST, SECRET, cert)
    )
    assert params["sslmode"] == "verify-full"
    assert params["sslrootcert"] == str(cert)


def test_cli_rejects_a_missing_certificate_file(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(provision_roles, "read_owner_password", pytest.fail)
    argv = ["--project-ref", REF, "--pooler-host", HOST, "--sslrootcert", str(tmp_path / "x")]
    assert provision_roles.main(argv) == 2


# Generated connection strings


def test_generated_urls_pass_the_application_tls_check():
    from pydantic import SecretStr

    from growthpulse_ai.persistence.connection import secure_conninfo

    url = database_url("growthpulse_writer", generate_password(), REF, HOST)
    params = psycopg.conninfo.conninfo_to_dict(secure_conninfo(SecretStr(url), "DATABASE_URL"))
    assert params["user"] == f"growthpulse_writer.{REF}"
    assert params["host"] == HOST
    assert params["port"] == "5432"
    assert params["dbname"] == "postgres"
    assert params["sslmode"] == "require"


# .env layouts


@pytest.mark.parametrize(
    "text",
    [
        "export DATABASE_URL=postgresql://x\n",
        "DATABASE_URL = postgresql://x\n",
        "  DASHBOARD_DATABASE_URL=postgresql://x\n",
    ],
)
def test_other_dotenv_forms_count_as_existing_urls(text: str):
    assert env_has_database_urls(text) is True


def test_a_key_set_twice_is_refused(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=\nAPP_ENV=local\nDATABASE_URL=postgresql://stale\n")
    database = FakeDatabase()
    with pytest.raises(ProvisioningError, match="more than once"):
        provision(database.connect(), project_ref=REF, pooler_host=HOST, env_path=env, rotate=True)
    assert database.statements == []


def test_env_update_replaces_the_export_form_in_place():
    text = "A=1\nexport DATABASE_URL=old\nB=2\n"
    assert updated_env(text, {"DATABASE_URL": "new"}) == "A=1\nDATABASE_URL=new\nB=2\n"


def test_env_update_keeps_every_other_line_byte_for_byte():
    text = "# keep me\nANTHROPIC_API_KEY=sk-test-value\n\nLOG_LEVEL=INFO  \nDATABASE_URL=\n"
    result = updated_env(text, {"DATABASE_URL": "new", "DASHBOARD_DATABASE_URL": "dash"})
    assert result.splitlines()[:4] == text.splitlines()[:4]
    assert result.endswith("DATABASE_URL=new\nDASHBOARD_DATABASE_URL=dash\n")


@pytest.mark.parametrize("module", ["provision_roles", "connection_check"])
def test_tools_import_nothing_that_reaches_the_network_or_runs_programs(module: str):
    import ast

    from growthpulse_ai.config import PROJECT_ROOT

    source = PROJECT_ROOT / "src" / "growthpulse_ai" / "persistence" / f"{module}.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {
        "argparse",
        "getpass",
        "os",
        "re",
        "secrets",
        "string",
        "sys",
        "warnings",
        "pathlib",
        "collections",
        "psycopg",
        "growthpulse_ai",
    }
    assert imported <= allowed, imported - allowed
