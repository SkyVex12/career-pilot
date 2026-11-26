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
    """Each finalize() turns the audio since the last one into the next scripted question.

    The final arrives a little after the speech-end frame, as from a real STT; emitted inside
    finalize() it would race the turn detector (see copilot_gateway.testing.TextSTTControl).
    """

    final_delay_s = 0.02

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
        final = STTResult(text, True, self.start, self.ms, flushed=True)
        asyncio.get_running_loop().call_later(self.final_delay_s, self.queue.put_nowait, final)
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

    # Two separate questions back to back, with no mic audio: the gateway would join the second
    # to the first (continuation_gap_ms), which this latency test is not about.
    overrides: dict[str, Any] = {
        "env": "test", "llm_providers": ["anthropic"], "embedder": "none",
        "continuation_gap_ms": 0,
    }  # fmt: skip
    settings = Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
    uid = "0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b"
    from uuid import UUID

    return Services(
        settings=settings,
        store=GatewayStore(FakeAsyncValkey(), settings),
        jobs=MemoryJobQueue(),
        router=build_router(settings, [ScriptedProvider("anthropic")]),
        verifier=StaticVerifier({"tok": Principal(UUID(uid), "Alex", None)}),
        stt_providers=[("script", lambda _: ScriptSTT())],
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
    # QID messages contain no source span: once the second audio segment starts,
    # receipt order alone cannot distinguish its QID from a delayed replacement.
    assert s["first_word"]["n"] == 1
    assert s["receipt_stages_s"]["fast"]["first_text"]["n"] == 1
    assert s["receipt_stages_s"]["fast"]["first_text"]["missing"] == 1
    assert s["receipt_stages_s"]["fast"]["completion"]["n"] == 1
    for index, q in enumerate(report["questions"]):
        if index == 0:
            assert 0.2 < q["first_word_s"] < 2.0
            assert q["vad_hangover_ms"] == 200
        else:
            assert q["first_word_s"] is None
            assert q["audio_attribution"] == "ambiguous_segments"
        assert q["server_first_delta_ms"] is not None
    assert s["stages"]["turn_end_ms"]["n"] == 2
    assert r.closed.result() == 1000
    if drop:
        assert s["reconnects"] == 1
    seqs = [int(m["seq"]) for _, m in r.events]
    assert seqs == list(range(1, len(seqs) + 1))
    rp.print_report(report)


def measured_replay() -> rp.Replay:
    pcm, segments = two_questions()
    replay = rp.Replay(
        "ws://unused", "unused", pcm, rp.schedule(pcm, segments), segments, quiet=True
    )
    replay.t0 = 100.0
    return replay


def send_edge(replay: rp.Replay, index: int, at: float, flag: int) -> None:
    send = next(item for item in replay.sends if item.segment == index and item.flags & flag)
    replay._record_send(send, at)


def detected(replay: rp.Replay, qid: str, at: float, *, speculative: bool = False) -> None:
    replay._on_message(
        at,
        {
            "type": "question.detected",
            "question_id": qid,
            "text": "A question?",
            "question_type": "technical",
            "confidence": 0.9,
            "speculative": speculative,
        },
    )


def delta(replay: rp.Replay, qid: str, at: float, text: str, pass_: str = "fast") -> None:
    replay._on_message(
        at, {"type": "answer.delta", "question_id": qid, "pass": pass_, "text": text}
    )


def done(replay: rp.Replay, qid: str, at: float, pass_: str = "fast") -> None:
    replay._on_message(at, {"type": "answer.done", "question_id": qid, "pass": pass_})


async def test_early_question_links_started_segment_before_end_flag() -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    detected(replay, "early", 100.9, speculative=True)
    question = replay.questions["early"]
    assert question.segment == 0
    assert question.audio_attribution == "single_started_segment_heuristic"
    assert question.speech_end_at == 101.5
    assert question.end_flag_at is None
    assert not question.forced
    delta(replay, "early", 101.2, "An early answer. ")
    send_edge(replay, 0, 101.7, 0b10)
    detected(replay, "early", 101.8)
    assert question.end_flag_at == 101.7
    assert question.receipt_s(question.first_text_at, "fast") == -0.3
    assert question.segment == 0  # Confirmation cannot move the original audio anchor.


@pytest.mark.parametrize("previous_identified", [False, True])
async def test_subsequent_segment_never_uses_previous_end_flag(previous_identified: bool) -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    if previous_identified:
        detected(replay, "first", 101.0)
    send_edge(replay, 0, 101.7, 0b10)
    send_edge(replay, 1, 103.1, 0b01)
    detected(replay, "second", 103.2)
    question = replay.questions["second"]
    assert question.segment is None
    assert question.speech_end_at is None
    assert question.audio_attribution == "ambiguous_segments"


async def test_late_detection_after_multiple_unclaimed_segments_stays_unknown() -> None:
    replay = measured_replay()
    for index, start, end in ((0, 100.6, 101.7), (1, 103.1, 104.2)):
        send_edge(replay, index, start, 0b01)
        send_edge(replay, index, end, 0b10)
    detected(replay, "ambiguous", 104.5)
    delta(replay, "ambiguous", 104.6, "Received text. ")
    done(replay, "ambiguous", 104.7)
    report = rp.summarize(replay)
    assert report["questions"][0]["audio_attribution"] == "ambiguous_segments"
    assert report["summary"]["receipt_stages_s"]["fast"]["first_text"] == {
        "p50": None,
        "p90": None,
        "p95": None,
        "n": 0,
        "missing": 1,
        "total": 1,
    }


async def test_manual_force_is_not_assigned_an_active_audio_segment() -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    replay._on_message(
        100.9,
        {
            "type": "question.detected",
            "question_id": "manual",
            "text": "Typed question?",
            "question_type": "technical",
            "confidence": 1.0,
            "speculative": False,
        },
    )
    assert replay.questions["manual"].forced
    assert replay.questions["manual"].speech_end_at is None
    assert replay.questions["manual"].audio_attribution == "forced_without_audio_attribution"


async def test_per_pass_receipts_skip_whitespace_and_filler_without_redefining_legacy() -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    detected(replay, "q", 101.0)
    delta(replay, "q", 101.6, " \n")
    delta(replay, "q", 101.8, "Sure. ")
    delta(replay, "q", 101.9, "Latency is 3.")
    assert not replay.questions["q"].first_sentence_at
    delta(replay, "q", 102.0, "5 seconds.")
    assert not replay.questions["q"].first_sentence_at
    delta(replay, "q", 102.2, " Next")
    delta(replay, "q", 102.4, "Detailed answer.", "deep")
    done(replay, "q", 102.6)
    done(replay, "q", 102.8, "deep")
    report = rp.summarize(replay)
    row = report["questions"][0]
    assert row["first_word_s"] == 0.1  # Raw-delta compatibility, not the new text endpoint.
    assert row["receipt_latency_s"] == {
        "fast": {"first_text": 0.3, "first_sentence_boundary": 0.7, "completion": 1.1},
        "deep": {"first_text": 0.9, "first_sentence_boundary": 1.3, "completion": 1.3},
    }
    assert row["deep_done_s"] == 1.3
    assert report["summary"]["first_word"]["p90_s"] == 0.1


async def test_error_partial_and_cancelled_questions_remain_in_receipt_denominators() -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    detected(replay, "complete", 101.0)
    delta(replay, "complete", 101.8, "Complete sentence. ")
    done(replay, "complete", 102.0)
    # Multiple QIDs can belong to the same sole audio segment.
    detected(replay, "partial", 102.1)
    delta(replay, "partial", 102.2, "An unfinished clause")
    replay._on_message(
        102.4,
        {"type": "answer.error", "question_id": "partial", "pass": "fast", "code": "timeout"},
    )
    detected(replay, "cancelled", 102.5, speculative=True)
    replay._on_message(102.6, {"type": "question.cancelled", "question_id": "cancelled"})
    report = rp.summarize(replay)
    summary = report["summary"]
    assert summary["questions"] == 2  # Historical table's denominator stays compatible.
    assert summary["observed_question_ids"] == 3
    assert summary["receipt_stages_s"]["fast"]["first_text"] == {
        "p50": 0.5,
        "p90": 0.66,
        "p95": 0.68,
        "n": 2,
        "missing": 1,
        "total": 3,
    }
    assert summary["receipt_stages_s"]["fast"]["first_sentence_boundary"]["missing"] == 2
    assert summary["receipt_stages_s"]["fast"]["completion"]["missing"] == 2
    assert summary["receipt_stages_s"]["deep"]["completion"]["missing"] == 3
    partial = next(row for row in report["questions"] if row["question_id"] == "partial")
    assert partial["receipt_latency_s"]["fast"]["completion"] is None
    assert partial["receipt_latency_s"]["fast"]["first_sentence_boundary"] is None
    assert partial["errors"] == ["fast:timeout"]
    rp.print_report(report)


@pytest.mark.parametrize(
    ("chunks", "boundary_on_done"),
    [
        (["Sure.", " ", "A useful boundary.", " "], False),
        (["A complete sentence", "."], True),
        (["A final fragment without punctuation"], False),
    ],
)
async def test_sentence_boundary_across_chunks_and_final_receipt(
    chunks: list[str], boundary_on_done: bool
) -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    detected(replay, "q", 101.0)
    for index, chunk in enumerate(chunks):
        delta(replay, "q", 102.0 + index, chunk)
    before_done = replay.questions["q"].first_sentence_at.get("fast")
    done(replay, "q", 110.0)
    after_done = replay.questions["q"].first_sentence_at.get("fast")
    if boundary_on_done:
        assert before_done is None
        assert after_done == 110.0
    elif len(chunks) > 1:
        assert before_done == after_done == 105.0
    else:
        assert before_done is after_done is None


async def test_replacement_qid_after_next_segment_starts_is_not_assigned_to_it() -> None:
    replay = measured_replay()
    send_edge(replay, 0, 100.6, 0b01)
    detected(replay, "original", 101.0, speculative=True)
    replay.questions["original"].text = "A partial first question"
    send_edge(replay, 0, 101.7, 0b10)
    replay._on_message(101.8, {"type": "question.cancelled", "question_id": "original"})
    send_edge(replay, 1, 103.1, 0b01)
    # This new QID is actually a delayed revision of the FIRST utterance. The wire
    # does not expose that lineage, so an unclaimed second segment is not proof.
    detected(replay, "replacement", 103.2)
    replay.questions["replacement"].text = "A complete revision of the first question?"
    delta(replay, "replacement", 103.3, "An answer to the first utterance. ")
    done(replay, "replacement", 103.4)
    question = replay.questions["replacement"]
    assert replay.questions["original"].segment == 0
    assert question.segment is None
    assert question.speech_end_at is None
    assert question.audio_attribution == "ambiguous_segments"
    row = next(
        item for item in rp.summarize(replay)["questions"] if item["question_id"] == "replacement"
    )
    assert row["receipt_latency_s"]["fast"] == {
        "first_text": None,
        "first_sentence_boundary": None,
        "completion": None,
    }


@pytest.mark.parametrize("frame_ms", [20, 40, 100])
def test_frame_size_preserves_pcm_burst_edges_and_capture_clock(frame_ms: int) -> None:
    pcm, segments = two_questions()
    baseline = rp.schedule(pcm, segments)
    candidate = rp.schedule(pcm, segments, frame_ms=frame_ms)
    assert b"".join(send.audio for send in candidate) == b"".join(send.audio for send in baseline)
    assert [(s.segment, s.at_sample, s.capture_ms) for s in candidate if s.flags] == [
        (s.segment, s.at_sample, s.capture_ms) for s in baseline if s.flags
    ]
    assert [s.at_sample for s in candidate] == sorted(s.at_sample for s in candidate)
    for i, segment_ in enumerate(segments):
        initial = [s for s in candidate if s.segment == i and s.at_sample == segment_.start_decided]
        assert initial == [
            s for s in baseline if s.segment == i and s.at_sample == segment_.start_decided
        ]
        streamed = [s for s in candidate if s.segment == i and s.at_sample > segment_.start_decided]
        assert all(len(s.audio) <= frame_ms * 32 for s in streamed)
        assert all(
            s.capture_ms * 16 + len(s.audio) // 2 == s.at_sample for s in streamed if s.audio
        )
    if frame_ms == 100:
        assert candidate == baseline


@pytest.mark.parametrize("invalid", [0, -1, 41, 1000, True, 40.0])
def test_frame_size_invalid_rejected(invalid: Any) -> None:
    pcm, segments = two_questions()
    with pytest.raises(ValueError, match="frame_ms"):
        rp.schedule(pcm, segments, frame_ms=invalid)
