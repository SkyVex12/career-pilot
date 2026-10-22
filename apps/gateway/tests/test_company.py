"""Standups and meetings at a company: the gateway loads or renders that company's pack, adds
the meeting's agenda, searches the company's memory live as app_gateway, and never shows
another company's or another user's memory. Needs Postgres from the docker compose stack."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from copilot_core import keys
from copilot_core.db import create_pool
from copilot_core.ids import uuid7
from copilot_core.llm import LLMRequest
from copilot_core.teststack import StackUnavailableError, as_role, temp_database
from copilot_core.workplace import local_today, render_employer_pack
from copilot_gateway.testing import GatewayServer, ProtocolClient, Reply, ScriptedProvider

from .conftest import USER_A, USER_B, Env

pytestmark = pytest.mark.stack

MEMORY = re.compile(r"<memory>\n(.*?)\n</memory>", re.S)
CITE = re.compile(r"\[(n:w[0-9a-f]+)\]")


@dataclass
class Db:
    url: str
    admin: asyncpg.Connection
    pool: asyncpg.Pool
    fernhill: UUID  # user A's company
    oldcorp: UUID  # user A's earlier company
    bcorp: UUID  # user B's company


async def node(db: asyncpg.Connection, uid: UUID, eid: UUID, kind: str, title: str,
               summary: str, day: date | None = None, **body: Any) -> UUID:  # fmt: skip
    node_id = uuid7()
    await db.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source, valid_from) values ($1, $2, $3, $4, 'workplace', $5, $6, $7,"
        ' \'{"type": "worklog"}\', $8)',
        node_id,
        uid,
        eid,
        kind,
        title,
        summary,
        json.dumps(body),
        day or datetime.now(UTC).date(),
    )
    return node_id


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def db() -> AsyncIterator[Db]:
    try:
        tmp = temp_database("copilot_gateway")
        url = await tmp.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    admin = await asyncpg.connect(url)
    eids: dict[str, UUID] = {}
    for uid, name in ((USER_A, "Alex"), (USER_B, "Blair")):
        await admin.execute(
            "insert into app.users (id, email, display_name) values ($1, $2, $3)",
            uid,
            f"{uid.hex}@example.com",
            name,
        )
    for uid, name in ((USER_A, "Fernhill Logistics"), (USER_A, "Old Corp"), (USER_B, "B Corp")):
        eids[name] = uuid7()
        await admin.execute(
            "insert into app.employers (id, user_id, name, title) values ($1, $2, $3, $4)",
            eids[name],
            uid,
            name,
            "Backend Engineer",
        )
    f, o, b = eids["Fernhill Logistics"], eids["Old Corp"], eids["B Corp"]
    await node(
        admin,
        USER_A,
        f,
        "system",
        "Label service",
        "Prints carrier labels; retries carrier calls with backoff",
        stack=["Go"],
    )
    await node(admin, USER_A, f, "decision", "Keep SQS for dispatch",
               "Keep SQS for the dispatch queue this quarter; revisit Kafka in Q1")  # fmt: skip
    await node(admin, USER_A, f, "work_item", "Label retry fix",
               "Fixed label retries: backoff 1s, 4s, 16s", state="done")  # fmt: skip
    await node(admin, USER_A, o, "system", "Legacy label printer",
               "Old label retries without backoff")  # fmt: skip
    await node(admin, USER_B, b, "system", "B label API", "Label retries with jitter")
    pool = await create_pool(as_role(url, "app_gateway"), 1, 3)
    try:
        yield Db(url, admin, pool, f, o, b)
    finally:
        await pool.close()
        await admin.close()
        await tmp.__aexit__(None, None, None)


class Capture:
    """A scripted provider that keeps every request and cites the first memory line."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, LLMRequest]] = []
        self.provider = ScriptedProvider("anthropic", self.reply)

    def reply(self, job: str, req: LLMRequest) -> Reply:
        from copilot_gateway.testing import default_reply

        self.requests.append((job, req))
        if job == "detector":
            return default_reply(job, req)
        content = str(req.content)
        found = MEMORY.search(content)
        cite = CITE.search(found.group(1)) if found else None
        if cite is None and len(req.system) > 2:
            cite = CITE.search(req.system[2].text)  # the pack's first company fact
        line = f"- Label retries back off [{cite.group(1)}]\n" if cite else "- No memory\n"
        return Reply([line])

    def of(self, job: str) -> list[LLMRequest]:
        return [r for j, r in self.requests if j == job]


