"""Enable login for the two GrowthPulse roles with generated passwords and write .env.

Usage, run by the project owner only:

    uv run python -m growthpulse_ai.persistence.provision_roles \\
        --project-ref <ref> --pooler-host <aws-N-region.pooler.supabase.com>

How it keeps the credentials safe:

- It prompts once for the owner (postgres) password, without echo, and refuses to run
  without an interactive terminal. The password is used for this one connection only,
  with SCRAM authentication required, so it is never sent to the server in plain text.
  It is never stored, logged or printed.
- It generates a 40-character random password per role. Nobody sees or types them.
- libpq hashes each password on this machine (SCRAM-SHA-256). The server receives
  only the hash, so the plaintext never reaches Supabase, its logs, or any history.
- Login and password are set in one statement, and both roles in one transaction.
- The connection strings are written only to .env, created with mode 600. The new
  .env is prepared before the transaction commits and moved into place after it, so a
  failure leaves the roles unchanged or reports exactly what to do.
- Nothing secret is printed. Errors show only their type.

It refuses to run if .env already holds database URLs, or if a role can already log in,
unless --rotate is given. Rotating replaces both passwords.
"""

import argparse
import getpass
import os
import re
import secrets
import string
import sys
import warnings
from pathlib import Path

import psycopg
from psycopg import conninfo as pg_conninfo
from psycopg import sql

from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.persistence.connection import BACKEND_ROLE, DASHBOARD_ROLE

ROLES = {BACKEND_ROLE: "DATABASE_URL", DASHBOARD_ROLE: "DASHBOARD_DATABASE_URL"}
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 40
PROJECT_REF_PATTERN = re.compile(r"[a-z]{20}")
POOLER_HOST_PATTERN = re.compile(r"aws-\d+-[a-z0-9-]+\.pooler\.supabase\.com")
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"


class ProvisioningError(Exception):
    """Provisioning stopped. The message is safe to print."""


def generate_password() -> str:
    """Letters and digits only, so the password needs no URL encoding."""
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def database_url(role: str, password: str, project_ref: str, pooler_host: str) -> str:
    return (
        f"postgresql://{role}.{project_ref}:{password}@{pooler_host}:5432/postgres?sslmode=require"
    )


def _key_pattern(key: str) -> re.Pattern[str]:
    """A dotenv assignment of key, including `export KEY=` and spaces around `=`."""
    return re.compile(rf"^\s*(?:export\s+)?{key}\s*=\s*(.*)$")


def _assignments(text: str, key: str) -> list[tuple[int, str]]:
    pattern = _key_pattern(key)
    return [
        (index, match.group(1))
        for index, line in enumerate(text.splitlines())
        if (match := pattern.match(line))
    ]


def env_has_database_urls(text: str) -> bool:
    return any(value.strip() for key in ROLES.values() for _, value in _assignments(text, key))


def check_env_layout(text: str) -> None:
    """Refuse a key assigned twice: the later line would silently win when .env loads."""
    for key in ROLES.values():
        if len(_assignments(text, key)) > 1:
            raise ProvisioningError(f"{key} appears more than once in .env. Keep one line first")


def updated_env(text: str, values: dict[str, str]) -> str:
    """Set each KEY=value line, replacing its existing line or appending one.

    Every other line is kept exactly as it was.
    """
    lines = text.splitlines()
    for key, value in values.items():
        replacement = f"{key}={value}"
        found = _assignments("\n".join(lines), key)
        if found:
            lines[found[0][0]] = replacement
        else:
            lines.append(replacement)
    return "\n".join(lines) + "\n"


