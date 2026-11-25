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
import subprocess
import time
import tomllib
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, get_args
from uuid import NAMESPACE_URL, uuid4, uuid5

from copilot_core import prompts
from copilot_core.llm import AttemptEvent, LLMDone, LLMRequest, ModelRouter, Route, error_chain
from copilot_core.packs import NodeRef
from copilot_core.providers import close_providers, make_providers
from copilot_evals.live_answer_artifacts import DispatchJournal, run_lock, source_archive
from copilot_evals.live_answer_eval import Run, fingerprint
from copilot_evals.live_answer_variants import (
    EvaluationEngine,
    Variant,
    load_rate_card,
    load_variants,
    validate_route_prices,
)
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
    "answer_style",
    "response_length",
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
    "llm_connect_timeout_s",
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

    def prepared_source_version(self, policy: str) -> str | None:
        return None  # Generation-only cases have no prepared answer cache.

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
        return self.case.get("answer_style", "bullets")

    def response_length(self) -> str:
        return self.case.get("response_length", "default")

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
    prompt_rules: str | None = None,
) -> dict[str, Any]:
    host = Host(settings, router, case)
    engine = (
        EvaluationEngine(host, prompt_rules) if prompt_rules is not None else AnswerEngine(host)
    )
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
        question_type=prompts.WIRE_TYPES.get(question.qtype, question.qtype),
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


async def preflight(
    cases: list[dict[str, Any]], variants: dict[str, Variant] | None = None
) -> None:
    from copilot_gateway.testing import Reply, ScriptedProvider

    coverage = {
        (
            case["type"],
            case["mode"],
            case.get("depth"),
            case.get("answer_style"),
            case.get("response_length"),
        ): case
        for case in cases
    }
    for strategy in variants if variants else STRATEGIES.values():
        settings = (
            variants[strategy].settings
            if variants
            else Settings(
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
        )
        provider = ScriptedProvider(
            "openai", lambda job, request: Reply(["A direct answer. ", "Useful context."])
        )
        for case in coverage.values():
            result = await trial(
                case,
                strategy,
                0,
                settings,
                build_router(settings, [provider]),
                10,
                variants[strategy].prompt_rules if variants else None,
            )
            messages = [event["message"] for event in result["events"]]
            if result["failure"] or not any(
                message["type"] == "answer.done" for message in messages
            ):
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


class AdmissionBlocked(Exception):
    """A local circuit prevented dispatch; no provider request or reservation was made."""


class ProviderAdmission:
    """One experiment-wide gate, shared by every provider, route, hedge and retry."""

    def __init__(self, limit: int, on_stop: Callable[[str], None] | None = None):
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("Provider inflight limit must be between one and 32")
        self.limit, self.on_stop = limit, on_stop
        self.semaphore = asyncio.Semaphore(limit)
        self.active = self.peak = self.waiting = self.admitted = self.blocked = 0
        self.reason: str | None = None

    def stop(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
            if self.on_stop is not None:
                self.on_stop(reason)

    def observe_error(self, error: BaseException) -> None:
        status = getattr(error, "status_code", None)
        names = set(error_chain(error))
        if status == 429 or "RateLimitError" in names:
            self.stop("rate_limit")
        elif status in (401, 403) or names & {"AuthenticationError", "PermissionDeniedError"}:
            self.stop("authentication")
        elif (isinstance(status, int) and 400 <= status < 500 and status != 408) or names & {
            "BadRequestError",
            "NotFoundError",
            "UnprocessableEntityError",
        }:
            self.stop("request_configuration")

    async def enter(self) -> float:
        started = time.monotonic()
        if self.reason:
            self.blocked += 1
            raise AdmissionBlocked(self.reason)
        self.waiting += 1
        try:
            await self.semaphore.acquire()
        finally:
            self.waiting -= 1
        if self.reason:
            self.semaphore.release()
            self.blocked += 1
            raise AdmissionBlocked(self.reason)
        self.active += 1
        self.admitted += 1
        self.peak = max(self.peak, self.active)
        return (time.monotonic() - started) * 1000

    def leave(self) -> None:
        self.active -= 1
        self.semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "active": self.active,
            "peak_active": self.peak,
            "waiting": self.waiting,
            "admitted": self.admitted,
            "blocked_before_reservation": self.blocked,
            "stop_reason": self.reason,
            "scope": "shared across all providers, variants, retry and hedge attempts",
            "queue_timing": "admission_wait_ms is local backpressure, included in route deadlines",
        }


async def bounded_blocks(
    blocks: list[list[tuple[dict[str, Any], str, int]]],
    concurrency: int,
    run_one: Callable[[dict[str, Any], str, int], Awaitable[None]],
    stopped: Callable[[], bool],
) -> None:
    """Keep only a fixed number of workers, with each case's conditions consecutive."""
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("Trial concurrency must be between one and eight")
    pending = iter(blocks)

    async def worker() -> None:
        for block in pending:
            for case, strategy, rep in block:
                if stopped():
                    return
                await run_one(case, strategy, rep)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(blocks))):
            group.create_task(worker())


