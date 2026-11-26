"""Socket-free HTTP/2 cancellation regressions with self-contained peer fixtures.

Real H2/h11 framing and authored Responses SSE exercise receive credit, resets,
concurrent siblings and cleanup ownership. No socket or model call is permitted.
The fixture output slots are authored; these are mechanism tests, not latency tests.
"""

from __future__ import annotations

# The mock backend implements httpcore's timeout-bearing protocol signatures.
# ruff: noqa: ASYNC109
import asyncio
import json
import socket
import ssl
import time
from collections import deque
from contextlib import suppress
from typing import Any, cast

import h2.config
import h2.connection
import h2.errors
import h2.events
import h11
import httpcore2
import httpx2
import openai
import pytest
from httpcore2._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from copilot_core._http2_cancel import (
    CancellationSafeHTTP2Connection,
    CancellationSafePool,
)

MOCK_TLS_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

DELTA = (
    b'data: {"type":"response.output_text.delta","delta":"OK","content_index":0,'
    b'"output_index":0,"item_id":"msg_mock","sequence_number":1}\n\ndata: [DONE]\n\n'
)


def no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Actual network access is forbidden in this reproduction")


class Harness:
    def __init__(self, http2: bool, orphan_bytes: int):
        self.http2 = http2
        self.orphan_bytes = orphan_bytes
        self.modes: deque[str] = deque()
        self.streams: list[Peer] = []
        self.received = asyncio.Event()
        self.response_opened = asyncio.Event()
        self.latest: tuple[Peer, int] | None = None
        self.requests = 0
        self.resets = 0
        self.orphan_sent = 0
        self.window_updates = 0

    def arm(self, mode: str) -> None:
        self.received.clear()
        self.response_opened.clear()
        self.modes.append(mode)

    def accept(self, peer: Peer, sid: int) -> str:
        assert self.modes, "Unexpected hidden retry/request"
        mode = self.modes.popleft()
        self.requests += 1
        self.latest = (peer, sid)
        self.received.set()
        return mode

    def snapshot(self) -> dict[str, Any]:
        return {
            "connections_created": len(self.streams),
            "connections_closed": sum(p.closed for p in self.streams),
            "requests": self.requests,
            "rst_streams_received": self.resets,
            "orphan_body_bytes_sent": self.orphan_sent,
            "connection_window_updates": self.window_updates,
            "server_connection_windows": [
                p.h2.outbound_flow_control_window for p in self.streams if p.h2 and not p.closed
            ],
            "server_open_streams": [
                p.h2.open_outbound_streams for p in self.streams if p.h2 and not p.closed
            ],
            "unconsumed_peer_bytes": sum(len(p.buffer) for p in self.streams if not p.closed),
        }


