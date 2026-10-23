"""An interview for an application uses its JD and its paired resume version only
(ARCHITECTURE.md, "Resume versions"): the gateway loads that version's digest and the
application's pack only when the pack was built from that version, reading the pairing from
Postgres as app_gateway. Needs Postgres from the docker compose stack."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from copilot_core import keys, resumes
from copilot_core.db import create_pool
from copilot_core.ids import uuid7
from copilot_core.packs import Digest, NodeRef, SessionPack
from copilot_core.teststack import StackUnavailableError, as_role, temp_database
from copilot_gateway.testing import GatewayServer, ProtocolClient

from .conftest import USER_A, USER_B
from .test_company import Capture

pytestmark = pytest.mark.stack

BASE_FACT = "Payments migration: p95 latency down 40%"
TAILORED_FACT = "Kubernetes autoscaler rewrite: cut cloud spend 55%"


@dataclass
class Db:
    url: str
    admin: asyncpg.Connection
    pool: asyncpg.Pool
    base: UUID
    tailored: UUID
    on_base: UUID  # an application paired with the base automatically
    on_tailored: UUID  # an application with its own tailored version
    other_users_app: UUID


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def db() -> AsyncIterator[Db]:
    try:
        tmp = temp_database("copilot_gateway_resumes")
        url = await tmp.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    admin = await asyncpg.connect(url)
    for uid, name in ((USER_A, "Alex"), (USER_B, "Blair")):
        await admin.execute(
            "insert into app.users (id, email, display_name) values ($1, $2, $3)",
            uid, f"{uid.hex}@example.com", name,
        )  # fmt: skip
    on_base, on_tailored, other = uuid7(), uuid7(), uuid7()
    for app_id, uid, company in ((on_base, USER_A, "Globex"), (on_tailored, USER_A, "Initech"),
                                 (other, USER_B, "Hooli")):  # fmt: skip
        await admin.execute(
            "insert into app.applications (id, user_id, company, role, jd_text)"
            " values ($1, $2, $3, 'Engineer', 'Build things')",
            app_id, uid, company,
        )  # fmt: skip
    base, tailored = uuid7(), uuid7()
    for vid, kind, app_id in ((base, "base", None), (tailored, "tailored", on_tailored)):
        await admin.execute(
            "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
            " text_enc, key_version, content_sha256, status)"
            " values ($1, $2, $3, $4, 'Resume', 'text', '\\x00', 1, $5, 'extracted')",
            vid, USER_A, kind, app_id, "0" * 64,
        )  # fmt: skip
    pool = await create_pool(as_role(url, "app_gateway"), 1, 3)
    try:
        yield Db(url, admin, pool, base, tailored, on_base, on_tailored, other)
    finally:
        await pool.close()
        await admin.close()
        await tmp.__aexit__(None, None, None)


def digest_of(fact: str) -> Digest:
    text = f"# Career digest (version 2026-10-22)\n## Top stories\n[n:1a2b] {fact}\n"
    return Digest.model_validate({
        "version": Digest.version_of(text), "text": text,
        "refs": {"n:1a2b": NodeRef(node_id=uuid7(), label=fact)},
    })  # fmt: skip


async def seed(env, db: Db) -> tuple[Digest, Digest, SessionPack, SessionPack]:
    vk = env.client()
    base, tailored = digest_of(BASE_FACT), digest_of(TAILORED_FACT)
    await vk.set(keys.resume_digest(USER_A, db.base), base.model_dump_json(by_alias=True))
    await vk.set(keys.digest(USER_A), base.model_dump_json(by_alias=True))
    await vk.set(keys.resume_digest(USER_A, db.tailored), tailored.model_dump_json(by_alias=True))
    packs: list[SessionPack] = []
    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute("select set_config('app.user_id', $1, true)", str(USER_A))
        pairings = [await resumes.pairing(conn, a) for a in (db.on_base, db.on_tailored)]
    cases = (
        (db.on_base, db.base, base, pairings[0]),
        (db.on_tailored, db.tailored, tailored, pairings[1]),
    )
    for app_id, version, digest, paired in cases:
        assert paired is not None
        pack = SessionPack.model_validate({
            "pack_id": str(uuid7()), "user_id": str(USER_A), "mode": "interview",
            "built_at": "2026-10-22T10:00:00Z", "digest_version": digest.version,
            "context": {"company": str(app_id)}, "resume_version_id": str(version),
            "pairing": resumes.pairing_tag(paired),
        })  # fmt: skip
        await vk.set(keys.pack(USER_A, pack.pack_id), pack.model_dump_json(by_alias=True))
        await vk.hset(keys.pack_index(USER_A), f"job:{app_id}", str(pack.pack_id))
        packs.append(pack)
    return base, tailored, packs[0], packs[1]


async def test_each_application_gets_its_own_versions_digest_and_pack(env, db: Db) -> None:
    base, tailored, base_pack, tailored_pack = await seed(env, db)
    cap = Capture()
    env.providers = [cap.provider]
    vk = env.client()
    async with GatewayServer(env.services(env.settings(), pool=db.pool)) as gw:
        for app_id, digest, pack, version in (
            (db.on_base, base, base_pack, db.base),
            (db.on_tailored, tailored, tailored_pack, db.tailored),
        ):
            c = ProtocolClient(gw.url)
            await c.start(job_id=str(app_id))
            state = await vk.hgetall(keys.sess_state(c.session_id))
            assert state[b"digest_version"].decode() == digest.version
            assert state[b"pack_id"].decode() == str(pack.pack_id)
            assert state[b"resume_version_id"].decode() == str(version)
            await c.send({"type": "question.force", "text": "Tell me about your best project."})
            await c.wait_type("answer.done", pass_="fast")
            await c.send({"type": "session.end"})
            assert await c.closed() == 1000
    prompts = [json.dumps([b.text for b in r.system]) for r in cap.of("fast")]
    assert len(prompts) == 2
    assert BASE_FACT in prompts[0]
    assert TAILORED_FACT not in prompts[0]
    assert TAILORED_FACT in prompts[1]
    assert BASE_FACT not in prompts[1]


async def test_a_pack_of_another_version_is_never_loaded(env, db: Db) -> None:
    _, tailored, base_pack, _ = await seed(env, db)
    vk = env.client()
    # The index points the tailored application at a pack built from the base (as it would
    # just after the pairing changed, before the worker rebuilt it).
    await vk.hset(keys.pack_index(USER_A), f"job:{db.on_tailored}", str(base_pack.pack_id))
    async with GatewayServer(env.services(env.settings(), pool=db.pool)) as gw:
        c = ProtocolClient(gw.url)
        await c.start(job_id=str(db.on_tailored))
        state = await vk.hgetall(keys.sess_state(c.session_id))
        assert state[b"pack_id"] == b""
        assert state[b"digest_version"].decode() == tailored.version
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    builds = [m for m in env.jobs.sent if m["type"] == "pack.build"]
    assert [m["payload"]["context"] for m in builds] == [f"job:{db.on_tailored}"]
    assert f":{db.tailored.hex[:12]}:" in builds[0]["idempotency_key"]


async def test_another_users_application_loads_nothing(env, db: Db) -> None:
    await seed(env, db)
    vk = env.client()
    async with GatewayServer(env.services(env.settings(), pool=db.pool)) as gw:
        c = ProtocolClient(gw.url)
        await c.start(job_id=str(db.other_users_app))
        state = await vk.hgetall(keys.sess_state(c.session_id))
        assert (state[b"pack_id"], state[b"digest_version"]) == (b"", b"")
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
