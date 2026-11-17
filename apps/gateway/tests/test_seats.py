"""Guest seats (ARCHITECTURE.md, "Guest seats"): a guest follows the host's call from another
gateway task without touching the host's connection, sees only what the allow-list lets
through, sends notes only the host sees, and is closed when removed or when the call ends."""

import asyncio
import contextlib
import json
import time
from typing import Any
from uuid import UUID

from copilot_core import keys, seats
from copilot_core.ids import uuid7
from copilot_gateway.seatview import FORWARDED
from copilot_gateway.testing import GatewayServer, ProtocolClient

GUEST_TYPES = {"seat.ready", "seat.snapshot", "seat.host", "seat.update", "seat.command.result"}
NOTE = "Mention the forty percent latency cut"


def fast(env: Any, **over: Any) -> Any:
    """Seat timers shortened so the tests run in seconds."""
    base = {
        "seat_poll_s": 0.1,
        "seat_block_ms": 200,
        "seat_view_interval_s": 0.2,
        "seat_view_wait_s": 0.5,
        "seat_note_min_interval_s": 0.0,
    }
    return env.services(env.settings(**{**base, **over}))


async def publish(
    env: Any,
    sid: str,
    seat_id: str | None = None,
    status: str = "admitted",
    caps: tuple[str, ...] = ("view", "note"),
    rev: int = 1,
    guest: UUID | None = None,
) -> str:
    record = seats.SeatRecord(
        seat_id=seat_id or str(uuid7()),
        guest_id=str(guest or env.user_b),
        name="Blair",
        status=status,
        capabilities=list(caps),
        rev=rev,
        host_name="Alex",
    )
    await seats.publish(env.client(), sid, record, 7200)
    return record.seat_id


def seat_url(gw: GatewayServer) -> str:
    return gw.url.replace("/v1/session", "/v1/seat")


async def join(
    gw: GatewayServer, sid: str, seat_id: str, token: str = "token-b", last_seq: int = 0
) -> ProtocolClient:
    from websockets.exceptions import ConnectionClosed

    guest = ProtocolClient(seat_url(gw), token)
    await guest.connect()
    with contextlib.suppress(ConnectionClosed):  # a refusal may come before the join
        await guest.send(
            {
                "type": "seat.join",
                "session_id": sid,
                "seat_id": seat_id,
                "last_seq": last_seq,
                "client_version": "test",
            }
        )
    return guest


async def asked(host: ProtocolClient, text: str) -> str:
    """The host's call hears a question; returns its id once both answers are done."""
    start = len(host.messages)
    await host.say(text)
    q = await host.wait(
        lambda m: m["type"] == "question.detected" and not m["speculative"], start=start
    )
    qid = q["question_id"]
    for p in ("fast", "deep"):
        await host.wait(
            lambda m, p=p: (
                m["type"] == "answer.done" and m["question_id"] == qid and m["pass"] == p
            ),
            start=start,
        )
    return qid


def snapshot(guest: ProtocolClient) -> list[dict[str, Any]]:
    return guest.of_type("seat.snapshot")[-1]["messages"]


async def test_a_guest_follows_the_call_from_another_task_without_touching_the_host(env) -> None:
    async with GatewayServer(fast(env)) as a, GatewayServer(fast(env)) as b:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        first = await asked(host, "How did you scale the ledger?")
        seat_id = await publish(env, sid)
        await host.wait_type("seats.update")

        guest = await join(b, sid, seat_id)
        ready = await guest.wait_type("seat.ready")
        assert ready["host_name"] == "Alex"
        assert ready["capabilities"] == ["view", "note"]
        assert ready["host_connected"] is True
        assert ready["sharing"] is True
        await guest.wait_type("seat.snapshot")
        joined = snapshot(guest)
        assert any(m["type"] == "transcript.final" and "ledger" in m["text"] for m in joined)
        assert any(
            m.get("question_id") == first and m["type"] == "question.detected" for m in joined
        )
        deltas = [m for m in joined if m["type"] == "answer.delta" and m["question_id"] == first]
        assert {d["pass"] for d in deltas} == {"fast", "deep"}  # one merged chunk per pass

        # Live, in seq order, from the other task.
        second = await asked(host, "What would you do differently next time?")
        await guest.wait(lambda m: m["type"] == "answer.done" and m["question_id"] == second)
        live = [m["seq"] for m in guest.messages if m["type"] in FORWARDED]
        assert live == sorted(live)
        assert len(live) == len(set(live))
        host_seqs = {m["seq"] for m in host.messages if m["type"] in FORWARDED}
        assert set(live) <= host_seqs

        # The host's connection, lease and live-session slot are as they were.
        assert host.close_code is None
        store = a.services.store
        assert await store.lease_holder(sid) == a.services.task_id
        active = await env.client().get(keys.user_active_session(env.user_a))
        assert active.decode() == sid
        assert not b.manager.sessions  # the guest's task holds no session
        await guest.close()
        await host.send({"type": "session.end"})
        assert await host.closed() == 1000