class Peer(AsyncNetworkStream):
    def __init__(self, harness: Harness):
        self.harness = harness
        self.closed = False
        self.buffer = bytearray()
        self.available = asyncio.Event()
        self.pending: dict[int, tuple[int, bool]] = {}
        self.responses: dict[int, str] = {}
        self.reset_ids: set[int] = set()
        self.h2 = (
            h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
            if harness.http2
            else None
        )
        self.h11 = h11.Connection(h11.SERVER) if not harness.http2 else None
        self.request_id = 0
        if self.h2:
            self.h2.initiate_connection()
            self._enqueue(self.h2.data_to_send())

    def _enqueue(self, data: bytes) -> None:
        if data:
            self.buffer.extend(data)
            self.available.set()

    async def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> Peer:
        assert server_hostname == "mock.invalid"
        # Simulation only. No certificate or real TLS operation is performed.
        return self

    def selected_alpn_protocol(self) -> str:
        return "h2" if self.harness.http2 else "http/1.1"

    def get_extra_info(self, info: str) -> Any:
        if info == "ssl_object":
            return self
        if info == "is_readable":
            return bool(self.buffer) or self.closed
        return None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        while not self.buffer and not self.closed:
            self.available.clear()
            if timeout is None:
                await self.available.wait()
            else:
                try:
                    await asyncio.wait_for(self.available.wait(), timeout)
                except TimeoutError as error:
                    raise httpcore2.ReadTimeout("mock read deadline") from error
        if not self.buffer:
            return b""
        data = bytes(self.buffer[:max_bytes])
        del self.buffer[:max_bytes]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        assert not self.closed
        if not buffer:
            return  # An empty write is not a peer EOF.
        if self.h2:
            events = self.h2.receive_data(buffer)
            for event in events:
                if isinstance(event, h2.events.DataReceived):
                    self.h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    self._respond_h2(event.stream_id)
                elif isinstance(event, h2.events.StreamReset):
                    self.harness.resets += 1
                    self.reset_ids.add(event.stream_id)
                    self.pending.pop(event.stream_id, None)
                elif isinstance(event, h2.events.WindowUpdated) and event.stream_id == 0:
                    self.harness.window_updates += 1
            self._pump_h2()
            self._enqueue(self.h2.data_to_send())
        else:
            assert self.h11
            self.h11.receive_data(buffer)
            while True:
                event = self.h11.next_event()
                if event is h11.NEED_DATA or event is h11.PAUSED:
                    break
                if isinstance(event, h11.EndOfMessage):
                    self.request_id += 1
                    mode = self.harness.accept(self, self.request_id)
                    self.responses[self.request_id] = mode
                    if mode != "cancel_headers":
                        self._enqueue(
                            self.h11.send(
                                h11.Response(
                                    status_code=200,
                                    headers=[
                                        (b"content-type", b"text/event-stream"),
                                        (b"transfer-encoding", b"chunked"),
                                    ],
                                )
                            )
                        )
                    if mode == "healthy":
                        self._enqueue(self.h11.send(h11.Data(data=DELTA)))
                        self._enqueue(self.h11.send(h11.EndOfMessage()))
                        self.h11.start_next_cycle()

    def _respond_h2(self, sid: int) -> None:
        assert self.h2
        mode = self.harness.accept(self, sid)
        self.responses[sid] = mode
        if mode != "cancel_headers":
            self.h2.send_headers(
                sid, [(b":status", b"200"), (b"content-type", b"text/event-stream")]
            )
        if mode == "healthy":
            self.pending[sid] = (len(DELTA), False)

    def _pump_h2(self) -> None:
        assert self.h2
        for sid in list(self.pending):
            remaining, orphan = self.pending[sid]
            while remaining:
                allowed = min(
                    remaining,
                    self.h2.local_flow_control_window(sid),
                    self.h2.max_outbound_frame_size,
                )
                if allowed <= 0:
                    break
                offset = (self.harness.orphan_bytes if orphan else len(DELTA)) - remaining
                data = b"x" * allowed if orphan else DELTA[offset : offset + allowed]
                remaining -= allowed
                self.h2.send_data(sid, data, end_stream=remaining == 0)
                if orphan:
                    self.harness.orphan_sent += allowed
            if remaining:
                self.pending[sid] = (remaining, orphan)
            else:
                del self.pending[sid]

    def release_cancelled(self, sid: int) -> None:
        if self.closed or sid in self.reset_ids:
            return
        mode = self.responses[sid]
        assert mode.startswith("cancel")
        if self.h2:
            if mode == "cancel_headers":
                self.h2.send_headers(
                    sid, [(b":status", b"200"), (b"content-type", b"text/event-stream")]
                )
            if self.harness.orphan_bytes:
                self.pending[sid] = (self.harness.orphan_bytes, True)
            else:
                self.h2.end_stream(sid)
            self._pump_h2()
            self._enqueue(self.h2.data_to_send())
        else:
            assert self.h11
            if mode == "cancel_headers":
                self._enqueue(
                    self.h11.send(
                        h11.Response(
                            status_code=200,
                            headers=[
                                (b"content-type", b"text/event-stream"),
                                (b"transfer-encoding", b"chunked"),
                            ],
                        )
                    )
                )
            self._enqueue(self.h11.send(h11.Data(data=b"x" * self.harness.orphan_bytes)))
            self._enqueue(self.h11.send(h11.EndOfMessage()))
            self.harness.orphan_sent += self.harness.orphan_bytes
            self.h11.start_next_cycle()

    async def aclose(self) -> None:
        self.closed = True
        self.available.set()
        self.pending.clear()
        self.buffer.clear()


