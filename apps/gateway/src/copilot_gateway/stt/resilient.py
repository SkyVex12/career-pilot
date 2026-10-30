"""STT supervisor: reconnect, switch provider, map times back to capture time.

Failure row "STT stream drops" (ARCHITECTURE.md, "Failure modes"): no results for 3 s while
VAD hears speech, or a dropped connection, counts as a failure. The supervisor reconnects and
replays the audio not yet covered by a final; after two consecutive failures on one provider it
switches to the next one in the list.

Before a silent stream counts as stalled, it is nudged: a Finalize asks it for what it has, and
only if nothing comes within `nudge_s` does it reconnect. The stalled stream is closed in the
background, so the new one does not wait for it. On 2026-10-30 Deepgram went silent 6 times in
a 19-minute standup; two of those landed on questions and added 3 to 4 s each.

Only speech is sent to the provider, so its timeline is shorter than the call. Each chunk sent
records (provider ms, capture ms), and result times are mapped back to capture ms.
"""

import asyncio
import bisect
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from copilot_gateway.stt.base import STTFactory, STTResult, STTStream

log = logging.getLogger(__name__)

BYTES_PER_MS = 32  # 16 kHz * 2 bytes
REPLAY_MAX_MS = 10_000
# Rounding in the provider's result times. A reply to the latest Finalize covers at least the
# client's 200 ms pre-roll more than the one before it.
FINALIZE_SLACK_MS = 100


@dataclass(frozen=True)
class TimedResult:
    text: str
    is_final: bool
    flushed: bool
    start_ms: int  # capture ms
    end_ms: int
    provider: str


class StallError(Exception):
    pass


class _Timeline:
    def __init__(self) -> None:
        self.stream_starts: list[int] = []
        self.capture_starts: list[int] = []
        self.stream_ms = 0

    def add(self, capture_ms: int, nbytes: int) -> None:
        self.stream_starts.append(self.stream_ms)
        self.capture_starts.append(capture_ms)
        self.stream_ms += nbytes // BYTES_PER_MS

    def to_capture(self, stream_ms: int) -> int:
        i = bisect.bisect_right(self.stream_starts, stream_ms) - 1
        if i < 0:
            return self.capture_starts[0] if self.capture_starts else 0
        return self.capture_starts[i] + (stream_ms - self.stream_starts[i])


type ResultHook = Callable[[TimedResult], Awaitable[None]]
type StatusHook = Callable[[str, str], None]  # state ("ok", "reconnecting"), message


