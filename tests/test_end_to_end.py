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
from copilot_worker.fixtures import ClaudeFixtures, DeepgramFixtures, fixture_provider
from copilot_worker.llm import build_router as worker_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.runtime import HANDLERS, Job
from copilot_worker.stt import DeepgramPrerecorded

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
    def __init__(self, api: httpx2.AsyncClient, gateway_url: str, ctx: Ctx, queue: MemoryJobQueue,
                 token: str) -> None:  # fmt: skip
        self.api = api
        self.gateway_url = gateway_url
        self.ctx = ctx
        self.queue = queue
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    async def work(self) -> list[str]:
        """Run queued jobs like a worker until the queue is empty."""
        ran: list[str] = []
        for _ in range(100):
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
        stt=DeepgramPrerecorded("fixture", transport=DeepgramFixtures(FIXTURES)),
    )

    gw_overrides: dict[str, Any] = {
        "env": "test", "valkey_url": VALKEY_URL, "llm_providers": ["anthropic"],
        "embedder": "none", "speculative_start_ms": 100, "silence_timer_ms": 250,
        "final_wait_max_ms": 500,
    }  # fmt: skip
    gw_settings = GatewaySettings(_env_file=None, **gw_overrides)  # pyright: ignore[reportCallIssue]
    control = TextSTTControl()
    gw_svc = GatewayServices(
        settings=gw_settings,
        store=GatewayStore(make_client(gw_settings), gw_settings),
        jobs=queue,
        router=gateway_router(gw_settings, [ScriptedProvider("anthropic")]),
        verifier=verifier,
        stt_providers=[("text", lambda: TextSTT(control, "text"))],
    )
    async with GatewayServer(gw_svc) as gw:
        try:
            yield World(api, gw.url, ctx, queue, token)
        finally:
            await api.aclose()
            for pool in (api_svc.pool, ctx.pool, ctx.system_pool):
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
    assert ran.count("memory.extract") == 2
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
    await c.send({"type": "question.force", "text": "How do you make payouts exactly-once?"})
    await c.wait_type("answer.done", pass_="fast", timeout=8)
    await c.send({"type": "session.end"})
    assert await c.closed() == 1000

    # After the socket closes: the user's own audio, then the workers.
    r = await w.api.post(f"/v1/sessions/{sid}/me-audio", headers=w.headers, json={})
    # The session row may not exist yet (no flush before the end); the API checks Valkey.
    assert r.status_code == 201, r.text
    target = r.json()["upload"]
    async with httpx2.AsyncClient() as s3:
        assert (
            await s3.put(target["url"], content=b"RIFF", headers=target["headers"])
        ).status_code == 200
    r = await w.api.post(f"/v1/sessions/{sid}/me-audio/complete", headers=w.headers)
    assert r.status_code == 202, r.text
    # Workers: session.ended writes the log and creates the row; the transcribe job the API
    # enqueued waits for it, transcribes the audio, deletes it and runs the post-call job.
    ran = await w.work()
    assert ran[0] == "session.ended"
    assert "me_audio.transcribe" in ran
    assert "session.postcall" in ran

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