def services(env: Env, db: Db, cap: Capture, **settings: Any) -> Any:
    env.providers = [cap.provider]
    return env.services(env.settings(**settings), pool=db.pool)


async def test_meeting_renders_the_companys_pack_and_searches_it(env: Env, db: Db) -> None:
    cap = Capture()
    async with GatewayServer(services(env, db, cap)) as server:
        c = ProtocolClient(server.url)
        await c.start(
            "meeting",
            employer_id=str(db.fernhill),
            meeting_title="Dispatch sync",
            agenda="Should the dispatch queue move to Kafka?",
        )
        await c.send({"type": "question.force", "text": "How do our label retries back off?"})
        done = await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    [fast] = cap.of("fast")
    system = fast.system[0].text
    assert "At work: Alex is in a meeting at Fernhill Logistics" in system
    assert "In a work meeting the talk is technical and open" in system
    pack = fast.system[2].text
    assert "## This meeting" in pack
    assert "Agenda: Should the dispatch queue move to Kafka?" in pack
    assert "Label service" in pack  # the brief
    assert "Keep SQS for dispatch" in pack
    for other in ("Legacy label printer", "B label API"):
        assert other not in pack + str(fast.content), f"{other} leaked into the prompt"
    # The pack was rendered by the gateway (no stored one) and a build was asked for.
    builds = [j for j in env.jobs.sent if j["type"] == "pack.build"]
    assert [b["payload"]["context"] for b in builds] == [f"employer:{db.fernhill}"]
    # The cited node is a real memory ref, and the pass logged its memory timings.
    assert done["memory_refs"]
    assert done["latency_ms"]["memory_wait_ms"] >= 0
    vk: Any = env.client()
    state = await vk.hgetall(keys.sess_state(c.session_id))
    assert state[b"employer_id"].decode() == str(db.fernhill)
    pack_id = state[b"pack_id"].decode()
    stored = await vk.get(keys.pack(USER_A, pack_id))
    assert stored is not None  # the session's copy, for a resume


async def test_live_memory_finds_what_the_pack_leaves_out(env: Env, db: Db) -> None:
    """Work older than the brief's two weeks is not in the pack; live search finds it, scoped
    to this company, and the answer's citation of it becomes a memory ref."""
    old_day = datetime.now(UTC).date() - timedelta(days=40)
    old = await node(db.admin, USER_A, db.fernhill, "work_item", "Carrier webhook retries",
                     "Added carrier webhook retries with a dead-letter queue", day=old_day,
                     state="done")  # fmt: skip
    other = await node(db.admin, USER_A, db.oldcorp, "work_item", "Carrier webhook retries v0",
                       "Old Corp webhook retries", day=old_day, state="done")  # fmt: skip
    cap = Capture()
    try:
        # A forced question's search starts with its fast pass; give it time on a busy runner.
        async with GatewayServer(services(env, db, cap, memory_fast_wait_ms=2000)) as server:
            c = ProtocolClient(server.url)
            await c.start("meeting", employer_id=str(db.fernhill))
            await c.send(
                {"type": "question.force", "text": "How did we handle carrier webhook retries?"}
            )
            done = await c.wait_type("answer.done", pass_="fast")
            await c.send({"type": "session.end"})
            await c.closed()
    finally:
        await db.admin.execute("delete from app.memory_nodes where id = any($1)", [old, other])
    [fast] = cap.of("fast")
    assert "Carrier webhook retries" not in fast.system[2].text  # not in the brief
    found = MEMORY.search(str(fast.content))
    assert found is not None
    assert "Added carrier webhook retries with a dead-letter queue" in found.group(1)
    assert "Old Corp" not in found.group(1)
    assert [r["node_id"] for r in done["memory_refs"]] == [str(old)]
    assert done["latency_ms"]["memory_hits"] >= 1


