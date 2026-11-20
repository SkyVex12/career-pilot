"""Companion boundaries, replay fold and at-most-once controls without provider calls."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fakeredis import FakeAsyncValkey
from pydantic import ValidationError

from copilot_core import companions, keys
from copilot_gateway.companionhost import CompanionDesk, CompanionFold


def record():
    return companions.Attachment(
        str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "Phone", time.time() + 60
    )


def state(r):
    return {"account_id": r.account_id, "user_id": r.profile_id, "status": "active"}


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "other"},
        {"user_id": "other"},
        {"private_context_account_id": "other"},
        {"status": "ended"},
        {"platform": "mobile_in_person"},
        {"client_version": "mobile-0.1.0"},
    ],
)
def test_account_profile_and_sticky_private_boundary(change):
    r = record()
    assert companions.allowed(r, state(r), r.account_id)
    assert not companions.allowed(r, {**state(r), **change}, r.account_id)


def test_expired_revoked_and_explicit_end_confirmation():
    r = record()
    assert not companions.allowed(r, state(r), r.account_id, now=r.expires_at)
    r.revoked = True
    assert not companions.allowed(r, state(r), r.account_id)
    with pytest.raises(ValidationError):
        companions.Command(type="companion.command", command_id=uuid4(), kind="end")
    with pytest.raises(ValidationError):
        companions.Command(type="companion.command", command_id=uuid4(), kind="followup", text=" ")
    with pytest.raises(ValidationError):
        companions.Command.model_validate(
            {"type": "companion.command", "command_id": str(uuid4()), "kind": "audio"}
        )


async def test_queue_is_atomic_idempotent_and_conflict_detected():
    client = FakeAsyncValkey()
    r = record()
    command = companions.Command(
        type="companion.command", command_id=uuid4(), kind="followup", text="why?"
    )
    assert await companions.enqueue(client, r, command) == "queued"
    assert await companions.enqueue(client, r, command) == "queued"
    assert await client.xlen(keys.sess_ctl(r.session_id)) == 1
    changed = command.model_copy(update={"text": "different"})
    assert await companions.enqueue(client, r, changed) == "conflict"
    assert await companions.claim(client, r.session_id, str(command.command_id))
    assert not await companions.claim(client, r.session_id, str(command.command_id))
    assert await companions.enqueue(client, r, command) == "uncertain"
    await companions.finish(client, r.session_id, str(command.command_id), "executed")
    assert await companions.enqueue(client, r, command) == "executed"
    await client.aclose()


def test_private_same_account_fold_ignores_guest_sharing_and_dedupes_chunks():
    fold = CompanionFold()
    for seq, message in enumerate(
        [
            {"type": "sharing", "on": False},
            {"type": "question.detected", "question_id": "q", "text": "Private question"},
            {"type": "answer.delta", "question_id": "q", "pass": "fast", "text": "Hello"},
            {"type": "answer.delta", "question_id": "q", "pass": "fast", "text": " world"},
            {"type": "audio", "data": "must never forward"},
        ],
        1,
    ):
        fold.add(seq, json.dumps({**message, "seq": seq}))
    fold.add(
        4,
        json.dumps(
            {"type": "answer.delta", "question_id": "q", "pass": "fast", "text": "duplicate"}
        ),
    )
    assert len(fold.messages()) == 2
    assert fold.messages()[1]["text"] == "Hello world"
    loaded = CompanionFold.load(fold.dump())
    assert loaded is not None
    assert loaded.messages() == fold.messages()


@pytest.mark.parametrize("revoked", [False, True])
async def test_host_control_checks_grant_and_never_repeats(revoked):
    client = FakeAsyncValkey()
    r = record()
    await companions.publish(client, r)
    command = companions.Command(
        type="companion.command", command_id=uuid4(), kind="followup", text="Explain"
    )
    await companions.enqueue(client, r, command)
    entries = await client.xrange(keys.sess_ctl(r.session_id))
    raw = entries[0][1][b"d"].decode()
    store = SimpleNamespace(
        client=client,
        load_state=AsyncMock(return_value=state(r)),
        lease_holder=AsyncMock(return_value="task"),
        put_result=AsyncMock(),
    )
    session = SimpleNamespace(
        svc=SimpleNamespace(store=store, task_id="task"),
        ending=False,
        account_id=r.account_id,
        private_context_account_id=None,
        sid=r.session_id,
        seq=0,
        ring=[],
        handle=AsyncMock(),
    )
    desk = CompanionDesk(session, {})
    if revoked:
        await client.hdel(companions.records_key(r.session_id), r.attachment_id)
    await desk.command(raw)
    await desk.command(raw)
    assert session.handle.await_count == (0 if revoked else 1)
    assert await companions.enqueue(client, r, command) == ("denied" if revoked else "executed")
    await client.aclose()


async def test_relay_replays_controls_and_revoke_preserves_capture(env, monkeypatch):
    from copilot_core.auth import AuthError
    from copilot_gateway.companionrelay import CompanionRelay
    from copilot_gateway.testing import GatewayServer, ProtocolClient

    async def checked(self, principal, sid, aid):
        grant = await companions.read(self.svc.store.client, sid, aid)
        live = await self.svc.store.load_state(sid)
        if not companions.allowed(grant, live, str(principal.user_id)):
            raise AuthError("denied")
        return grant

    # DB/RLS checked in real-stack API test; this exercises actual websocket transport.
    monkeypatch.setattr(CompanionRelay, "authorize", checked)
    async with GatewayServer(env.services()) as desktop, GatewayServer(env.services()) as remote:
        host = ProtocolClient(desktop.url)
        await host.start()
        sid = host.session_id
        grant = companions.Attachment(
            str(uuid4()), str(env.user_a), str(env.user_a), sid, "Phone", time.time() + 60
        )
        await companions.publish(env.client(), grant)
        phone = ProtocolClient(remote.url.replace("/v1/session", "/v1/companion"))
        await phone.connect()
        await phone.send(
            {"type": "companion.join", "session_id": sid, "attachment_id": grant.attachment_id}
        )
        await phone.wait_type("companion.snapshot")
        assert not remote.manager.sessions
        assert await desktop.services.store.lease_holder(sid) == desktop.services.task_id
        command = {
            "type": "companion.command",
            "command_id": str(uuid4()),
            "kind": "followup",
            "text": "Explain the design",
        }
        await phone.send(command)
        await phone.wait_type("companion.command.result", result="executed")
        await phone.wait_type("answer.done")
        before = len(host.of_type("question.detected"))
        await phone.send(command)
        await phone.wait_type("companion.command.result", result="executed")
        await asyncio.sleep(0.6)
        assert len(host.of_type("question.detected")) == before
        assert host.close_code is None
        await env.client().hdel(companions.records_key(sid), grant.attachment_id)
        await phone.wait_type("companion.error")
        assert host.close_code is None
        await host.send({"type": "session.end"})
        assert await host.closed() == 1000
        await phone.close()
