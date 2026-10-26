"""Deterministic stand-ins for STT and Claude, used by the tests and by offline runs.

`TextSTT` treats the PCM bytes it receives as UTF-8 text, so a test can "speak" words: each
chunk extends the interim transcript and finalize() turns it into a flushed final. Failures are
scripted through a shared `TextSTTControl`, which lives across reconnects.

`ScriptedProvider` answers detector, fast, deep and summary requests from simple rules, with
optional delays and failures per call.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from copilot_core.llm import LLMChunk, LLMDone, LLMRequest, LLMUsage
from copilot_gateway.stt import STTResult


@dataclass
class TextSTTControl:
    connects: int = 0
    connect_failures: int = 0  # the next N connects fail
    stall: bool = False  # swallow audio, emit nothing
    drop_after_chunks: int | None = None  # close the connection after N audio chunks
    final_suffix: str = ""  # appended to finals only: an interim that missed the last words
    # A real STT's final comes back over the network some time after the speech-end frame.
    # Emitting it inside finalize() instead makes the turn detector's view at t0 depend on
    # how the event loop batches socket reads: on Linux the STT reader drains the interim and
    # the final before the turn task first runs, on Windows it does not.
    final_delay_s: float = 0.02
    sent_text: list[str] = field(default_factory=list[str])
    names: list[str] = field(default_factory=list[str])
    keyterms: list[list[str]] = field(default_factory=list[list[str]])  # per connect


class TextSTT:
    def __init__(self, control: TextSTTControl, name: str = "text") -> None:
        self.control = control
        self.name = name
        self.queue: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self.words: list[str] = []
        self.ms = 0
        self.seg_start = 0
        self.chunks = 0
        self.pending: list[tuple[asyncio.TimerHandle, STTResult]] = []

    async def connect(self) -> None:
        self.control.connects += 1
        self.control.names.append(self.name)
        if self.control.connect_failures > 0:
            self.control.connect_failures -= 1
            raise ConnectionError("scripted connect failure")

    async def send_audio(self, pcm: bytes) -> None:
        text = pcm.decode("utf-8", errors="ignore").strip("\0 ")
        self.ms += len(pcm) // 32
        self.chunks += 1
        if (
            self.control.drop_after_chunks is not None
            and self.chunks > self.control.drop_after_chunks
        ):
            self.control.drop_after_chunks = None
            self.queue.put_nowait(None)
            return
        if self.control.stall:
            return
        if text:
            self.control.sent_text.append(text)
            self.words.append(text)
        self.queue.put_nowait(STTResult(" ".join(self.words), False, self.seg_start, self.ms))

    async def finalize(self) -> None:
        if self.control.stall:
            return
        text = " ".join(self.words) + (self.control.final_suffix if self.words else "")
        self.words = []
        result = STTResult(text, True, self.seg_start, self.ms, flushed=True)
        self.seg_start = self.ms
        if self.control.final_delay_s <= 0:
            self.queue.put_nowait(result)
            return

        def deliver() -> None:
            self.pending = [p for p in self.pending if p[1] is not result]
            self.queue.put_nowait(result)

        handle = asyncio.get_running_loop().call_later(self.control.final_delay_s, deliver)
        self.pending.append((handle, result))

    async def keepalive(self) -> None:
        return None

    async def results(self) -> AsyncIterator[STTResult]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        # Like a real STT closing its stream: finals still in flight arrive first.
        for handle, result in self.pending:
            handle.cancel()
            self.queue.put_nowait(result)
        self.pending = []
        self.queue.put_nowait(None)


def text_frame_audio(text: str) -> bytes:
    """Encode words as 'audio' for TextSTT: UTF-8, padded to whole 100 ms pcm16 frames."""
    data = (" " + text).encode()
    size = max(3200, -(-len(data) // 3200) * 3200)
    return data.ljust(size, b"\0")


@dataclass
class Reply:
    chunks: list[str]
    first_delay_s: float = 0.0
    chunk_delay_s: float = 0.0
    error: BaseException | None = None
    stop_reason: str = "end_turn"


def default_reply(job: str, req: LLMRequest) -> Reply:
    content = req.content if isinstance(req.content, str) else json.dumps(list(req.content))
    if job == "detector":
        utterance = content.split("<utterance>")[-1].split("</utterance>")[0].strip()
        is_q = utterance.rstrip().endswith("?")
        lower = utterance.lower()
        qtype = (
            "behavioral"
            if "tell me about a time" in lower
            else "system_design"
            if "design" in lower
            else "technical"
            if "how" in lower or "what" in lower
            else "other"
        )
        body = {"answer": qtype if is_q else "none"}
        return Reply([json.dumps(body)])
    if job == "summary":
        lines = content.split("<new_lines>")[-1].split("</new_lines>")[0].strip().splitlines()
        return Reply([f"- Earlier: {len(lines)} lines, from {lines[0][:40] if lines else ''}"])
    if job == "fast":
        return Reply(
            ["- Led the payments migration [n:3f2a]\n", "- Cut p95 latency 40% [n:3f2a]\n"]
        )
    return Reply(
        ["Situation: the ledger was slow. ", "Action: I moved it. ", "Result: 40% faster."]
    )


def job_of(req: LLMRequest) -> str:
    if req.json_schema is not None:
        return "detector"
    system = req.system[0].text if req.system else ""
    if system.startswith("You keep the running summary"):
        return "summary"
    return "fast" if "talking" in system else "deep"


class ScriptedProvider:
    def __init__(
        self,
        name: str = "scripted",
        reply: Callable[[str, LLMRequest], Reply] = default_reply,
    ) -> None:
        self.name = name
        self.reply = reply
        self.calls: list[tuple[str, str]] = []  # (job, model)
        self.requests: list[tuple[str, LLMRequest]] = []  # (job, request)
        self.prewarms: list[tuple[str, LLMRequest]] = []  # (model, request)
        self.cancelled = 0

    async def prewarm(self, model: str, req: LLMRequest, params: Mapping[str, Any]) -> LLMDone:
        self.prewarms.append((model, req))
        return LLMDone(LLMUsage(1000, 0, 0), "max_tokens", self.name, model)

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        job = job_of(req)
        self.calls.append((job, model))
        self.requests.append((job, req))
        reply = self.reply(job, req)
        try:
            if reply.first_delay_s:
                await asyncio.sleep(reply.first_delay_s)
            if reply.error is not None:
                raise reply.error
            out = 0
            for i, chunk in enumerate(reply.chunks):
                if i and reply.chunk_delay_s:
                    await asyncio.sleep(reply.chunk_delay_s)
                out += max(1, len(chunk) // 4)
                yield chunk
            yield LLMDone(LLMUsage(1000, 900, out), reply.stop_reason, self.name, model)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


# A real gateway server and a protocol client, for tests and local runs.


class GatewayServer:
    """Runs the app under uvicorn on a free localhost port, inside the current event loop."""

    def __init__(self, services: Any) -> None:
        import uvicorn

        from copilot_gateway.main import create_app, uvicorn_options

        self.services = services
        options = uvicorn_options(services.settings)
        options.pop("loop", None)
        config = uvicorn.Config(
            create_app(services),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            **options,  # pyright: ignore[reportArgumentType]
        )
        self.server = uvicorn.Server(config)
        self.task: asyncio.Task[None] | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/session"

    @property
    def manager(self) -> Any:
        return self.server.config.app.state.manager  # pyright: ignore

    async def __aenter__(self) -> "GatewayServer":
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():
                self.task.result()
            await asyncio.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.server.should_exit = True
        if self.task is not None:
            await asyncio.wait_for(self.task, 10)


class ProtocolClient:
    """Speaks the client side of the protocol: JSON messages, audio frames, seq tracking."""

    def __init__(self, url: str, token: str = "token-a") -> None:
        self.url = url
        self.token = token
        self.ws: Any = None
        self.messages: list[dict[str, Any]] = []
        self.last_seq = 0
        self._session_id: str | None = None
        self.frame_seq = {0: 0, 1: 0}
        self.sent_frames: list[bytes] = []
        self.close_code: int | None = None
        self._reader: asyncio.Task[None] | None = None
        self._new = asyncio.Event()

    @property
    def session_id(self) -> str:
        assert self._session_id is not None, "no session yet"
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session_id = value

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.ws = await connect(self.url, additional_headers=headers, max_size=None)
        self.close_code = None
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.messages.append(msg)
                self.last_seq = max(self.last_seq, int(msg.get("seq", 0)))
                self._new.set()
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self.ws.close_code or 1006
            self._new.set()

    async def send(self, message: Mapping[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def start(self, mode: str = "interview", **extra: Any) -> dict[str, Any]:
        await self.connect()
        await self.send(
            {
                "type": "session.start",
                "mode": mode,
                "platform": "zoom",
                "client_version": "test",
                "consent": {"store_transcript": True},
                "audio": {"sample_rate": 16000, "encoding": "pcm16"},
                **extra,
            }
        )
        ready = await self.wait_type("session.ready")
        self.session_id = ready["session_id"]
        return ready

    async def resume(self) -> None:
        await self.connect()
        await self.send(
            {"type": "session.resume", "session_id": self.session_id, "last_seq": self.last_seq}
        )

    async def frame(
        self, channel: int, flags: int, audio: bytes = b"", capture_ms: int = 0
    ) -> bytes:
        from copilot_protocol.audio import encode_frame
        from copilot_protocol.models import AudioFrameHeader

        header = AudioFrameHeader.model_validate(
            {
                "channel": channel,
                "flags": flags,
                "frame_seq": self.frame_seq[channel],
                "capture_ms": capture_ms,
            }
        )
        self.frame_seq[channel] += 1
        data = encode_frame(header, audio)
        self.sent_frames.append(data)
        await self.ws.send(data)
        return data

    async def say(self, text: str, capture_ms: int = 0) -> None:
        """Speech on the them channel: start flag with the words, then a speech-end frame."""
        await self.frame(0, 0b01, text_frame_audio(text), capture_ms)
        await self.frame(0, 0b10, b"", capture_ms + 100)

    async def say_me(self, text: str, capture_ms: int = 0, end_ms: int | None = None) -> None:
        """The user's own speech on the me channel, as a client sends it when session.ready
        said me_audio: start flag with the words, then a speech-end frame."""
        await self.frame(1, 0b01, text_frame_audio(text), capture_ms)
        await self.frame(1, 0b10, b"", end_ms if end_ms is not None else capture_ms + 100)

    async def wait(
        self, pred: Callable[[dict[str, Any]], bool], timeout: float = 5.0, start: int = 0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for m in self.messages[start:]:
                if pred(m):
                    return m
            if self.close_code is not None and self._reader is not None and self._reader.done():
                raise AssertionError(f"socket closed ({self.close_code}) while waiting")
            remaining = deadline - loop.time()
            if remaining <= 0:
                types = [m["type"] for m in self.messages]
                raise AssertionError(f"timed out waiting; got {types}")
            self._new.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), remaining)

    async def wait_type(self, type_: str, timeout: float = 5.0, **match: Any) -> dict[str, Any]:
        """Wait for a message of `type_` whose fields equal `match` (`pass_` means `pass`)."""
        match = {("pass" if k == "pass_" else k): v for k, v in match.items()}
        return await self.wait(
            lambda m: m["type"] == type_ and all(m.get(k) == v for k, v in match.items()), timeout
        )

    async def closed(self, timeout: float = 5.0) -> int:
        if self._reader is not None:
            await asyncio.wait_for(asyncio.shield(self._reader), timeout)
        return self.close_code or 1006

    async def drop(self) -> None:
        """Lose the connection without a close handshake, as a network drop would."""
        self.ws.transport.abort()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    def of_type(self, type_: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["type"] == type_]
