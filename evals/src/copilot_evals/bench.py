"""Latency benchmark: replay the scripted mock interview through the real gateway with real
providers, several times, and score each run.

    uv run copilot-bench-audio                       # once: TTS audio for the script
    uv run copilot-bench run --label baseline --runs 3
    uv run copilot-bench compare baseline fix1 ...   # markdown before/after table

`run` starts the gateway itself (the working tree's code, `.env` settings) on its own port with
a dedicated benchmark user (`BENCH_USER_ID`), so the dev user's data is never touched. Before
each run it writes the fictional profile's hot digest and interview pack to Valkey for that user,
with the prepared answers embedded by the configured embedding model. Each run gets a fresh
digest build line, so every run starts with a cold prompt cache like a new session (use
`--warm-cache` to keep it). Consent is off, as in the live test of 2026-10-07: nothing is stored
as text and the exact-repeat cache cannot answer from an earlier run.

Each run is scored against the script's labels (evals/bench/mock_interview.toml):
- latency per stage, from each fast answer's `latency_ms` and the client's receive times; first
  word is measured from the true end of speech in the audio, so it includes the client's VAD
  hangover (the overlay render, under 50 ms, is not included);
- detection precision and recall over the labeled utterances, visible false starts (a question
  shown, then cancelled) and false answers shown;
- speculative waste (fast passes started and thrown away) and prepared-answer hits;
- cost: every model call the session logged (answers, detector calls, cancelled passes,
  cache prewarms) plus Deepgram for the audio streamed, also scaled to one live hour.

Results go to evals/bench/results/<label>/: one JSON per run and summary.json.
"""

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np

from copilot_evals import replay as rp
from copilot_evals.benchdata import (
    BENCH_DIR,
    TIMELINE,
    WAV,
    Profile,
    Span,
    Utterance,
    load_profile,
    load_script,
    load_timeline,
    normalize,
)
from copilot_evals.vad import SAMPLE_RATE, segment, silero_probabilities

BENCH_USER_ID = "01a00000-be0c-7000-8000-000000000001"
BENCH_PACK_ID = "01a00000-be0c-7000-8000-0000000000aa"
RESULTS = BENCH_DIR / "results"
DEFAULT_PORT = 8091
# Deepgram Nova-3 streaming, pay as you go: $0.0077/min list ($0.0048 promotional, Oct 2026).
STT_PRICE_PER_MIN = 0.0077
FORCED_WINDOW_S = 1.0  # a question.detected this soon after a hotkey press is that press's

# Stages reported, in ms unless the name says seconds.
STAGES = (
    "detector_ms",
    "final_ms",
    "turn_end_ms",
    "start_ms",
    "first_token_ms",
    "server_first_word_ms",
    "first_word_ms",
    "deep_first_word_ms",
)


# Seeding the benchmark user's digest and pack.


async def embed_prepared(profile: Profile) -> tuple[str | None, dict[str, str]]:
    """Prepared questions embedded by the configured embedder (cached on disk by model)."""
    from copilot_core.config import CoreSettings
    from copilot_core.embed import make_embedder
    from copilot_core.packs import encode_embedding

    embedder = make_embedder(CoreSettings())
    if embedder is None:
        return None, {}
    cache = (
        BENCH_DIR
        / "audio"
        / f"embeddings-{embedder.model_id.replace(':', '_').replace('@', '_')}.json"
    )
    known: dict[str, str] = {}
    if cache.exists():
        known = cast(dict[str, str], json.loads(cache.read_text("utf-8")))
    missing = [p.question for p in profile.prepared if p.question not in known]
    if missing:
        vectors = await embedder.embed(missing)
        known.update({q: encode_embedding(v) for q, v in zip(missing, vectors, strict=True)})
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known), "utf-8")
    return embedder.model_id, known


def node_id(short_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"copilot-bench:{short_id}"))