class AuditProvider(MeteredProvider):
    def __init__(
        self,
        provider: Any,
        budget: AuditBudget,
        requests: list[dict[str, Any]],
        directory: Path,
        *,
        admission: ProviderAdmission | None = None,
    ):
        super().__init__(provider, budget)
        self.requests, self.directory = requests, directory
        self.admission = admission

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
            "status": "queued" if self.admission is not None else "started",
            "dispatch_admitted": False,
        }
        admitted = False
        self.requests.append(record)
        save(self.directory / "request-identities.json", self.requests)
        try:
            record["admission_wait_ms"] = await self.admission.enter() if self.admission else 0.0
            admitted = True
            record.update(status="started", dispatch_admitted=True, admitted_epoch_s=time.time())
            save(self.directory / "request-identities.json", self.requests)
            ticket = self.budget.reserve(
                model,
                asdict(req),
                req.max_tokens + int(params.get("max_output_tokens_extra", 0)),
                str(params.get("service_tier", "standard")),
            )
            try:
                # Hold the slot until the actual provider stream closes, including cancellation.
                async with aclosing(self.inner.stream(model, req, params)) as stream:
                    async for chunk in stream:
                        if isinstance(chunk, LLMDone):
                            self.budget.finish(ticket, chunk)
                            record["status"] = "completed"
                        yield chunk
            except Exception as error:
                self.connection_failure(ticket, error)
                raise
            record["status"] = "completed"
        except BaseException as error:
            if self.admission is not None:
                self.admission.observe_error(error)
                if isinstance(error, BudgetBlocked):
                    self.admission.stop("budget_blocked")
            if isinstance(error, AdmissionBlocked):
                record["admission_blocked_reason"] = str(error)
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
            record.setdefault("admission_wait_ms", (time.monotonic() - started) * 1000)
            if admitted and self.admission is not None:
                self.admission.leave()
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


