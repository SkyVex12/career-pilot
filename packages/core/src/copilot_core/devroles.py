"""Give the five database roles a password and LOGIN on the local stack.

Migrations create the roles NOLOGIN and hold no passwords (README, "Database roles"); each
environment grants login on its own. In AWS that is Terraform (build step 5); locally it is
this command, run once after ``copilot-migrate``::

    uv run copilot-dev-roles            # ALTER ROLE ... LOGIN PASSWORD '<role>-local'
    uv run copilot-dev-roles --print    # only print the per-service URLs

The passwords are fixed and public on purpose: they only ever exist on a Postgres bound to
127.0.0.1. The command refuses any other host.
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from copilot_core.migrate import DEFAULT_DATABASE_URL

ROLES = ("app_owner", "app_api", "app_gateway", "app_worker", "app_system")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def dev_password(role: str) -> str:
    return f"{role}-local"


def role_url(admin_url: str, role: str) -> str:
    parts = urlsplit(admin_url)
    host = parts.hostname or "localhost"
    netloc = f"{role}:{dev_password(role)}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


async def grant_login(admin_url: str) -> None:
    conn = await asyncpg.connect(admin_url)
    try:
        for role in ROLES:
            exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
            if not exists:
                sys.exit(f"role {role} does not exist; run `uv run copilot-migrate` first")
            # ALTER ROLE takes no bind parameters; the password is a fixed local constant.
            await conn.execute(f"alter role {role} login password '{dev_password(role)}'")
    finally:
        await conn.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="Local database logins for the app roles.")
    parser.add_argument(
        "--database-url", default=os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument("--print", action="store_true", help="print the URLs, change nothing")
    args = parser.parse_args()
    url: str = args.database_url
    host = urlsplit(url).hostname or "localhost"
    if host not in LOCAL_HOSTS:
        sys.exit(f"refusing to set dev passwords on {host}: local Postgres only")
    if not args.print:
        asyncio.run(grant_login(args.database_url))
        print(f"LOGIN granted to {', '.join(ROLES)}")
    for name, role in (
        ("COPILOT_API_DATABASE_URL", "app_api"),
        ("COPILOT_WORKER_DATABASE_URL", "app_worker"),
        ("COPILOT_SYSTEM_DATABASE_URL", "app_system"),
        ("COPILOT_GATEWAY_DATABASE_URL", "app_gateway"),
    ):
        print(f"{name}={role_url(args.database_url, role)}")