class Backend(AsyncNetworkBackend):
    def __init__(self, harness: Harness):
        self.harness = harness

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Peer:
        assert host == "mock.invalid"
        assert port == 443
        peer = Peer(self.harness)
        self.harness.streams.append(peer)
        return peer

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Peer:
        raise AssertionError("Unix socket access forbidden")

    async def sleep(self, seconds: float) -> None:
        raise AssertionError("No connection retry is authorized")


async def consume(client: openai.AsyncOpenAI, harness: Harness) -> str:
    stream = await client.responses.create(
        model="authored-local-mock",
        input="Local authored fixture",
        store=False,
        max_output_tokens=32,
        stream=True,
    )
    harness.response_opened.set()
    try:
        async for event in stream:
            if event.type == "response.output_text.delta":
                return event.delta
        raise AssertionError("No authored text")
    finally:
        await stream.close()


async def cancel_one(client: openai.AsyncOpenAI, harness: Harness, mode: str) -> None:
    harness.arm(mode)
    task = asyncio.create_task(consume(client, harness))
    try:
        await asyncio.wait_for(harness.received.wait(), 2)
        if mode == "cancel_body":
            await asyncio.wait_for(harness.response_opened.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected canceled authored request")
        assert harness.latest is not None
        peer, sid = harness.latest
        peer.release_cancelled(sid)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def healthy_one(client: openai.AsyncOpenAI, harness: Harness) -> str:
    harness.arm("healthy")
    try:
        result = await asyncio.wait_for(consume(client, harness), 0.25)
        assert result == "OK"
        return "delivered"
    except TimeoutError:
        return "deadline"


def sse_output(slots: int) -> bytes:
    assert 1 <= slots <= 3248
    chunks = []
    for i in range(slots):
        event = {
            "type": "response.output_text.delta",
            "item_id": "msg_authored_cancel_fixture",
            "output_index": 0,
            "content_index": 0,
            "delta": "word",
            "sequence_number": i + 1,
        }
        chunks.append(
            b"event: response.output_text.delta\ndata: "
            + json.dumps(event, separators=(",", ":")).encode()
            + b"\n\n"
        )
    chunks.append(b"data: [DONE]\n\n")
    return b"".join(chunks)


class SSEHarness(Harness):
    def __init__(self, slots: int):
        self.payload = sse_output(slots)
        super().__init__(True, len(self.payload))
        self.slots = slots
        self.sent_by_stream: dict[int, int] = {}


class SSEPeer(Peer):
    harness: SSEHarness

    def _pump_h2(self) -> None:
        assert self.h2
        for sid in list(self.pending):
            remaining, orphan = self.pending[sid]
            payload = self.harness.payload if orphan else DELTA
            while remaining:
                allowed = min(
                    remaining,
                    self.h2.local_flow_control_window(sid),
                    self.h2.max_outbound_frame_size,
                )
                if allowed <= 0:
                    break
                offset = len(payload) - remaining
                remaining -= allowed
                self.h2.send_data(
                    sid, payload[offset : offset + allowed], end_stream=remaining == 0
                )
                self.harness.sent_by_stream[sid] = self.harness.sent_by_stream.get(sid, 0) + allowed
                if orphan:
                    self.harness.orphan_sent += allowed
            if remaining:
                self.pending[sid] = (remaining, orphan)
            else:
                del self.pending[sid]


class SSEBackend(Backend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> SSEPeer:
        assert host == "mock.invalid"
        assert port == 443
        peer = SSEPeer(self.harness)
        self.harness.streams.append(peer)
        return peer


def make_client(harness: SSEHarness, fixed: bool):
    transport = httpx2.AsyncHTTPTransport(
        http1=False, http2=True, retries=0, verify=MOCK_TLS_CONTEXT
    )
    cls = CancellationSafePool if fixed else httpcore2.AsyncConnectionPool
    pool = cls(
        http1=False,
        http2=True,
        retries=0,
        max_connections=1,
        max_keepalive_connections=1,
        keepalive_expiry=120,
        network_backend=SSEBackend(harness),
        ssl_context=MOCK_TLS_CONTEXT,
    )
    transport._pool = pool
    client = openai.AsyncOpenAI(
        api_key="local-authored-dummy",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        timeout=httpx2.Timeout(5, connect=2),
        http_client=httpx2.AsyncClient(transport=transport, trust_env=False),
    )
    return client, pool


async def run_case(*, fixed: bool, slots: int, cycles: int, phase: str) -> dict[str, Any]:
    harness = SSEHarness(slots)
    client, pool = make_client(harness, fixed)
    statuses = []
    start = time.perf_counter()
    try:
        for _ in range(cycles):
            await cancel_one(client, harness, phase)
            assert len(pool._requests) == 0
            statuses.append(await healthy_one(client, harness))
            if statuses[-1] != "delivered":
                for _ in range(2):
                    statuses.append(await healthy_one(client, harness))
                break
        snapshots = []
        for connection in pool.connections:
            h2c = cast(Any, connection)._connection
            snapshots.append(
                {
                    "queued_ack_bytes": getattr(h2c, "reclaimed_queued_bytes", None),
                    "orphan_ack_bytes": getattr(h2c, "reclaimed_orphan_bytes", None),
                    "cancel_resets": getattr(h2c, "cancel_resets", None),
                }
            )
        before = harness.snapshot()
    finally:
        await client.close()
    return {
        "fixed": fixed,
        "authored_output_slots": slots,
        "sse_body_bytes": len(harness.payload),
        "delta_text_bytes": 4 * slots,
        "planned_cycles": cycles,
        "cancel_phase": phase,
        "delivered_probes": statuses.count("delivered"),
        "deadlines": statuses.count("deadline"),
        "probe_outcomes": statuses,
        "before_close": before,
        "cleanup_counters": snapshots,
        "all_connections_closed": all(p.closed for p in harness.streams),
        "pending_pool_requests": len(pool._requests),
        "elapsed_s": time.perf_counter() - start,
    }


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)


async def open_stream(client, harness):
    harness.arm("cancel_body")
    stream = await client.responses.create(
        model="authored", input="local", store=False, max_output_tokens=3248, stream=True
    )
    assert harness.latest is not None
    return stream, harness.latest[0], harness.latest[1]


def connection(pool):
    return pool.connections[0]._connection


async def collect(stream):
    try:
        return [getattr(e, "delta", None) async for e in stream]
    finally:
        await stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [False, True])
