"""The replay tool end to end against an in-process gateway (scripted STT and Claude)."""

import asyncio
import io
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from copilot_evals import replay as rp
from copilot_evals.vad import SAMPLE_RATE, WINDOW, Segment, segment, silero_probabilities

from copilot_core.jobs import MemoryJobQueue
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore
from copilot_gateway.stt import STTResult
from copilot_gateway.testing import GatewayServer, ScriptedProvider

QUESTIONS = ["How did you scale the ledger?", "Tell me about a time you failed?"]


class ScriptSTT:
    """Each finalize() turns the audio since the last one into the next scripted question."""

    name = "script"
    index = 0

    def __init__(self) -> None:
        self.queue: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self.ms = 0
        self.start = 0

    async def connect(self) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.ms += len(pcm) // 32
        self.queue.put_nowait(STTResult("...", False, self.start, self.ms))

    async def finalize(self) -> None:
        text = QUESTIONS[ScriptSTT.index % len(QUESTIONS)]
        ScriptSTT.index += 1
        self.queue.put_nowait(STTResult(text, True, self.start, self.ms, flushed=True))
        self.start = self.ms

    async def keepalive(self) -> None:
        return None

    async def results(self) -> AsyncIterator[STTResult]:
        while (item := await self.queue.get()) is not None:
            yield item

    async def close(self) -> None:
        self.queue.put_nowait(None)


def services() -> Services:
    from fakeredis import FakeAsyncValkey

    overrides: dict[str, Any] = {"env": "test", "llm_providers": ["anthropic"], "embedder": "none"}
    settings = Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
    uid = "0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b"
    from uuid import UUID

    return Services(
        settings=settings,
        store=GatewayStore(FakeAsyncValkey(), settings),
        jobs=MemoryJobQueue(),
        router=build_router(settings, [ScriptedProvider("anthropic")]),
        verifier=StaticVerifier({"tok": Principal(UUID(uid), "Alex", None)}),
        stt_providers=[("script", ScriptSTT)],
    )


def two_questions() -> tuple[bytes, list[Segment]]:
    seconds = 6
    pcm = (np.random.default_rng(1).normal(0, 0.1, SAMPLE_RATE * seconds) * 32767).astype("<i2")
    segs = [
        Segment(start_edge=8000, start_decided=9600, end_edge=24000, end_decided=27200),
        Segment(start_edge=48000, start_decided=49600, end_edge=64000, end_decided=67200),
    ]
    return pcm.tobytes(), segs


def test_schedule_sends_only_speech_with_flags() -> None:
    pcm, segs = two_questions()
    sends = rp.schedule(pcm, segs, preroll_ms=200)
    starts = [s for s in sends if s.flags & 0b01]
    ends = [s for s in sends if s.flags & 0b10]
    assert len(starts) == 2
    assert len(ends) == 2
    assert all(s.audio == b"" for s in ends)
    # Pre-roll: audio from 200 ms before the speech edge, sent when VAD decides.
    assert starts[0].capture_ms == (8000 - 3200) // 16
    assert starts[0].at_sample == 9600
    sent = sum(len(s.audio) for s in sends) // 2
    assert sent == (27200 - 4800) + (67200 - 44800)
    assert all(len(s.audio) <= 64 * 1024 - 10 for s in sends)
    assert [s.at_sample for s in sends] == sorted(s.at_sample for s in sends)


def test_segment_state_machine() -> None:
    probs = [0.0] * 10 + [0.9] * 30 + [0.1] * 3 + [0.9] * 10 + [0.0] * 10 + [0.9] * 2 + [0.0] * 10
    segs = segment(probs, min_silence_ms=200, min_speech_ms=120)
    assert len(segs) == 1  # a 3-window dip does not end speech; the 64 ms blip is dropped
    seg = segs[0]
    assert seg.start_edge == 10 * WINDOW
    assert seg.end_edge == 53 * WINDOW
    assert seg.hangover_ms == 224  # 7 windows of 32 ms reach 200 ms


def test_silero_runs_on_silence() -> None:
    probs = silero_probabilities(b"\0" * SAMPLE_RATE * 2)  # one second of pcm16
    assert len(probs) == SAMPLE_RATE // WINDOW
    assert max(probs) < 0.5


def test_load_wav_resamples_and_mixes(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.zeros(8000 * 2, "<i2").tobytes())
    path = tmp_path / "x.wav"
    path.write_bytes(buf.getvalue())
    pcm = rp.load_wav(path)
    assert abs(len(pcm) // 2 - SAMPLE_RATE) < 50


@pytest.mark.parametrize("drop", [None, 2.0])
async def test_replay_reports_first_word_latency(drop: float | None) -> None:
    ScriptSTT.index = 0
    pcm, segs = two_questions()
    async with GatewayServer(services()) as gw:
        r = rp.Replay(
            gw.url, "tok", pcm, rp.schedule(pcm, segs), segs, tail_s=1.5, drop_at_s=drop, quiet=True
        )
        await r.run()
    report: dict[str, Any] = rp.summarize(r)
    s = report["summary"]
    assert s["questions"] == 2
    assert [q["text"] for q in report["questions"]] == QUESTIONS
    assert s["first_word"]["n"] == 2
    for q in report["questions"]:
        # From the true end of speech: VAD hangover (200 ms) + timers + answer.
        assert 0.2 < q["first_word_s"] < 2.0
        assert q["vad_hangover_ms"] == 200
        assert q["server_first_delta_ms"] is not None
    assert s["stages"]["turn_end_ms"]["n"] == 2
    assert r.closed.result() == 1000
    if drop:
        assert s["reconnects"] == 1
    seqs = [int(m["seq"]) for _, m in r.events]
    assert seqs == list(range(1, len(seqs) + 1))
    rp.print_report(report)