class ResilientSTT:
    def __init__(
        self,
        providers: Sequence[tuple[str, STTFactory]],
        on_result: ResultHook,
        on_status: StatusHook,
        *,
        stall_s: float = 3.0,
        failures_before_switch: int = 2,
        keepalive_s: float = 5.0,
        keyterms: Sequence[str] = (),
        nudge_s: float | None = 1.0,
        label: str = "",
    ) -> None:
        if not providers:
            raise ValueError("at least one STT provider is needed")
        self.providers = list(providers)
        self.on_result = on_result
        self.on_status = on_status
        self.stall_s = stall_s
        self.failures_before_switch = failures_before_switch
        self.keepalive_s = keepalive_s
        self.keyterms = list(keyterms)
        self.nudge_s = nudge_s
        self.label = label  # the channel, for the logs
        self.nudges = 0
        self.nudge_saves = 0  # nudges a result answered: reconnects avoided
        self.nudged_at: float | None = None
        self.last_result_at = 0.0
        self.stalled_at: float | None = None  # a reconnect after a stall, until its first result
        self._closing: set[asyncio.Task[None]] = set()

        self.index = 0
        self.failures = 0
        self.total_failures = 0
        self.switches = 0
        self.degraded = False
        self.stream: STTStream | None = None
        self.timeline = _Timeline()
        # Audio not yet covered by a final: (capture_ms, pcm), replayed after a reconnect.
        self.pending: deque[tuple[int, bytes]] = deque()
        self.pending_ms = 0
        self.in_speech = False
        self.flush_pending = False
        # The stream's audio end (provider ms) at the last two Finalize requests; see _handle.
        self.finalized_at: tuple[int | None, int | None] = (None, None)
        self.awaiting_since: float | None = None  # first audio sent after the last result
        self.last_audio_at = 0.0
        self.connected_at = 0.0
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()

    @property
    def provider(self) -> str:
        return self.providers[self.index][0]

    # Called from the session's socket reader.

    def speech(self, active: bool) -> None:
        self.in_speech = active

    async def send(self, pcm: bytes, capture_ms: int) -> None:
        if not pcm:
            return
        # Under the lock: a reconnect replays `pending` while holding it, and awaits between
        # chunks, so audio arriving meanwhile must wait rather than change the deque.
        async with self._lock:
            self.pending.append((capture_ms, pcm))
            self.pending_ms += len(pcm) // BYTES_PER_MS
            while self.pending_ms > REPLAY_MAX_MS and len(self.pending) > 1:
                _, old = self.pending.popleft()
                self.pending_ms -= len(old) // BYTES_PER_MS
            stream = self.stream
            if stream is None:
                return  # replayed on connect
            self.timeline.add(capture_ms, len(pcm))
            await self._send(stream, stream.send_audio(pcm))
        self.last_audio_at = time.monotonic()
        if self.awaiting_since is None:
            self.awaiting_since = self.last_audio_at

    async def finalize(self) -> None:
        self.flush_pending = True
        self.finalized_at = (self.finalized_at[1], self.timeline.stream_ms)
        async with self._lock:
            if self.stream is not None:
                await self._send(self.stream, self.stream.finalize())

    async def _send(self, stream: STTStream, op: Awaitable[None]) -> None:
        try:
            await op
        except Exception as e:  # the reader sees the drop and the run loop reconnects
            log.info("%s send failed: %s", stream.name, e)

    # The supervisor task.

    async def run(self) -> None:
        try:
            while True:
                await self._run_once()
        finally:
            stream, self.stream = self.stream, None
            if stream is not None:
                await _close_quietly(stream)
            for task in list(self._closing):
                task.cancel()

    async def _run_once(self) -> None:
        name, factory = self.providers[self.index]
        stream = factory(self.keyterms)
        try:
            await stream.connect()
        except Exception as e:
            await self._failed(f"{name} connect failed: {e}")
            return
        async with self._lock:
            self.timeline = _Timeline()
            self.finalized_at = (None, None)  # a new stream answers only the replayed Finalize
            for capture_ms, pcm in self.pending:  # replay audio not covered by a final
                self.timeline.add(capture_ms, len(pcm))
                await self._send(stream, stream.send_audio(pcm))
            if self.flush_pending:
                await self._send(stream, stream.finalize())
            self.stream = stream
            self.connected_at = time.monotonic()
            self.awaiting_since = self.connected_at if self.pending else None
        tasks = [
            asyncio.create_task(self._read(stream)),
            asyncio.create_task(self._watchdog()),
            asyncio.create_task(self._keepalive(stream)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            reason = "connection ended"
            for task in done:
                if (exc := task.exception()) is not None:
                    reason = f"{type(exc).__name__}: {exc}"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with self._lock:
                self.stream = None
            # In the background: a stalled stream can take its full 2 s to close, and the
            # new connection should not wait for it.
            closing = asyncio.create_task(_close_quietly(stream))
            self._closing.add(closing)
            closing.add_done_callback(self._closing.discard)
        await self._failed(f"{name}: {reason}")

    async def _failed(self, reason: str) -> None:
        self.failures += 1
        self.total_failures += 1
        log.warning(
            "stt failure %d on %s%s: %s (last result %.1f s ago, %d ms of audio to replay)",
            self.failures, self.provider, f" ({self.label})" if self.label else "", reason,
            time.monotonic() - self.last_result_at if self.last_result_at else -1.0,
            self.pending_ms,
        )  # fmt: skip
        self.stalled_at = time.monotonic()
        self.degraded = True
        self.on_status("reconnecting", "Reconnecting transcription")
        if self.failures >= self.failures_before_switch and len(self.providers) > 1:
            self.index = (self.index + 1) % len(self.providers)
            self.failures = 0
            self.switches += 1
            log.warning("switching stt provider to %s", self.provider)
        await asyncio.sleep(min(0.1 * self.failures, 1.0))

    async def _read(self, stream: STTStream) -> None:
        async for result in stream.results():
            await self._handle(stream, result)

    async def _handle(self, stream: STTStream, r: STTResult) -> None:
        now = time.monotonic()
        self.awaiting_since = None
        self.last_result_at = now
        self.failures = 0
        if self.nudged_at is not None:
            self.nudged_at = None
            self.nudge_saves += 1
            log.info(
                "stt %s%s answered a nudge", stream.name, f" ({self.label})" if self.label else ""
            )
        if self.stalled_at is not None:
            log.info(
                "stt %s%s: first result %d ms after the failure",
                stream.name, f" ({self.label})" if self.label else "",
                int((now - self.stalled_at) * 1000),
            )  # fmt: skip
            self.stalled_at = None
        if self.degraded:
            self.degraded = False
            self.on_status("ok", "Transcription back")
        if r.is_final:
            self._drop_covered(r.end_ms)
        # Only a reply to the latest speech end's Finalize flushes a turn: not one to an
        # earlier Finalize, nor a nudge's when no speech end is waiting for its final.
        flushed = r.flushed and self.flush_pending and not self._answers_earlier_finalize(r)
        if flushed:
            self.flush_pending = False
        timed = TimedResult(
            text=r.text,
            is_final=r.is_final,
            flushed=flushed,
            start_ms=self.timeline.to_capture(r.start_ms),
            end_ms=self.timeline.to_capture(r.end_ms),
            provider=stream.name,
        )
        await self.on_result(timed)

    def _answers_earlier_finalize(self, r: STTResult) -> bool:
        """A Finalize reply covers only the audio sent before that Finalize. When a question is
        split by a pause, two speech ends send two Finalize messages; the first reply can
        arrive after the second speech end, and taken as the latest turn's flush it made the
        detector decide on the first half alone (benchmark, October 2026: 7 of 106 questions,
        most of the first-word p95). A reply that ends no later than the audio at the Finalize
        before the latest one answers that earlier Finalize."""
        earlier = self.finalized_at[0]
        return earlier is not None and r.end_ms <= earlier + FINALIZE_SLACK_MS

    def _drop_covered(self, stream_end_ms: int) -> None:
        """Forget pending audio a final has covered, so a reconnect does not resend it."""
        covered_capture = self.timeline.to_capture(stream_end_ms)
        while self.pending:
            capture_ms, pcm = self.pending[0]
            if capture_ms + len(pcm) // BYTES_PER_MS > covered_capture:
                break
            self.pending.popleft()
            self.pending_ms -= len(pcm) // BYTES_PER_MS

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(0.25, self.stall_s / 4))
            since = self.awaiting_since
            if since is None or not (self.in_speech or self.flush_pending):
                self.nudged_at = None
                continue
            now = time.monotonic()
            if now - max(since, self.connected_at) <= self.stall_s:
                continue
            if self.nudge_s is not None and self.nudged_at is None:
                await self._nudge()
                continue
            if self.nudged_at is not None and now - self.nudged_at <= (self.nudge_s or 0):
                continue
            self.nudged_at = None
            raise StallError(f"no results for {self.stall_s:g} s while speech is active")

    async def _nudge(self) -> None:
        """Ask a silent stream for what it has (a Finalize) before calling it stalled. Its
        reply is not a turn's flush (`_handle`): it counts as one more Finalize, so the reply
        to the real one is still told apart (`_answers_earlier_finalize`)."""
        self.nudged_at = time.monotonic()
        self.nudges += 1
        self.finalized_at = (self.finalized_at[1], self.timeline.stream_ms)
        async with self._lock:
            if self.stream is not None:
                await self._send(self.stream, self.stream.finalize())

    async def _keepalive(self, stream: STTStream) -> None:
        while True:
            await asyncio.sleep(self.keepalive_s)
            if time.monotonic() - self.last_audio_at >= self.keepalive_s:
                async with self._lock:
                    await self._send(stream, stream.keepalive())


async def _close_quietly(stream: STTStream) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stream.close(), 2)
