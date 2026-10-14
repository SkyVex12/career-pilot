"""copilot-db-logins: the AWS counterpart of copilot-dev-roles (passwords from Secrets
Manager through the environment). Roles are cluster-wide, so the test restores the local
stack's dev passwords afterwards."""

import secrets

import asyncpg
import pytest

from copilot_core import dblogins, devroles
from copilot_core.teststack import StackUnavailableError, admin_url, temp_database

pytestmark = pytest.mark.stack


def test_passwords_come_from_the_environment() -> None:
    long = "x" * 32
    env = {"COPILOT_DB_PASSWORD_APP_API": long, "COPILOT_DB_PASSWORD_APP_SYSTEM": long,
           "COPILOT_DB_PASSWORD_APP_GATEWAY": ""}  # fmt: skip
    assert dblogins.passwords_from(env) == {"app_api": long, "app_system": long}
    with pytest.raises(SystemExit, match="shorter"):
        dblogins.passwords_from({"COPILOT_DB_PASSWORD_APP_API": "short"})


async def test_grants_login_with_the_given_passwords_only() -> None:
    try:
        db = temp_database("copilot_logins")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    # A quote in the password must survive the quoting in ALTER ROLE.
    password = secrets.token_urlsafe(24) + "'q"
    try:
        granted = await dblogins.grant_logins(url, {"app_api": password})
        assert granted == ["app_api"]
        conn = await asyncpg.connect(devroles.role_url(url, "app_api").replace(
            devroles.dev_password("app_api"), password.replace("'", "%27")
        ))  # fmt: skip
        assert await conn.fetchval("select current_user") == "app_api"
        await conn.close()
        with pytest.raises(asyncpg.InvalidAuthorizationSpecificationError):
            await asyncpg.connect(devroles.role_url(url, "app_gateway"))
    finally:
        await devroles.grant_login(admin_url())
        await db.__aexit__(None, None, None)
