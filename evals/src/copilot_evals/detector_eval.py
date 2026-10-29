"""The question detector alone, on the benchmark's labeled utterances: accuracy and latency.

    uv run copilot-detector-eval                                # the configured detector model
    uv run copilot-detector-eval --model gpt-5.4-nano --model gpt-6-luna --reps 3
    uv run copilot-detector-eval --system candidate_prompt.txt  # try a detector prompt
    uv run copilot-detector-eval --script evals/bench/group_standup.toml --mode standup

Each utterance of evals/bench/mock_interview.toml goes to the detector as the gateway sends it:
the detector prompt, the transcript of the 60 s before it (the interviewer's lines, and the
candidate's answers as "(Alex Rivera spoke for N s)"), and the utterance. A question with a
mid-sentence pause is also checked on its first half alone, which must not fire (the speaker
is not done). An utterance fires when the parsed detection passes the gateway's rule
(`Detection.fires`). Reported per model: false fires, misses, type
accuracy on the fired questions, and latency (request to the whole JSON) p50 and p95.

Every call runs through `OpenAIProvider` with the model's own reasoning setting, so latency is
what the gateway would see from this machine.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from copilot_evals.benchdata import Utterance, load_profile, load_script

# Reasoning setting per model family: GPT-5.6 and later and the GPT-5.4 small models take
# "none"; gpt-5-nano starts at "minimal"; gpt-4.1 models have no reasoning.
REASONING = {
    "gpt-4.1": None,
    "gpt-5-nano": "minimal",
    "gpt-5-mini": "minimal",
}


def reasoning_for(model: str) -> str | None:
    for prefix, effort in REASONING.items():
        if model.startswith(prefix):
            return effort
    return "none"


@dataclass(frozen=True)
class Case:
    id: str
    text: str
    transcript: str
    should_fire: bool | None  # None: not scored
    qtype: str | None
    turns: str = ""  # the gateway's <turn_taking> block (group calls only)


def cases(script: list[Utterance], user_name: str) -> list[Case]:
    """Each utterance with the transcript of the 60 s before it, timed from the script, and
    the turn-taking block the gateway would send with it."""
    from copilot_core import prompts

    out: list[Case] = []
    t = 0.0
    history: list[tuple[float, float, str]] = []  # (start, end, line)

    def turns(now: float) -> str:
        spans = [("them" if line.startswith("Them:") else "me", s, e) for s, e, line in history]
        return prompts.turn_taking(user_name, spans, now)

    for u in script:
        if u.speaker == "me":  # the user's own line: context only, never a case
            t += u.gap_s
            history.append((t, t + 0.4 * len(u.text.split()), f"{user_name}: {u.text}"))
            t += 0.4 * len(u.text.split())
            continue
        if history and u.gap_s >= 6:  # a long gap stands for the candidate answering
            history.append((t, t + u.gap_s - 1, f"({user_name} spoke for {int(u.gap_s - 1)} s)"))
        t += u.gap_s
        start = t
        length = 0.4 * len(u.text.split())  # about 150 words a minute
        lines = [
            f"[{int(s) // 60:02d}:{int(s) % 60:02d}] {line}"
            for s, e, line in history
            if e >= start - 60
        ]
        transcript = "\n".join(lines)
        expect = {"answer": True, "none": False, "either": None}[u.expect]
        if len(u.parts) > 1:
            # Not scored: the first half of a paused question is often a complete question on
            # its own, and only the speaker going on shows it was not; that is turn-taking.
            out.append(Case(f"{u.id}:first_half", u.parts[0], transcript, None, None, turns(start)))
        out.append(Case(u.id, u.text, transcript, expect, u.qtype, turns(start)))
        t += length
        history.append((start, t, f"Them: {u.text}"))
    return out


async def detect_once(
    provider: Any,
    model: str,
    case: Case,
    user_name: str,
    system: str | None,
    mode: str = "interview",
) -> tuple[Any, float]:
    from copilot_core import prompts
    from copilot_core.llm import LLMDone, LLMRequest, SystemBlock
    from copilot_gateway.detector import parse_detection

    req = prompts.detector(user_name, case.transcript, case.text, 80, mode, case.turns)
    if system is not None:
        phrase = prompts.MODE_PHRASES.get(mode, f"a {mode}")
        req = LLMRequest(
            system=[SystemBlock(system.format(user_name=user_name, mode=phrase))],
            content=req.content,
            max_tokens=req.max_tokens,
            json_schema=req.json_schema,
        )
    effort = reasoning_for(model)
    params: dict[str, Any] = {} if effort is None else {"reasoning": {"effort": effort}}
    if effort not in (None, "none"):
        params["max_output_tokens_extra"] = 1024
    started = time.monotonic()
    parts: list[str] = []
    async for chunk in provider.stream(model, req, params):
        if not isinstance(chunk, LLMDone):
            parts.append(chunk)
    return parse_detection("".join(parts)), (time.monotonic() - started) * 1000


async def eval_model(
    provider: Any, model: str, all_cases: list[Case], reps: int, user_name: str,
    system: str | None, mode: str = "interview",
) -> dict[str, Any]:  # fmt: skip
    latencies: list[float] = []
    false_fires: dict[str, int] = {}
    misses: dict[str, int] = {}
    wrong_types: dict[str, list[str]] = {}  # case id: the types it got instead of its own
    type_right = type_total = errors = 0
    scored = 0
    for _ in range(reps):
        for case in all_cases:
            try:
                detection, ms = await detect_once(provider, model, case, user_name, system, mode)
            except Exception as e:
                errors += 1
                print(f"  {model} {case.id}: {type(e).__name__}: {e}"[:200])
                continue
            latencies.append(ms)
            if case.should_fire is None:
                continue
            scored += 1
            fired = detection.fires()
            if fired and not case.should_fire:
                false_fires[case.id] = false_fires.get(case.id, 0) + 1
            elif case.should_fire and not fired:
                misses[case.id] = misses.get(case.id, 0) + 1
            elif fired and case.qtype:
                type_total += 1
                type_right += int(detection.qtype == case.qtype)
                if detection.qtype != case.qtype:
                    wrong_types.setdefault(case.id, []).append(detection.qtype)
    wrong = sum(false_fires.values()) + sum(misses.values())
    return {
        "model": model,
        "reasoning": reasoning_for(model),
        "calls": len(latencies),
        "errors": errors,
        "accuracy": round(1 - wrong / scored, 3) if scored else None,
        "false_fires": false_fires,
        "misses": misses,
        "type_accuracy": round(type_right / type_total, 3) if type_total else None,
        "wrong_types": wrong_types,
        "latency_p50_ms": round(float(np.percentile(latencies, 50))) if latencies else None,
        "latency_p95_ms": round(float(np.percentile(latencies, 95))) if latencies else None,
        "latency_mean_ms": round(statistics.mean(latencies)) if latencies else None,
    }


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Evaluate the question detector on the benchmark labels."
    )
    p.add_argument(
        "--model", action="append", help="OpenAI model (repeatable); default: configured"
    )
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--system", type=Path, help="a detector system prompt to try instead")
    p.add_argument("--json", type=Path)
    p.add_argument("--script", type=Path, help="labeled utterances (default: the mock interview)")
    p.add_argument("--mode", default="interview", choices=["interview", "standup", "meeting"])
    args = p.parse_args(argv)

    from copilot_core.config import CoreSettings
    from copilot_core.dotenv import load_dotenv
    from copilot_core.llm import OpenAIProvider
    from copilot_core.providers import openai_client

    load_dotenv()
    settings = CoreSettings()
    provider = OpenAIProvider("openai", openai_client(settings))
    await provider.warm("")
    models = args.model or [settings.openai_route("detector")[0]]
    system = args.system.read_text("utf-8") if args.system else None
    profile = load_profile()
    all_cases = cases(load_script(args.script) if args.script else load_script(), profile.user_name)
    results: list[dict[str, Any]] = []
    for model in models:
        r = await eval_model(
            provider, model, all_cases, args.reps, profile.user_name, system, args.mode
        )
        results.append(r)
        print(json.dumps(r))
    print(f"\n{'model':<14} {'reason':<8} {'acc':>5} {'type':>5} {'p50':>6} {'p95':>6}  wrong")
    for r in results:
        cells = [r["model"], r["reasoning"], r["accuracy"], r["type_accuracy"]]
        cells += [r["latency_p50_ms"], r["latency_p95_ms"]]
        widths = (-14, -8, 5, 5, 6, 6)
        line = " ".join(
            f"{c!s:<{-w}}" if w < 0 else f"{c!s:>{w}}" for c, w in zip(cells, widths, strict=True)
        )
        print(f"{line}  false fires {r['false_fires']}, misses {r['misses']}")
    if args.json:
        args.json.write_text(
            json.dumps({"cases": len(all_cases), "results": results}, indent=2), "utf-8"
        )
    return 0


def run() -> None:
    sys.exit(asyncio.run(main()))
