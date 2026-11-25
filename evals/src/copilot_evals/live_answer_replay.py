"""Reproducible generation-only A/B/C/D replay, using the shipped AnswerEngine.

Default invocation performs an offline preflight only. --live explicitly enables the
configured providers under a persisted hard reservation budget. Output must be private.
This isolates generation: classification, STT, retrieval search and UI are not benchmarked.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import json
import logging
import math
import random
import subprocess
import time
import tomllib
from collections.abc import AsyncGenerator, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, get_args
from uuid import NAMESPACE_URL, uuid4, uuid5

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMRequest, ModelRouter
from copilot_core.packs import NodeRef
from copilot_core.providers import make_providers
from copilot_evals.live_answer_eval import Run, fingerprint
from copilot_evals.sensei_judge import collect_answers
from copilot_evals.sensei_runtime import Budget, MeteredProvider, private_directory, save
from copilot_gateway.answer import AnswerEngine, Question
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_protocol import models

STRATEGIES = {
    "A": "legacy_dual",
    "B": "adaptive",
    "C": "adaptive_hybrid",
    "D": "adaptive_on_demand",
}
TRIAL = contextvars.ContextVar("live_eval_trial", default="")
CASE_FIELDS = {
    "id",
    "question",
    "type",
    "category",
    "mode",
    "digest",
    "recent",
    "memory",
    "source",
    "depth",
    "refs",
    "context_sha256",
}
CONFIG_FIELDS = {
    "llm_providers",
    "openai_fast_model",
    "openai_deep_model",
    "openai_deep_light_model",
    "openai_fast_reasoning",
    "openai_deep_reasoning",
    "openai_fast_service_tier",
    "openai_deep_service_tier",
    "openai_fast_retry_tier",
    "openai_deep_retry_tier",
    "fast_pass_max_tokens",
    "deep_pass_max_tokens",
    "adaptive_max_tokens",
    "adaptive_stream_idle_timeout_s",
    "adaptive_stream_total_timeout_s",
    "openai_first_token_timeout_ms",
    "openai_deep_first_token_timeout_ms",
    "openai_reasoning_headroom_tokens",
    "openai_http2",
    "first_token_timeout_ms",
    "llm_keepalive_s",
    "haiku_model",
    "sonnet_model",
    "bedrock_haiku_model",
    "bedrock_haiku_retry_model",
    "bedrock_sonnet_model",
    "bedrock_sonnet_retry_model",
    "bedrock_refusal_model",
    "deep_pass_effort",
    "deep_pass_thinking",
    "deep_routing",
    "deep_pass_auto",
    "deep_pass_auto_types",
    "llm_first_token_timeout_s",
    "llm_total_timeout_s",
    "fast_hedge_ms",
    "deep_hedge_ms",
    "fast_pass_priority",
    "first_show_min_words",
    "model_prices",
    "tier_price_multipliers",
}


def root_directory() -> Path:
    return Path(__file__).resolve().parents[3]


def load_cases(root: Path | None = None) -> list[dict[str, Any]]:
    """Only question/source fields are copied; historical assistant outputs are omitted."""
    root = root or root_directory()
    profile = tomllib.loads((root / "evals/bench/profile.toml").read_text("utf-8"))
    fixture = json.loads((root / "evals/sensei/turncare-paraphrased.json").read_text("utf-8"))
    cases: list[dict[str, Any]] = []
    for turn in fixture["turns"]:
        if turn["in_primary_scope"] and turn["addressee"] == "candidate":
            cases.append(
                {
                    "id": "history-interview-" + str(turn["id"]),
                    "question": turn["question"],
                    "type": turn["type"],
                    "mode": "interview",
                    "digest": profile["digest"],
                    "recent": "",
                    "memory": [],
                    "depth": None,
                    "refs": profile["refs"],
                    "source": "historical question paraphrase; fixed fictional profile",
                }
            )
    standup = tomllib.loads((root / "evals/sessions/standup_review.toml").read_text("utf-8"))
    memory = [item["title"] + ": " + item["summary"] for item in standup["item"]]
    recent: list[str] = []
    for index, line in enumerate(standup["line"]):
        if line.get("expect") == "answer" and line.get("addressee") == "me":
            cases.append(
                {
                    "id": "history-standup-" + str(index),
                    "question": line["text"],
                    "type": line.get("type", "other"),
                    "mode": "standup",
                    "digest": "Alex is the backend engineer responsible for these reports.",
                    "recent": "\n".join(recent[-8:]),
                    "memory": memory,
                    "depth": None,
                    "refs": profile["refs"],
                    "source": "historical standup paraphrase; fictional evidence and prior speech",
                }
            )
        recent.append(("Alex" if line["speaker"] == "me" else "Dana") + ": " + line["text"])
    extra = [
        ("simple-known", "Which programming languages do you use?", "technical", profile["digest"]),
        ("simple-location", "Where are you based?", "other", "Alex is based in Chicago."),
        ("intro", "Tell me about yourself.", "behavioral", profile["digest"]),
        ("redis", "Why did you choose Redis for rate limiting?", "technical", profile["digest"]),
        (
            "incident",
            "Describe a production incident you handled.",
            "behavioral",
            profile["digest"],
        ),
        (
            "missing-story",
            "Tell me about a time you handled a major database outage.",
            "behavioral",
            "Alex is a software engineer. No incident history has been supplied.",
        ),
        (
            "design-notifications",
            "Design a distributed notification system with email, SMS, retries,"
            " and per-user preferences. Explain failure handling and trade-offs.",
            "system_design",
            profile["digest"],
        ),
        (
            "design-orders",
            "Design an order-processing system that prevents duplicate charges"
            " when messages are delivered more than once.",
            "system_design",
            profile["digest"],
        ),
        (
            "debug",
            "How would you diagnose a p95 latency spike after a deployment when CPU is normal"
            " and database connections are exhausted?",
            "technical",
            profile["digest"],
        ),
        (
            "tradeoffs",
            "Compare Redis and PostgreSQL for a distributed rate limiter."
            " Explain consistency and failure trade-offs.",
            "technical",
            profile["digest"],
        ),
    ]
    for identifier, question, qtype, digest in extra:
        cases.append(
            {
                "id": "supplement-" + identifier,
                "question": question,
                "type": qtype,
                "mode": "interview",
                "digest": digest,
                "recent": "",
                "memory": [],
                "depth": None,
                "source": "explicit synthetic coverage case",
                "refs": profile["refs"],
            }
        )
    for case in cases:
        case["category"] = (
            "standup"
            if case["mode"] == "standup"
            else "simple"
            if case["id"].startswith("supplement-simple")
            else case["type"]
        )
    return validate_cases(cases)


def validate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Cases require distinct nonempty IDs")
    for case in cases:
        if set(case) - CASE_FIELDS:
            raise ValueError("Unrecognized case fields; historical answer leakage is not allowed")
        if not all(
            isinstance(case.get(key), str)
            for key in ("id", "question", "type", "mode", "digest", "recent", "source")
        ):
            raise ValueError("Cases require explicit question and source context strings")
        if case["mode"] not in ("interview", "standup", "meeting"):
            raise ValueError("Unsupported replay mode")
        if not isinstance(case.get("memory"), list) or any(
            not isinstance(line, str) for line in case["memory"]
        ):
            raise ValueError("Memory must contain source text lines")
        digest = hashlib.sha256(
            json.dumps(
                {key: case[key] for key in ("digest", "recent", "memory")}, sort_keys=True
            ).encode()
        ).hexdigest()
        if case.get("context_sha256") not in (None, digest):
            raise ValueError("Source context fingerprint changed")
        case["context_sha256"] = digest
        case.setdefault("category", case["type"])
    return cases


class Host:
    def __init__(self, settings: Settings, router: ModelRouter, case: dict[str, Any]):
        self.settings, self.router, self.case = settings, router, case
        self.events: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def emit(self, cls: type[Any], **fields: Any) -> int:
        kind = get_args(cls.model_fields["type"].annotation)[0]
        message = cls(type=kind, seq=len(self.events) + 1, **fields)
        self.events.append(
            {
                "at": time.monotonic() - self.started,
                "message": message.model_dump(by_alias=True, mode="json"),
            }
        )
        return len(self.events)

    def log_entry(self, entry: Mapping[str, Any]) -> None:
        self.logs.append(dict(entry))

    def store_text(self) -> bool:
        return True

    def call_context(self) -> prompts.CallContext:
        return prompts.CallContext(recent=self.case["recent"])

    def prompt_context(self) -> prompts.PromptContext:
        return prompts.PromptContext("Alex Rivera", self.case["mode"], self.case["digest"], None)

    def refs(self) -> dict[str, NodeRef]:
        return {
            key: NodeRef(node_id=uuid5(NAMESPACE_URL, key), label=value)
            for key, value in self.case.get("refs", {}).items()
        }

    def llm_status(self, ok: bool) -> None:
        pass

    def cache_version(self) -> str:
        return "controlled-generation-eval"

    def answer_style(self) -> str:
        return "bullets"

    def response_length(self) -> str:
        return "default"

    def cache_extra(self) -> dict[str, str]:
        return {}

    async def memory_lines(self, text: str, pass_: str) -> tuple[list[str], dict[str, int]]:
        return list(self.case["memory"]), {
            "memory_wait_ms": 0,
            "memory_hits": len(self.case["memory"]),
        }

    async def qa_get(self, qhash: str) -> dict[str, Any] | None:
        return None

    async def qa_put(self, qhash: str, value: Mapping[str, Any]) -> None:
        pass


async def trial(
    case: dict[str, Any],
    strategy: str,
    rep: int,
    settings: Settings,
    router: ModelRouter,
    timeout_s: float,
) -> dict[str, Any]:
    host = Host(settings, router, case)
    engine = AnswerEngine(host)
    question = Question(
        qid=uuid4(),
        text=case["question"],
        qtype=case["type"],
        confidence=1,
        addressed=True,
        speculative=False,
        forced=False,
        t0=time.monotonic(),
        detector_ms=None,
        match=None,
        depth=case.get("depth"),
    )
    host.started = question.t0
    # This is an injected preclassified boundary, not a measured detector result.
    host.emit(
        models.QuestionDetected,
        question_id=question.qid,
        text=question.text,
        question_type=question.qtype,
        confidence=1,
        speculative=False,
    )
    failure: str | None = None
    try:
        engine.add(question)
        engine.confirm(question.qid, question.text, 0)
        async with asyncio.timeout(timeout_s):
            while True:
                await engine.wait_idle()
                await asyncio.sleep(0)
                if all(task.done() for task in question.tasks.values()):
                    break
    except Exception as error:
        failure = type(error).__name__
    finally:
        engine.cancel_all()
        await engine.wait_idle()
    return {
        "id": case["id"],
        "architecture": strategy,
        "rep": rep,
        "question": case["question"],
        "question_id": str(question.qid),
        "type": case["type"],
        "category": case["category"],
        "mode": case["mode"],
        "context_sha256": case["context_sha256"],
        "events": host.events,
        "metadata_logs": host.logs,
        "failure": failure
        or next(
            (
                event["message"]["code"]
                for event in host.events
                if event["message"]["type"] == "answer.error"
            ),
            None,
        ),
        "elapsed_s": time.monotonic() - host.started,
    }


async def preflight(cases: list[dict[str, Any]]) -> None:
    from copilot_gateway.testing import Reply, ScriptedProvider

    for strategy in STRATEGIES.values():
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            llm_providers=["openai"],
            answer_architecture=strategy,  # pyright: ignore[reportArgumentType]
            openai_detector_model="offline-fast",
            openai_fast_model="offline-fast",
            openai_deep_model="offline-expert",
            openai_summary_model="offline-fast",
            openai_detector_reasoning="none",
            openai_fast_reasoning="none",
            openai_deep_reasoning="low",
            openai_summary_reasoning="none",
        )
        provider = ScriptedProvider(
            "openai", lambda job, request: Reply(["A direct answer. ", "Useful context."])
        )
        result = await trial(
            cases[0], strategy, 0, settings, build_router(settings, [provider]), 10
        )
        messages = [event["message"] for event in result["events"]]
        if result["failure"] or not any(message["type"] == "answer.done" for message in messages):
            raise ValueError("Offline host/protocol preflight failed")
        if [message["seq"] for message in messages] != list(range(1, len(messages) + 1)):
            raise ValueError("Offline preflight found invalid event sequence")


class BudgetBlocked(ValueError):
    """The dispatch guard rejected a request; this is not a provider failure."""


class AuditBudget(Budget):
    def __init__(self, directory: Path, settings: Any, limit: float):
        super().__init__(directory, settings, limit)
        self.blocked_trials: set[str] = set()
        self.blocked = False

    def reserve(self, model: str, payload: Any, output: int, tier: str = "standard") -> int:
        try:
            ticket = super().reserve(model, payload, output, tier)
        except ValueError as error:
            if str(error) == "Benchmark budget exhausted before dispatch":
                self.blocked = True
                self.blocked_trials.add(TRIAL.get())
                raise BudgetBlocked("Budget prevented dispatch") from error
            raise
        self.entries[ticket].update(trial_id=TRIAL.get(), purpose="answer")
        save(self.path, self.entries)
        return ticket


class AuditProvider(MeteredProvider):
    def __init__(
        self, provider: Any, budget: AuditBudget, requests: list[dict[str, Any]], directory: Path
    ):
        super().__init__(provider, budget)
        self.requests, self.directory = requests, directory

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        started = time.monotonic()
        record: dict[str, Any] = {
            "started_epoch_s": time.time(),
            "trial_id": TRIAL.get(),
            "model": model,
            "request_sha256": fingerprint(asdict(req)),
            "params_sha256": fingerprint(dict(params)),
            "output_limit": req.max_tokens,
            "status": "started",
        }
        self.requests.append(record)
        save(self.directory / "request-identities.json", self.requests)
        try:
            async for chunk in super().stream(model, req, params):
                if isinstance(chunk, LLMDone):
                    record["status"] = "completed"
                yield chunk
            record["status"] = "completed"
        except BaseException as error:
            if record["status"] != "completed":
                record["status"] = type(error).__name__
                causes: list[str] = []
                current: BaseException | None = error
                while current is not None:
                    causes.append(type(current).__name__)
                    current = current.__cause__
                record["error_types"] = causes
            raise
        finally:
            record["elapsed_s"] = time.monotonic() - started
            save(self.directory / "request-identities.json", self.requests)


def experiment_identity(
    cases: list[dict[str, Any]],
    settings: Settings,
    reps: int,
    seed: int,
    concurrency: int,
    timeout_s: float,
) -> dict[str, Any]:
    root = root_directory()
    code = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in (
            "apps/gateway/src",
            "packages/core/src",
            "packages/protocol/python",
            "evals/src",
        )
        for path in sorted((root / folder).rglob("*.py"))
    }
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--",
            "apps/gateway",
            "packages/core",
            "packages/protocol",
            "evals",
        ],
        cwd=root,
        capture_output=True,
        check=True,
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True)
    return {
        "schema": 1,
        "scope": "generation-only; preclassified questions; fixed source memory",
        "origin": "confirmed_question_generation_start",
        "case_sha256": fingerprint(cases),
        "repetitions": reps,
        "seed": seed,
        "concurrency": concurrency,
        "timeout_s": timeout_s,
        "strategies": STRATEGIES,
        "settings": settings.model_dump(mode="json", include=CONFIG_FIELDS),
        "code": code,
        "git_head": head.stdout.decode().strip(),
        "git_diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
        "limitations": [
            "Paraphrases and fictional evidence are not original historical sessions.",
            "No STT, detector, live retrieval, network-display or UI latency measurement.",
        ],
    }


async def execute(args: argparse.Namespace) -> None:
    cases = (
        validate_cases(json.loads(args.cases.read_text("utf-8"))) if args.cases else load_cases()
    )
    await preflight(cases)
    if not args.live:
        print(
            json.dumps(
                {"status": "offline_preflight_passed", "cases": len(cases), "provider_calls": 0}
            )
        )
        return
    if (
        not 1 <= args.reps <= 10
        or not 1 <= args.concurrency <= 4
        or not math.isfinite(args.max_cost)
        or not math.isfinite(args.timeout)
        or args.max_cost <= 0
        or args.timeout <= 0
    ):
        raise ValueError("Invalid finite replay bounds")
    directory = private_directory(args.out)
    settings = Settings(
        openai_detector_retry_tier=None, openai_fast_retry_tier=None, openai_deep_retry_tier=None
    )
    identity = experiment_identity(
        cases, settings, args.reps, args.seed, args.concurrency, args.timeout
    )
    identity["budget_limit_usd"] = args.max_cost
    identity_path = directory / "identity.json"
    if identity_path.exists() and json.loads(identity_path.read_text("utf-8")) != identity:
        raise ValueError("Run code, configuration, or source changed; use a fresh private output")
    save(identity_path, identity)
    save(directory / "budget-limit.json", args.max_cost)
    save(directory / "cases.json", cases)
    budget = AuditBudget(directory, settings, limit=args.max_cost)
    initial_cost = budget.total
    request_path = directory / "request-identities.json"
    requests: list[dict[str, Any]] = (
        json.loads(request_path.read_text("utf-8")) if request_path.exists() else []
    )
    providers = [
        AuditProvider(provider, budget, requests, directory)
        for provider in make_providers(settings, timeout_s=args.timeout)
    ]
    routers = {
        strategy: build_router(
            settings.model_copy(update={"answer_architecture": strategy}), providers
        )
        for strategy in STRATEGIES.values()
    }
    planned = [
        (case, strategy, rep)
        for rep in range(args.reps)
        for case in cases
        for strategy in STRATEGIES.values()
    ]
    random.Random(args.seed).shuffle(planned)
    target = directory / "trials.json"
    results: list[dict[str, Any]] = json.loads(target.read_text("utf-8")) if target.exists() else []
    have = {(result["id"], result["architecture"], result["rep"]) for result in results}
    if len(have) != len(results):
        raise ValueError("Duplicate completed trial identity")
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_one(case: dict[str, Any], strategy: str, rep: int) -> None:
        if (case["id"], strategy, rep) in have:
            return
        async with semaphore:
            if budget.blocked:
                return
            trial_id = f"{case['id']}:{strategy}:{rep}"
            token = TRIAL.set(f"{case['id']}:{strategy}:{rep}")
            try:
                result = await trial(
                    case,
                    strategy,
                    rep,
                    settings.model_copy(update={"answer_architecture": strategy}),
                    routers[strategy],
                    args.timeout,
                )
            finally:
                TRIAL.reset(token)
            result["status"] = (
                "budget_blocked"
                if trial_id in budget.blocked_trials
                else "provider_or_engine_failure"
                if result["failure"]
                else "completed"
            )
            result["request_attempts"] = sum(r["trial_id"] == trial_id for r in requests)
            results.append(result)
            save(target, results)
            save(
                directory / "progress.json",
                {
                    "completed": len(results),
                    "total": len(planned),
                    "cost_start_usd": initial_cost,
                    "cost_upper_usd": budget.total,
                    "failures": sum(
                        row.get("status") == "provider_or_engine_failure" for row in results
                    ),
                    "budget_blocked_trials": sum(
                        row.get("status") == "budget_blocked" for row in results
                    ),
                    "not_started": len(planned) - len(results),
                },
            )

    await asyncio.gather(*(run_one(case, strategy, rep) for case, strategy, rep in planned))
    completed_ids = {(r["id"], r["architecture"], r["rep"]) for r in results}
    save(
        directory / "not-started.json",
        [
            {
                "id": case["id"],
                "architecture": strategy,
                "rep": rep,
                "reason": "budget_blocked" if budget.blocked else "not_run",
            }
            for case, strategy, rep in planned
            if (case["id"], strategy, rep) not in completed_ids
        ],
    )
    save(
        directory / "accounting.json",
        {
            "budget_limit_usd": args.max_cost,
            "cost_start_usd": initial_cost,
            "cost_upper_usd": budget.total,
            "usage_priced_usd": sum(
                e["charged_usd"] for e in budget.entries if e["status"] == "usage"
            ),
            "unresolved_reserved_usd": sum(
                e["charged_usd"] for e in budget.entries if e["status"] == "reserved"
            ),
            "failed_trials": sum(
                row.get("status") == "provider_or_engine_failure" for row in results
            ),
            "budget_blocked_trials": sum(row.get("status") == "budget_blocked" for row in results),
            "not_started": len(planned) - len(results),
            "completed_trials": len(results),
            "planned_trials": len(planned),
        },
    )
    print(
        json.dumps(
            {
                "status": "partial_budget" if budget.blocked else "complete",
                "trials": len(results),
                "cost_upper_usd": budget.total,
            }
        )
    )


def normalized_runs(
    trials: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    identity: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> list[Run]:
    """Adapt current generation-only trials without changing the private source artifacts."""
    by_case = {case["id"]: case for case in cases}
    ids = {(row["id"], row["architecture"], row["rep"]) for row in trials}
    if len(ids) != len(trials):
        raise ValueError("Duplicate trial identity")
    if any(row["architecture"] not in STRATEGIES.values() for row in trials):
        raise ValueError("Unknown trial architecture")
    runs: list[Run] = []
    for label, strategy in STRATEGIES.items():
        events: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        sent: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []
        selected = sorted(
            (item for item in trials if item["architecture"] == strategy),
            key=lambda item: (item["id"], item["rep"]),
        )
        for index, item in enumerate(selected):
            case = by_case[item["id"]]
            if (
                item["question"] != case["question"]
                or item["context_sha256"] != case["context_sha256"]
            ):
                raise ValueError("Trial question or context does not match frozen case")
            identifier, origin = f"{item['id']}:r{item['rep']}", index * 1000.0
            qid = item.get("question_id") or next(
                (
                    event["message"].get("question_id")
                    for event in item["events"]
                    if event["message"].get("question_id")
                ),
                None,
            )
            turns.append(
                {
                    "id": identifier,
                    "original_id": item["id"],
                    "rep": item["rep"],
                    "question": case["question"],
                    "type": case["type"],
                    "category": case["category"],
                    "needs_answer": True,
                    "mode": case["mode"],
                    "failure": item.get("failure"),
                    "status": item.get("status"),
                    "evidence": {
                        key: case[key] for key in ("digest", "recent", "memory", "source")
                    },
                }
            )
            sent.append({"id": identifier, "at": origin})
            if qid and not any(
                event["message"]["type"] == "question.detected" for event in item["events"]
            ):
                events.append(
                    {
                        "at": origin,
                        "message": {
                            "type": "question.detected",
                            "question_id": qid,
                            "text": case["question"],
                            "question_type": case["type"],
                            "speculative": False,
                        },
                    }
                )
            events.extend(
                {"at": origin + event["at"], "message": event["message"]}
                for event in item["events"]
            )
            logs.extend(item["metadata_logs"])
        replay = {
            "sent": sent,
            "events": events,
            "metadata_logs": logs,
            "speed": 1,
            "origin": "confirmed_question_generation_start",
            "generation_only": True,
        }
        rows = collect_answers(replay, turns)
        runs.append(
            Run(
                label,
                rows,
                replay,
                {**identity, "answer_architecture": strategy},
                {"case_sha256": fingerprint(cases)},
                {"authorized_cases": cases},
                [entry for entry in ledger if entry["trial_id"].rsplit(":", 2)[1] == strategy]
                if ledger and all(entry.get("trial_id") for entry in ledger)
                else ledger,
            )
        )
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument(
        "--live", action="store_true", help="Enable billed providers after offline preflight"
    )
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-cost", type=float, default=2.50)
    parser.add_argument("--timeout", type=float, default=60)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(execute(parser.parse_args(argv)))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