async def seed(valkey: Any, profile: Profile, nonce: str | None) -> None:
    from copilot_core import keys
    from copilot_core.packs import (
        Digest,
        NodeRef,
        PackContext,
        PreparedAnswer,
        RankedStory,
        SessionPack,
    )

    text = profile.digest
    if nonce:  # a new prefix: the first question of the run sees a cold prompt cache
        first, _, rest = text.partition("\n")
        text = f"{first} build {nonce}\n{rest}"
    refs = {k: NodeRef(node_id=uuid.UUID(node_id(k)), label=v) for k, v in profile.refs.items()}
    version = Digest.version_of(text)
    digest = Digest(schema="digest.v1", version=version, text=text, refs=refs, built_at=None)
    model_id, vectors = await embed_prepared(profile)
    pack = SessionPack(
        schema="session_pack.v1",
        pack_id=uuid.UUID(BENCH_PACK_ID),
        user_id=uuid.UUID(BENCH_USER_ID),
        mode="interview",
        built_at=datetime.now(UTC).isoformat(),
        digest_version=digest.version,
        context=PackContext(
            company=profile.company,
            role_title=profile.role_title,
            round=profile.round,
            job_summary=profile.job_summary,
        ),
        ranked_stories=[
            RankedStory(
                node_id=uuid.UUID(node_id(s.short_id)),
                short_id=s.short_id,
                title=s.title,
                one_liner=s.one_liner,
                competencies=s.competencies,
            )
            for s in profile.stories
        ],
        prepared_answers=[
            PreparedAnswer(
                question=p.question,
                embedding_b64=vectors.get(p.question),
                talking_points=p.points,
                node_ids=[uuid.UUID(node_id(n)) for n in p.nodes],
            )
            for p in profile.prepared
        ],
        embedding_model=model_id,
    )
    uid = BENCH_USER_ID
    await valkey.set(keys.digest(uid), digest.model_dump_json(by_alias=True))
    await valkey.set(keys.pack(uid, BENCH_PACK_ID), pack.model_dump_json(by_alias=True))
    await valkey.hset(keys.pack_index(uid), "mode:interview", BENCH_PACK_ID)
    await valkey.delete(keys.user_active_session(uid))
    async for key in valkey.scan_iter(match=f"qa:{{{uid}}}:*"):
        await valkey.delete(key)


async def session_log(valkey: Any, sid: str) -> list[dict[str, Any]]:
    from copilot_core import keys

    entries = await valkey.xrange(keys.sess_log(sid))
    out: list[dict[str, Any]] = []
    for _, fields in entries:
        raw = fields.get(b"e") or fields.get("e")
        if raw is not None:
            out.append(cast(dict[str, Any], json.loads(raw)))
    return out


# The gateway under test.