async def test_live_memory_skips_what_the_pack_shows(env: Env, db: Db) -> None:
    extra = await node(db.admin, USER_A, db.fernhill, "term", "Linehaul",
                       "Linehaul is the long-distance leg between two sort centers")  # fmt: skip
    cap = Capture()
    try:
        async with GatewayServer(services(env, db, cap)) as server:
            c = ProtocolClient(server.url)
            await c.start("meeting", employer_id=str(db.fernhill))
            await c.send({"type": "question.force", "text": "What does linehaul mean here?"})
            await c.wait_type("answer.done", pass_="fast")
            await c.send({"type": "session.end"})
            await c.closed()
    finally:
        await db.admin.execute("delete from app.memory_nodes where id = $1", extra)
    [fast] = cap.of("fast")
    content = str(fast.content)
    # The term is in the pack brief, so live search adds nothing it already shows.
    assert "Linehaul" in fast.system[2].text
    found = MEMORY.search(content)
    assert found is None or "Linehaul" not in found.group(1)
    assert "Legacy" not in content


async def test_a_company_that_is_not_the_users_is_ignored(env: Env, db: Db) -> None:
    cap = Capture()
    async with GatewayServer(services(env, db, cap)) as server:
        c = ProtocolClient(server.url)
        await c.start("meeting", employer_id=str(db.bcorp))  # user B's company, asked by A
        await c.send({"type": "question.force", "text": "How do our label retries back off?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    [fast] = cap.of("fast")
    text = " ".join(b.text for b in fast.system) + str(fast.content)
    assert "B Corp" not in text
    assert "B label API" not in text
    assert "At work:" not in text
    vk: Any = env.client()
    state = await vk.hgetall(keys.sess_state(c.session_id))
    assert state[b"employer_id"] == b""


async def test_a_current_stored_pack_is_used_as_it_is(env: Env, db: Db) -> None:
    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute("select set_config('app.user_id', $1, true)", str(USER_A))
        pack = await render_employer_pack(conn, USER_A, db.fernhill, "standup", local_today({}))
    assert pack is not None
    vk: Any = env.client()
    await vk.set(keys.pack(USER_A, pack.pack_id), pack.model_dump_json(by_alias=True))
    await vk.hset(
        keys.pack_index(USER_A),
        mapping={keys.employer_pack_field(db.fernhill, "standup"): str(pack.pack_id)},
    )
    cap = Capture()
    async with GatewayServer(services(env, db, cap)) as server:
        c = ProtocolClient(server.url)
        await c.start("standup", employer_id=str(db.fernhill))
        await c.send({"type": "question.force", "text": "What's your update?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    state = await vk.hgetall(keys.sess_state(c.session_id))
    assert state[b"pack_id"].decode() == str(pack.pack_id)
    assert not [j for j in env.jobs.sent if j["type"] == "pack.build"]
    [fast] = cap.of("fast")
    assert "## Standup on" in fast.system[2].text
    assert "In a standup, give Alex's update" in fast.system[0].text


async def test_without_a_database_the_stored_pack_still_loads(env: Env, db: Db) -> None:
    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute("select set_config('app.user_id', $1, true)", str(USER_A))
        pack = await render_employer_pack(conn, USER_A, db.fernhill, "meeting", local_today({}))
    assert pack is not None
    vk: Any = env.client()
    await vk.set(keys.pack(USER_A, pack.pack_id), pack.model_dump_json(by_alias=True))
    await vk.hset(
        keys.pack_index(USER_A),
        mapping={keys.employer_pack_field(db.fernhill, "meeting"): str(pack.pack_id)},
    )
    cap = Capture()
    env.providers = [cap.provider]
    async with GatewayServer(env.services(env.settings())) as server:  # no pool
        c = ProtocolClient(server.url)
        await c.start("meeting", employer_id=str(db.fernhill))
        await c.send({"type": "question.force", "text": "How do our label retries back off?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    [fast] = cap.of("fast")
    assert "Label service" in fast.system[2].text
    assert "<memory>" not in str(fast.content)


async def test_switching_from_meeting_to_standup_loads_the_standup_pack(env: Env, db: Db) -> None:
    cap = Capture()
    async with GatewayServer(services(env, db, cap)) as server:
        c = ProtocolClient(server.url)
        await c.start("meeting", employer_id=str(db.fernhill))
        await c.send({"type": "mode.set", "mode": "standup"})
        await asyncio.sleep(0.3)
        await c.send({"type": "question.force", "text": "What's your update?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    fast = cap.of("fast")[-1]
    assert "## Standup on" in fast.system[2].text
    assert "a standup at Fernhill Logistics" in fast.system[0].text