@pytest.mark.parametrize("phase", ["cancel_headers", "cancel_body"])
async def test_capped_sse_credit_exhaustion_and_reset_fix(fixed, phase):
    result = await run_case(fixed=fixed, slots=1200, cycles=160, phase=phase)
    assert result["pending_pool_requests"] == 0
    assert result["all_connections_closed"]
    assert result["before_close"]["connections_created"] == 1
    if fixed:
        assert result["delivered_probes"] == 160
        assert result["deadlines"] == 0
        assert result["before_close"]["rst_streams_received"] == 160
        assert result["before_close"]["orphan_body_bytes_sent"] == 0
    else:
        assert result["delivered_probes"] == 73
        assert result["deadlines"] == 3
        assert result["before_close"]["server_connection_windows"] == [0]
        assert result["before_close"]["orphan_body_bytes_sent"] >= 2**24


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [False, True])
async def test_queued_data_credit_preserved_across_completed_remote_streams(fixed):
    harness = SSEHarness(3248)
    client, pool = make_client(harness, fixed)
    delivered = 0
    peer: Any = None
    try:
        for _ in range(64):
            stream, peer, sid = await open_stream(client, harness)
            peer.release_cancelled(sid)  # Real DATA sent before the client closes.
            outcome = await healthy_one(client, harness)  # Sibling reads/queues it.
            await stream.close()
            if outcome != "delivered":
                break
            delivered += 1
        h2c = connection(pool)
        if fixed:
            assert delivered == 64
            assert h2c.reclaimed_queued_bytes == 64 * len(harness.payload)
            assert harness.window_updates > 1
            assert harness.resets == 0  # Remote END_STREAM already closed it.
        else:
            assert delivered < 64
            assert peer is not None
            assert peer.h2.outbound_flow_control_window == 0
    finally:
        await client.close()
    assert len(pool._requests) == 0