class Gateway:
    def __init__(
        self,
        port: int,
        env: dict[str, str],
        log_dir: Path,
        user_name: str,
        python: str = sys.executable,
    ) -> None:
        self.port = port
        # Another checkout's interpreter runs that checkout's gateway (its venv has the
        # workspace installed from there); the working directory, and so `.env`, stays this one.
        self.python = python
        self.url = f"ws://127.0.0.1:{port}/v1/session"
        self.env = {
            **os.environ,
            "PORT": str(port),
            "COPILOT_DEV_USER_ID": BENCH_USER_ID,
            "COPILOT_DEV_USER_NAME": user_name,
            "COPILOT_LOG_DIR": str(log_dir),
            **env,
        }
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_dir = log_dir

    def start(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        out = (self.log_dir / "gateway-console.txt").open("wb")
        self.proc = subprocess.Popen(
            [self.python, "-c", "from copilot_gateway.main import run; run()"],
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"gateway exited with {self.proc.returncode}; see {out.name}")
            with contextlib.suppress(OSError):
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1)
                return
            time.sleep(0.5)
        raise SystemExit("gateway did not become healthy in 90 s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(10)


# Provider latency at the time of a run: model latency drifts by the minute, so every run
# records the detector model's own latency, measured outside the gateway, next to its results.


async def probe(calls: int = 5) -> dict[str, Any] | None:
    from copilot_core import prompts
    from copilot_core.config import CoreSettings
    from copilot_core.llm import OpenAIProvider
    from copilot_core.providers import openai_client, openai_params

    settings = CoreSettings()
    if "openai" not in settings.llm_providers or settings.openai_api_key is None:
        return None
    model, _ = settings.openai_route("detector")
    provider = OpenAIProvider("openai", openai_client(settings))
    req = prompts.detector(
        "Alex Rivera", "", "Tell me about a time you had to deal with a production incident.", 80
    )
    with contextlib.suppress(Exception):
        await provider.warm()
    totals: list[float] = []
    for _ in range(calls):
        started = time.monotonic()
        try:
            async for _chunk in provider.stream(model, req, openai_params(settings, "detector")):
                pass
        except Exception:
            continue
        totals.append((time.monotonic() - started) * 1000)
    await provider.client.close()
    if not totals:
        return None
    return {"model": model, "median_ms": round(statistics.median(totals)), "n": len(totals)}


# One run.


@dataclass
class Audio:
    pcm: bytes
    segments: list[Any]
    sends: list[rp.Send]
    seconds: float
    speech_sent_s: float


def load_audio() -> Audio:
    pcm = rp.load_wav(WAV)
    segments = segment(silero_probabilities(pcm), total_samples=len(pcm) // 2)
    sends = rp.schedule(pcm, segments)
    sent = sum(len(s.audio) for s in sends) / 2 / SAMPLE_RATE
    return Audio(pcm, segments, sends, len(pcm) / 2 / SAMPLE_RATE, sent)


def force_actions(script: list[Utterance], spans: list[Span]) -> list[tuple[float, dict[str, Any]]]:
    by_id = {s.id: s for s in spans}
    return [
        (by_id[u.id].end_s + u.force_after_s, {"type": "question.force"})
        for u in script
        if u.force_after_s is not None
    ]


async def run_once(
    url: str, token: str, audio: Audio, actions: list[tuple[float, dict[str, Any]]], quiet: bool
) -> rp.Replay:
    replay = rp.Replay(
        url,
        token,
        audio.pcm,
        audio.sends,
        audio.segments,
        tail_s=10.0,
        quiet=quiet,
        consent=False,
        actions=actions,
    )
    await replay.run()
    return replay


# Scoring.


def span_for(spans: list[Span], start_s: float, end_s: float) -> Span | None:
    best: tuple[float, Span] | None = None
    for s in spans:
        overlap = min(end_s, s.end_s) - max(start_s, s.start_s)
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, s)
    return best[1] if best else None


def prepared_used(answer: str, profile: Profile) -> str | None:
    text = normalize(answer)
    for p in profile.prepared:
        if p.points and normalize(p.points[0]) in text:
            return p.id
    return None


def pct(values: list[float], p: float) -> float:
    return float(np.percentile(np.array(values), p))


def stored_latencies(log: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Each pass's answers.latency_ms as the flush builds it: the answer entry's, plus the
    latency_report entries logged after it (a fast pass that ended before its question was
    confirmed gets turn_end_ms and final_ms that way)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for e in log:
        key = (str(e.get("question_id")), str(e.get("pass")))
        if e.get("kind") == "answer":
            out[key] = dict(cast(dict[str, Any], e.get("latency_ms", {})))
        elif e.get("kind") == "latency_report" and key in out:
            skip = {"kind", "question_id", "pass", "at"}
            out[key].update({k: v for k, v in e.items() if k not in skip})
    return out


def rescore(label: str) -> dict[str, Any]:
    """Recompute a label's summary from its saved runs, refilling the confirmation timings a
    question's fast answer did not carry (see `stored_latencies`)."""
    out_dir = RESULTS / label
    runs: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("run-*.json")):
        run = cast(dict[str, Any], json.loads(path.read_text("utf-8")))
        latency = stored_latencies(run["log"])
        for row in run["questions"]:
            stored = latency.get((row["question_id"], "fast"), {})
            for key in ("turn_end_ms", "final_ms"):
                if row.get(key) is None and stored.get(key) is not None:
                    row[key] = stored[key]
        runs.append(run)
    old = cast(dict[str, Any], json.loads((out_dir / "summary.json").read_text("utf-8")))
    summary = summarize(runs)
    probes = [r["probe"]["median_ms"] for r in runs if r.get("probe")]
    summary["provider_probe_ms"] = round(statistics.median(probes)) if probes else None
    for key in ("label", "env", "interleaved_with", "at"):
        if key in old:
            summary[key] = old[key]
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    return summary


def score_run(
    replay: rp.Replay,
    log: list[dict[str, Any]],
    script: list[Utterance],
    spans: list[Span],
    profile: Profile,
    audio: Audio,
    stt_price: float,
) -> dict[str, Any]:
    utts = {u.id: u for u in script}
    confidences: dict[str, float] = {}
    for _, m in replay.events:
        if m.get("type") == "question.detected":
            confidences[m["question_id"]] = float(m["confidence"])
    force_times = [t for t, m in replay.sent_actions if m.get("type") == "question.force"]
    stored_latency = stored_latencies(log)

    rows: list[dict[str, Any]] = []
    for q in replay.questions.values():
        if q.detected_at is None:
            continue
        forced_at = next(
            (
                t
                for t in force_times
                if 0 <= q.detected_at - t <= FORCED_WINDOW_S
                and confidences.get(q.question_id) == 1.0
            ),
            None,
        )
        span: Span | None = None
        if forced_at is not None:
            at_s = forced_at - replay.t0
            ended = [s for s in spans if s.end_s <= at_s]
            span = ended[-1] if ended else None
        elif q.segment is not None:
            seg = replay.segments[q.segment]
            span = span_for(spans, seg.start_edge / SAMPLE_RATE, seg.end_edge / SAMPLE_RATE)
        fast = q.done.get("fast", {})
        lat = cast(dict[str, Any], fast.get("latency_ms", {}))
        stored = stored_latency.get((q.question_id, "fast"), {})
        first_delta = q.first_delta_at.get("fast")
        anchor = forced_at if forced_at is not None else q.speech_end_at
        deep_delta = q.first_delta_at.get("deep")
        row: dict[str, Any] = {
            "question_id": q.question_id,
            "utterance": span.id if span else None,
            "expect": utts[span.id].expect if span else "none",
            "forced": forced_at is not None,
            "fired": q.confirmed_at is not None,
            "shown_then_cancelled": q.cancelled and q.speculative_at is not None,
            "answer_shown": bool(q.first_delta_at),
            "speculative": q.speculative_at is not None,
            "type": q.qtype,
            "text": q.text,
            "fast_source": fast.get("source"),
            "prepared_used": prepared_used(q.text_by_pass.get("fast", ""), profile)
            if fast.get("source") == "prepared"
            else None,
            "prepared_expected": utts[span.id].prepared if span else None,
            "detector_ms": lat.get("detector_ms"),
            "final_ms": lat.get("final_ms", stored.get("final_ms")),
            "turn_end_ms": lat.get("turn_end_ms", stored.get("turn_end_ms")),
            "start_ms": lat.get("start_ms"),
            "first_token_ms": lat.get("first_token_ms"),
            "server_first_word_ms": lat.get("first_delta_ms"),
            "first_word_ms": round((first_delta - anchor) * 1000)
            if first_delta is not None and anchor is not None
            else None,
            "deep_first_word_ms": round((deep_delta - anchor) * 1000)
            if deep_delta is not None
            and anchor is not None
            and "expand_ms" not in q.done.get("deep", {}).get("latency_ms", {})
            else None,
            "errors": q.errors,
        }
        rows.append(row)

    fired = [r for r in rows if r["fired"] and not r["forced"]]
    answer_utts = {u.id for u in script if u.expect == "answer"}
    hit_utts = {r["utterance"] for r in fired if r["expect"] == "answer"}
    false_fires = [r for r in fired if r["expect"] == "none"]
    per_utt = Counter(r["utterance"] for r in fired if r["expect"] == "answer")
    tp, fp = len(hit_utts), len(false_fires)

    starts = [
        e
        for e in log
        if e.get("pass") == "fast" and e.get("kind") in ("answer", "answer_error", "pass_cancelled")
    ]
    wasted = [
        e
        for e in log
        if e.get("kind") == "pass_cancelled"
        and e.get("pass") == "fast"
        and e.get("reason") != "superseded"
    ]

    def cost_of(e: dict[str, Any]) -> float:
        v = e.get("cost_usd", e.get("cost_usd_est"))
        return float(v) if isinstance(v, int | float) else 0.0

    by_kind: dict[str, float] = {}
    for e in log:
        if e.get("kind") in ("answer", "detector", "pass_cancelled", "prewarm"):
            key = (
                f"{e['kind']}:{e['pass']}"
                if e.get("kind") in ("answer", "pass_cancelled")
                else str(e["kind"])
            )
            by_kind[key] = by_kind.get(key, 0.0) + cost_of(e)
    stt_cost = audio.speech_sent_s / 60 * stt_price
    model_cost = sum(by_kind.values())
    total = model_cost + stt_cost
    prepared_rows = [
        r for r in fired + [r for r in rows if r["forced"]] if r["fast_source"] == "prepared"
    ]
    detector_entries = [e for e in log if e.get("kind") == "detector"]

    return {
        "session_id": replay.session_id,
        "questions": rows,
        "detection": {
            "answer_utterances": len(answer_utts),
            "true_positives": tp,
            "false_fires": fp,
            "false_fire_utterances": sorted({str(r["utterance"]) for r in false_fires}),
            "missed": sorted(answer_utts - hit_utts),
            "duplicate_fires": sum(n - 1 for n in per_utt.values() if n > 1),
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / len(answer_utts), 3) if answer_utts else None,
            "shown_then_cancelled": sum(1 for r in rows if r["shown_then_cancelled"]),
            "false_answers_shown": sum(
                1
                for r in rows
                if r["answer_shown"]
                and (r["expect"] == "none" or (r["shown_then_cancelled"] and not r["fired"]))
            ),
            "detector_calls": len(detector_entries) if detector_entries else None,
        },
        "speculative": {
            "fast_starts": len(starts),
            "wasted": len(wasted),
            "waste_rate": round(len(wasted) / len(starts), 3) if starts else None,
            "waste_cost_usd": round(sum(cost_of(e) for e in wasted), 6),
        },
        "prepared": {
            "opportunities": sum(1 for r in fired if r["prepared_expected"]),
            "hits": len(prepared_rows),
            "correct": sum(
                1 for r in prepared_rows if r["prepared_used"] == r["prepared_expected"]
            ),
            "wrong": sum(1 for r in prepared_rows if r["prepared_used"] != r["prepared_expected"]),
        },
        "cost": {
            "model_usd": round(model_cost, 6),
            "by_kind_usd": {k: round(v, 6) for k, v in sorted(by_kind.items())},
            "stt_usd": round(stt_cost, 6),
            "total_usd": round(total, 6),
            "audio_s": round(audio.seconds, 1),
            "speech_sent_s": round(audio.speech_sent_s, 1),
            "per_live_hour_usd": round(total / audio.seconds * 3600, 4),
            "model_per_live_hour_usd": round(model_cost / audio.seconds * 3600, 4),
        },
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    auto = [
        r
        for run in runs
        for r in run["questions"]
        if r["fired"] and not r["forced"] and r["expect"] == "answer"
    ]
    forced = [r for run in runs for r in run["questions"] if r["forced"]]

    def stage(rows: list[dict[str, Any]], key: str) -> dict[str, float] | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return {"p50": round(pct(vals, 50)), "p95": round(pct(vals, 95)), "n": len(vals)}

    det = [run["detection"] for run in runs]
    tp = sum(d["true_positives"] for d in det)
    fp = sum(d["false_fires"] for d in det)
    n_answer = sum(d["answer_utterances"] for d in det)
    spec = [run["speculative"] for run in runs]
    starts = sum(s["fast_starts"] for s in spec)
    prep = [run["prepared"] for run in runs]
    costs = [run["cost"] for run in runs]
    per_run_fw = [
        pct(
            [
                float(r["first_word_ms"])
                for r in run["questions"]
                if r["fired"]
                and not r["forced"]
                and r["expect"] == "answer"
                and r["first_word_ms"] is not None
            ],
            50,
        )
        for run in runs
        if any(r["first_word_ms"] is not None for r in run["questions"])
    ]
    calls = [d["detector_calls"] for d in det if d["detector_calls"] is not None]
    return {
        "runs": len(runs),
        "stages": {k: stage(auto, k) for k in STAGES},
        "forced_first_word_ms": stage(forced, "first_word_ms"),
        "first_word_p50_per_run_ms": [round(v) for v in per_run_fw],
        "detection": {
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / n_answer, 3) if n_answer else None,
            "false_fires_per_run": round(fp / len(runs), 2),
            "false_fire_utterances": dict(
                Counter(u for d in det for u in d["false_fire_utterances"])
            ),
            "missed": dict(Counter(u for d in det for u in d["missed"])),
            "duplicate_fires_per_run": round(sum(d["duplicate_fires"] for d in det) / len(runs), 2),
            "shown_then_cancelled_per_run": round(
                sum(d["shown_then_cancelled"] for d in det) / len(runs), 2
            ),
            "false_answers_shown_per_run": round(
                sum(d["false_answers_shown"] for d in det) / len(runs), 2
            ),
            "detector_calls_per_run": round(statistics.mean(calls), 1) if calls else None,
        },
        "speculative": {
            "fast_starts_per_run": round(starts / len(runs), 1),
            "waste_rate": round(sum(s["wasted"] for s in spec) / starts, 3) if starts else None,
            "waste_cost_per_run_usd": round(statistics.mean(s["waste_cost_usd"] for s in spec), 6),
        },
        "prepared": {
            "opportunities": sum(p["opportunities"] for p in prep),
            "hits": sum(p["hits"] for p in prep),
            "correct": sum(p["correct"] for p in prep),
            "wrong": sum(p["wrong"] for p in prep),
        },
        "cost": {
            "per_run_usd": round(statistics.mean(c["total_usd"] for c in costs), 4),
            "model_per_run_usd": round(statistics.mean(c["model_usd"] for c in costs), 4),
            "per_live_hour_usd": round(statistics.mean(c["per_live_hour_usd"] for c in costs), 3),
            "model_per_live_hour_usd": round(
                statistics.mean(c["model_per_live_hour_usd"] for c in costs), 3
            ),
            "by_kind_per_run_usd": {
                k: round(statistics.mean(c["by_kind_usd"].get(k, 0.0) for c in costs), 6)
                for k in sorted({k for c in costs for k in c["by_kind_usd"]})
            },
        },
    }


