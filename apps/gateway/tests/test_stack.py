"""Gateway against the docker compose stack: real Valkey (Lua, streams, hash tags) and the
ElasticMQ jobs queue. STT and Claude stay scripted. Needs `docker compose up -d --wait`."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import valkey.asyncio as valkey_async

from copilot_core import keys
from copilot_core.aws import boto3_client
from copilot_core.jobs import SqsJobQueue
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore, LeaseLostError
from copilot_gateway.testing import GatewayServer, ProtocolClient, TextSTT

pytestmark = pytest.mark.stack

VALKEY_URL = os.environ.get("COPILOT_VALKEY_URL", "redis://localhost:6379/0")
SQS_ENDPOINT = os.environ.get("COPILOT_SQS_ENDPOINT", "http://localhost:9324")


@pytest.fixture
async def vk() -> AsyncIterator[Any]:
    client: Any = valkey_async.Valkey.from_url(VALKEY_URL, socket_timeout=2)
    try:
        await client.ping()
    except Exception as e:
        pytest.fail(
            f"cannot reach Valkey at {VALKEY_URL} ({e}); run `docker compose up -d --wait`",
            pytrace=False,
        )
    yield client
    await client.aclose()


@pytest.fixture
def sqs_queue() -> Iterator[tuple[Any, str]]:
    sqs = boto3_client(
        "sqs",
        endpoint_url=SQS_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    try:
        url = sqs.create_queue(QueueName=f"gw-{uuid.uuid4().hex[:12]}")["QueueUrl"]
    except Exception as e:
        pytest.fail(
            f"cannot reach ElasticMQ at {SQS_ENDPOINT} ({e}); run `docker compose up -d --wait`",
            pytrace=False,
        )
    yield sqs, url
    sqs.delete_queue(QueueUrl=url)


def receive_all(sqs: Any, url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        batch = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            MessageSystemAttributeNames=["MessageGroupId"],
        ).get("Messages", [])
        if not batch:
            return out
        for m in batch:
            body = json.loads(m["Body"])
            body["_group"] = m.get("Attributes", {}).get("MessageGroupId")
            out.append(body)
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


async def test_store_scripts_on_real_valkey(vk: Any, env: Any) -> None:
    store = GatewayStore(vk, env.settings())
    sid, uid = uuid.uuid4(), uuid.uuid4()
    try:
        await store.take_lease(sid, "task-a")
        last = await store.append(
            sid,
            "task-a",
            [(1, '{"seq":1}'), (2, '{"seq":2}')],
            ['{"kind":"x"}'],
            {"mode": "interview"},
        )
        assert last
        assert await store.events_after(sid, 1) == [(2, '{"seq":2}')]
        assert await store.last_event_seq(sid) == 2
        assert (await store.load_state(sid))["mode"] == "interview"
        assert 0 < await vk.ttl(keys.sess_state(sid)) <= env.settings().state_ttl_s
        # A resume elsewhere takes the lease; the old task is fenced off.
        await store.take_lease(sid, "task-b")
        with pytest.raises(LeaseLostError):
            await store.append(sid, "task-a", [(3, "{}")], [], {})
        assert not await store.renew_lease(sid, "task-a")
        assert await store.renew_lease(sid, "task-b")
        # A seq that goes backwards is refused by Valkey itself.
        with pytest.raises(Exception, match="ID"):
            await store.append(sid, "task-b", [(2, "{}")], [], {})
        # One live session per user.
        assert await store.claim_active(uid, sid) is None
        assert await store.claim_active(uid, uuid.uuid4()) == str(sid)
        await store.release_active(uid, sid)
        assert await vk.get(keys.user_active_session(uid)) is None
    finally:
        await vk.delete(
            *(k(sid) for k in (keys.sess_state, keys.sess_events, keys.sess_log, keys.sess_lease))
        )


async def test_job_envelope_on_elasticmq(sqs_queue: tuple[Any, str]) -> None:
    sqs, url = sqs_queue
    queue = SqsJobQueue(url, "us-east-1", client=sqs)
    from copilot_core.jobs import envelope

    uid = uuid.uuid4()
    await queue.send(envelope("session.ended", "session.ended:s1", uid, {"session_id": "s1"}))
    [message] = receive_all(sqs, url)
    assert message["_group"] == str(uid)
    assert message["type"] == "session.ended"
    assert message["idempotency_key"] == "session.ended:s1"
    assert message["version"] == 1
    assert message["enqueued_at"].endswith("Z")


async def test_session_resume_across_tasks_on_the_stack(
    vk: Any, sqs_queue: tuple[Any, str], env: Any
) -> None:
    sqs, url = sqs_queue
    settings = env.settings(flush_interval_s=0.5)

    def services() -> Services:
        return Services(
            settings=settings,
            store=GatewayStore(
                valkey_async.Valkey.from_url(VALKEY_URL, socket_timeout=2), settings
            ),
            jobs=SqsJobQueue(url, "us-east-1", client=sqs),
            router=build_router(settings, env.providers),
            verifier=env.verifier,
            stt_providers=[("text", lambda _: TextSTT(env.stt))],
        )

    await vk.delete(keys.user_active_session(env.user_a))
    async with GatewayServer(services()) as a, GatewayServer(services()) as b:
        c = ProtocolClient(a.url)
        await c.start()
        await c.say("How did you scale the ledger?", capture_ms=0)
        await c.wait_type("answer.done", pass_="fast")
        await asyncio.sleep(0.8)  # one session.flush
        await c.drop()
        await asyncio.sleep(0.3)
        c.url = b.url
        await c.resume()
        await c.send({"type": "question.force", "text": "What would you change?"})
        q = await c.wait(lambda m: m["type"] == "question.detected" and "change" in m["text"])
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
        seqs = [m["seq"] for m in c.messages]
        assert seqs == list(range(1, len(seqs) + 1))

    sid = c.session_id
    try:
        events = await vk.xrange(keys.sess_events(sid))
        assert [int(i.split(b"-")[0]) for i, _ in events] == seqs
        assert (await vk.hget(keys.sess_state(sid), "status")) == b"ended"
        assert await vk.get(keys.user_active_session(env.user_a)) is None
        log = [json.loads(f[b"e"]) for _, f in await vk.xrange(keys.sess_log(sid))]
        assert [e["kind"] for e in log].count("resumed") == 1
        jobs = receive_all(sqs, url)
        types = [j["type"] for j in jobs]
        assert types.count("session.ended") == 1
        assert "session.flush" in types
        assert all(j["_group"] == str(env.user_a) for j in jobs)
    finally:
        await vk.delete(
            *(k(sid) for k in (keys.sess_state, keys.sess_events, keys.sess_log, keys.sess_lease))
        )