@pytest.mark.asyncio
async def test_late_orphan_data_reclaims_connection_credit():
    harness = SSEHarness(3248)
    client, pool = make_client(harness, True)
    peer: Any = None
    h2c: Any = None
    try:
        for _ in range(64):
            stream, peer, sid = await open_stream(client, harness)
            h2c = connection(pool)
            # Authored old-state cleanup simulates an orphan not locally reset.
            # Calls the unmodified base method only for this fixture's stream.
            await cast(Any, super(CancellationSafeHTTP2Connection, h2c))._response_closed(sid)
            peer.release_cancelled(sid)
            assert await healthy_one(client, harness) == "delivered"
            await stream.close()
        assert h2c is not None
        assert peer is not None
        assert h2c.reclaimed_orphan_bytes == 64 * len(harness.payload)
        assert harness.window_updates > 1
        assert peer.h2.outbound_flow_control_window > 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_canceled_stream_does_not_reset_or_close_live_sibling():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        first, peer, first_id = await open_stream(client, harness)
        sibling, same_peer, sibling_id = await open_stream(client, harness)
        assert same_peer is peer
        reader = asyncio.create_task(collect(sibling))
        canceled = asyncio.create_task(collect(first))
        await asyncio.sleep(0)
        canceled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled
        assert peer.reset_ids == {first_id}
        assert not peer.closed
        peer.responses[sibling_id] = "healthy"
        peer.pending[sibling_id] = (len(DELTA), False)
        peer._pump_h2()
        peer._enqueue(peer.h2.data_to_send())
        assert await asyncio.wait_for(reader, 1) == ["OK"]
        assert sibling_id not in peer.reset_ids
        assert await healthy_one(client, harness) == "delivered"
        assert len(pool._requests) == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reentrant_write_cleanup_defers_flush_without_deadlock():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        h2c = connection(pool)
        request = h2c._close_requests[sid]
        original = peer.write
        entered = False

        async def reentrant_write(data, timeout=None):
            nonlocal entered
            if not entered:
                entered = True
                await h2c._response_closed(sid)
            await original(data, timeout)

        peer.write = reentrant_write
        h2c._h2_state.ping(b"12345678")
        await asyncio.wait_for(h2c._write_outgoing_data(request), 1)
        assert h2c.deferred_reentrant_flushes == 1
        assert peer.reset_ids == {sid}
        assert not h2c._control_flush_pending
        await stream.close()
        assert await healthy_one(client, harness) == "delivered"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_failure_during_close_is_not_silent_or_reused():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, _ = await open_stream(client, harness)
        h2c = connection(pool)

        async def fail_write(data, timeout=None):
            raise httpcore2.WriteError("authored transport failure")

        peer.write = fail_write
        with pytest.raises(httpcore2.WriteError, match="authored transport failure"):
            await asyncio.wait_for(stream.close(), 1)
        assert h2c._connection_error
        assert not h2c.is_available()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_does_not_overrelease_slots():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, _peer, sid = await open_stream(client, harness)
        h2c = connection(pool)
        await stream.close()
        await h2c._response_closed(sid)
        await stream.close()
        assert harness.resets == 1
        assert not h2c._events
        assert not h2c._close_requests
        assert await healthy_one(client, harness) == "delivered"
        assert len(pool._requests) == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_remote_refused_stream_then_healthy_sibling():
    harness = SSEHarness(1200)
    client, _pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        peer.h2.reset_stream(sid, h2.errors.ErrorCodes.REFUSED_STREAM)
        peer._enqueue(peer.h2.data_to_send())
        with pytest.raises(openai.APIConnectionError) as error:
            await asyncio.wait_for(collect(stream), 1)
        assert isinstance(error.value.__cause__, httpx2.RemoteProtocolError)
        assert sid not in peer.reset_ids  # Already remotely reset; no redundant CANCEL.
        assert await healthy_one(client, harness) == "delivered"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_refusal_sse_normal_eof_never_sends_cancel():
    harness = SSEHarness(1200)
    client, _pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        data = (
            b'data: {"type":"response.refusal.delta","delta":"No","item_id":"x",'
            b'"output_index":0,"content_index":0,"sequence_number":1}\n\ndata: [DONE]\n\n'
        )
        peer.h2.send_data(sid, data, end_stream=True)
        peer._enqueue(peer.h2.data_to_send())
        assert await asyncio.wait_for(collect(stream), 1) == ["No"]
        assert harness.resets == 0
        assert await healthy_one(client, harness) == "delivered"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_normal_eof_is_unchanged_for_repeated_requests():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        for _ in range(64):
            assert await healthy_one(client, harness) == "delivered"
        h2c = connection(pool)
        assert h2c.cancel_resets == 0
        assert h2c.reclaimed_orphan_bytes == h2c.reclaimed_queued_bytes == 0
        assert len(harness.streams) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_second_cancellation_retains_cleanup_until_state_lock_released():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        h2c = connection(pool)
        async with h2c._state_lock:
            close = asyncio.create_task(stream.close())
            for _ in range(10):
                await asyncio.sleep(0)
                if h2c._closing_tasks:
                    break
            assert sid in h2c._closing_tasks
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close
            assert sid in h2c._closing_tasks
            assert not h2c._closing_tasks[sid].done()
        task = h2c._closing_tasks.get(sid)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 1)
        assert sid not in h2c._events
        assert peer.reset_ids == {sid}
        assert await healthy_one(client, harness) == "delivered"
        assert not h2c.cleanup_failures
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancellation_during_control_flush_is_owned_and_client_close_drains():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    stream, peer, sid = await open_stream(client, harness)
    h2c = connection(pool)
    original = peer.write
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def held_write(data, timeout=None):
        entered.set()
        await gate.wait()
        await original(data, timeout)

    peer.write = held_write
    close = asyncio.create_task(stream.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert sid in h2c._closing_tasks
        gate.set()
        await asyncio.wait_for(asyncio.shield(h2c._closing_tasks[sid]), 1)
        assert peer.reset_ids == {sid}
        assert not h2c.cleanup_failures
    finally:
        gate.set()
        await asyncio.wait_for(client.close(), 1)
    assert not h2c._closing_tasks


@pytest.mark.asyncio
async def test_goaway_close_has_no_cleanup_self_join():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        h2c = connection(pool)
        peer.h2.close_connection(error_code=h2.errors.ErrorCodes.NO_ERROR, last_stream_id=sid)
        peer._enqueue(peer.h2.data_to_send())
        with pytest.raises(openai.APIConnectionError) as error:
            await asyncio.wait_for(collect(stream), 1)
        assert isinstance(error.value.__cause__, httpx2.RemoteProtocolError)
        assert h2c.is_closed()
        assert not h2c._events
    finally:
        await asyncio.wait_for(client.close(), 1)


@pytest.mark.asyncio
async def test_cleanup_deadline_retires_connection_and_keeps_no_unhandled_task():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, _sid = await open_stream(client, harness)
        h2c = connection(pool)
        h2c.CLEANUP_TIMEOUT_S = 0.03
        async with h2c._state_lock:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(stream.close(), 1)
        await asyncio.sleep(0)
        assert not h2c.is_available()
        assert h2c.is_closed()
        assert peer.closed
        assert h2c.cleanup_failures == ["TimeoutError"]
        assert not h2c._closing_tasks
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_late_inflight_data_during_state_lock_wait_is_credited_by_h2():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    try:
        stream, peer, sid = await open_stream(client, harness)
        h2c = connection(pool)
        # Frames already in flight before RST arrives are legal; encode them now.
        for _ in range(8):
            peer.h2.send_data(sid, b"x" * 16384, end_stream=False)
        peer._enqueue(peer.h2.data_to_send())
        async with h2c._state_lock:
            close = asyncio.create_task(stream.close())
            for _ in range(10):
                await asyncio.sleep(0)
                if h2c._h2_state.streams[sid].closed:
                    break
            assert h2c._h2_state.streams[sid].closed
            # Read via an existing request context, not a new request that needs
            # the held state lock. h2 handles DATA on its locally reset stream.
            request = next(iter(h2c._closing_tasks.values()))
            assert not request.done()
            from httpcore2._models import Request

            probe = Request(
                "GET", "https://mock.invalid/local", extensions={"timeout": {"read": 1, "write": 1}}
            )
            while peer.buffer:
                await asyncio.wait_for(h2c._receive_events(probe), 1)
            manager = h2c._h2_state._inbound_flow_control_window_manager
            assert manager._bytes_processed >= 8 * 16384
        await asyncio.wait_for(close, 1)
        assert h2c.reclaimed_queued_bytes == 0
        assert h2c.reclaimed_orphan_bytes == 0  # h2 itself handles closed-stream DATA.
        assert await healthy_one(client, harness) == "delivered"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_socket_close_timeout_still_drains_owned_cleanup():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    stream, peer, _sid = await open_stream(client, harness)
    h2c = connection(pool)
    original_close = peer.aclose
    entered = asyncio.Event()
    h2c.SOCKET_CLOSE_TIMEOUT_S = 0.03

    async def blocked_close():
        entered.set()
        await asyncio.Event().wait()

    peer.aclose = blocked_close
    try:
        async with h2c._state_lock:
            close_stream = asyncio.create_task(stream.close())
            for _ in range(10):
                await asyncio.sleep(0)
                if h2c._closing_tasks:
                    break
            close_connection = asyncio.create_task(h2c.aclose())
            await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(close_stream, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(close_connection, 1)
        assert not h2c._closing_tasks
    finally:
        peer.aclose = original_close
        await client.close()


def test_source_drift_rejected_before_connection_creation(monkeypatch):
    from copilot_core import _http2_cancel as candidate

    monkeypatch.setitem(candidate.SUPPORTED_SOURCES, "_async/http2.py", "0" * 64)
    with pytest.raises(RuntimeError, match="source differs"):
        candidate.CancellationSafePool(http2=True)


def test_proxy_is_explicitly_outside_scoped_factory():
    from httpcore2._models import Origin, Proxy

    from copilot_core._http2_cancel import CancellationSafePool

    pool = CancellationSafePool(proxy=Proxy("http://mock.invalid:8080"))
    with pytest.raises(ValueError, match="Proxy transport"):
        pool.create_connection(Origin(b"https", b"mock.invalid", 443))


@pytest.mark.asyncio
async def test_owned_cleanup_capacity_fails_closed_without_extra_task():
    harness = SSEHarness(1200)
    client, pool = make_client(harness, True)
    first, peer, first_id = await open_stream(client, harness)
    second, _, _ = await open_stream(client, harness)
    h2c = connection(pool)
    h2c.CLEANUP_TASK_LIMIT = 1
    try:
        async with h2c._state_lock:
            closing = asyncio.create_task(first.close())
            for _ in range(10):
                await asyncio.sleep(0)
                if first_id in h2c._closing_tasks:
                    break
            with pytest.raises(RuntimeError, match="cleanup capacity exhausted"):
                await asyncio.wait_for(second.close(), 1)
            assert len(h2c._closing_tasks) == 1
            assert not h2c.is_available()
            assert peer.closed
        await asyncio.wait_for(closing, 1)
    finally:
        await client.close()
    assert not h2c._closing_tasks