def write_private_file(path: Path, text: str) -> None:
    """Create a new file readable only by its owner. Fails if it already exists."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Make a rename in this directory durable."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def check_preconditions(cursor: psycopg.Cursor, owner_role: str, rotate: bool) -> None:
    if cursor.execute("select current_user").fetchone()[0] != owner_role:
        raise ProvisioningError(f"Connect as the project owner ({owner_role})")
    encryption = cursor.execute("show password_encryption").fetchone()[0]
    if encryption != "scram-sha-256":
        raise ProvisioningError("The server's password_encryption must be scram-sha-256")
    rows = cursor.execute(
        """
        select rolname, rolsuper or rolcreaterole or rolcreatedb or rolbypassrls
            or rolreplication, rolcanlogin
        from pg_catalog.pg_roles where rolname = any(%s)
        """,
        (list(ROLES),),
    ).fetchall()
    found = {name: (elevated, can_login) for name, elevated, can_login in rows}
    for role in ROLES:
        if role not in found:
            raise ProvisioningError(f"Role {role} does not exist")
        elevated, can_login = found[role]
        if elevated:
            raise ProvisioningError(f"Role {role} has an elevated attribute; stopping")
        if can_login and not rotate:
            raise ProvisioningError(f"Role {role} can already log in. Use --rotate to replace")
    memberships = cursor.execute(
        """
        select count(*) from pg_catalog.pg_auth_members as m
        join pg_catalog.pg_roles as r on r.oid = m.member
        where r.rolname = any(%s)
        """,
        (list(ROLES),),
    ).fetchone()[0]
    if memberships:
        raise ProvisioningError("An app role is a member of another role; stopping")


def provision(
    connection: psycopg.Connection,
    *,
    project_ref: str,
    pooler_host: str,
    env_path: Path = ENV_PATH,
    env_example_path: Path = ENV_EXAMPLE_PATH,
    rotate: bool = False,
    owner_role: str = "postgres",
) -> list[str]:
    """Set both roles' passwords and login, and write their URLs to env_path."""
    if not PROJECT_REF_PATTERN.fullmatch(project_ref):
        raise ProvisioningError("project_ref must be the 20-letter Supabase project ref")
    if not POOLER_HOST_PATTERN.fullmatch(pooler_host):
        raise ProvisioningError("pooler_host must look like aws-N-region.pooler.supabase.com")

    current = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    if current is not None:
        check_env_layout(current)
        if env_has_database_urls(current) and not rotate:
            raise ProvisioningError(".env already holds database URLs. Use --rotate to replace")
    base = current if current is not None else env_example_path.read_text(encoding="utf-8")

    staged = env_path.with_name(".env.provisioning")
    if staged.exists():
        raise ProvisioningError(f"Remove the leftover {staged.name} file first")
    passwords = {role: generate_password() for role in ROLES}

    committing = False
    try:
        with connection.transaction(), connection.cursor() as cursor:
            check_preconditions(cursor, owner_role, rotate)
            for role, password in passwords.items():
                verifier = connection.pgconn.encrypt_password(
                    password.encode(), role.encode(), b"scram-sha-256"
                )
                cursor.execute(
                    sql.SQL("alter role {} with login password {}").format(
                        sql.Identifier(role), sql.Literal(verifier.decode())
                    )
                )
            urls = {
                ROLES[role]: database_url(role, password, project_ref, pooler_host)
                for role, password in passwords.items()
            }
            write_private_file(staged, updated_env(base, urls))
            urls.clear()
            # Nothing below can fail except the COMMIT that leaving this block sends.
            committing = True
    except BaseException as error:
        if not committing:
            # The transaction rolled back: no role changed, so discard the new values.
            staged.unlink(missing_ok=True)
            raise
        # The COMMIT itself failed, for example because the connection dropped. The
        # server may or may not have applied it, so keep the only copy of the passwords.
        raise ProvisioningError(
            f"The commit outcome is unknown ({type(error).__name__}). The new connection "
            f"strings are kept in {staged.name} (mode 600). Run the read-only verification: "
            f"if both roles show login=t, rename {staged.name} to .env; if they show "
            f"login=f, nothing changed, so delete {staged.name} and run this again"
        ) from None
    finally:
        passwords.clear()

    try:
        os.replace(staged, env_path)
        os.chmod(env_path, 0o600)
        _fsync_directory(env_path.parent)
    except OSError:
        raise ProvisioningError(
            f"Roles were updated, but .env could not be replaced. The new values are in "
            f"{staged.name} (mode 600). Rename it to .env, or rerun with --rotate"
        ) from None
    return list(ROLES)


def read_owner_password() -> str:
    """Prompt without echo. Refuse anything that is not an interactive terminal.

    Without a terminal, getpass falls back to reading stdin with echo on and only warns.
    That warning is turned into a refusal here.
    """
    if not sys.stdin.isatty():
        raise ProvisioningError("Run this in an interactive terminal; the prompt needs a TTY")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Supabase database password for postgres (not echoed): ")
        except getpass.GetPassWarning:
            raise ProvisioningError("The password prompt cannot hide input here") from None
    if not password:
        raise ProvisioningError("No password entered")
    return password


def owner_conninfo(
    project_ref: str, pooler_host: str, password: str, sslrootcert: Path | None
) -> str:
    """Owner connection settings.

    require_auth=scram-sha-256 makes libpq refuse any server that asks for the password in
    plain text or as MD5, so even a server impersonating the pooler cannot obtain it.
    With a CA certificate, verify-full also checks the server's identity.
    """
    tls: dict[str, str] = {"sslmode": "require"}
    if sslrootcert is not None:
        tls = {"sslmode": "verify-full", "sslrootcert": str(sslrootcert)}
    return pg_conninfo.make_conninfo(
        "",
        host=pooler_host,
        port=5432,
        dbname="postgres",
        user=f"postgres.{project_ref}",
        password=password,
        require_auth="scram-sha-256",
        connect_timeout=10,
        application_name="growthpulse-provision-roles",
        **tls,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--pooler-host", required=True)
    parser.add_argument("--rotate", action="store_true", help="replace existing passwords")
    parser.add_argument(
        "--sslrootcert",
        type=Path,
        help="Supabase CA certificate file; switches TLS to verify-full",
    )
    args = parser.parse_args(argv)
    if not PROJECT_REF_PATTERN.fullmatch(args.project_ref) or not POOLER_HOST_PATTERN.fullmatch(
        args.pooler_host
    ):
        print("Invalid --project-ref or --pooler-host.")
        return 2
    if args.sslrootcert is not None and not args.sslrootcert.is_file():
        print("--sslrootcert must point to an existing certificate file.")
        return 2

    try:
        owner_password = read_owner_password()
    except ProvisioningError as error:
        print(f"Stopped: {error}. No role was changed.")
        return 1
    conninfo = owner_conninfo(args.project_ref, args.pooler_host, owner_password, args.sslrootcert)
    del owner_password
    try:
        with psycopg.connect(conninfo, autocommit=True, prepare_threshold=None) as connection:
            del conninfo
            if not connection.pgconn.ssl_in_use:
                print("Refusing to continue: the connection is not using TLS.")
                return 1
            roles = provision(
                connection,
                project_ref=args.project_ref,
                pooler_host=args.pooler_host,
                rotate=args.rotate,
            )
    except ProvisioningError as error:
        print(f"Stopped: {error}")
        return 1
    except psycopg.Error as error:
        # Every database error that reaches here happened before COMMIT, so the
        # transaction rolled back. COMMIT failures are reported by provision() instead.
        print(f"Stopped: database error ({type(error).__name__}). No role was changed.")
        return 1
    print(f"Login enabled with new passwords for: {', '.join(roles)}.")
    print("Their connection strings are in .env (mode 600). No password was displayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
