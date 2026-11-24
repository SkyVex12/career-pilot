"""Build step 4a end to end on the local stack, with recorded Claude and Deepgram responses:

upload a resume and a job description through the API (straight to S3 with the presigned
URLs), ingest both, schedule an interview, and let the workers build the digest and the
session pack. Then run a session through the real gateway, which loads that pack, answer a
typed question, end the session, upload the user's audio, and let the workers flush the log,
transcribe and write the post-call outputs. `GET /sessions/{id}` returns the summary.

The API, gateway and workers share one in-process queue, so the test runs each job the way a
worker would; everything else (Postgres with each service's role, Valkey, S3) is the stack.
"""

import asyncio
import base64
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import pytest_asyncio

from copilot_api.config import ApiSettings
from copilot_api.deps import Services as ApiServices
from copilot_api.main import create_app
from copilot_core import jobs, keys
from copilot_core.auth import Principal, StaticVerifier
from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import S3Store
from copilot_core.teststack import (
    S3_ENDPOINT,
    VALKEY_URL,
    StackUnavailableError,
    as_role,
    temp_database,
)
from copilot_core.valkey_client import make_valkey
from copilot_gateway.config import Settings as GatewaySettings
from copilot_gateway.metering import ConnectedLiveMeter, LivePrice
from copilot_gateway.router import build_router as gateway_router
from copilot_gateway.session import Services as GatewayServices
from copilot_gateway.store import GatewayStore, make_client
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    ScriptedProvider,
    TextSTT,
    TextSTTControl,
)
from copilot_worker import documents, packs, sessionlog, system
from copilot_worker.config import WorkerSettings
from copilot_worker.context import Ctx
from copilot_worker.fixtures import ClaudeFixtures, fixture_provider
from copilot_worker.llm import build_router as worker_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.runtime import HANDLERS, Job

pytestmark = pytest.mark.stack

FIXTURES = Path(__file__).resolve().parent.parent / "apps" / "worker" / "tests" / "fixtures"
HANDLER_MODULES = (documents, packs, sessionlog, system)
COMMON: dict[str, Any] = {
    "env": "test",
    "valkey_url": VALKEY_URL,
    "s3_endpoint": S3_ENDPOINT,
    "s3_bucket": "copilot-test",
    "s3_access_key": "local",
    "s3_secret_key": "local-secret",
    "key_provider": "dev",
    "llm_providers": ["anthropic"],
    "embedder": "none",
}


class World:
    def __init__(
        self,
        api: httpx2.AsyncClient,
        gateway: GatewayServer,
        ctx: Ctx,
        queue: MemoryJobQueue,
        token: str,
    ) -> None:
        self.api = api
        self.gateway = gateway
        self.gateway_url = gateway.url
        self.ctx = ctx
        self.queue = queue
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    async def work(self) -> list[str]:
        """Run queued jobs like a worker until the queue is empty."""
        ran: list[str] = []
        for _ in range(100):
            await self.ctx.dispatcher.once()
            if not self.queue.sent:
                return ran
            message = self.queue.sent.pop(0)
            await HANDLERS[message["type"]](Job.parse(json.dumps(message)), self.ctx)
            ran.append(message["type"])
        raise AssertionError("the queue never drained")


@pytest_asyncio.fixture(loop_scope="session")
async def world() -> AsyncIterator[World]:
    try:
        db = temp_database("copilot_e2e")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    master = os.urandom(32)
    provider = DevKeyProvider(master)
    queue = MemoryJobQueue()
    uid = uuid.uuid4()
    token = f"token-{uid}"
    verifier = StaticVerifier({token: Principal(uid, "Alex Doe", None, email="alex@example.com")})
    common = {**COMMON, "dev_master_key": base64.b64encode(master).decode()}

    api_settings = ApiSettings(_env_file=None, **common)  # pyright: ignore[reportCallIssue]
    store = S3Store(api_settings)
    await store.ensure_bucket()
    api_svc = ApiServices(
        settings=api_settings,
        pool=await create_pool(as_role(url, "app_api"), 1, 3),
        valkey=make_valkey(VALKEY_URL),
        queue=queue,
        store=store,
        crypto=UserCrypto(provider),
        verifier=verifier,
    )
    app = create_app(api_svc)
    app.state.services = api_svc
    api = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://api.test")

    worker_settings = WorkerSettings(_env_file=None, **common)  # pyright: ignore[reportCallIssue]
    claude = ClaudeFixtures(FIXTURES, JOB_PROMPTS)
    ctx = Ctx(
        settings=worker_settings,
        pool=await create_pool(as_role(url, "app_worker"), 1, 3),
        system_pool=await create_pool(as_role(url, "app_system"), 1, 2),
        valkey=make_valkey(VALKEY_URL),
        queue=queue,
        store=store,
        crypto=UserCrypto(provider),
        router=worker_router(worker_settings, [fixture_provider(claude)]),
    )

    gw_overrides: dict[str, Any] = {
        "env": "test", "valkey_url": VALKEY_URL, "llm_providers": ["anthropic"],
        "embedder": "none", "speculative_start_ms": 100, "silence_timer_ms": 250,
        "final_wait_max_ms": 500,
    }  # fmt: skip
    gw_settings = GatewaySettings(_env_file=None, **gw_overrides)  # pyright: ignore[reportCallIssue]
    control = TextSTTControl()
    gateway_pool = await create_pool(as_role(url, "app_gateway"), 1, 3)
    gw_svc = GatewayServices(
        settings=gw_settings,
        store=GatewayStore(make_client(gw_settings), gw_settings),
        jobs=queue,
        router=gateway_router(gw_settings, [ScriptedProvider("anthropic")]),
        verifier=verifier,
        stt_providers=[("text", lambda _: TextSTT(control, "text"))],
        pool=gateway_pool,
    )
    async with GatewayServer(gw_svc) as gw:
        try:
            yield World(api, gw, ctx, queue, token)
        finally:
            await api.aclose()
            for pool in (api_svc.pool, ctx.pool, ctx.system_pool, gateway_pool):
                await pool.close()
            await api_svc.valkey.aclose()
            await ctx.valkey.aclose()
            await db.__aexit__(None, None, None)


