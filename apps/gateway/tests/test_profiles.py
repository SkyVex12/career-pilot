"""A session for one of the account's profiles (ARCHITECTURE.md, "Profiles"): the client names
it in the `profile` query parameter; the gateway checks it against the account's
`app.profiles` as app_gateway and then runs the session as the profile, with the profile's
digest. Another account's profile, or an unknown one, is refused. Needs Postgres from the
docker compose stack."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from copilot_core import keys
from copilot_core.db import create_pool
from copilot_core.ids import uuid7
from copilot_core.teststack import StackUnavailableError, as_role, temp_database
from copilot_gateway.testing import GatewayServer, ProtocolClient

from .conftest import USER_A, USER_B
from .test_resume_pairing import digest_of

pytestmark = pytest.mark.stack

CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4410


@dataclass
class Db:
    pool: asyncpg.Pool
    profile: UUID  # USER_A's profile


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def db() -> AsyncIterator[Db]:
    try:
        tmp = temp_database("copilot_gateway_profiles")
        url = await tmp.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    admin = await asyncpg.connect(url)
    profile = uuid7()
    for uid, name, owner in ((USER_A, "Alex", None), (USER_B, "Blair", None),
                             (profile, "Casey Poe", USER_A)):  # fmt: skip
        await admin.execute(
            "insert into app.users (id, email, display_name, owner_id) values ($1, $2, $3, $4)",
            uid, f"{uid.hex}@example.com", name, owner,
        )  # fmt: skip
    await admin.execute(
        "insert into app.profiles (id, user_id, career_os_user_id, name)"
        " values ($1, $2, 'u3', 'Casey Poe')",
        profile, USER_A,
    )  # fmt: skip
    # USER_A has linked Career-OS; USER_B has not (test_a_required_link_gates_sessions).
    await admin.execute(
        "insert into app.integrations (user_id, provider, token_enc, key_version, scopes)"
        " values ($1, 'career_os', 'sealed', 1, '{}')",
        USER_A,
    )  # fmt: skip
    pool = await create_pool(as_role(url, "app_gateway"), 1, 3)
    try:
        yield Db(pool, profile)
    finally:
        await pool.close()
        await admin.close()
        await tmp.__aexit__(None, None, None)


async def test_a_session_runs_as_the_profile(env, db: Db) -> None:
    vk = env.client()
    mine, theirs = digest_of("Alex's own story"), digest_of("Casey's migration story")
    await vk.set(keys.digest(USER_A), mine.model_dump_json(by_alias=True))
    await vk.set(keys.digest(db.profile), theirs.model_dump_json(by_alias=True))
    async with GatewayServer(env.services(env.settings(), pool=db.pool)) as gw:
        c = ProtocolClient(f"{gw.url}?profile={db.profile}")
        await c.start()
        state = await vk.hgetall(keys.sess_state(c.session_id))
        assert state[b"user_id"].decode() == str(db.profile)
        assert state[b"digest_version"].decode() == theirs.version
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
        # Without the parameter, the same token is the account itself.
        c = ProtocolClient(gw.url)
        await c.start()
        state = await vk.hgetall(keys.sess_state(c.session_id))
        assert state[b"user_id"].decode() == str(USER_A)
        assert state[b"digest_version"].decode() == mine.version
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000


async def test_another_accounts_or_an_unknown_profile_is_refused(env, db: Db) -> None:
    async with GatewayServer(env.services(env.settings(), pool=db.pool)) as gw:
        for token, profile in (("token-b", db.profile), ("token-a", uuid7()),
                               ("token-a", "not-a-uuid")):  # fmt: skip
            c = ProtocolClient(f"{gw.url}?profile={profile}", token=token)
            await c.connect()
            assert await c.closed() == CLOSE_UNAUTHORIZED, (token, profile)


async def test_profiles_need_the_database(env, db: Db) -> None:
    async with GatewayServer(env.services(env.settings())) as gw:
        c = ProtocolClient(f"{gw.url}?profile={db.profile}")
        await c.connect()
        assert await c.closed() == CLOSE_UNAUTHORIZED


async def test_a_required_link_gates_sessions(env, db: Db) -> None:
    settings = env.settings(require_career_os=True)
    async with GatewayServer(env.services(settings, pool=db.pool)) as gw:
        # Linked: the account and its profile both start (the link is the account's).
        for url in (gw.url, f"{gw.url}?profile={db.profile}"):
            c = ProtocolClient(url)
            await c.start()
            await c.send({"type": "session.end"})
            assert await c.closed() == 1000
        # Not linked: refused with the reason, and a close the app does not reconnect after.
        c = ProtocolClient(gw.url, token="token-b")
        await c.connect()
        assert await c.closed() == CLOSE_REPLACED
        [error] = [m for m in c.messages if m.get("type") == "error"]
        assert error["code"] == "career_os_required"
    # Without a database to ask, nobody starts.
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.connect()
        assert await c.closed() == CLOSE_REPLACED
