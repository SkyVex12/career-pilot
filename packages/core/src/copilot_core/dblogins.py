"""Give the app roles their real passwords and LOGIN, outside the local stack.

Migrations create the roles NOLOGIN and hold no passwords (README, "Database roles"). Locally
`copilot-dev-roles` sets fixed passwords; in AWS this command does it with the passwords
Terraform generated and keeps in Secrets Manager, which ECS hands to the migration task as
environment variables (never on the command line, never logged)::

    COPILOT_DB_PASSWORD_APP_API=... COPILOT_DB_PASSWORD_APP_WORKER=... copilot-db-logins

It runs after ``copilot-migrate`` in the same one-off task, as the database's superuser
(``COPILOT_DATABASE_URL``). A role without a password variable is left NOLOGIN, so a service
that should not connect yet (the gateway, until phase 2 retrieval) cannot. Rerunning it
rotates nothing unless the secret changed; it is idempotent.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

import asyncpg

from copilot_core.devroles import ROLES
from copilot_core.migrate import DEFAULT_DATABASE_URL

MIN_PASSWORD_LENGTH = 24


def env_name(role: str) -> str:
    return f"COPILOT_DB_PASSWORD_{role.upper()}"


def passwords_from(environ: Mapping[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for role in ROLES:
        value = environ.get(env_name(role), "")
        if not value:
            continue
        if len(value) < MIN_PASSWORD_LENGTH:
            raise SystemExit(f"{env_name(role)} is shorter than {MIN_PASSWORD_LENGTH} characters")
        found[role] = value
    return found


async def grant_logins(admin_url: str, passwords: Mapping[str, str]) -> list[str]:
    conn = await asyncpg.connect(admin_url)
    try:
        for role in ROLES:
            if not await conn.fetchval("select 1 from pg_roles where rolname = $1", role):
                raise SystemExit(f"role {role} does not exist; run copilot-migrate first")
            if role in passwords:
                # ALTER ROLE takes no bind parameters: format() quotes the name and literal.
                sql = await conn.fetchval(
                    "select format('alter role %I login password %L', $1::text, $2::text)",
                    role,
                    passwords[role],
                )
            else:
                sql = await conn.fetchval("select format('alter role %I nologin', $1::text)", role)
            await conn.execute(sql)
    finally:
        await conn.close()
    return sorted(passwords)


def run() -> None:
    parser = argparse.ArgumentParser(description="Set the app roles' passwords and LOGIN.")
    parser.add_argument(
        "--database-url", default=os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    args = parser.parse_args()
    passwords = passwords_from(os.environ)
    if not passwords:
        sys.exit("no COPILOT_DB_PASSWORD_<ROLE> variables set; nothing to do")
    granted = asyncio.run(grant_logins(args.database_url, passwords))
    print(f"LOGIN granted to {', '.join(granted)}; NOLOGIN for the rest")