async def test_a_guest_never_receives_what_is_not_theirs(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        seat_id = await publish(env, sid)
        guest = await join(a, sid, seat_id)
        await guest.wait_type("seat.snapshot")
        await host.send({"type": "no.such.message"})  # an error for the host
        await host.wait_type("error")
        await guest.send(
            {"type": "seat.command", "command_id": str(uuid7()), "kind": "note", "text": NOTE}
        )
        await host.wait_type("seat.note")
        await asked(host, "How did you scale the ledger?")
        await guest.wait(lambda m: m["type"] == "answer.done" and m["pass"] == "deep")
        # A second guest joins late: the snapshot follows the same rules.
        late = await join(a, sid, await publish(env, sid, guest=env.user_b))
        await late.wait_type("seat.snapshot")

        for g in (guest, late):
            types = {m["type"] for m in g.messages}
            assert types <= FORWARDED | GUEST_TYPES, types - FORWARDED - GUEST_TYPES
            folded = [m for s in g.of_type("seat.snapshot") for m in s["messages"]]
            assert {m["type"] for m in folded} <= FORWARDED
            for done in [m for m in g.messages + folded if m["type"] == "answer.done"]:
                assert "latency_ms" not in done
                assert "source" not in done
                assert done["usage"] == {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
            assert not any(
                NOTE in json.dumps(m) for m in g.messages if m["type"] != "seat.command.result"
            )
        await host.send({"type": "session.end"})


async def test_a_note_reaches_the_host_and_nothing_else(env) -> None:
    async with GatewayServer(fast(env)) as a, GatewayServer(fast(env)) as b:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        seat_id = await publish(env, sid)
        guest = await join(b, sid, seat_id)
        await guest.wait_type("seat.snapshot")
        cid = str(uuid7())
        await guest.send({"type": "seat.command", "command_id": cid, "kind": "note", "text": NOTE})
        note = await host.wait_type("seat.note")
        assert note["text"] == NOTE
        assert note["name"] == "Blair"
        assert note["seat_id"] == seat_id
        assert note["note_id"] == cid
        result = await guest.wait_type("seat.command.result", command_id=cid)
        assert result["result"] == "ok"
        await asked(host, "How did you scale the ledger?")

        session = next(iter(a.manager.sessions.values()))
        assert not any(NOTE in line.text for line in session.conv.lines)
        provider = env.providers[0]
        assert not any(NOTE in str(req.content) for _, req in provider.requests)
        await host.send({"type": "session.end"})
        assert await host.closed() == 1000
        entries = await env.client().xrange(keys.sess_log(sid))
        logged = [e[1][b"e"].decode() for e in entries]
        assert not any(NOTE in e for e in logged)
        assert any('"kind":"seat.note"' in e for e in logged)


async def test_a_note_needs_the_grant(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        seat_id = await publish(env, sid, caps=("view",))
        guest = await join(a, sid, seat_id)
        await guest.wait_type("seat.snapshot")
        cid = str(uuid7())
        await guest.send({"type": "seat.command", "command_id": cid, "kind": "note", "text": NOTE})
        assert (await guest.wait_type("seat.command.result", command_id=cid))["result"] == "denied"
        # The grant narrowed after the relay let a note through: the host's session refuses it.
        await publish(env, sid, seat_id, caps=("view", "note"), rev=2)
        await guest.wait_type("seat.update")
        await env.client().xadd(
            keys.sess_ctl(sid),
            {"k": "note", "d": json.dumps({"command_id": str(uuid7()), "seat_id": seat_id,
                                           "guest_id": str(env.user_a), "text": NOTE,
                                           "expires_at": time.time() + 60})},
        )  # fmt: skip
        stale = str(uuid7())
        await env.client().xadd(
            keys.sess_ctl(sid),
            {"k": "note", "d": json.dumps({"command_id": stale, "seat_id": seat_id,
                                           "guest_id": str(env.user_b), "text": NOTE,
                                           "expires_at": time.time() - 1})},
        )  # fmt: skip
        assert (await guest.wait_type("seat.command.result", command_id=stale))[
            "result"
        ] == "expired"
        assert host.of_type("seat.note") == []  # the other account's note was denied too
        await host.send({"type": "session.end"})


async def test_removal_closes_the_guest_and_the_end_of_the_call_closes_all(env) -> None:
    async with GatewayServer(fast(env)) as a, GatewayServer(fast(env)) as b:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        removed = await publish(env, sid)
        staying = await publish(env, sid, guest=env.user_b)
        g1, g2 = await join(b, sid, removed), await join(a, sid, staying)
        await g1.wait_type("seat.snapshot")
        await g2.wait_type("seat.snapshot")
        t0 = time.monotonic()
        await publish(env, sid, removed, status="removed", rev=2)
        assert await g1.closed() == 4411
        assert time.monotonic() - t0 < 2.0
        assert g2.close_code is None
        await host.send({"type": "session.end"})
        assert await host.closed() == 1000
        assert await g2.closed() == 1000


async def test_who_may_join(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        accepted = await publish(env, sid, status="accepted")
        assert await (await join(a, sid, accepted)).closed() == 4412  # not admitted yet
        theirs = await publish(env, sid)
        # Another account's token for Blair's seat.
        assert await (await join(a, sid, theirs, token="token-a")).closed() == 4412
        assert await (await join(a, str(uuid7()), theirs)).closed() == 4412  # another call
        await publish(env, sid)
        await publish(env, sid)  # three admitted seats: over the most guests (2)
        assert await (await join(a, sid, theirs)).closed() == 4412
        nobody = ProtocolClient(seat_url(a), token="nope")
        await nobody.connect()
        assert await nobody.closed() == 4401
        await host.send({"type": "session.end"})


async def test_a_newer_connection_takes_the_seat_over(env) -> None:
    async with GatewayServer(fast(env)) as a, GatewayServer(fast(env)) as b:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        seat_id = await publish(env, sid)
        old = await join(a, sid, seat_id)
        await old.wait_type("seat.snapshot")
        new = await join(b, sid, seat_id)
        await new.wait_type("seat.snapshot")
        assert await old.closed() == 4410
        assert new.close_code is None
        assert host.close_code is None
        await host.send({"type": "session.end"})


async def test_notes_are_rate_limited_and_sized(env) -> None:
    async with GatewayServer(fast(env, seat_note_min_interval_s=1.0)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        guest = await join(a, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        ids = [str(uuid7()) for _ in range(2)]
        for cid in ids:
            await guest.send(
                {"type": "seat.command", "command_id": cid, "kind": "note", "text": NOTE}
            )
        results = {
            cid: (await guest.wait_type("seat.command.result", command_id=cid))["result"]
            for cid in ids
        }
        assert results == {ids[0]: "ok", ids[1]: "rate_limited"}
        await guest.send(
            {"type": "seat.command", "command_id": str(uuid7()), "kind": "note", "text": "x" * 1001}
        )
        assert (await guest.wait_type("error"))["code"] == "bad_message"
        await guest.send({"type": "question.force", "text": "answer this"})
        assert (await guest.wait_type("error", code="unexpected_message"))["fatal"] is False
        assert len(host.of_type("seat.note")) == 1
        assert host.of_type("question.detected") == []  # the guest cannot ask on the call
        await host.send({"type": "session.end"})


async def test_pausing_sharing_hides_the_call_then_and_on_replay(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        guest = await join(a, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        await host.send({"type": "sharing.set", "on": False})
        await host.wait_type("sharing", on=False)
        await guest.wait_type("seat.host", sharing=False)
        private = await asked(host, "What salary are you looking for in this role?")
        await host.send({"type": "sharing.set", "on": True})
        await guest.wait_type("seat.host", sharing=True)
        shared = await asked(host, "How did you scale the ledger?")
        await guest.wait(lambda m: m["type"] == "answer.done" and m["question_id"] == shared)
        assert not any(m.get("question_id") == private for m in guest.messages)
        assert not any("salary" in json.dumps(m) for m in guest.messages)
        # A guest who joins afterwards gets no trace of it either.
        await asyncio.sleep(0.5)  # the host's view is written
        late = await join(a, sid, await publish(env, sid))
        await late.wait_type("seat.snapshot")
        folded = snapshot(late)
        assert not any(m.get("question_id") == private for m in folded)
        assert any(m.get("question_id") == shared for m in folded)
        await host.send({"type": "session.end"})


async def test_a_late_guest_gets_the_whole_call_after_the_stream_was_trimmed(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        await publish(env, sid, status="invited")  # a seat exists: the view is kept in Valkey
        first = await asked(host, "How did you scale the ledger?")
        for i in range(3):
            await asked(host, f"Tell me about project number {i} please?")
        # The stream keeps its last ~2,000 events; trim it hard, as a long call would.
        await env.client().xtrim(keys.sess_events(sid), maxlen=5, approximate=False)
        stream = await env.client().xrange(keys.sess_events(sid))
        assert int(stream[0][0].decode().split("-")[0]) > 10  # the start of the call is gone
        await asyncio.sleep(0.5)
        guest = await join(a, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        assert any(m.get("question_id") == first for m in snapshot(guest))
        await host.send({"type": "session.end"})


async def test_guests_follow_the_call_when_the_host_takes_it_over_elsewhere(env) -> None:
    async with GatewayServer(fast(env)) as a, GatewayServer(fast(env)) as b:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        guest = await join(b, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        # The host's app reconnects to task B: the old connection gets 4410, the guest nothing.
        other = ProtocolClient(b.url, token="token-a2")
        other.session_id, other.last_seq = sid, host.last_seq
        await other.resume()
        assert await host.closed() == 4410
        after = await asked(other, "How did you scale the ledger?")
        await guest.wait(lambda m: m["type"] == "answer.done" and m["question_id"] == after)
        assert guest.close_code is None
        await other.send({"type": "session.end"})
        assert await guest.closed() == 1000


async def test_a_failing_seat_desk_never_touches_the_call(env, monkeypatch) -> None:
    svc = fast(env)

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("scripted failure")

    monkeypatch.setattr(svc.store, "read_ctl", broken)
    async with GatewayServer(svc) as a:
        host = ProtocolClient(a.url)
        await host.start()
        await publish(env, host.session_id)
        await asyncio.sleep(0.5)  # the desk fails and retries meanwhile
        await asked(host, "How did you scale the ledger?")
        assert host.close_code is None
        await host.send({"type": "session.end"})
        assert await host.closed() == 1000


async def test_an_older_host_app_gets_no_seat_messages(env) -> None:
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start(client_version="desktop-0.2.14")
        sid = host.session_id
        guest = await join(a, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        cid = str(uuid7())
        await guest.send({"type": "seat.command", "command_id": cid, "kind": "note", "text": NOTE})
        assert (await guest.wait_type("seat.command.result", command_id=cid))["result"] == "failed"
        assert not {m["type"] for m in host.messages} & {"seats.update", "seat.note"}
        await host.send({"type": "session.end"})


async def test_seats_can_be_turned_off(env) -> None:
    async with GatewayServer(fast(env, seats_enabled=False)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        guest = await join(a, host.session_id, await publish(env, host.session_id))
        assert await guest.closed() == 4412
        assert host.of_type("seats.update") == []
        await host.send({"type": "session.end"})


async def test_a_guest_too_far_behind_gets_a_fresh_snapshot(env) -> None:
    # A queue of nothing: every live message finds the guest "behind", as a slow network would.
    async with GatewayServer(fast(env, seat_queue_max=0)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        guest = await join(a, sid, await publish(env, sid))
        await guest.wait_type("seat.snapshot")
        qid = await asked(host, "How did you scale the ledger?")
        await guest.wait(
            lambda m: (
                m["type"] == "seat.snapshot"
                and any(
                    x["type"] == "answer.done" and x["question_id"] == qid for x in m["messages"]
                )
            )
        )
        assert host.close_code is None
        assert guest.close_code is None
        await host.send({"type": "session.end"})


async def test_the_host_sees_who_is_following_the_call(env) -> None:
    """An invited guest shows as not yet joined, as watching while their app follows the call,
    and as not joined again once they leave."""
    async with GatewayServer(fast(env)) as a:
        host = ProtocolClient(a.url)
        await host.start()
        sid = host.session_id
        seat_id = await publish(env, sid)

        def connected(m: dict[str, Any], value: bool) -> bool:
            return m["type"] == "seats.update" and any(
                x["seat_id"] == seat_id and x.get("connected") is value for x in m["seats"]
            )

        await host.wait(lambda m: connected(m, False))
        guest = await join(a, sid, seat_id)
        await guest.wait_type("seat.snapshot")
        await host.wait(lambda m: connected(m, True))
        start = len(host.messages)
        await guest.close()
        await host.wait(lambda m: connected(m, False), start=start)
        await host.send({"type": "session.end"})