def print_run(i: int, run: dict[str, Any]) -> None:
    print(f"\nRun {i}: session {run['session_id']}")
    cols = ("det", "final", "turn", "start", "ttft", "srv1st", "1st", "deep")
    head = " ".join(f"{c:>6}" for c in cols)
    print(f"{'utterance':<18} {'exp':<6} {'fired':<6} {'src':<8} {head}")
    keys = (
        "detector_ms", "final_ms", "turn_end_ms", "start_ms", "first_token_ms",
        "server_first_word_ms", "first_word_ms", "deep_first_word_ms",
    )  # fmt: skip
    for r in sorted(run["questions"], key=lambda r: r["first_word_ms"] or 0):
        cells = " ".join(f"{'-' if r.get(k) is None else r[k]:>6}" for k in keys)

        tag = (
            "forced"
            if r["forced"]
            else ("yes" if r["fired"] else ("cxl" if r["shown_then_cancelled"] else "no"))
        )
        print(f"{r['utterance']!s:<18} {r['expect']:<6} {tag:<6} {r['fast_source']!s:<8} {cells}")
    d, s, p, cost = run["detection"], run["speculative"], run["prepared"], run["cost"]
    print(
        f"precision {d['precision']} recall {d['recall']} false fires {d['false_fire_utterances']} "
        f"missed {d['missed']} | waste {s['wasted']}/{s['fast_starts']} | "
        f"prepared {p['correct']}/{p['opportunities']} (wrong {p['wrong']}) | "
        f"cost ${cost['total_usd']:.4f} (${cost['per_live_hour_usd']:.2f}/live h)"
    )


