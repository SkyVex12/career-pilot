"""Replay a recorded call through the gateway in real time and measure latency.

    uv run copilot-replay clip.wav
    uv run copilot-replay clip.wav --url ws://127.0.0.1:8081/v1/session --json run.json
    uv run copilot-replay clip.wav --drop-at 30   # cut the socket at 30 s, then resume

The WAV plays as the "them" channel. Silero VAD runs on the client side, as on the desktop:
only speech is sent, in 100 ms frames, with a speech-start flag on the first frame (after a
short pre-roll) and an empty speech-end frame once the silence passes `--min-silence-ms`.
The tool prints the transcript, detected questions and streamed answers as they arrive, then
a per-question latency table and first-word p50/p95 against the 0.9 s / 1.5 s target
(ARCHITECTURE.md, "SLOs, alerts and feature flags").

First-word latency is measured from the true end of speech in the audio (the last voiced VAD
window), so it includes the client's VAD hangover, to the first answer.delta of that question.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from copilot_evals.vad import SAMPLE_RATE, Segment, segment, silero_probabilities
from copilot_protocol.audio import encode_frame
from copilot_protocol.models import AudioFrameHeader

FRAME_SAMPLES = 1600  # 100 ms
RING_S = 10.0  # the client resends this much "them" audio after a resume
TARGET_P50_S, TARGET_P95_S = 0.9, 1.5


def load_wav(path: Path) -> bytes:
    """16 kHz mono pcm16 bytes from any PCM WAV."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 1:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        samples = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 4:
        samples = np.frombuffer(raw, "<i4").astype(np.float32) / 2**31
    else:
        raise SystemExit(f"{path}: {width * 8}-bit WAV is not supported; convert to 16-bit PCM")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        import soxr  # pyright: ignore[reportMissingTypeStubs]

        samples = soxr.resample(samples, rate, SAMPLE_RATE)
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


@dataclass(frozen=True)
class Send:
    at_sample: int  # when the client can send it (audio captured up to here)
    flags: int
    audio: bytes
    capture_ms: int
    segment: int


