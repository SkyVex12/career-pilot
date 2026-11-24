"""Connected live usage has explicit start/stop boundaries, never inferred wall time."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from copilot_core import keys
from copilot_gateway.auth import Principal
from copilot_gateway.metering import ConnectedLiveMeter, LivePrice
from copilot_gateway.session import HostSendQueue, Session
from copilot_gateway.testing import GatewayServer, ProtocolClient


class Clock:
    def __init__(self) -> None:
        self.seconds = 100.0
        self.utc = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds
        self.utc += timedelta(seconds=seconds)


def make_meter(clock, entries):
    return ConnectedLiveMeter(
        uuid4(),
        LivePrice("test-v1", "USD", 500),
        entries.append,
        monotonic=lambda: clock.seconds,
        utc_now=lambda: clock.utc,
    )


def test_monotonic_intervals_ignore_wall_clock_changes_and_include_silence() -> None:
    clock, entries = Clock(), []
    meter = make_meter(clock, entries)
    payer = uuid4()
    clock.advance(20)  # preparation before ready is unmetered
    meter.flush()
    assert entries == []
    meter.start(payer)
    clock.advance(10)
    meter.flush()
    clock.utc -= timedelta(hours=1)
    clock.advance(2.25)
    meter.stop()
    clock.advance(100)  # post-call work is unmetered
    meter.flush()
    assert [entry["duration_ms"] for entry in entries] == [10_000, 2_250]
    assert entries[0]["ended_at"] == entries[1]["started_at"]
    assert entries[0]["meter_epoch"] == entries[1]["meter_epoch"]
    assert len({entry["event_id"] for entry in entries}) == 2
    for entry in entries:
        assert entry["payer_account_id"] == str(payer)
        assert entry["service"] == "live"
        start = datetime.fromisoformat(entry["started_at"])
        end = datetime.fromisoformat(entry["ended_at"])
        assert start.microsecond % 1000 == end.microsecond % 1000 == 0
        assert (end - start).total_seconds() * 1000 == entry["duration_ms"]
        UUID(entry["event_id"])


def test_reconnect_excludes_gap_and_has_a_new_epoch() -> None:
    clock, entries = Clock(), []
    meter = make_meter(clock, entries)
    payer = uuid4()
    meter.start(payer)
    clock.advance(5)
    meter.stop()
    clock.advance(120)
    meter.start(payer)
    clock.advance(3)
    meter.stop()
    meter.stop()  # duplicate disconnect or end cannot duplicate usage
    assert [entry["duration_ms"] for entry in entries] == [5_000, 3_000]
    assert entries[0]["meter_epoch"] != entries[1]["meter_epoch"]
    gap = datetime.fromisoformat(entries[1]["started_at"]) - datetime.fromisoformat(
        entries[0]["ended_at"]
    )
    assert gap.total_seconds() == 120


def test_lost_lease_discards_unconfirmed_tail() -> None:
    clock, entries = Clock(), []
    meter = make_meter(clock, entries)
    meter.start(uuid4())
    clock.advance(10)
    meter.flush()
    clock.advance(7)
    meter.stop(discard=True)
    assert sum(entry["duration_ms"] for entry in entries) == 10_000
    assert not meter.active


def test_reconnect_after_wall_clock_rollback_does_not_overlap_confirmed_usage() -> None:
    clock, entries = Clock(), []
    meter = make_meter(clock, entries)
    payer = uuid4()
    meter.start(payer)
    clock.advance(10)
    meter.stop()
    clock.utc -= timedelta(hours=1)
    clock.advance(5)  # the disconnected gap still exists in the monotonic UTC mapping
    meter.start(payer)
    clock.advance(5)
    meter.stop()
    assert sum(entry["duration_ms"] for entry in entries) == 15_000
    gap = datetime.fromisoformat(entries[1]["started_at"]) - datetime.fromisoformat(
        entries[0]["ended_at"]
    )
    assert gap.total_seconds() == 5


def test_socket_queue_bounds_bytes_entries_and_can_close_when_full() -> None:
    q = HostSendQueue(2, 6)
    q.put_nowait("éé")  # UTF-8 bytes, not character count
    with pytest.raises(asyncio.QueueFull):
        q.put_nowait("abc")
    q.put_nowait("ok")
    with pytest.raises(asyncio.QueueFull):
        q.put_nowait("")
    assert q.bytes == 6
    q.close()
    assert q.get_nowait() is None
    assert q.bytes == 0


async def intervals(env, sid):
    return [
        entry
        for _, fields in await env.client().xrange(keys.sess_log(sid))
        if (entry := json.loads(fields[b"e"]))["kind"] == "usage.interval"
    ]


async def test_live_silence_disconnect_replacement_and_end_are_metered(env) -> None:
    settings = env.settings(usage_interval_s=0.04)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        session = next(iter(gw.manager.sessions.values()))
        await asyncio.sleep(0.12)  # no speech or model calls
        await c.drop()
        await asyncio.sleep(0.08)
        assert not session.usage_meter.active
        assert await session.writer.drain(within_s=1)
        before = await intervals(env, c.session_id)
        assert before
        await asyncio.sleep(0.12)
        assert await intervals(env, c.session_id) == before
        last = c.last_seq
        await c.resume()
        await c.wait(lambda m: m["type"] == "session.language" and m["seq"] > last)
        await asyncio.sleep(0.07)
        other = ProtocolClient(gw.url)
        other.session_id, other.last_seq = c.session_id, c.last_seq
        await other.resume()
        assert await c.closed() == 4410
        await other.wait_type("session.language")
        await asyncio.sleep(0.07)
        await other.send({"type": "session.end"})
        assert await other.closed() == 1000
        after = await intervals(env, c.session_id)
        assert len({entry["meter_epoch"] for entry in after}) == 3
        assert {entry["payer_account_id"] for entry in after} == {str(env.user_a)}
        assert {entry["rate_minor_per_hour"] for entry in after} == {500}
        assert not session.usage_meter.active
        await asyncio.sleep(0.08)
        assert await intervals(env, c.session_id) == after


async def test_no_call_has_no_live_usage(env) -> None:
    async with GatewayServer(env.services(env.settings(usage_interval_s=0.02))) as gw:
        c = ProtocolClient(gw.url)
        await c.start(no_call=True)
        await asyncio.sleep(0.08)
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
        assert await intervals(env, c.session_id) == []


async def test_startup_before_ready_delivery_is_unmetered(env, monkeypatch) -> None:
    gate = asyncio.Event()
    blocked = asyncio.Event()
    original = Session._sender

    async def delayed_sender(self, ws, queue, replay=()):
        blocked.set()
        await gate.wait()
        await original(self, ws, queue, replay)

    monkeypatch.setattr(Session, "_sender", delayed_sender)
    async with GatewayServer(env.services(env.settings(usage_interval_s=0.02))) as gw:
        c = ProtocolClient(gw.url)
        starting = asyncio.create_task(c.start())
        try:
            await asyncio.wait_for(blocked.wait(), timeout=2)
            session = next(iter(gw.manager.sessions.values()))
            await asyncio.sleep(0.08)
            assert not session.usage_meter.active
            assert await intervals(env, str(session.sid)) == []
            gate.set()
            await starting
            await asyncio.sleep(0.04)
            await c.send({"type": "session.end"})
            assert await c.closed() == 1000
            assert await intervals(env, c.session_id)
        finally:
            gate.set()
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)


async def test_cross_task_resume_keeps_original_price(env) -> None:
    first = env.settings(usage_interval_s=0.03, billing_rate_minor_per_hour=500)
    second = env.settings(
        usage_interval_s=0.03, billing_rate_minor_per_hour=900, billing_price_version="next-v2"
    )
    async with GatewayServer(env.services(first)) as a, GatewayServer(env.services(second)) as b:
        c = ProtocolClient(a.url)
        await c.start()
        await asyncio.sleep(0.08)
        await c.drop()
        session = next(iter(a.manager.sessions.values()))
        assert await session.writer.drain(within_s=1)
        c.url = b.url
        last = c.last_seq
        await c.resume()
        await c.wait(lambda m: m["type"] == "session.language" and m["seq"] > last)
        await asyncio.sleep(0.08)
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
        usage = await intervals(env, c.session_id)
        assert len({entry["meter_epoch"] for entry in usage}) == 2
        assert {entry["rate_minor_per_hour"] for entry in usage} == {500}
        assert {entry["price_version"] for entry in usage} == {"live-usd-v1"}


async def test_lease_failure_never_registers_or_readies_session(env, monkeypatch) -> None:
    svc = env.services()

    async def failed_lease(*args):
        raise ValkeyConnectionError("lease unavailable")

    monkeypatch.setattr(svc.store, "take_lease", failed_lease)
    async with GatewayServer(svc) as gw:
        c = ProtocolClient(gw.url)
        await c.connect()
        await c.send(
            {
                "type": "session.start",
                "mode": "interview",
                "platform": "zoom",
                "client_version": "test",
                "consent": {"store_transcript": True},
                "audio": {"sample_rate": 16000, "encoding": "pcm16"},
            }
        )
        assert await c.closed() == 4500
        assert gw.manager.sessions == {}
        assert c.of_type("session.ready") == []
        assert await env.client().get(keys.user_active_session(env.user_a)) is None


async def test_local_resume_rejects_another_payer_even_for_same_workspace(env, monkeypatch) -> None:
    svc = env.services()
    async with GatewayServer(svc) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        session = next(iter(gw.manager.sessions.values()))

        # Account B is legitimately allowed to resolve to the same workspace as A.
        async def same_workspace(account, profile):
            return Principal(env.user_a, "Shared", None)

        monkeypatch.setattr(gw.manager, "as_profile", same_workspace)
        other = ProtocolClient(f"{gw.url}?profile=shared", token="token-b")
        other.session_id = c.session_id
        await other.resume()
        assert await other.closed() == 4401
        assert other.of_type("error")[0]["code"] == "payer_mismatch"
        assert session.usage_payer_account_id == env.user_a
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000


async def test_legacy_resume_does_not_reclassify_old_time(env) -> None:
    async with (
        GatewayServer(env.services()) as a,
        GatewayServer(env.services(env.settings(usage_interval_s=0.02))) as b,
    ):
        c = ProtocolClient(a.url)
        await c.start()
        await c.drop()
        old = next(iter(a.manager.sessions.values()))
        await old.abandon(1001, "test upgrade")
        vk = env.client()
        await vk.hdel(
            keys.sess_state(c.session_id),
            "metering_version",
            "usage_payer_account_id",
            "billing_price_version",
            "billing_currency",
            "billing_rate_minor_per_hour",
        )
        before = await intervals(env, c.session_id)
        c.url = b.url
        last = c.last_seq
        await c.resume()
        await c.wait(lambda m: m["type"] == "session.language" and m["seq"] > last)
        restored = next(iter(b.manager.sessions.values()))
        assert restored.metering_version == "legacy-wall-v0"
        await asyncio.sleep(0.08)
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
        assert await intervals(env, c.session_id) == before


async def test_retention_overflow_stops_meter_and_ends_cleanly(env) -> None:
    settings = env.settings(retained_log_max_entries=3, usage_interval_s=10)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        session = next(iter(gw.manager.sessions.values()))
        assert await session.writer.drain(within_s=1)
        env.valkey_server.connected = False
        for i in range(5):
            session.log_entry({"kind": "test", "number": i})
        assert session._retention_failed
        assert not session.usage_meter.active
        error = await c.wait_type("error", code="retention_unavailable")
        assert error["fatal"]
        env.valkey_server.connected = True
        assert await c.closed(timeout=5) == 4500


async def test_resume_does_not_overwrite_active_slot_after_failed_cas(env, monkeypatch) -> None:
    async with GatewayServer(env.services()) as a, GatewayServer(env.services()) as b:
        c = ProtocolClient(a.url)
        await c.start()
        await c.drop()
        old = next(iter(a.manager.sessions.values()))
        await old.abandon(1001, "test handover")
        competing = uuid4()
        await env.client().set(keys.user_active_session(env.user_a), str(competing))

        async def lost_cas(*args):
            return False

        monkeypatch.setattr(b.services.store, "replace_active", lost_cas)
        c.url = b.url
        await c.resume()
        assert await c.closed() == 4409
        assert b.manager.sessions == {}
        active = await env.client().get(keys.user_active_session(env.user_a))
        assert active == str(competing).encode()


@pytest.mark.parametrize("target", ["local", "remote"])
@pytest.mark.parametrize("final_state", ["ended", "deleted"])
async def test_delayed_resume_cannot_revive_finished_or_deleted_session(
    env, monkeypatch, target: str, final_state: str
) -> None:
    settings = env.settings(usage_interval_s=0.02, lease_renew_s=10)
    async with (
        GatewayServer(env.services(settings)) as a,
        GatewayServer(env.services(settings)) as b,
    ):
        c = ProtocolClient(a.url)
        await c.start()
        old = next(iter(a.manager.sessions.values()))
        stopped = asyncio.Event()
        stop_meter = old.usage_meter.stop

        def observed_stop(*, discard: bool = False) -> None:
            stop_meter(discard=discard)
            stopped.set()

        monkeypatch.setattr(old.usage_meter, "stop", observed_stop)
        await c.drop()
        await asyncio.wait_for(stopped.wait(), timeout=2)
        assert await old.writer.drain(within_s=1)
        if target == "remote":
            await old.abandon(1001, "test handover")
        destination = a if target == "local" else b
        store = destination.services.store
        take_resume_lease = store.take_resume_lease
        checked = False

        async def finish_before_acquisition(sid: UUID, task_id: str) -> bool:
            nonlocal checked
            # The resume already selected its in-memory session or read resumable state.
            assert (await store.load_state(sid))["status"] != "ended"
            checked = True
            await store.client.delete(keys.sess_lease(sid))
            if final_state == "ended":
                await store.mark_ended(sid, "test finished while resume was delayed")
            else:
                await store.client.delete(keys.sess_state(sid))
            return await take_resume_lease(sid, task_id)

        monkeypatch.setattr(store, "take_resume_lease", finish_before_acquisition)
        before = await intervals(env, c.session_id)
        c.url = destination.url
        await c.resume()
        assert await c.closed() == 4408
        assert checked
        assert c.of_type("error")[-1]["code"] == "session_not_found"
        if target == "local":
            assert old.end_task is not None
            await old.end_task
        assert destination.manager.sessions == {}
        assert not old.usage_meter.active
        assert await store.lease_holder(c.session_id) is None
        assert await store.client.get(keys.user_active_session(env.user_a)) is None
        state = await store.load_state(c.session_id)
        assert state.get("status") == ("ended" if final_state == "ended" else None)
        assert await intervals(env, c.session_id) == before