def print_summary(label: str, s: dict[str, Any]) -> None:
    print(f"\n== {label}: {s['runs']} runs ==")
    for k, v in s["stages"].items():
        if v:
            print(f"  {k:<22} p50 {v['p50']:>6}  p95 {v['p95']:>6}  (n={v['n']})")
    if s["forced_first_word_ms"]:
        f = s["forced_first_word_ms"]
        print(f"  {'forced first word':<22} p50 {f['p50']:>6}  p95 {f['p95']:>6}  (n={f['n']})")
    print(f"  first word p50 per run: {s['first_word_p50_per_run_ms']}")
    print(f"  detection: {json.dumps(s['detection'])}")
    print(f"  speculative: {json.dumps(s['speculative'])}")
    print(f"  prepared: {json.dumps(s['prepared'])}")
    print(f"  cost: {json.dumps(s['cost'])}")


@dataclass
class Variant:
    """One gateway under test: this checkout's (`--label`) or another's (`--also`)."""

    label: str
    python: str
    out_dir: Path
    gateway: Gateway | None = None
    url: str | None = None


async def cmd_run(args: argparse.Namespace) -> int:
    from copilot_core.dotenv import load_dotenv
    from copilot_core.valkey_client import make_valkey

    load_dotenv()
    if not WAV.exists() or not TIMELINE.exists():
        raise SystemExit("no benchmark audio: run `uv run copilot-bench-audio` first")
    script, profile, spans = load_script(), load_profile(), load_timeline()
    token = args.token or os.environ.get("COPILOT_DEV_TOKEN")
    if not token:
        raise SystemExit("no token: pass --token or set COPILOT_DEV_TOKEN")
    env = dict(kv.split("=", 1) for kv in args.env)
    variants = [Variant(args.label, args.python, RESULTS / args.label)]
    for spec in args.also:
        label, _, python = spec.partition("=")
        variants.append(Variant(label, python, RESULTS / label))
    audio = load_audio()
    actions = force_actions(script, spans)
    valkey = make_valkey(os.environ.get("COPILOT_VALKEY_URL", "redis://localhost:6379/0"))
    runs: dict[str, list[dict[str, Any]]] = {v.label: [] for v in variants}
    try:
        for n, v in enumerate(variants):
            v.out_dir.mkdir(parents=True, exist_ok=True)
            if args.url is not None and n == 0:
                v.url = args.url
                continue
            v.gateway = Gateway(args.port + n, env, v.out_dir / "logs", profile.user_name, v.python)
            v.gateway.start()
            v.url = v.gateway.url
        # Variants alternate run by run, so they see the same provider conditions.
        for i in range(1, args.runs + 1):
            for v in variants:
                assert v.url is not None
                nonce = None if args.warm_cache else f"{v.label}-{i}-{int(time.time())}"
                await seed(valkey, profile, nonce)
                measured = await probe() if not args.no_probe else None
                print(
                    f"\n--- {v.label} run {i}/{args.runs} ({audio.seconds:.0f} s of audio; "
                    f"provider probe {measured['median_ms'] if measured else '-'} ms) ---",
                    flush=True,
                )
                replay = await run_once(v.url, token, audio, actions, args.quiet)
                await asyncio.sleep(2.0)  # the session's last log entries reach Valkey
                log = await session_log(valkey, replay.session_id or "")
                run = score_run(replay, log, script, spans, profile, audio, args.stt_price)
                run["probe"] = measured
                run["log"] = log
                run["events"] = [{"t": round(t, 4), **m} for t, m in replay.events]
                path = v.out_dir / f"run-{i}.json"
                path.write_text(json.dumps(run, indent=1, default=str), "utf-8")
                runs[v.label].append(run)
                print_run(i, run)
    finally:
        for v in variants:
            if v.gateway is not None:
                v.gateway.stop()
        await valkey.aclose()
    for v in variants:
        summary = summarize(runs[v.label])
        probes = [r["probe"]["median_ms"] for r in runs[v.label] if r.get("probe")]
        summary["provider_probe_ms"] = round(statistics.median(probes)) if probes else None
        summary["label"] = v.label
        summary["env"] = env
        summary["interleaved_with"] = [w.label for w in variants if w is not v]
        summary["at"] = datetime.now(UTC).isoformat()
        (v.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
        print_summary(v.label, summary)
    return 0


def forced_p50(s: dict[str, Any]) -> Any:
    forced = cast(dict[str, Any] | None, s["forced_first_word_ms"])
    return forced["p50"] if forced else None


def _cell(v: Any) -> str:
    return "-" if v is None else str(v)


def compare(labels: list[str]) -> str:
    """A markdown table of the main numbers, one column per label."""
    sums = [
        cast(dict[str, Any], json.loads((RESULTS / lbl / "summary.json").read_text("utf-8")))
        for lbl in labels
    ]

    def st(s: dict[str, Any], k: str, p: str) -> str:
        v = s["stages"].get(k)
        return _cell(v[p] if v else None)

    rows: list[tuple[str, list[str]]] = [
        ("Runs", [str(s["runs"]) for s in sums]),
        ("Provider probe, detector model (ms)", [_cell(s.get("provider_probe_ms")) for s in sums]),
        ("First word p50 (ms)", [st(s, "first_word_ms", "p50") for s in sums]),
        ("First word p95 (ms)", [st(s, "first_word_ms", "p95") for s in sums]),
        (
            "Server first word p50 / p95",
            [
                f"{st(s, 'server_first_word_ms', 'p50')} / {st(s, 'server_first_word_ms', 'p95')}"
                for s in sums
            ],
        ),
        (
            "Detector p50 / p95",
            [f"{st(s, 'detector_ms', 'p50')} / {st(s, 'detector_ms', 'p95')}" for s in sums],
        ),
        (
            "Turn confirmed p50 / p95",
            [f"{st(s, 'turn_end_ms', 'p50')} / {st(s, 'turn_end_ms', 'p95')}" for s in sums],
        ),
        (
            "Fast start p50 / p95",
            [f"{st(s, 'start_ms', 'p50')} / {st(s, 'start_ms', 'p95')}" for s in sums],
        ),
        (
            "Fast TTFT p50 / p95",
            [f"{st(s, 'first_token_ms', 'p50')} / {st(s, 'first_token_ms', 'p95')}" for s in sums],
        ),
        (
            "Deep first word p50 / p95",
            [
                f"{st(s, 'deep_first_word_ms', 'p50')} / {st(s, 'deep_first_word_ms', 'p95')}"
                for s in sums
            ],
        ),
        (
            "Forced first word p50",
            [_cell(forced_p50(s)) for s in sums],
        ),
        (
            "Precision / recall",
            [
                f"{_cell(s['detection']['precision'])} / {_cell(s['detection']['recall'])}"
                for s in sums
            ],
        ),
        ("False fires per run", [_cell(s["detection"]["false_fires_per_run"]) for s in sums]),
        (
            "Shown then cancelled per run",
            [_cell(s["detection"]["shown_then_cancelled_per_run"]) for s in sums],
        ),
        ("Detector calls per run", [_cell(s["detection"]["detector_calls_per_run"]) for s in sums]),
        ("Speculative waste", [_cell(s["speculative"]["waste_rate"]) for s in sums]),
        (
            "Prepared hits (correct / chances, wrong)",
            ["{correct} / {opportunities}, {wrong}".format(**s["prepared"]) for s in sums],
        ),
        (
            "Model cost per live hour",
            [f"${s['cost']['model_per_live_hour_usd']:.2f}" for s in sums],
        ),
        (
            "Total cost per live hour (with STT)",
            [f"${s['cost']['per_live_hour_usd']:.2f}" for s in sums],
        ),
    ]
    head = "| | " + " | ".join(labels) + " |\n|---|" + "---|" * len(labels) + "\n"
    return head + "\n".join(f"| {name} | " + " | ".join(vals) + " |" for name, vals in rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Latency benchmark over the scripted mock interview.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="replay the script through the gateway and score it")
    r.add_argument("--label", required=True)
    r.add_argument("--runs", type=int, default=3)
    r.add_argument(
        "--url",
        help="use a running gateway instead of starting one (its user must be BENCH_USER_ID)",
    )
    r.add_argument("--port", type=int, default=DEFAULT_PORT)
    r.add_argument("--token")
    r.add_argument(
        "--env", action="append", default=[], help="KEY=VALUE for the gateway, repeatable"
    )
    r.add_argument("--warm-cache", action="store_true", help="keep the digest prefix between runs")
    r.add_argument("--stt-price", type=float, default=STT_PRICE_PER_MIN, help="USD per minute")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--python", default=sys.executable, help="interpreter for the gateway")
    r.add_argument(
        "--also",
        action="append",
        default=[],
        metavar="LABEL=PYTHON",
        help="another gateway (another checkout's venv python), interleaved run by run",
    )
    r.add_argument("--no-probe", action="store_true", help="skip the provider latency probe")
    c = sub.add_parser("compare", help="markdown table over result labels")
    c.add_argument("labels", nargs="+")
    rs = sub.add_parser("rescore", help="recompute summaries from the saved runs")
    rs.add_argument("labels", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "rescore":
        for label in args.labels:
            print_summary(label, rescore(label))
        return 0
    if args.cmd == "compare":
        print(compare(args.labels))
        return 0
    return asyncio.run(cmd_run(args))


def run() -> None:
    sys.exit(main())