def schedule(pcm: bytes, segments: list[Segment], preroll_ms: int = 200) -> list[Send]:
    """The frames a VAD client sends: speech only, start flag first, then a speech-end frame."""
    out: list[Send] = []
    preroll = preroll_ms * SAMPLE_RATE // 1000
    prev_end = 0
    for i, seg in enumerate(segments):
        pos = max(prev_end, seg.start_edge - preroll, 0)
        first = True
        # The pre-roll and the audio up to the start decision go out at once, in chunks of at
        # most 1 s so a frame stays well under 64 KB.
        burst_end = min(seg.start_decided, seg.end_decided)
        while pos < burst_end:
            end = min(pos + SAMPLE_RATE, burst_end)
            out.append(Send(burst_end, 0b01 if first else 0, pcm[pos * 2 : end * 2], pos // 16, i))
            first, pos = False, end
        while pos < seg.end_decided:  # then 100 ms frames in real time
            end = min(pos + FRAME_SAMPLES, seg.end_decided)
            out.append(Send(end, 0b01 if first else 0, pcm[pos * 2 : end * 2], pos // 16, i))
            first, pos = False, end
        out.append(Send(seg.end_decided, 0b10, b"", seg.end_decided * 1000 // SAMPLE_RATE, i))
        prev_end = seg.end_decided
    return out


@dataclass
class QuestionRecord:
    question_id: str
    text: str = ""
    qtype: str = ""
    forced: bool = False
    segment: int | None = None
    speech_end_at: float | None = None  # wall time of the true end of speech
    end_flag_at: float | None = None
    detected_at: float | None = None
    speculative_at: float | None = None
    confirmed_at: float | None = None
    cancelled: bool = False
    first_delta_at: dict[str, float] = field(default_factory=dict[str, float])
    done_at: dict[str, float] = field(default_factory=dict[str, float])
    done: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    text_by_pass: dict[str, str] = field(default_factory=dict[str, str])
    errors: list[str] = field(default_factory=list[str])

    def first_word_s(self) -> float | None:
        if self.speech_end_at is None or not self.first_delta_at:
            return None
        return min(self.first_delta_at.values()) - self.speech_end_at


class Printer:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.partial: dict[tuple[str, str], str] = {}

    def line(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def delta(self, qid: str, pass_: str, text: str) -> None:
        buf = self.partial.get((qid, pass_), "") + text
        *lines, rest = buf.split("\n")
        for line in lines:
            if line.strip():
                self.line(f"    {pass_:>4}> {line}")
        self.partial[(qid, pass_)] = rest

    def flush(self, qid: str, pass_: str) -> None:
        rest = self.partial.pop((qid, pass_), "")
        if rest.strip():
            self.line(f"    {pass_:>4}> {rest}")


class Replay:
    def __init__(
        self,
        url: str,
        token: str,
        pcm: bytes,
        sends: list[Send],
        segments: list[Segment],
        *,
        mode: str = "interview",
        speed: float = 1.0,
        tail_s: float = 8.0,
        drop_at_s: float | None = None,
        quiet: bool = False,
    ) -> None:
        self.url = url
        self.token = token
        self.pcm = pcm
        self.sends = sends
        self.segments = segments
        self.mode = mode
        self.speed = speed
        self.tail_s = tail_s
        self.drop_at_s = drop_at_s
        self.out = Printer(quiet)
        self.ws: Any = None
        self.connected = asyncio.Event()
        self.session_id: str | None = None
        self.last_seq = 0
        self.t0 = 0.0
        self.frame_seq = 0
        self.ring: deque[tuple[float, bytes]] = deque()
        self.events: list[tuple[float, dict[str, Any]]] = []
        self.questions: dict[str, QuestionRecord] = {}
        self.end_flags: list[tuple[float, int]] = []  # (wall time, segment)
        self.closed: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.reconnects = 0
        self.reconnecting = False
        self._ready = asyncio.Event()

    def wall(self, sample: int) -> float:
        return self.t0 + sample / SAMPLE_RATE / self.speed

    async def _open(self) -> None:
        from websockets.asyncio.client import connect

        self.ws = await connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.token}"}, max_size=None
        )

    async def run(self) -> None:
        await self._open()
        await self.ws.send(
            json.dumps(
                {
                    "type": "session.start",
                    "mode": self.mode,
                    "platform": "replay",
                    "client_version": "copilot-replay",
                    "consent": {"store_transcript": True},
                    "audio": {"sample_rate": SAMPLE_RATE, "encoding": "pcm16"},
                }
            )
        )
        reader = asyncio.create_task(self._reader())
        await asyncio.wait_for(self._ready.wait(), 10)
        self.connected.set()
        self.t0 = time.monotonic()
        dropper = asyncio.create_task(self._drop_later()) if self.drop_at_s is not None else None
        try:
            await self._send_all()
            await asyncio.sleep(self.tail_s)
            await self._send_json({"type": "session.end"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.closed), 15)
        finally:
            if dropper is not None:
                dropper.cancel()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.ws.close()

    async def _send_json(self, message: dict[str, Any]) -> None:
        await self.connected.wait()
        await self.ws.send(json.dumps(message))

    async def _send_all(self) -> None:
        for send in self.sends:
            delay = self.wall(send.at_sample) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            header = AudioFrameHeader.model_validate(
                {
                    "channel": 0,
                    "flags": send.flags,
                    "frame_seq": self.frame_seq,
                    "capture_ms": send.capture_ms,
                }
            )
            self.frame_seq += 1
            data = encode_frame(header, send.audio)
            now = time.monotonic()
            self.ring.append((now, data))
            while self.ring and now - self.ring[0][0] > RING_S:
                self.ring.popleft()
            if send.flags & 0b10:
                self.end_flags.append((now, send.segment))
            if not self.connected.is_set():
                continue  # in the ring buffer; resent after the resume
            try:
                await self.ws.send(data)
            except Exception:
                continue

    async def _drop_later(self) -> None:
        assert self.drop_at_s is not None
        await asyncio.sleep(max(0.0, self.t0 + self.drop_at_s - time.monotonic()))
        self.out.line(f"--- dropping the socket at {self.drop_at_s:.1f} s ---")
        self.reconnecting = True
        self.connected.clear()
        self.ws.transport.abort()
        await asyncio.sleep(0.5)
        await self._open()
        self.reconnects += 1
        await self.ws.send(
            json.dumps(
                {"type": "session.resume", "session_id": self.session_id, "last_seq": self.last_seq}
            )
        )
        for _, frame in list(self.ring):  # the server drops what it already has
            await self.ws.send(frame)
        self.reconnecting = False
        self.connected.set()
        self.out.line(f"--- resumed after seq {self.last_seq}, resent {len(self.ring)} frames ---")

    async def _reader(self) -> None:
        from websockets.exceptions import ConnectionClosed

        while True:
            ws = self.ws
            with contextlib.suppress(ConnectionClosed):
                async for raw in ws:
                    self._on_message(time.monotonic(), json.loads(raw))
            if self.reconnecting or ws is not self.ws:
                await self.connected.wait()  # the dropper is resuming on a new socket
                continue
            if not self.closed.done():
                self.closed.set_result(ws.close_code or 1006)
            return

    def _question(self, qid: str) -> QuestionRecord:
        if qid not in self.questions:
            self.questions[qid] = QuestionRecord(qid)
        return self.questions[qid]

    def _on_message(self, at: float, m: dict[str, Any]) -> None:
        seq = int(m.get("seq", 0))
        if seq and seq <= self.last_seq:
            return  # a replayed message we already handled
        self.last_seq = max(self.last_seq, seq)
        self.events.append((at - self.t0 if self.t0 else 0.0, m))
        t = m["type"]
        if t == "session.ready":
            self.session_id = m["session_id"]
            self._ready.set()
            self.out.line(f"session {self.session_id} ready")
        elif t == "transcript.final":
            self.out.line(f"  [{m['start_ms'] / 1000:6.1f}s] them: {m['text']}")
        elif t == "question.detected":
            q = self._question(m["question_id"])
            q.text, q.qtype = m["text"], m["question_type"]
            if q.detected_at is None:
                q.detected_at = at
                self._link_segment(q, at)
            if m["speculative"]:
                q.speculative_at = at
            else:
                q.confirmed_at = at
                tag = "" if q.speculative_at else " (not speculative)"
                self.out.line(f"  Q [{q.qtype} {m['confidence']:.2f}]{tag} {q.text}")
        elif t == "question.cancelled":
            q = self._question(m["question_id"])
            q.cancelled = True
            self.out.line("    (speculative question cancelled: the speaker kept talking)")
        elif t == "answer.delta":
            q = self._question(m["question_id"])
            q.first_delta_at.setdefault(m["pass"], at)
            q.text_by_pass[m["pass"]] = q.text_by_pass.get(m["pass"], "") + m["text"]
            self.out.delta(q.question_id, m["pass"], m["text"])
        elif t == "answer.done":
            q = self._question(m["question_id"])
            q.done_at[m["pass"]] = at
            q.done[m["pass"]] = m
            self.out.flush(q.question_id, m["pass"])
            fw = q.first_word_s() if m["pass"] == "fast" else None
            extra = f", first word {fw:.2f} s" if fw is not None else ""
            self.out.line(f"    {m['pass']:>4}  done ({m.get('source', 'model')}{extra})")
        elif t == "answer.error":
            q = self._question(m["question_id"])
            q.errors.append(f"{m['pass']}:{m['code']}")
            self.out.flush(q.question_id, m["pass"])
            self.out.line(f"    {m['pass']:>4}  error {m['code']}")
        elif t == "status":
            self.out.line(f"  status {m['component']} {m['state']}: {m['message']}")
        elif t == "error":
            self.out.line(f"  error {m['code']}: {m['message']}")

    def _link_segment(self, q: QuestionRecord, at: float) -> None:
        """A question belongs to the last speech end the client sent before it arrived."""
        before = [(t, i) for t, i in self.end_flags if t <= at]
        if not before:
            q.forced = True
            return
        flag_at, i = before[-1]
        q.segment = i
        q.end_flag_at = flag_at
        q.speech_end_at = self.wall(self.segments[i].end_edge)


def pct(values: list[float], p: float) -> float:
    return float(np.percentile(np.array(values), p))


def summarize(r: Replay) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for q in r.questions.values():
        if q.cancelled and not q.confirmed_at:
            continue
        fast = q.done.get("fast", {})
        lat = fast.get("latency_ms", {})
        fw = q.first_word_s()
        rows.append(
            {
                "question_id": q.question_id,
                "text": q.text,
                "type": q.qtype,
                "speculative": q.speculative_at is not None,
                "vad_hangover_ms": r.segments[q.segment].hangover_ms
                if q.segment is not None
                else None,
                "detector_ms": lat.get("detector_ms"),
                "turn_end_ms": lat.get("turn_end_ms"),
                "first_token_ms": lat.get("first_token_ms"),
                "server_first_delta_ms": lat.get("first_delta_ms"),
                "first_word_s": round(fw, 3) if fw is not None else None,
                "fast_source": fast.get("source"),
                "deep_first_delta_s": round(q.first_delta_at["deep"] - q.speech_end_at, 3)
                if "deep" in q.first_delta_at and q.speech_end_at
                else None,
                "deep_done_s": round(q.done_at["deep"] - q.speech_end_at, 3)
                if "deep" in q.done_at and q.speech_end_at
                else None,
                "usage_fast": fast.get("usage"),
                "errors": q.errors,
                "fast_answer": q.text_by_pass.get("fast", ""),
                "deep_answer": q.text_by_pass.get("deep", ""),
            }
        )
    first_words = [row["first_word_s"] for row in rows if row["first_word_s"] is not None]

    def stage(key: str) -> dict[str, float] | None:
        vals = [float(row[key]) for row in rows if row.get(key) is not None]
        if not vals:
            return None
        return {"p50": round(pct(vals, 50), 3), "p95": round(pct(vals, 95), 3), "n": len(vals)}

    starts = sum(1 for q in r.questions.values() if q.speculative_at is not None)
    cancels = sum(1 for q in r.questions.values() if q.cancelled)
    summary: dict[str, Any] = {
        "questions": len(rows),
        "speech_segments": len(r.segments),
        "speculative_starts": starts,
        "speculative_cancels": cancels,
        "prepared_hits": sum(1 for row in rows if row["fast_source"] == "prepared"),
        "cache_hits": sum(1 for row in rows if row["fast_source"] == "cache"),
        "reconnects": r.reconnects,
        "stages": {
            k: stage(k)
            for k in (
                "vad_hangover_ms",
                "detector_ms",
                "turn_end_ms",
                "first_token_ms",
                "server_first_delta_ms",
                "first_word_s",
                "deep_first_delta_s",
            )
        },
    }
    if first_words:
        p50, p95 = pct(first_words, 50), pct(first_words, 95)
        summary["first_word"] = {
            "p50_s": round(p50, 3),
            "p95_s": round(p95, 3),
            "target_p50_s": TARGET_P50_S,
            "target_p95_s": TARGET_P95_S,
            "meets_target": p50 <= TARGET_P50_S and p95 <= TARGET_P95_S,
            "n": len(first_words),
        }
    return {"summary": summary, "questions": rows}


def print_report(report: dict[str, Any]) -> None:
    rows = report["questions"]
    s = report["summary"]
    print("\nPer question (ms unless noted; first word and deep in seconds from end of speech)")
    print(
        f"{'#':>2} {'type':<13} {'spec':>4} {'vad':>4} {'det':>5} {'turn':>5} {'ttft':>5} "
        f"{'srv1st':>6} {'1st word':>8} {'deep1st':>7}  question"
    )

    def cell(row: dict[str, Any], key: str, width: int = 5) -> str:
        v = row.get(key)
        return f"{'-' if v is None else v:>{width}}"

    for i, row in enumerate(rows, 1):
        print(
            f"{i:>2} {row['type']:<13} {'y' if row['speculative'] else 'n':>4} "
            f"{cell(row, 'vad_hangover_ms', 4)} {cell(row, 'detector_ms')} "
            f"{cell(row, 'turn_end_ms')} {cell(row, 'first_token_ms')} "
            f"{cell(row, 'server_first_delta_ms', 6)} {cell(row, 'first_word_s', 8)} "
            f"{cell(row, 'deep_first_delta_s', 7)}  {row['text'][:60]}"
        )
    print("\nStages (p50 / p95):")
    for name, v in s["stages"].items():
        if v:
            print(f"  {name:<22} {v['p50']:>8} / {v['p95']:<8} (n={v['n']})")
    print(
        f"\nQuestions {s['questions']}, speech segments {s['speech_segments']}, "
        f"speculative starts {s['speculative_starts']}, cancels {s['speculative_cancels']}, "
        f"prepared hits {s['prepared_hits']}, cache hits {s['cache_hits']}"
    )
    fw = s.get("first_word")
    if fw:
        verdict = "meets" if fw["meets_target"] else "MISSES"
        print(
            f"First word: p50 {fw['p50_s']:.2f} s, p95 {fw['p95_s']:.2f} s "
            f"over {fw['n']} questions "
            f"({verdict} the {fw['target_p50_s']} s / {fw['target_p95_s']} s target)"
        )
    else:
        print("First word: no answered questions")


def dotenv_token() -> str | None:
    if token := os.environ.get("COPILOT_DEV_TOKEN"):
        return token
    path = Path(".env")
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if line.startswith("COPILOT_DEV_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Replay a WAV through the gateway as the them channel.")
    p.add_argument("wav", type=Path)
    p.add_argument("--url", default="ws://127.0.0.1:8081/v1/session")
    p.add_argument("--token", help="access token (default: COPILOT_DEV_TOKEN from env or .env)")
    p.add_argument("--mode", default="interview", choices=["interview", "standup", "meeting"])
    p.add_argument("--threshold", type=float, default=0.5, help="Silero speech threshold")
    p.add_argument("--min-silence-ms", type=int, default=200, help="VAD hangover before speech end")
    p.add_argument("--preroll-ms", type=int, default=200)
    p.add_argument("--start", type=float, default=0.0, help="skip this many seconds")
    p.add_argument("--max-seconds", type=float, help="play at most this much audio")
    p.add_argument("--speed", type=float, default=1.0, help="above 1 is faster; latency invalid")
    p.add_argument("--tail", type=float, default=8.0, help="seconds to wait for answers at the end")
    p.add_argument("--drop-at", type=float, help="abort the socket at this second, then resume")
    p.add_argument("--json", type=Path, help="write the report and every event here")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    token = args.token or dotenv_token()
    if not token:
        p.error("no token: pass --token or set COPILOT_DEV_TOKEN")
    pcm = load_wav(args.wav)
    pcm = pcm[int(args.start * SAMPLE_RATE) * 2 :]
    if args.max_seconds:
        pcm = pcm[: int(args.max_seconds * SAMPLE_RATE) * 2]
    segments = segment(
        silero_probabilities(pcm),
        threshold=args.threshold,
        min_silence_ms=args.min_silence_ms,
        total_samples=len(pcm) // 2,
    )
    sends = schedule(pcm, segments, args.preroll_ms)
    seconds = len(pcm) / 2 / SAMPLE_RATE
    print(f"{args.wav}: {seconds:.1f} s, {len(segments)} speech segments; streaming in real time")
    if args.speed != 1.0:
        print("note: --speed is not 1, so the latency numbers are not meaningful")
    replay = Replay(
        args.url,
        token,
        pcm,
        sends,
        segments,
        mode=args.mode,
        speed=args.speed,
        tail_s=args.tail,
        drop_at_s=args.drop_at,
        quiet=args.quiet,
    )
    await replay.run()
    report = summarize(replay)
    print_report(report)
    if args.json:
        report["events"] = [{"t": round(t, 4), **m} for t, m in replay.events]
        report["config"] = {k: str(v) for k, v in vars(args).items() if k != "token"}
        args.json.write_text(json.dumps(report, indent=2), "utf-8")
        print(f"wrote {args.json}")
    return 0


def run() -> None:
    sys.exit(asyncio.run(main()))