async def connectivity_check(settings: Settings) -> dict[str, Any]:
    """Verify transport availability without credentials or billed inference."""
    import httpx2

    if "openai" not in settings.llm_providers:
        return {"ready": True, "checked": False, "reason": "provider probe unavailable"}
    started = time.monotonic()
    base = settings.openai_base_url or "https://api.openai.com/v1"
    try:
        async with httpx2.AsyncClient(timeout=5.0, http2=settings.openai_http2) as client:
            response = await client.get(base.rstrip("/") + "/models")
        return {
            "ready": response.status_code < 500 and response.status_code not in (408, 429),
            "checked": True,
            "http_status": response.status_code,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "scope": "unauthenticated transport probe; credentials/model access remain unverified",
        }
    except Exception as error:
        return {
            "ready": False,
            "checked": True,
            "error_type": type(error).__name__,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


def budget_limit(value: str) -> float | None:
    """An explicit 'none' opts into a finite schedule without a currency cutoff."""
    if value.casefold() == "none":
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("Budget must be positive finite dollars or 'none'")
    return parsed


def settings_overrides(args: argparse.Namespace) -> dict[str, Any]:
    fields = {
        "fast_model": "openai_fast_model",
        "expert_model": "openai_deep_model",
        "fast_reasoning": "openai_fast_reasoning",
        "expert_reasoning": "openai_deep_reasoning",
    }
    return {
        field: value
        for arg, field in fields.items()
        if (value := getattr(args, arg, None)) is not None
    }


def tier_overrides(router: ModelRouter, args: Any) -> None:
    """Apply recorded eval-only tiers to both primary and retry routes, without extra retries."""
    for job, arg in (("fast", "fast_tier"), ("deep_light", "fast_tier"), ("deep", "expert_tier")):
        tier = getattr(args, arg, None)
        if tier is None:
            continue
        routes: list[Route] = []
        for route in router.routes[job]:
            if route.provider.name != "openai":
                routes.append(route)
                continue
            params = dict(route.params)
            if tier == "default":
                params.pop("service_tier", None)
            else:
                params["service_tier"] = tier
            routes.append(replace(route, params=params))
        router.routes = {**router.routes, job: routes}


async def execute(args: argparse.Namespace) -> None:
    from copilot_evals.live_answer_corpus import representative_cases

    cases = (
        validate_cases(json.loads(args.cases.read_text("utf-8"))) if args.cases else load_cases()
    )
    if not args.cases and getattr(args, "corpus", "original") == "representative":
        cases = validate_cases(representative_cases(cases, root_directory()))
    for case in cases:
        case["answer_style"] = getattr(args, "answer_style", "bullets")
        case["response_length"] = getattr(args, "response_length", "default")
    settings = Settings(
        openai_detector_retry_tier=None,
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        **settings_overrides(args),
    )
    rate_identity = None
    if getattr(args, "rate_card", None):
        settings, rate_identity = load_rate_card(settings, args.rate_card)
    custom_variants: dict[str, Variant] | None = None
    if getattr(args, "variants", None):
        custom_variants = load_variants(args.variants, settings, root_directory())
        for variant in custom_variants.values():
            variant.fast_tier = variant.fast_tier or getattr(args, "fast_tier", None)
            variant.expert_tier = variant.expert_tier or getattr(args, "expert_tier", None)
    await preflight(cases, custom_variants)
    if getattr(args, "write_cases", None):
        target = args.write_cases.resolve()
        private_directory(target.parent)
        if target.exists():
            raise ValueError("Refusing to overwrite frozen cases")
        save(target, cases)
    if not args.live:
        print(
            json.dumps(
                {"status": "offline_preflight_passed", "cases": len(cases), "provider_calls": 0}
            )
        )
        return
    if (
        not 1 <= args.reps <= 10
        or not 1 <= args.concurrency <= 8
        or not 1 <= getattr(args, "max_provider_inflight", 8) <= 32
        or (args.max_cost is not None and (not math.isfinite(args.max_cost) or args.max_cost <= 0))
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise ValueError("Invalid finite replay bounds")
    directory = private_directory(args.out)
    with run_lock(directory):
        await execute_live(args, cases, settings, rate_identity, custom_variants, directory)


async def execute_live(
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    settings: Settings,
    rate_identity: dict[str, Any] | None,
    custom_variants: dict[str, Variant] | None,
    directory: Path,
) -> None:
    from copilot_evals.live_answer_schedule import FailureCircuit, case_blocks, classify_failure

    labels = getattr(args, "architectures", ["A", "D"])
    if (
        not labels
        or len(set(labels)) != len(labels)
        or any(label not in STRATEGIES for label in labels)
    ):
        raise ValueError("Select distinct architecture labels")
    conditions = custom_variants or {
        STRATEGIES[label]: Variant(
            label,
            STRATEGIES[label],
            settings.model_copy(update={"answer_architecture": STRATEGIES[label]}),
            fast_tier=getattr(args, "fast_tier", None),
            expert_tier=getattr(args, "expert_tier", None),
        )
        for label in labels
    }
    strategies = {variant.label: condition for condition, variant in conditions.items()}
    circuit = FailureCircuit(getattr(args, "transport_failure_limit", 3))
    if not 1 <= circuit.threshold <= 10:
        raise ValueError("Circuit threshold must be between one and ten")

    admission_stop_path = directory / "provider-admission-stop.json"

    def stop_dispatch(reason: str) -> None:
        if circuit.reason is None:
            circuit.reason = reason
        save(admission_stop_path, {"schema": 1, "reason": circuit.reason})

    admission = ProviderAdmission(getattr(args, "max_provider_inflight", 8), stop_dispatch)
    if admission_stop_path.exists():
        previous_stop = json.loads(admission_stop_path.read_text("utf-8"))
        if previous_stop.get("schema") != 1 or not isinstance(previous_stop.get("reason"), str):
            raise ValueError("Invalid persisted provider admission stop")
        admission.stop(previous_stop["reason"])
    blocks = case_blocks(cases, list(strategies.values()), args.reps, args.seed)
    planned = [item for block in blocks for item in block]
    identity = experiment_identity(
        cases, settings, args.reps, args.seed, args.concurrency, args.timeout
    )
    identity["rate_card"] = rate_identity
    identity["budget_limit_usd"] = args.max_cost
    identity["currency_cutoff_disabled"] = args.max_cost is None
    identity["provider_overrides"] = {
        **settings_overrides(args),
        "fast_tier": getattr(args, "fast_tier", None),
        "expert_tier": getattr(args, "expert_tier", None),
    }
    identity["strategies"] = strategies
    identity["variants"] = {key: value.identity(CONFIG_FIELDS) for key, value in conditions.items()}
    identity["variant_manifest_sha256"] = (
        fingerprint(json.loads(args.variants.read_text("utf-8")))
        if getattr(args, "variants", None)
        else None
    )
    identity["order_design"] = "counterbalanced_case_blocks"
    identity["transport_failure_limit"] = circuit.threshold
    identity["max_provider_inflight"] = admission.limit
    identity["provider_admission_policy"] = "bounded-v1-stop-on-rate-auth-config"
    identity["connectivity_check"] = not getattr(args, "skip_connectivity_check", False)
    identity["answer_style"] = getattr(args, "answer_style", "bullets")
    identity["response_length"] = getattr(args, "response_length", "default")
    identity_path = directory / "identity.json"
    if identity_path.exists():
        previous_identity = json.loads(identity_path.read_text("utf-8"))
        if not previous_identity.get("source_archive"):
            raise ValueError(
                "Legacy run lacks a source archive; preserve it and use a fresh output"
            )
        if {
            key: value for key, value in previous_identity.items() if key != "source_archive"
        } != identity:
            raise ValueError(
                "Run code, configuration, or source changed; use a fresh private output"
            )
    identity["source_archive"] = source_archive(
        directory,
        root_directory(),
        identity["code"],
        identity,
        {
            f"{label}:{kind}": text
            for label, variant in conditions.items()
            for kind, text in (variant.prompt_sources or {}).items()
        },
    )
    if identity_path.exists() and json.loads(identity_path.read_text("utf-8")) != identity:
        raise ValueError("Archived source or dependencies changed; use a fresh private output")
    save(identity_path, identity)
    save(directory / "budget-limit.json", args.max_cost)
    save(directory / "cases.json", cases)
    save(
        directory / "schedule.json",
        [
            [
                {"id": case["id"], "architecture": strategy, "rep": rep}
                for case, strategy, rep in block
            ]
            for block in blocks
        ],
    )
    budget = AuditBudget(
        directory, settings, limit=math.inf if args.max_cost is None else args.max_cost
    )
    initial_cost = budget.total
    request_path = directory / "request-identities.json"
    requests: list[dict[str, Any]] = (
        json.loads(request_path.read_text("utf-8")) if request_path.exists() else []
    )
    raw_providers = make_providers(settings, timeout_s=args.timeout)
    providers = [
        AuditProvider(provider, budget, requests, directory, admission=admission)
        for provider in raw_providers
    ]
    attempt_path = directory / "attempt-events.json"
    attempt_events: list[dict[str, Any]] = (
        json.loads(attempt_path.read_text("utf-8")) if attempt_path.exists() else []
    )

    def on_attempt(event: AttemptEvent) -> None:
        attempt_events.append({"trial_id": TRIAL.get(), **asdict(event)})
        save(attempt_path, attempt_events)

    routers = {
        strategy: build_router(
            conditions[strategy].settings,
            providers,
            on_attempt=on_attempt,
        )
        for strategy in strategies.values()
    }
    for condition, router in routers.items():
        tier_overrides(router, conditions[condition])
        conditions[condition].limit_routes(router)
        validate_route_prices(router, conditions[condition].settings)
    target = directory / "trials.json"
    results: list[dict[str, Any]] = json.loads(target.read_text("utf-8")) if target.exists() else []
    save(target, results)
    have = {(result["id"], result["architecture"], result["rep"]) for result in results}
    if len(have) != len(results):
        raise ValueError("Duplicate completed trial identity")
    journal = DispatchJournal(
        directory,
        [f"{case['id']}:{strategy}:{rep}" for case, strategy, rep in planned],
        {f"{identifier}:{strategy}:{rep}" for identifier, strategy, rep in have},
        {str(row["trial_id"]) for row in requests if row.get("trial_id")}
        | {str(row["trial_id"]) for row in budget.entries if row.get("trial_id")},
    )

    async def run_one(case: dict[str, Any], strategy: str, rep: int) -> None:
        if (case["id"], strategy, rep) in have:
            return
        if budget.blocked or circuit.reason:
            return
        trial_id = f"{case['id']}:{strategy}:{rep}"
        if not journal.start(trial_id):
            return
        token = TRIAL.set(f"{case['id']}:{strategy}:{rep}")
        try:
            result = await trial(
                case,
                strategy,
                rep,
                conditions[strategy].settings,
                routers[strategy],
                args.timeout,
                conditions[strategy].prompt_rules,
            )
        finally:
            TRIAL.reset(token)
        result["answer_architecture"] = conditions[strategy].architecture
        result["variant_label"] = conditions[strategy].label
        result["status"] = (
            "budget_blocked"
            if trial_id in budget.blocked_trials
            else "provider_or_engine_failure"
            if result["failure"]
            else "completed"
        )
        attempts = [record for record in requests if record["trial_id"] == trial_id]
        result["request_attempts"] = len(attempts)
        if attempts and all(not record.get("dispatch_admitted", True) for record in attempts):
            result["status"] = "admission_blocked"
            result["failure_kind"] = "admission_blocked"
        else:
            result["failure_kind"] = classify_failure(result, attempts)
            circuit.observe(result, attempts)
        if circuit.reason:
            admission.stop(circuit.reason)
        results.append(result)
        save(target, results)
        journal.finish(trial_id)
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
                "not_started": sum(state == "not_started" for state in journal.states.values()),
                "in_flight": sum(state == "in_flight" for state in journal.states.values()),
                "interrupted_outcome_unknown": sum(
                    state == "ambiguous" for state in journal.states.values()
                ),
                "circuit_reason": circuit.reason,
                "provider_admission": admission.snapshot(),
            },
        )

    if not getattr(args, "skip_connectivity_check", False):
        health = await connectivity_check(settings)
        save(directory / "connectivity.json", health)
        if not health["ready"]:
            circuit.reason = "connectivity_preflight"

    try:
        await bounded_blocks(
            blocks, args.concurrency, run_one, lambda: budget.blocked or bool(circuit.reason)
        )
    finally:
        journal.checkpoint()
        await close_providers(raw_providers)
    completed_ids = {(r["id"], r["architecture"], r["rep"]) for r in results}
    save(
        directory / "not-started.json",
        [
            {
                "id": case["id"],
                "architecture": strategy,
                "rep": rep,
                "status": "interrupted_outcome_unknown"
                if journal.states[f"{case['id']}:{strategy}:{rep}"] == "ambiguous"
                else "not_started",
                "reason": "prior_dispatch_unresolved"
                if journal.states[f"{case['id']}:{strategy}:{rep}"] == "ambiguous"
                else "budget_blocked"
                if budget.blocked
                else circuit.reason or "not_run",
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
                e["charged_usd"]
                for e in budget.entries
                if e["status"] in ("reserved", "usage_unpriced")
            ),
            "failed_trials": sum(
                row.get("status") == "provider_or_engine_failure" for row in results
            ),
            "budget_blocked_trials": sum(row.get("status") == "budget_blocked" for row in results),
            "not_started": sum(state == "not_started" for state in journal.states.values()),
            "completed_trials": len(results),
            "planned_trials": len(planned),
            "interrupted_outcome_unknown": sum(
                state == "ambiguous" for state in journal.states.values()
            ),
            "circuit_reason": circuit.reason,
            "successful_trials": sum(row.get("status") == "completed" for row in results),
            "admission_blocked_trials": sum(
                row.get("status") == "admission_blocked" for row in results
            ),
            "provider_admission": admission.snapshot(),
        },
    )
    print(
        json.dumps(
            {
                "status": "partial_budget"
                if budget.blocked
                else "partial_circuit"
                if circuit.reason
                else "partial_ambiguous"
                if any(state == "ambiguous" for state in journal.states.values())
                else "complete",
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
    not_started: list[dict[str, Any]] | None = None,
) -> list[Run]:
    """Adapt current generation-only trials without changing the private source artifacts."""
    from copilot_evals.live_answer_schedule import classify_failure

    by_case = {case["id"]: case for case in cases}
    ids = {(row["id"], row["architecture"], row["rep"]) for row in trials}
    if len(ids) != len(trials):
        raise ValueError("Duplicate trial identity")
    allowed = set(identity.get("strategies", STRATEGIES).values())
    if any(row["architecture"] not in allowed for row in trials):
        raise ValueError("Unknown trial architecture")
    pending: list[dict[str, Any]] = [
        {
            **item,
            "question": by_case[item["id"]]["question"],
            "context_sha256": by_case[item["id"]]["context_sha256"],
            "status": item.get("status", "not_started"),
            "events": [],
            "metadata_logs": [],
        }
        for item in (not_started or [])
    ]
    pending_ids = {(row["id"], row["architecture"], row["rep"]) for row in pending}
    if ids & pending_ids or len(pending_ids) != len(pending):
        raise ValueError("Started and not-started trial identities must be distinct")
    runs: list[Run] = []
    for label, strategy in identity.get("strategies", STRATEGIES).items():
        events: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        sent: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        seen_qids: set[str] = set()
        selected = sorted(
            (item for item in [*trials, *pending] if item["architecture"] == strategy),
            key=lambda item: (item["id"], item["rep"]),
        )
        for index, item in enumerate(selected):
            event_start = len(events)
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
            if qid is not None:
                if str(qid) in seen_qids:
                    raise ValueError("Question ID reused across independent trials")
                seen_qids.add(str(qid))
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
                    "failure_kind": item.get("failure_kind"),
                    "failure_kind_reclassified": (
                        "admission_blocked"
                        if item.get("status") == "admission_blocked"
                        else classify_failure(item, [])
                    ),
                    "not_started_reason": item.get("reason"),
                    "evidence": {
                        key: case[key] for key in ("digest", "recent", "memory", "source")
                    },
                }
            )
            if item.get("status") != "not_started":
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
            # Generation trials have explicit case/repetition ownership. Reconstruct each
            # independently; text matching across prior trials creates false ambiguity for
            # repeated questions and can mix cards when question texts are identical.
            rows.extend(
                collect_answers(
                    {
                        "sent": sent[-1:] if item.get("status") != "not_started" else [],
                        "events": events[event_start:],
                    },
                    turns[-1:],
                )
            )
        replay = {
            "sent": sent,
            "events": events,
            "metadata_logs": logs,
            "speed": 1,
            "origin": "confirmed_question_generation_start",
            "generation_only": True,
        }
        runs.append(
            Run(
                label,
                rows,
                replay,
                {
                    **identity,
                    "condition_id": strategy,
                    "answer_architecture": identity.get("variants", {})
                    .get(strategy, {})
                    .get("architecture", strategy),
                },
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
    parser.add_argument("--variants", type=Path, help="Explicit eval-only variant manifest")
    parser.add_argument(
        "--rate-card", type=Path, help="Dated rates with source URLs; merges configured rates"
    )
    parser.add_argument("--corpus", choices=("original", "representative"), default="original")
    parser.add_argument(
        "--write-cases", type=Path, help="Freeze the selected corpus without billed calls"
    )
    parser.add_argument("--architectures", nargs="+", choices=tuple(STRATEGIES), default=["A", "D"])
    parser.add_argument("--answer-style", choices=("bullets", "paragraph"), default="bullets")
    parser.add_argument(
        "--response-length", choices=("default", "concise", "detailed"), default="default"
    )
    parser.add_argument("--transport-failure-limit", type=int, default=3)
    parser.add_argument("--skip-connectivity-check", action="store_true")
    parser.add_argument(
        "--live", action="store_true", help="Enable billed providers after offline preflight"
    )
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--concurrency", type=int, default=2, help="Concurrent case blocks, 1-8")
    parser.add_argument(
        "--max-provider-inflight",
        type=int,
        default=8,
        help="Aggregate open provider streams across variants/retries/hedges, 1-32",
    )
    parser.add_argument(
        "--max-cost",
        type=budget_limit,
        default=2.50,
        help="Positive dollar cap, or explicit 'none' for a finite uncapped schedule",
    )
    parser.add_argument("--fast-model")
    parser.add_argument("--expert-model")
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    parser.add_argument("--fast-reasoning", choices=efforts)
    parser.add_argument("--expert-reasoning", choices=efforts)
    tiers = ("default", "priority", "fast", "flex")
    parser.add_argument("--fast-tier", choices=tiers)
    parser.add_argument("--expert-tier", choices=tiers)
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
