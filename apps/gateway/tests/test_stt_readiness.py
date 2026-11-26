"""Local connection readiness and write-only STT failure recovery; no provider calls."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from copilot_gateway.stt.base import STTResult
from copilot_gateway.stt.resilient import ResilientSTT


class Stream:
    name = "controlled"

    def __init__(self, *, fail: str | None = None, fail_audio_at: int = 1) -> None:
        self.fail = fail
        self.fail_audio_at = fail_audio_at
        self.connecting = asyncio.Event()
        self.allow_connect = asyncio.Event()
        self.allow_connect.set()
        self.reading = asyncio.Event()
        self.drop = asyncio.Event()
        self.reader_cancelled = asyncio.Event()
        self.sending = asyncio.Event()
        self.allow_send = asyncio.Event()
        self.allow_send.set()
        self.closing = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()
        self.closed = asyncio.Event()
        self.calls: list[tuple[str, bytes | None]] = []
        self.audio_attempts = 0
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        self.connecting.set()
        await self.allow_connect.wait()

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_attempts += 1
        self.calls.append(("audio", pcm))
        self.sending.set()
        await self.allow_send.wait()
        if self.fail == "audio" and self.audio_attempts == self.fail_audio_at:
            raise ConnectionError("write failed while reader remains open")

    async def finalize(self) -> None:
        self.calls.append(("finalize", None))
        if self.fail == "finalize":
            raise ConnectionError("finalize write failed")

    async def keepalive(self) -> None:
        if self.fail == "keepalive":
            raise ConnectionError("idle keepalive write failed")

    async def results(self) -> AsyncIterator[STTResult]:
        self.reading.set()
        try:
            await self.drop.wait()
            raise ConnectionError("receive side closed")
            yield STTResult("", False, 0, 0)
        finally:
            self.reader_cancelled.set()

    async def close(self) -> None:
        self.closing.set()
        await self.allow_close.wait()
        self.closed.set()


def supervisor(*streams: Stream) -> tuple[ResilientSTT, list[Stream]]:
    opened: list[Stream] = []

    def factory(_):
        stream = streams[len(opened)]
        opened.append(stream)
        return stream

    async def on_result(_):
        pass

    return (
        ResilientSTT(
            [("controlled", factory)],
            on_result,
            lambda *_: None,
            stall_s=60,
            nudge_s=None,
            keepalive_s=0.01,
        ),
        opened,
    )


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)


@pytest.mark.parametrize("operation", ["audio", "finalize", "keepalive"])
async def test_write_only_failure_reconnects_without_waiting_for_reader_or_stall(operation):
    """A failed write is definitive even if the provider's receive side never closes."""
    first, second = Stream(fail=operation), Stream()
    stt, opened = supervisor(first, second)
    task = asyncio.create_task(stt.run())
    try:
        await asyncio.wait_for(first.reading.wait(), 1)
        if operation == "audio":
            await stt.send(b"a" * 3200, 1000)
        elif operation == "finalize":
            await stt.finalize()
        # Idle keepalive has no active speech/flush watchdog at all. The others have
        # a 60 s watchdog, so this observes supervisor wakeup, not a shorter timer.
        await asyncio.wait_for(second.reading.wait(), 0.75)
        assert opened == [first, second]
        assert first.reader_cancelled.is_set()
        assert first.closed.is_set()
        assert stt.total_failures == 1
        if operation == "audio":
            assert second.calls == [("audio", b"a" * 3200)]
        elif operation == "finalize":
            assert second.calls == [("finalize", None)]
    finally:
        await stop(task)
    assert second.closed.is_set()


async def test_readiness_requires_connected_replayed_socket_and_active_reader():
    stream = Stream()
    stream.allow_connect.clear()
    stream.allow_send.clear()
    stt, opened = supervisor(stream)
    await stt.send(b"a" * 3200, 1000)
    await stt.send(b"b" * 3200, 1100)
    await stt.finalize()
    task = asyncio.create_task(stt.run())
    try:
        await asyncio.wait_for(stream.connecting.wait(), 1)
        assert not stt.connected
        assert not await stt.await_ready(0.02)
        stream.allow_connect.set()
        await asyncio.wait_for(stream.sending.wait(), 1)
        assert not stream.reading.is_set()
        assert not await stt.await_ready(0.02)
        stream.allow_send.set()
        assert await stt.await_ready(1)
        assert stt.connected
        assert stream.reading.is_set()
        assert opened == [stream]
        assert stream.calls == [
            ("audio", b"a" * 3200),
            ("audio", b"b" * 3200),
            ("finalize", None),
        ]
    finally:
        await stop(task)
    assert not stt.connected


async def test_readiness_wait_timeout_and_cancellation_leave_stream_and_audio_owned():
    stream = Stream()
    stream.allow_connect.clear()
    stt, _ = supervisor(stream)
    task = asyncio.create_task(stt.run())
    try:
        await asyncio.wait_for(stream.connecting.wait(), 1)
        assert not await stt.await_ready(0.02)
        waiter = asyncio.create_task(stt.await_ready(1))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not task.done()
        assert not stream.closed.is_set()
        await stt.send(b"a" * 3200, 1000)
        stream.allow_connect.set()
        assert await stt.await_ready(1)
        assert stream.calls == [("audio", b"a" * 3200)]
    finally:
        await stop(task)


