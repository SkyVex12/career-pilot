"""Paired, controlled hidden-route benchmark; no external provider or quality judge.

Run from the repository root with the workspace Python environment:
    .venv/Scripts/python.exe evals/bench/hidden_route_selection.py --pairs 8

The production Session, TurnDetector, AnswerEngine and router run unchanged through
the existing localhost websocket/synthetic-STT replay. Only the classification and
provider delays/output are scripted. Request identity is a quality-preservation
check, not a model-quality score. Physical audio/STT and native/WebView paint are
outside the measurements. All fixture content is fictional and safe to check in.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from collections.abc import AsyncGenerator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from copilot_evals.live_answer_session_replay import replay_session, replay_settings

from copilot_core.llm import Decided, LLMChunk, LLMDone, LLMRequest, LLMUsage
from copilot_gateway.session import Session
from copilot_gateway.testing import ScriptedProvider

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "evals/answers/results/hidden-route-selection-2026-11-27"
PROFILE = (
    "Fictional benchmark profile. Alex uses PostgreSQL and Python. "
    "Alex implemented transaction retries and idempotency keys in a sample service. "
    "No employment outcomes or production metrics have been supplied."
)
ANSWER = (
    "Transactions keep related updates atomic and isolated. "
    "Use an isolation level that fits the invariant, retry serialization failures, "
    "and make retried operations idempotent."
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    qtype: str
    depth: str
    strategy: str
    expected_budget: int
    expected_model_role: str
    changes_route: bool


CASES = (
    Case(
        "technical-light-to-legacy",
        "How do database transactions keep related updates correct "
        "when two clients write at the same time?",
        "technical",
        "light",
        "legacy_dual",
        250,
        "fast",
        True,
    ),
    Case(
        "technical-deep-to-expert",
        "How do database transactions stay correct across concurrent updates "
        "and interrupted operations?",
        "technical",
        "deep",
        "adaptive",
        1200,
        "expert",
        True,
    ),
    Case(
        "unchanged-other-light-control",
        "Could the next example explain the usual steps involved in checking "
        "an operation before trying it again?",
        "other",
        "light",
        "adaptive",
        400,
        "fast",
        False,
    ),
)


class ControlledProvider(ScriptedProvider):
    """Record every full invocation, including canceled provisional generations."""

    def __init__(self, case: Case) -> None:
        super().__init__("openai")
        self.case = case
        self.started = time.monotonic()
        self.invocations: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def now_ms(self) -> float:
        return round((time.monotonic() - self.started) * 1000, 3)

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        entry = {
            "model": model,
            "input": input,
            "questions": questions,
            "started_ms": self.now_ms(),
            "status": "started",
        }
        self.decisions.append(entry)
        try:
            await asyncio.sleep(0.080)
            answer = [("turn", "answer"), ("type", self.case.qtype), ("depth", self.case.depth)]
            entry.update(status="completed", completed_ms=self.now_ms(), answer=answer)
            return Decided(
                [
                    {"type": "choice", "name": name, "choice": value, "confidence": 0.95}
                    for name, value in answer
                ],
                LLMUsage(1000, cache_write_tokens=0, reasoning_tokens=0),
                self.name,
                model,
            )
        except asyncio.CancelledError:
            entry.update(status="cancelled", completed_ms=self.now_ms())
            raise

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        assert req.json_schema is None, "The detector must use its genuine Decisions path"
        identity = {"model": model, "request": asdict(req), "params": dict(params)}
        entry = {
            **identity,
            "identity_sha256": digest(identity),
            "status": "started",
            "started_ms": self.now_ms(),
            "text": "",
            "usage": None,
        }
        self.invocations.append(entry)
        try:
            await asyncio.sleep(0.400)
            entry.update(first_chunk_ms=self.now_ms(), text=ANSWER)
            yield ANSWER
            # Keep the provisional request alive through normal release so both
            # branches exercise real cancellation/accounting, not invented usage.
            await asyncio.sleep(0.650)
            usage = LLMUsage(1000, 900, len(ANSWER) // 4, cache_write_tokens=0, reasoning_tokens=0)
            entry.update(status="completed", finished_ms=self.now_ms(), usage=asdict(usage))
            yield LLMDone(usage, "end_turn", self.name, model)
        except asyncio.CancelledError:
            entry.update(status="cancelled", finished_ms=self.now_ms())
            raise
        finally:
            if entry["status"] == "started":
                entry.update(status="closed", finished_ms=self.now_ms())


def settings_pair() -> tuple[Any, Any]:
    base = replay_settings(False, "adaptive").model_copy(
        update={
            "speculative_start_ms": 300,
            "silence_timer_ms": 400,
            "early_decision_interval_ms": 700,
            "first_show_min_words": 8,
            "early_stable_partial_ms": None,
            "early_final_release": False,
            "committed_final_recheck": False,
            "late_first_text_recovery_ms": None,
            "adaptive_technical_legacy": True,
            # Isolate the primary route; the preference is identical in both arms.
            "deep_pass_auto": False,
        }
    )
    baseline = base.model_copy(update={"early_route_selection": False})
    candidate = base.model_copy(update={"early_route_selection": True})
    left, right = baseline.model_dump(), candidate.model_dump()
    assert {key for key in left if left[key] != right[key]} == {"early_route_selection"}
    return baseline, candidate


def source_hashes() -> dict[str, str]:
    files = [
        "apps/gateway/src/copilot_gateway/" + name + ".py"
        for name in ("turn", "session", "answer", "config", "adaptive", "router")
    ] + [
        "evals/src/copilot_evals/live_answer_session_replay.py",
        "evals/bench/hidden_route_selection.py",
    ]
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def check_trial(
    result: dict[str, Any], provider: ControlledProvider, case: Case, settings: Any
) -> dict[str, Any]:
    assert result["status"] == "completed", result.get("status")
    assert not result["violations"], result["violations"]
    assert result["pending_after_close"] == 0
    assert result["late_events_after_close"] == 0
    assert not result["writer_pending_after_close"]
    end_ms = next(item["at_ms"] for item in result["injections"] if item["op"] == "end")
    answers = [event for event in result["events"] if event["message"]["type"] == "answer.delta"]
    assert answers
    assert all(event["at_ms"] >= end_ms for event in answers), "Answer leaked before speech end"
    primaries = [
        event
        for event in result["events"]
        if event["message"]["type"] == "answer.done" and event["message"]["pass"] == "fast"
    ]
    assert len(primaries) == 1, "Expected exactly one completed public primary"
    question_ids = {event["message"]["question_id"] for event in answers}
    assert question_ids == {primaries[0]["message"]["question_id"]}
    assert all(event["message"]["pass"] == "fast" for event in answers)
    output = "".join(event["message"]["text"] for event in answers)
    assert output == ANSWER
    requests = [entry for entry in provider.invocations if entry["status"] == "completed"]
    assert len(requests) == 1, "Expected exactly one completed intended provider request"
    request = requests[0]
    expected_model = (
        (
            settings.adaptive_expert_route.model
            if settings.adaptive_expert_route
            else settings.openai_deep_model
        )
        if case.expected_model_role == "expert"
        else settings.openai_fast_model
    )
    assert request["model"] == expected_model, (request["model"], expected_model)
    assert request["request"]["max_tokens"] == case.expected_budget
    assert PROFILE in "\n".join(block["text"] for block in request["request"]["system"])
    assert case.question in str(request["request"]["content"])
    assert result["profile_sha256"] == digest(PROFILE)
    assert len(provider.decisions) == 1
    assert provider.decisions[0]["status"] == "completed"
    # Provider's clock starts before replay's, so this comparison is conservative:
    # it proves the scripted classifier really finished before speech end.
    assert provider.decisions[0]["completed_ms"] < end_ms
    detected = [
        event["message"]
        for event in result["events"]
        if event["message"]["type"] == "question.detected"
    ]
    assert detected
    assert detected[-1]["answer_strategy"] == case.strategy
    assert detected[-1]["question_type"] == case.qtype
    cancelled = [entry for entry in provider.invocations if entry["status"] == "cancelled"]
    retained_cancel = [
        entry for entry in result["metadata_logs"] if entry.get("kind") == "pass_cancelled"
    ]
    if case.changes_route:
        assert len(provider.invocations) == 2
        assert len(cancelled) == 1
        assert retained_cancel, (
            "Canceled provisional generation was absent from retained accounting"
        )
        assert cancelled[0]["usage"] is None, "Canceled work must not invent completed usage"
    else:
        assert len(provider.invocations) == 1
        assert not cancelled
    assert not [entry for entry in provider.invocations if entry["status"] in {"started", "closed"}]
    return {
        "first_delta_from_end_ms": answers[0]["at_ms"] - end_ms,
        "complete_from_end_ms": primaries[0]["at_ms"] - end_ms,
        "primary_identity_sha256": request["identity_sha256"],
        "primary_request": {key: request[key] for key in ("model", "request", "params")},
        "output_sha256": digest(output),
        "output": output,
        "provider_invocations": len(provider.invocations),
        "cancelled_provisional": len(cancelled),
        "retained_cancellation_records": len(retained_cancel),
        "classification_completed_before_end": True,
        "invariants_passed": True,
    }


async def trial(case: Case, settings: Any, trial_id: str, output: Path) -> dict[str, Any]:
    provider = ControlledProvider(case)
    attempts: list[dict[str, Any]] = []
    scenario = {
        "id": case.name,
        "profile": PROFILE,
        "max_confirmed": 1,
        "actions": [
            {"op": "partial", "text": case.question, "start": True},
            {"op": "pause", "seconds": 0.500},
            {"op": "end"},
            {"op": "settle"},
        ],
    }
    result = await replay_session(
        scenario, settings, [provider], trial_id=trial_id, timeout_s=8, attempt_events=attempts
    )
    artifact = {
        "case": asdict(case),
        "early_route_selection": settings.early_route_selection,
        "provider_requests": provider.invocations,
        "decisions": provider.decisions,
        "provider_attempts": attempts,
        "replay": result,
    }
    try:
        artifact["checks"] = check_trial(result, provider, case, settings)
    except AssertionError as error:
        artifact["assertion_failure"] = str(error)
        raise
    finally:
        (output / (trial_id + ".json")).write_text(
            json.dumps(artifact, indent=2, default=str) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "trial": trial_id,
                **{
                    key: artifact["checks"][key]
                    for key in (
                        "first_delta_from_end_ms",
                        "cancelled_provisional",
                        "invariants_passed",
                    )
                },
            }
        ),
        flush=True,
    )
    return artifact["checks"]


async def run(args: argparse.Namespace) -> None:
    assert hasattr(Session, "classify_held"), (
        "Wait for the production classify_held implementation before running"
    )
    assert 1 <= args.pairs <= 12
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("*.json")):
        raise ValueError("Use an empty output directory; benchmark evidence is not overwritten")
    before = source_hashes()
    baseline, candidate = settings_pair()
    pairs: list[dict[str, Any]] = []
    selected_cases = tuple(case for case in CASES if not args.case or case.name in args.case)
    for index in range(args.pairs):
        # AB, BA, BA, AB rotates the ABBA start order; cases rotate independently.
        order = (False, True) if index % 4 in (0, 3) else (True, False)
        cases = (
            selected_cases[index % len(selected_cases) :]
            + selected_cases[: index % len(selected_cases)]
        )
        for case in cases:
            observed = {}
            for enabled in order:
                arm = "candidate" if enabled else "baseline"
                trial_id = f"pair-{index + 1:02d}-{case.name}-{arm}"
                observed[arm] = await trial(
                    case, candidate if enabled else baseline, trial_id, output
                )
            left, right = observed["baseline"], observed["candidate"]
            assert left["primary_request"] == right["primary_request"], (
                "Intended model/request/params changed"
            )
            assert left["output"] == right["output"], "Visible output changed"
            pairs.append(
                {
                    "pair": index + 1,
                    "case": case.name,
                    "order": ["candidate" if value else "baseline" for value in order],
                    "baseline_ms": left["first_delta_from_end_ms"],
                    "candidate_ms": right["first_delta_from_end_ms"],
                    "saved_ms": left["first_delta_from_end_ms"] - right["first_delta_from_end_ms"],
                    "identical_primary_request": True,
                    "identical_output": True,
                }
            )
    after = source_hashes()
    assert before == after, (
        "Production sources changed during the measurement; rerun in a fresh directory"
    )
    rows = []
    for case in selected_cases:
        selected = [pair for pair in pairs if pair["case"] == case.name]
        rows.append(
            {
                "case": case.name,
                "pairs": len(selected),
                "targeted_route_change": case.changes_route,
                **{
                    field: round(statistics.median(pair[field] for pair in selected), 2)
                    for field in ("baseline_ms", "candidate_ms", "saved_ms")
                },
                "minimum_saved_ms": min(pair["saved_ms"] for pair in selected),
                "maximum_saved_ms": max(pair["saved_ms"] for pair in selected),
            }
        )
    summary = {
        "scope": (
            "Controlled synthetic provider/detector timing through production "
            "Session/TurnDetector/AnswerEngine and localhost websocket; not real provider "
            "latency, audio/STT latency, paint latency, or a quality judge"
        ),
        "controls": {
            "provider_first_chunk_ms": 400,
            "provider_tail_ms": 650,
            "detector_ms": 80,
            "punctuated_partial_lead_ms": 500,
            "speculative_gate_ms": 300,
            "silence_gate_ms": 400,
            "first_show_min_words": 8,
            "deep_pass_auto": False,
            "settings_changed_between_arms": ["early_route_selection"],
            "fast_model": baseline.openai_fast_model,
            "expert_model": (
                baseline.adaptive_expert_route.model
                if baseline.adaptive_expert_route
                else baseline.openai_deep_model
            ),
        },
        "quality_checks": (
            "Exact full intended LLMRequest, model, inference params, fictional evidence/profile, "
            "output and preference identity; one public primary, no pre-end answer, "
            "retained provisional cancellation, no lifecycle violations"
        ),
        "pairs": pairs,
        "summary": rows,
        "source_sha256": before,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    report = [
        "# Controlled hidden-route selection benchmark",
        "",
        summary["scope"],
        "",
        "All paired intended requests and outputs were identical. "
        "All lifecycle and cancellation assertions passed.",
        "",
        "| Scenario | Pairs | Baseline p50 | Candidate p50 | Paired saving p50 |",
        "|---|---:|---:|---:|---:|",
    ]
    report += [
        f"| {row['case']} | {row['pairs']} | {row['baseline_ms']} ms "
        f"| {row['candidate_ms']} ms | {row['saved_ms']} ms |"
        for row in rows
    ]
    report += [
        "",
        "Provider first chunk: 400 ms; classifier: 80 ms; full punctuated partial: "
        "500 ms before speech end; normal gates: 300/400 ms. First-show threshold remains "
        "8 words. Both arms use the same explicit primary-only preference.",
        "",
        "The unchanged-route case is a negative control. These fixed injected delays "
        "establish the overlap mechanism, not real-network savings or model answer quality.",
        "",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"summary": rows, "output": str(output)}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", action="append", choices=[case.name for case in CASES])
    asyncio.run(run(parser.parse_args()))