async def upload(w: World, kind: str, path: Path) -> str:
    body = await asyncio.to_thread(path.read_bytes)
    r = await w.api.post(
        "/v1/documents",
        headers=w.headers,
        json={"kind": kind, "content_type": "text/markdown", "size_bytes": len(body),
              "sha256": hashlib.sha256(body).hexdigest()},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    target = r.json()["upload"]
    async with httpx2.AsyncClient() as s3:
        put = await s3.put(target["url"], content=body, headers=target["headers"])
        assert put.status_code == 200
    doc = r.json()["document_id"]
    assert (await w.api.post(f"/v1/documents/{doc}/ingest", headers=w.headers)).status_code == 202
    return doc


async def test_resume_to_interview_to_summary(world: World) -> None:
    w = world
    me = (await w.api.get("/v1/me", headers=w.headers)).json()  # first login creates the user
    assert me["display_name"] == "Alex Doe"

    await upload(w, "resume", FIXTURES / "inputs" / "resume.md")
    job_id = await upload(w, "job_description", FIXTURES / "inputs" / "job.md")
    ran = await w.work()
    assert ran.count("resume.extract") == 1  # the resume is the base version's content
    assert ran.count("memory.extract") == 1  # the job description
    assert "digest.rebuild" in ran
    assert "pack.build" in ran

    jobs = (await w.api.get("/v1/jobs", headers=w.headers)).json()["jobs"]
    assert [(j["id"], j["company"]) for j in jobs] == [(job_id, "Globex")]
    starts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r = await w.api.post(
        "/v1/interviews", headers=w.headers,
        json={"job_id": job_id, "round": "technical", "starts_at": starts},
    )  # fmt: skip
    assert r.status_code == 201
    await w.work()
    interviews = (await w.api.get("/v1/interviews", headers=w.headers)).json()["interviews"]
    pack = interviews[0]["pack"]
    assert pack is not None
    assert pack["available"] is True
    assert pack["round"] == "technical"
    assert pack["prepared_answers"] == 4
    jobs = (await w.api.get("/v1/jobs", headers=w.headers)).json()["jobs"]
    assert jobs[0]["pack"]["pack_id"] == interviews[0]["pack_id"]  # the gateway loads this one

    # A live session through the gateway, on the job the user picked.
    c = ProtocolClient(w.gateway_url, token=w.token)
    ready = await c.start(mode="interview", job_id=job_id)
    sid = c.session_id
    assert ready["type"] == "session.ready"
    assert not [m for m in c.messages if m["type"] == "status" and m["component"] == "memory"]
    live = (await w.api.get(f"/v1/sessions/{sid}", headers=w.headers)).json()
    assert live["status"] == "live"
    await c.say("Tell me about a migration of a critical data store you led?")
    await c.wait_type("question.detected", speculative=False)
    # The user's own words, transcribed live on the mic stream.
    await c.say_me("We moved the ledger to Postgres with dual writes", capture_ms=9000)
    await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
    await c.send({"type": "question.force", "text": "How do you make payouts exactly-once?"})
    await c.wait_type("answer.done", pass_="fast", timeout=8)
    await c.send({"type": "session.end"})
    assert await c.closed() == 1000

    # The durable session already exists. After the socket closes, persistence jobs
    # flush the final log and post-call work runs without waiting for an audio upload.
    ran = await w.work()
    assert "session.ended" in ran
    assert "me_audio.transcribe" not in ran
    assert "session.postcall" in ran
    r = await w.api.get(f"/v1/sessions/{sid}/transcript", headers=w.headers)
    assert ("me", "We moved the ledger to Postgres with dual writes") in [
        (s["channel"], s["text"]) for s in r.json()["segments"]
    ]

    detail = (await w.api.get(f"/v1/sessions/{sid}", headers=w.headers)).json()
    assert detail["status"] == "ready"
    assert detail["job_id"] == job_id
    assert detail["interview_id"] == interviews[0]["id"]
    assert detail["questions"] >= 2
    assert detail["outputs"]["summary"].startswith("A technical screen with Globex")
    assert set(detail["outputs"]) == {"summary", "feedback", "follow_up_email"}
    transcript = (await w.api.get(f"/v1/sessions/{sid}/transcript", headers=w.headers)).json()
    channels = {s["channel"] for s in transcript["segments"]}
    assert channels == {"them", "me"}
    sessions = (await w.api.get("/v1/sessions", headers=w.headers)).json()["sessions"]
    assert [s["id"] for s in sessions] == [sid]
    usage = (await w.api.get("/v1/usage", headers=w.headers)).json()
    assert usage["sessions"] == 1


async def test_connected_usage_survives_reconnect_and_history_deletion(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual gateway admission/receipts, worker roles and REST counters agree."""
    w = world
    me = (await w.api.get("/v1/me", headers=w.headers)).json()
    clock = [100.0]
    anchor = datetime(2026, 11, 23, 12, tzinfo=UTC)

    def meter(sid: uuid.UUID, price: LivePrice, record: Any) -> ConnectedLiveMeter:
        return ConnectedLiveMeter(
            sid, price, record, monotonic=lambda: clock[0], utc_now=lambda: anchor
        )

    monkeypatch.setattr("copilot_gateway.session.ConnectedLiveMeter", meter)
    client = ProtocolClient(w.gateway_url, token=w.token)
    await client.start(mode="meeting", consent={"store_transcript": False})
    sid = uuid.UUID(client.session_id)
    session = w.gateway.manager.sessions[sid]
    assert session.durable is not None

    async def attached(active: bool) -> None:
        async with asyncio.timeout(3):
            while session.usage_meter.active != active:  # noqa: ASYNC110
                await asyncio.sleep(0.01)

    async def totals() -> dict[str, Any]:
        response = await w.api.get(f"/v1/usage/totals?session_id={sid}", headers=w.headers)
        assert response.status_code == 200, response.text
        return response.json()

    await attached(True)
    initial = await totals()
    assert initial["total_seconds"] == initial["session_seconds"] == 0
    # The clock advances during silence, without sending speech or invoking a model.
    clock[0] += 60.25
    await client.drop()
    await attached(False)
    assert await session.writer.drain(within_s=2)
    retained = await w.ctx.valkey.xrange(keys.sess_log(sid))
    assert retained
    flush = jobs.envelope(
        "session.flush", f"usage-test-flush:{sid}", uuid.UUID(me["id"]),
        {"session_id": str(sid), "upto_entry_id": retained[-1][0].decode()},
    )  # fmt: skip
    await HANDLERS["session.flush"](Job.parse(json.dumps(flush)), w.ctx)
    first = await totals()
    assert first["total_seconds"] == first["session_seconds"] == 60.25

    clock[0] += 120
    assert (await totals())["session_seconds"] == 60.25
    await client.resume()
    await attached(True)
    clock[0] += 30.125
    await client.send({"type": "session.end"})
    assert await client.closed() == 1000
    assert "session.ended" in await w.work()

    final = await totals()
    assert final["total_seconds"] == final["session_seconds"] == 90.375
    assert final["total_minutes"] == final["session_minutes"] == 90.375 / 60
    assert final["total_sessions"] == 1
    assert final["tracking_only"] is True
    async with w.ctx.system_pool.acquire() as conn:
        segments = await conn.fetch(
            "select started_at,ended_at,duration_microseconds from app.live_usage_intervals"
            " where session_id=$1 order by started_at", sid,
        )  # fmt: skip
    assert sum(row["duration_microseconds"] for row in segments) == 90_375_000
    assert len(segments) == 2
    assert (segments[1]["started_at"] - segments[0]["ended_at"]).total_seconds() == 120
    deleted = await w.api.delete(f"/v1/sessions/{sid}", headers=w.headers)
    assert deleted.status_code == 204, deleted.text
    assert (await totals())["session_seconds"] == 90.375


def load_dev_profile() -> Any:
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "scripts" / "dev_profile.py"
    spec = importlib.util.spec_from_file_location("dev_profile", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_profile"] = module
    spec.loader.exec_module(module)
    return module


async def test_dev_profile_script_uses_the_api_path(world: World) -> None:
    """scripts/dev_profile.py: uploads, ingest, interview, then waits for the pack."""
    script = load_dev_profile()
    worker_stop = asyncio.Event()

    async def worker() -> None:
        while not worker_stop.is_set():
            await world.work()
            await asyncio.sleep(0.02)

    task = asyncio.create_task(worker())
    lines: list[str] = []
    world.api.headers.update(world.headers)
    try:
        result = await script.build_profile(
            world.api,
            FIXTURES / "inputs" / "resume.md",
            FIXTURES / "inputs" / "job.md",
            "system_design",
            datetime.now(UTC) + timedelta(hours=2),
            interval_s=0.05,
            timeout_s=30,
            say=lines.append,
        )
    finally:
        worker_stop.set()
        await task
    assert result["pack"]["available"] is True
    assert result["pack"]["round"] == "system_design"
    assert lines[-1].startswith(f"job id {result['job_id']} (Staff Backend Engineer")
    assert script.content_type(Path("cv.PDF")) == "application/pdf"