async def test_prepared_socket_becomes_ready_only_when_adopted_without_reconnect():
    stream = Stream()
    stt, opened = supervisor(stream)
    await stt.prepare()
    assert not stt.connected
    assert not await stt.await_ready(0.02)
    task = asyncio.create_task(stt.run())
    try:
        assert await stt.await_ready(1)
        assert opened == [stream]
        assert stream.connects == 1
    finally:
        await stop(task)


async def test_stop_clears_readiness_before_slow_close_and_drains_reader():
    stream = Stream()
    stream.allow_close.clear()
    stt, _ = supervisor(stream)
    task = asyncio.create_task(stt.run())
    try:
        assert await stt.await_ready(1)
        task.cancel()
        await asyncio.wait_for(stream.closing.wait(), 1)
        assert not stt.connected
        assert stream.reader_cancelled.is_set()
        assert not await stt.await_ready(0.02)
    finally:
        stream.allow_close.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert stream.closed.is_set()


async def test_failed_replay_keeps_unknown_accepted_prefix_and_never_reports_ready():
    """Retry unfinalized PCM once on the replacement; remote acceptance is unknowable."""
    first = Stream(fail="audio", fail_audio_at=2)
    second = Stream()
    second.allow_connect.clear()
    stt, opened = supervisor(first, second)
    chunks = [b"a" * 3200, b"b" * 3200, b"c" * 3200]
    await stt.send(chunks[0], 1000)
    await stt.send(chunks[1], 1100)
    await stt.finalize()
    task = asyncio.create_task(stt.run())
    try:
        await asyncio.wait_for(second.connecting.wait(), 1)
        assert not first.reading.is_set()
        assert not await stt.await_ready(0.02)
        await stt.send(chunks[2], 1200)
        assert first.calls == [("audio", chunks[0]), ("audio", chunks[1])]
        assert first.closed.is_set()
        second.allow_connect.set()
        assert await stt.await_ready(1)
        assert second.calls == [("audio", part) for part in chunks] + [("finalize", None)]
        assert [pcm for _, pcm in stt.pending] == chunks
        assert opened == [first, second]
    finally:
        await stop(task)


async def test_failed_live_send_buffers_new_audio_until_single_replacement_is_ready():
    first, second = Stream(fail="audio"), Stream()
    second.allow_connect.clear()
    stt, opened = supervisor(first, second)
    task = asyncio.create_task(stt.run())
    chunks = [b"a" * 3200, b"b" * 3200, b"c" * 3200]
    try:
        assert await stt.await_ready(1)
        await stt.send(chunks[0], 1000)
        assert not stt.connected
        # No scheduler turn between the failed send and this subsequent audio: it must
        # be retained, not attempted again on the broken connection.
        await stt.send(chunks[1], 1100)
        await asyncio.wait_for(second.connecting.wait(), 1)
        await stt.send(chunks[2], 1200)
        await stt.finalize()
        assert first.calls == [("audio", chunks[0])]
        second.allow_connect.set()
        assert await stt.await_ready(1)
        assert second.calls == [("audio", part) for part in chunks] + [("finalize", None)]
        assert opened == [first, second]
    finally:
        await stop(task)
    assert first.closed.is_set()
    assert second.closed.is_set()


@pytest.mark.parametrize("timeout_s", [0, -1, float("inf"), float("nan")])
async def test_readiness_rejects_unbounded_or_invalid_wait(timeout_s):
    stt, opened = supervisor(Stream())
    with pytest.raises(ValueError, match="finite and positive"):
        await stt.await_ready(timeout_s)
    assert opened == []


async def test_readiness_clears_during_reader_failure_and_next_handshake():
    first, second = Stream(), Stream()
    second.allow_connect.clear()
    stt, _ = supervisor(first, second)
    task = asyncio.create_task(stt.run())
    try:
        assert await stt.await_ready(1)
        first.drop.set()
        await asyncio.wait_for(second.connecting.wait(), 1)
        assert not stt.connected
        assert not await stt.await_ready(0.02)
        second.allow_connect.set()
        assert await stt.await_ready(1)
        assert second.reading.is_set()
    finally:
        await stop(task)


async def test_write_failures_use_existing_two_failure_provider_switch():
    first, second, fallback = Stream(fail="keepalive"), Stream(fail="keepalive"), Stream()
    opened: list[Stream] = []

    def primary_factory(_):
        stream = (first, second)[len(opened)]
        opened.append(stream)
        return stream

    def fallback_factory(_):
        opened.append(fallback)
        return fallback

    async def on_result(_):
        pass

    stt = ResilientSTT(
        [("primary", primary_factory), ("fallback", fallback_factory)],
        on_result,
        lambda *_: None,
        stall_s=60,
        keepalive_s=0.01,
    )
    task = asyncio.create_task(stt.run())
    try:
        await asyncio.wait_for(fallback.reading.wait(), 1)
        assert stt.connected
        assert opened == [first, second, fallback]
        assert stt.provider == "fallback"
        assert stt.total_failures == 2
        assert stt.switches == 1
        assert first.reader_cancelled.is_set()
        assert second.reader_cancelled.is_set()
    finally:
        await stop(task)
    assert all(stream.closed.is_set() for stream in opened)
