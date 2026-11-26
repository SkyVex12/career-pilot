"""Reproducible generation-only A/B/C/D replay, using the shipped AnswerEngine.

Default invocation performs an offline preflight only. --live explicitly enables the
configured providers with a finite schedule and optional reservation budget. Output must be private.
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
import os
import re
import signal
import subprocess
import time
import tomllib
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast, get_args
from urllib.request import getproxies
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx2

from copilot_core import prompts
from copilot_core.llm import (
    AttemptEvent,
    Decided,
    LLMDone,
    LLMRequest,
    ModelRouter,
    Route,
    error_chain,
)
from copilot_core.packs import NodeRef
from copilot_core.providers import close_providers, make_providers
from copilot_evals.live_answer_artifacts import DispatchJournal, source_archive
from copilot_evals.live_answer_async_checkpoints import (
    ASYNC_POLICY,
    AnchoredLoopLagMonitor,
    BufferedRecordStore,
    CheckpointUnavailable,
    DurableWriter,
    require_async_directory,
)
from copilot_evals.live_answer_checkpoints import (
    RecordDispatchJournal,
    RecordStore,
)
from copilot_evals.live_answer_diagnostics import (
    sanitized_exception_tree as sanitized_exception_tree,
)
from copilot_evals.live_answer_eval import Run, fingerprint
from copilot_evals.live_answer_variants import (
    EvaluationEngine,
    SyntheticFault,
    Variant,
    load_rate_card,
    load_variants,
    validate_route_prices,
)
from copilot_evals.sensei_judge import collect_answers
from copilot_evals.sensei_runtime import (
    Budget,
    MeteredProvider,
    budget_reporting,
    private_directory,
    save,
)
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
TRIAL_ORIGIN: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "live_eval_trial_origin", default=None
)
CONTEXT_TEXT_FIELDS = (
    "user_name",
    "spoken_language",
    "company",
    "role",
    "company_digest",
    "pack_text",
    "call_summary",
    "earlier_answers",
)
CONTEXT_SOURCE_FIELDS = ("pack_source", "earlier_answers_source")
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
    *CONTEXT_TEXT_FIELDS,
    *CONTEXT_SOURCE_FIELDS,
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
    "adaptive_expert_route",
    "adaptive_technical_legacy",
    "adaptive_failure_recovery",
    "adaptive_max_tokens",
    "adaptive_stream_idle_timeout_s",
    "adaptive_stream_total_timeout_s",
    "openai_first_token_timeout_ms",
    "openai_deep_first_token_timeout_ms",
    "openai_reasoning_headroom_tokens",
    "openai_http2",
    "openai_http2_cancel_fix",
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
        if ("answer_style" in case) != ("response_length" in case):
            raise ValueError("Response style and length must both be recorded or both absent")
        if "answer_style" in case and (
            case["answer_style"] not in ("bullets", "paragraph")
            or case["response_length"] not in ("default", "concise", "detailed")
        ):
            raise ValueError("Unsupported recorded response preference")
        if case["mode"] not in ("interview", "standup", "meeting"):
            raise ValueError("Unsupported replay mode")
        if not isinstance(case.get("memory"), list) or any(
            not isinstance(line, str) for line in case["memory"]
        ):
            raise ValueError("Memory must contain source text lines")
        for key in (*CONTEXT_TEXT_FIELDS, *CONTEXT_SOURCE_FIELDS):
            if key in case and not isinstance(case[key], str):
                raise ValueError(f"Optional context field {key} must be text; omit if unavailable")
        if case.get("earlier_answers") and case.get("earlier_answers_source") != "user_speech":
            raise ValueError(
                "Earlier answers must be declared original user speech, never generated answers"
            )
        if case.get("pack_text") and case.get("pack_source") != "source_facts_only":
            raise ValueError(
                "Static pack text must be declared source facts without prepared/generated answers"
            )
        if case.get("earlier_answers_source") not in (None, "user_speech"):
            raise ValueError("Unknown earlier-answer source declaration")
        if case.get("pack_source") not in (None, "source_facts_only"):
            raise ValueError("Unknown pack source declaration")
        # Absent optional fields leave legacy context hashes byte-for-byte unchanged.
        context = {key: case[key] for key in ("digest", "recent", "memory")}
        context.update(
            {
                key: case[key]
                for key in (*CONTEXT_TEXT_FIELDS, *CONTEXT_SOURCE_FIELDS)
                if key in case
            }
        )
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if case.get("context_sha256") not in (None, digest):
            raise ValueError("Source context fingerprint changed")
        case["context_sha256"] = digest
        case.setdefault("category", case["type"])
    return cases


def context_fidelity(case: dict[str, Any]) -> dict[str, Any]:
    """Describe declared controlled context; absence is not evidence that a fact is false."""
    workplace = case["mode"] in ("standup", "meeting")
    return {
        "scope": "generation_only_static_equal_evidence",
        "declared_fields": [key for key in CONTEXT_TEXT_FIELDS if key in case],
        "workplace_rules_enabled": workplace and bool(case.get("company")),
        "workplace_pack_missing": workplace and not bool(case.get("pack_text")),
        "workplace_company_digest_missing": workplace and not bool(case.get("company_digest")),
        "conversation_summary_missing": not bool(case.get("call_summary")),
        "earlier_user_speech_missing": not bool(case.get("earlier_answers")),
        "unconsumed_declared_fields": [
            key
            for key in ("company", "role", "company_digest")
            if case.get(key)
            and not (case.get("company") and case.get("mode") in prompts.GROUP_MODES)
        ],
        "retrieval_pass_invariant": True,
        "retrieval_wait_measured": False,
        "pack_source_declaration": case.get("pack_source"),
        "earlier_answers_source_declaration": case.get("earlier_answers_source"),
        "source_declarations_are_not_truth_verification": True,
    }


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
        return prompts.CallContext(
            recent=self.case["recent"],
            summary=self.case.get("call_summary", ""),
            answers=self.case.get("earlier_answers", ""),
        )

    def prompt_context(self) -> prompts.PromptContext:
        return prompts.PromptContext(
            user_name=self.case.get("user_name", "Alex Rivera"),
            mode=self.case["mode"],
            digest_text=self.case["digest"],
            pack_text=self.case.get("pack_text"),
            company=self.case.get("company"),
            role=self.case.get("role"),
            company_digest=self.case.get("company_digest"),
            spoken_language=self.case.get("spoken_language", "en"),
        )

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


class InjectedPreDispatchFailure:
    """Fail only an explicitly wrapped route; the audited provider is never invoked.

    Router attempt events still exercise real eligibility logic, but are tagged as
    synthetic in the result. They must never become observed provider outage/cost data.
    """

    def __init__(
        self,
        inner: Any,
        spec: SyntheticFault,
        variant: str,
        route_index: int,
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        self.inner, self.name = inner, inner.name
        self.spec, self.variant, self.route_index, self.record = spec, variant, route_index, record

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        origin = TRIAL_ORIGIN.get()
        started = time.monotonic()
        entry: dict[str, Any] = {
            "fault_id": str(uuid4()),
            "trial_id": TRIAL.get(),
            "variant": self.variant,
            "kind": self.spec.kind,
            "job": "fast",
            "route_index": self.route_index,
            "model": model,
            "request_sha256": fingerprint(asdict(req)),
            "params_sha256": fingerprint(dict(params)),
            "scheduled_delay_ms": self.spec.delay_ms,
            "started_epoch_s": time.time(),
            "status": "armed",
            "started_since_generation_ms": (started - origin) * 1000 if origin else None,
            "provider_dispatched": False,
            "api_cost_usd": 0.0,
            "scope": "synthetic pre-dispatch fault; no real provider latency/outage observation",
        }
        self.record(entry)
        try:
            if self.spec.delay_ms:
                await asyncio.sleep(self.spec.delay_ms / 1000)
            entry.update(status="injected", elapsed_ms=(time.monotonic() - started) * 1000)
            self.record(entry)
            raise httpx2.ConnectError("explicit synthetic pre-dispatch connection fault")
            yield ""  # pragma: no cover; async generator interface, never emits text
        except asyncio.CancelledError:
            entry.update(
                status="cancelled_before_injection", elapsed_ms=(time.monotonic() - started) * 1000
            )
            self.record(entry)
            raise


def apply_synthetic_fault(
    router: ModelRouter, variant: Variant, record: Callable[[dict[str, Any]], None]
) -> None:
    if variant.synthetic_fault is None:
        return
    if not router.routes.get("fast"):
        raise ValueError("Synthetic fault requires existing primary fast routes")
    # Copy the fast route list. A recovery route may otherwise share its provider/model.
    router.routes = {
        **router.routes,
        "fast": [
            replace(
                route,
                provider=InjectedPreDispatchFailure(
                    route.provider, variant.synthetic_fault, variant.label, index, record
                ),
            )
            for index, route in enumerate(router.routes["fast"])
        ],
    }


def annotate_synthetic_faults(
    result: dict[str, Any], variant: Variant, records: list[dict[str, Any]]
) -> None:
    if variant.synthetic_fault is None:
        return
    result["synthetic_fault_condition"] = asdict(variant.synthetic_fault)
    result["synthetic_faults"] = [dict(row) for row in records]
    for entry in result["metadata_logs"]:
        if entry.get("kind") == "provider_attempt" and entry.get("job") == "fast":
            entry["synthetic_fault"] = True
            entry["actual_provider_dispatched"] = False
    result["expected_injected_failure"] = (
        not variant.settings.adaptive_failure_recovery
        and result.get("failure") == "llm_unavailable"
        and bool(records)
        and all(row["status"] == "injected" for row in records)
    )


def real_failure_evidence(result: dict[str, Any]) -> dict[str, Any]:
    """Keep real recovery errors for circuits, excluding explicitly synthetic fast attempts."""
    return {
        **result,
        "failure": None if result.get("expected_injected_failure") else result.get("failure"),
        "metadata_logs": [
            entry for entry in result.get("metadata_logs", []) if not entry.get("synthetic_fault")
        ],
    }


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
    origin_token = TRIAL_ORIGIN.set(question.t0)
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
        TRIAL_ORIGIN.reset(origin_token)
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
        "context_fidelity": context_fidelity(case),
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
                adaptive_expert_route=None,
                adaptive_technical_legacy=False,
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
            router = build_router(settings, [provider])
            faults: dict[str, dict[str, Any]] = {}
            if variants:
                variant = variants[strategy]
                tier_overrides(router, variant)
                variant.limit_routes(router)
                apply_synthetic_fault(
                    router,
                    variant,
                    lambda row, faults=faults: faults.update({row["fault_id"]: row}),
                )
            result = await trial(
                case,
                strategy,
                0,
                settings,
                router,
                10,
                variants[strategy].prompt_rules if variants else None,
            )
            messages = [event["message"] for event in result["events"]]
            if variants:
                annotate_synthetic_faults(result, variants[strategy], list(faults.values()))
            if result.get("expected_injected_failure"):
                if any(message["type"] == "answer.done" for message in messages):
                    raise ValueError("Injected primary unexpectedly completed in offline preflight")
            elif result["failure"] or not any(
                message["type"] == "answer.done" for message in messages
            ):
                raise ValueError("Offline host/protocol preflight failed")
            if [message["seq"] for message in messages] != list(range(1, len(messages) + 1)):
                raise ValueError("Offline preflight found invalid event sequence")


class BudgetBlocked(ValueError):
    """The dispatch guard rejected a request; this is not a provider failure."""


class AuditBudget(Budget):
    def __init__(
        self, directory: Path, settings: Any, limit: float, *, store: RecordStore | None = None
    ):
        self.store = store
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
        return ticket

    def load_entries(self) -> list[dict[str, Any]]:
        return self.store.read("cost") if self.store is not None else super().load_entries()

    def checkpoint(self, ticket: int) -> None:
        self.entries[ticket].setdefault("trial_id", TRIAL.get())
        self.entries[ticket].setdefault("purpose", "answer")
        if self.store is not None:
            self.store.put("cost", str(ticket), self.entries[ticket])
        else:
            super().checkpoint(ticket)


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

    def check_open(self) -> None:
        """Recheck an existing admission after a durability await; do not acquire twice."""
        if self.reason is not None:
            raise AdmissionBlocked(self.reason)

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


class ReplayDrainTimeout(TimeoutError):
    """An owned task did not drain; no successful finalization may be claimed."""


def stop_policy(args: argparse.Namespace) -> dict[str, Any]:
    drain = getattr(args, "stop_drain_timeout", 30.0)
    if type(drain) not in (int, float) or not math.isfinite(drain) or not 0 < drain <= 300:
        raise ValueError("Stop drain timeout must be finite and between zero and 300 seconds")
    path = getattr(args, "stop_file", None)
    if path is not None and not isinstance(path, Path):
        raise ValueError("Stop file must be a filesystem path")
    return {
        "policy": "stop-admissions-bounded-drain-v1",
        "request_file": str(path.resolve()) if path is not None else "$out/stop.request",
        "poll_seconds": 0.25,
        "file_stat_timeout_seconds": 1.0,
        "file_stat_execution": "One off-loop stat; timeout stops admission and polling",
        "drain_timeout_seconds": float(drain),
        "cancel_grace_seconds": 5.0,
        "provider_close_timeout_seconds": 10.0,
        "signals": ["SIGINT", "SIGTERM"],
        "unresponsive_actor": "Fail finalization; retain ambiguity; require external supervision",
    }


class ExternalStop:
    """A payload-free stop file or supported signal requests admission stop, never retries."""

    FILE_STAT_TIMEOUT_S = 1.0

    def __init__(
        self,
        path: Path,
        on_stop: Callable[[str], None],
        record: Callable[[dict[str, Any]], None],
    ):
        self.path, self.on_stop, self.record = path, on_stop, record
        self.requested_epoch_s: float | None = None
        self.source: str | None = None
        self.poll_task: asyncio.Task[None] | None = None
        self.previous_handlers: dict[int, Any] = {}
        self.handlers: dict[int, Any] = {}
        self.drain: dict[str, Any] = {"status": "not_started", "workers_drained": True}
        self.signals_unavailable = False
        self.file_stat_failure: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "requested": self.source is not None,
            "source": self.source,
            "requested_epoch_s": self.requested_epoch_s,
            "request_file": str(self.path),
            "request_contents_read": False,
            "signals_unavailable": self.signals_unavailable,
            "file_stat_failure": self.file_stat_failure,
            "drain": self.drain,
        }

    def request(self, source: str) -> None:
        if self.source is not None:
            return
        self.source, self.requested_epoch_s = source, time.time()
        self.on_stop("external_stop" if source != "file_check_error" else "stop_control_error")
        self.record(self.snapshot())

    async def check_file(self) -> None:
        if self.source is not None:
            return
        try:
            # A slow filesystem must not block streaming. There is at most one
            # outstanding read-only stat; a timeout stops polling and admission.
            exists = await asyncio.wait_for(
                asyncio.to_thread(self.path.is_file), self.FILE_STAT_TIMEOUT_S
            )
            if exists:
                self.request("file")
        except (OSError, TimeoutError) as exc:
            self.file_stat_failure = "timeout" if isinstance(exc, TimeoutError) else "os_error"
            self.request("file_check_error")

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):

            def handler(_number: int, _frame: Any, name: str = sig.name) -> None:
                loop.call_soon_threadsafe(self.request, name)

            try:
                previous = signal.signal(sig, handler)
            except (ValueError, OSError):
                self.signals_unavailable = True
            else:
                self.previous_handlers[sig] = previous
                self.handlers[sig] = handler
        await self.check_file()
        if self.source is None:
            self.poll_task = asyncio.create_task(self._poll(), name="live-eval-stop-file")

    async def _poll(self) -> None:
        while self.source is None:
            await asyncio.sleep(0.25)
            await self.check_file()

    async def close(self) -> None:
        try:
            if self.poll_task is not None:
                self.poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.poll_task
        finally:
            for sig, previous in self.previous_handlers.items():
                if signal.getsignal(sig) is self.handlers[sig]:
                    signal.signal(sig, previous)


async def bounded_cleanup(
    work: Awaitable[None], timeout_s: float, cancel_grace_s: float = 5.0
) -> None:
    task = asyncio.ensure_future(work)
    cancelled = False
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        if not done:
            cancelled = True
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=cancel_grace_s)
            if not done:
                raise ReplayDrainTimeout(
                    "Owned cleanup ignored cancellation; finalization unconfirmed"
                )
            with suppress(asyncio.CancelledError):
                task.result()
            raise ReplayDrainTimeout("Owned cleanup exceeded its drain deadline")
        task.result()
    finally:
        if not task.done() and not cancelled:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=cancel_grace_s)
            if done:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()


async def drain_on_stop(
    work: Awaitable[None],
    stop_event: asyncio.Event,
    drain_timeout_s: float,
    cancel_grace_s: float = 5.0,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Graceful deadline then cooperative cancellation; never call unknown work complete."""
    state = state if state is not None else {}
    state.update(status="active", workers_drained=False)
    task = asyncio.ensure_future(work)
    stopped = asyncio.create_task(stop_event.wait())
    cancelled = False
    try:
        await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            done, _ = await asyncio.wait({task}, timeout=drain_timeout_s)
            if not done:
                cancelled = True
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=cancel_grace_s)
                if not done:
                    raise ReplayDrainTimeout(
                        "Owned trials ignored cancellation; outcomes ambiguous"
                    )
        if cancelled and task.cancelled():
            state.update(status="cancelled_after_grace", workers_drained=True)
            return state
        task.result()
        state.update(
            status="drained_after_stop" if stop_event.is_set() else "completed",
            workers_drained=True,
        )
        return state
    finally:
        state["workers_drained"] = task.done()
        if not task.done():
            state["status"] = "unconfirmed"
        if task.done() and not task.cancelled() and task.exception() is not None:
            state["status"] = "failed"
        stopped.cancel()
        with suppress(asyncio.CancelledError):
            await stopped
        if not task.done() and not cancelled:
            task.cancel()
            # Cancellation of the caller still owns a bounded cleanup observation.
            done, _ = await asyncio.wait({task}, timeout=cancel_grace_s)
            if done:
                state["workers_drained"] = True
                with suppress(asyncio.CancelledError, Exception):
                    task.result()


class ProgressSnapshots:
    """Non-authoritative live status must not cancel an otherwise durable paid trial."""

    def __init__(self, directory: Path):
        self.path = directory / "progress.json"
        self.failure_count = 0
        self.failures: list[dict[str, Any]] = []

    def _record(self, error: OSError, phase: str) -> None:
        self.failure_count += 1
        if len(self.failures) < 16:
            self.failures.append({"phase": phase, "error": sanitized_exception_tree(error)})

    def diagnostics(self) -> dict[str, Any]:
        return {
            "failures": self.failure_count,
            "samples": self.failures,
            "omitted_samples": self.failure_count - len(self.failures),
            "scope": "Progress snapshots only; authoritative checkpoint failures remain fatal",
        }

    def active(self, value: dict[str, Any]) -> None:
        try:
            save(self.path, value)
        except OSError as error:
            # Windows readers may temporarily deny atomic replacement. No live backoff.
            self._record(error, "active_workers")

    async def final(self, value: Callable[[], dict[str, Any]]) -> None:
        """Strict after provider drain; retry only short Windows replacement interference."""
        delays = (0.01, 0.03, 0.1)
        for attempt in range(len(delays) + 1):
            try:
                save(self.path, value())
                return
            except OSError as error:
                self._record(error, "drained")
                if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == len(delays):
                    raise
                await asyncio.sleep(delays[attempt])


def record_finalization_failure(directory: Path, error: BaseException) -> None:
    """Never write success progress from memory after a failed durable drain/export."""
    with suppress(OSError):
        save(
            directory / "finalization-failure.json",
            {
                "status": "finalization_failed",
                "finalized": False,
                "error_type": type(error).__name__,
                "scope": "Compatibility exports are not confirmed; inspect authoritative records",
            },
        )


class BufferedProgressSnapshots(ProgressSnapshots):
    def __init__(self, directory: Path, store: BufferedRecordStore):
        super().__init__(directory)
        self.buffered_store = store

    def active(self, value: Any) -> None:
        self.buffered_store.snapshot_file(
            "progress.json", {**value, "finalized": False}, best_effort=True
        )

    async def final(self, value: Callable[[], Any]) -> None:
        await super().final(lambda: {**value(), "finalized": True})

    def diagnostics(self) -> dict[str, Any]:
        return {
            **super().diagnostics(),
            "async_snapshot_errors": list(self.buffered_store.writer.snapshot_errors),
            "omitted_async_snapshot_errors": self.buffered_store.writer.omitted_snapshot_errors,
        }


def _reservation_tier(params: Mapping[str, Any]) -> str:
    """Normalize pricing aliases without changing the request sent to the provider."""
    tier = str(params.get("service_tier", "standard"))
    return "standard" if tier in ("default", "auto") else tier


class AuditProvider(MeteredProvider):
    def __init__(
        self,
        provider: Any,
        budget: AuditBudget,
        requests: list[dict[str, Any]],
        directory: Path,
        *,
        admission: ProviderAdmission | None = None,
        store: RecordStore | None = None,
        dispatch_guard: Callable[[], None] | None = None,
    ):
        super().__init__(provider, budget)
        self.requests, self.directory = requests, directory
        self.admission, self.store = admission, store
        self.dispatch_guard = dispatch_guard

    def check_dispatch_open(self) -> None:
        if self.admission is not None:
            self.admission.check_open()
        if self.dispatch_guard is not None:
            self.dispatch_guard()

    def checkpoint(self, record: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.put("requests", record["request_id"], record)
        else:
            save(self.directory / "request-identities.json", self.requests)

    async def _unary(
        self,
        operation: str,
        model: str,
        payload: Any,
        invoke: Callable[[], Awaitable[Any]],
        *,
        tier: str = "standard",
    ) -> Any:
        started = time.monotonic()
        record: dict[str, Any] = {
            "request_id": str(uuid4()),
            "started_epoch_s": time.time(),
            "trial_id": TRIAL.get(),
            "model": model,
            "operation": operation,
            "request_sha256": fingerprint(payload),
            "status": "queued",
            "dispatch_admitted": False,
            "provider_invoked": None,
        }
        self.requests.append(record)
        admitted = False
        ticket = None
        provider_started = None
        try:
            self.checkpoint(record)
            record["admission_wait_ms"] = await self.admission.enter() if self.admission else 0.0
            admitted = True
            record.update(status="started", dispatch_admitted=True)
            self.checkpoint(record)
            ticket = self.budget.reserve(model, payload, 0, tier)
            if isinstance(self.store, BufferedRecordStore):
                waiting = time.perf_counter()
                try:
                    if (
                        not isinstance(self.budget, AuditBudget)
                        or self.budget.store is not self.store
                    ):
                        raise CheckpointUnavailable(
                            "Request and reservation must share one durable store"
                        )
                    fence = self.store.writer.capture_request_dispatch(
                        record["request_id"], str(ticket)
                    )
                    record["durability_fence"] = {
                        "policy": "request-reservation-fence-v1",
                        "request_sequence": fence.request_receipt.sequence,
                        "reservation_sequence": fence.reservation_receipt.sequence,
                        "target_sequence": fence.target.sequence,
                    }
                    permit = await self.store.writer.wait_request_dispatch(fence)
                    self.store.writer.assert_request_dispatch_ready(permit)
                finally:
                    record["durability_wait_ms"] = (time.perf_counter() - waiting) * 1000
            self.check_dispatch_open()
            provider_started = time.monotonic()
            record.update(provider_invoked=True, provider_started_epoch_s=time.time())
            result = await invoke()
            if result is not None:
                done = (
                    LLMDone(result.usage, "decided", self.name, model)
                    if operation == "decide"
                    else result
                )
                self.budget.finish(ticket, done)
            record["status"] = "completed"
            return result
        except BaseException as error:
            record.update(status=type(error).__name__, error_types=error_chain(error))
            if self.admission is not None:
                self.admission.observe_error(error)
            if ticket is not None and record["provider_invoked"] and isinstance(error, Exception):
                self.connection_failure(ticket, error)
            raise
        finally:
            if admitted and self.admission is not None:
                self.admission.leave()
            # An interrupted durable intent remains unknown until this terminal observation.
            record["provider_invoked"] = provider_started is not None
            record["elapsed_s"] = time.monotonic() - started
            record["provider_elapsed_s"] = (
                time.monotonic() - provider_started if provider_started is not None else None
            )
            storage_failed = (
                isinstance(self.store, BufferedRecordStore) and self.store.writer.failed
            )
            if ticket is not None and not record["provider_invoked"] and not storage_failed:
                self.budget.entries[ticket].update(
                    charged_usd=0.0,
                    status="failed_before_submission",
                    local_reason="cancelled_or_blocked_before_provider_invocation",
                )
                self.budget.checkpoint(ticket)
            if not storage_failed:
                self.checkpoint(record)

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        if not isinstance(self.store, BufferedRecordStore):
            return await super().decide(model, input, questions)
        return await self._unary(
            "decide", model, [input, questions], lambda: self.inner.decide(model, input, questions)
        )

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> LLMDone | None:
        tier = _reservation_tier(params)
        if not isinstance(self.store, BufferedRecordStore):
            # Keep the metered path's lifecycle while reserving its configured tier.
            ticket = self.budget.reserve(model, asdict(req), 0, tier)
            try:
                done: LLMDone | None = await self.inner.prewarm(model, req, params)
            except Exception as error:
                self.connection_failure(ticket, error)
                raise
            if done is not None:
                self.budget.finish(ticket, done)
            return done
        return await self._unary(
            "prewarm",
            model,
            asdict(req),
            lambda: self.inner.prewarm(model, req, params),
            tier=tier,
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        started = time.monotonic()
        record: dict[str, Any] = {
            "request_id": str(uuid4()),
            "started_epoch_s": time.time(),
            "trial_id": TRIAL.get(),
            "model": model,
            "request_sha256": fingerprint(asdict(req)),
            "params_sha256": fingerprint(dict(params)),
            "output_limit": req.max_tokens,
            "status": "queued" if self.admission is not None else "started",
            "streamed_chars": 0,
            "dispatch_admitted": False,
            "provider_invoked": None,
        }
        admitted = False
        provider_started: float | None = None
        ticket: int | None = None
        self.requests.append(record)
        self.checkpoint(record)
        try:
            record["admission_wait_ms"] = await self.admission.enter() if self.admission else 0.0
            admitted = True
            record.update(status="started", dispatch_admitted=True, admitted_epoch_s=time.time())
            self.checkpoint(record)
            ticket = self.budget.reserve(
                model,
                asdict(req),
                req.max_tokens + int(params.get("max_output_tokens_extra", 0)),
                _reservation_tier(params),
            )
            if isinstance(self.store, BufferedRecordStore):
                waiting = time.perf_counter()
                try:
                    if (
                        not isinstance(self.budget, AuditBudget)
                        or self.budget.store is not self.store
                    ):
                        raise CheckpointUnavailable(
                            "Request and reservation must share one durable store"
                        )
                    fence = self.store.writer.capture_request_dispatch(
                        record["request_id"], str(ticket)
                    )
                    record["durability_fence"] = {
                        "policy": "request-reservation-fence-v1",
                        "request_sequence": fence.request_receipt.sequence,
                        "reservation_sequence": fence.reservation_receipt.sequence,
                        "target_sequence": fence.target.sequence,
                    }
                    permit = await self.store.writer.wait_request_dispatch(fence)
                    self.store.writer.assert_request_dispatch_ready(permit)
                finally:
                    record["durability_wait_ms"] = (time.perf_counter() - waiting) * 1000
            self.check_dispatch_open()
            record["provider_invoked"] = True
            record["provider_started_epoch_s"] = time.time()
            provider_started = time.monotonic()
            try:
                # Hold the slot until the actual provider stream closes, including cancellation.
                async with aclosing(self.inner.stream(model, req, params)) as stream:
                    async for chunk in stream:
                        if isinstance(chunk, str):
                            record["streamed_chars"] += len(chunk)
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
            # An interrupted durable intent remains unknown until this terminal observation.
            record["provider_invoked"] = provider_started is not None
            record["elapsed_s"] = time.monotonic() - started
            record["provider_elapsed_s"] = (
                time.monotonic() - provider_started if provider_started is not None else None
            )
            storage_failed = (
                isinstance(self.store, BufferedRecordStore) and self.store.writer.failed
            )
            if not record["provider_invoked"] and ticket is not None and not storage_failed:
                self.budget.entries[ticket].update(
                    charged_usd=0.0,
                    status="failed_before_submission",
                    local_reason="cancelled_or_blocked_before_provider_invocation",
                )
                self.budget.checkpoint(ticket)
            if storage_failed and isinstance(self.store, BufferedRecordStore):
                record["checkpoint_failure_reason"] = self.store.writer.failure_reason
            else:
                self.checkpoint(record)


def provider_configuration_fingerprint(settings: Settings) -> str:
    """Hash consumed provider configuration; never archive URLs, headers or secrets.

    SDK environment names are verified against the installed client constructors.
    HTTPX also reads OS/environment proxies and TLS certificate path overrides.
    This binds configuration, not DNS, trust-store contents, credentials or network state.
    """
    fields = {
        "llm_providers",
        "openai_base_url",
        "aws_region",
        "openai_http2",
        "openai_http2_cancel_fix",
        "llm_keepalive_s",
        "llm_connect_timeout_s",
    }
    environment = (
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    return fingerprint(
        {
            "schema": 1,
            "settings": settings.model_dump(mode="json", include=fields),
            "environment": {name: os.environ.get(name) for name in environment},
            "proxies": getproxies(),
            "credential_presence": {
                "openai": settings.openai_api_key is not None,
                "anthropic": settings.anthropic_api_key is not None,
                "bedrock_bearer": settings.bedrock_api_key is not None,
            },
        }
    )


def effective_provider_timeout(timeout_s: float, override: float | None = None) -> float:
    """SDK timeout; omission retains the replay's historical trial-timeout default."""
    value = timeout_s if override is None else override
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid provider timeout: expected finite positive seconds")
    return float(value)


def experiment_identity(
    cases: list[dict[str, Any]],
    settings: Settings,
    reps: int,
    seed: int,
    concurrency: int,
    timeout_s: float,
    provider_timeout_s: float | None = None,
) -> dict[str, Any]:
    sdk_timeout = effective_provider_timeout(timeout_s, provider_timeout_s)
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
        "provider_timeout_s": sdk_timeout,
        "strategies": STRATEGIES,
        "settings": settings.model_dump(mode="json", include=CONFIG_FIELDS),
        "provider_configuration_sha256": provider_configuration_fingerprint(settings),
        "code": code,
        "git_head": head.stdout.decode().strip(),
        "git_diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
        "limitations": [
            "Paraphrases and fictional evidence are not original historical sessions.",
            "No STT, detector, live retrieval, network-display or UI latency measurement.",
        ],
    }


def schedule_run_identity(args: argparse.Namespace, identity: dict[str, Any]) -> dict[str, Any]:
    """Bind execution using the same repository identity as live replay.

    Generate only after reviewed code is integrated and frozen. The source map
    comes from experiment_identity/root_directory, never a private manifest or
    copy of this module. The seed is recorded but cannot reorder explicit blocks.
    """
    integer_bounds = {
        "concurrency": (identity.get("concurrency"), 1, 8),
        "max_provider_inflight": (getattr(args, "max_provider_inflight", 8), 1, 32),
        "transport_failure_limit": (getattr(args, "transport_failure_limit", 3), 1, 10),
        "zero_output_timeout_limit": (getattr(args, "zero_output_timeout_limit", 8), 1, 100),
    }
    for name, (value, lower, upper) in integer_bounds.items():
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"Invalid explicit schedule execution control: {name}")
    seed = identity.get("seed")
    timeout = identity.get("timeout_s")
    skip_connectivity = getattr(args, "skip_connectivity_check", False)
    budget = getattr(args, "max_cost", 2.50)
    if (
        type(seed) is not int
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(skip_connectivity) is not bool
        or (
            budget is not None
            and (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0)
        )
    ):
        raise ValueError("Invalid explicit schedule seed, timeout, connectivity or budget")
    sdk_timeout = effective_provider_timeout(timeout, getattr(args, "provider_timeout", None))
    captured_timeout = effective_provider_timeout(timeout, identity.get("provider_timeout_s"))
    if captured_timeout != sdk_timeout:
        raise ValueError("Explicit schedule provider timeout differs from experiment identity")
    code = identity.get("code")
    source_fields = (identity.get("git_head"), identity.get("git_diff_sha256"))
    provider_configuration = identity.get("provider_configuration_sha256")
    if (
        not isinstance(code, dict)
        or not code
        or any(
            not isinstance(path, str)
            or not path.endswith(".py")
            or not isinstance(value, str)
            or re.fullmatch(r"[a-f0-9]{64}", value) is None
            for path, value in cast(dict[object, object], code).items()
        )
        or not isinstance(source_fields[0], str)
        or re.fullmatch(r"[a-f0-9]{40,64}", source_fields[0]) is None
        or not isinstance(source_fields[1], str)
        or re.fullmatch(r"[a-f0-9]{64}", source_fields[1]) is None
        or not isinstance(provider_configuration, str)
        or re.fullmatch(r"[a-f0-9]{64}", provider_configuration) is None
    ):
        raise ValueError("Explicit schedule requires the complete repository source identity")
    return {
        "timeout_s": float(timeout),
        "provider_timeout_s": sdk_timeout,
        "concurrency": integer_bounds["concurrency"][0],
        "max_provider_inflight": integer_bounds["max_provider_inflight"][0],
        "transport_failure_limit": integer_bounds["transport_failure_limit"][0],
        "zero_output_timeout_limit": integer_bounds["zero_output_timeout_limit"][0],
        "graceful_stop": stop_policy(args),
        "connectivity_check": not skip_connectivity,
        "provider_admission_policy": "bounded-v1-stop-on-rate-auth-config",
        "provider_configuration_sha256": provider_configuration,
        "seed": seed,
        "seed_effect": "recorded only; explicit block order is unchanged",
        "budget_limit_usd": None if budget is None else float(budget),
        "source": {
            "code_sha256": fingerprint(code),
            "git_head": source_fields[0],
            "git_diff_sha256": source_fields[1],
        },
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
    for job, arg in (
        ("fast", "fast_tier"),
        ("deep_light", "fast_tier"),
        ("deep", "expert_tier"),
        ("answer_recovery", "expert_tier"),
    ):
        tier = getattr(args, arg, None)
        if tier is None or job not in router.routes:
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


def configured_conditions(
    args: argparse.Namespace,
    settings: Settings,
    custom_variants: dict[str, Variant] | None,
) -> dict[str, Variant]:
    labels = getattr(args, "architectures", ["A", "D"])
    if (
        not labels
        or len(set(labels)) != len(labels)
        or any(label not in STRATEGIES for label in labels)
    ):
        raise ValueError("Select distinct architecture labels")
    return custom_variants or {
        STRATEGIES[label]: Variant(
            label,
            STRATEGIES[label],
            # CLI architecture comparisons keep their historical global-deep routes.
            # Product profiles and category policy require an explicit manifest condition.
            settings.model_copy(
                update={
                    "answer_architecture": STRATEGIES[label],
                    "adaptive_expert_route": None,
                    "adaptive_technical_legacy": False,
                }
            ),
            fast_tier=getattr(args, "fast_tier", None),
            expert_tier=getattr(args, "expert_tier", None),
        )
        for label in labels
    }


def variants_with_cli_tiers(
    variants: dict[str, Variant], args: argparse.Namespace
) -> dict[str, Variant]:
    """Resolve tier overlays before preflight; revalidate profile conflicts."""
    return {
        name: replace(
            variant,
            fast_tier=variant.fast_tier or getattr(args, "fast_tier", None),
            expert_tier=variant.expert_tier or getattr(args, "expert_tier", None),
        )
        for name, variant in variants.items()
    }


def replay_blocks(
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    conditions: dict[str, Variant],
    *,
    execution: dict[str, Any] | None = None,
) -> tuple[list[list[tuple[dict[str, Any], str, int]]], dict[str, Any] | None]:
    from copilot_evals.live_answer_schedule import case_blocks, explicit_case_blocks

    strategies = {variant.label: condition for condition, variant in conditions.items()}
    if path := getattr(args, "schedule_file", None):
        if execution is None:
            raise ValueError("Explicit schedule requires bound execution controls and source")
        return explicit_case_blocks(
            path,
            cases,
            strategies,
            {key: value.identity(CONFIG_FIELDS) for key, value in conditions.items()},
            args.reps,
            execution,
        )
    return case_blocks(cases, list(strategies.values()), args.reps, args.seed), None


async def execute(args: argparse.Namespace) -> None:
    effective_provider_timeout(args.timeout, getattr(args, "provider_timeout", None))
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
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        openai_detector_retry_tier=None,
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        # Keep baseline conditions stable when deployment defaults change speed tier.
        openai_deep_service_tier=None,
        **settings_overrides(args),
    )
    rate_identity = None
    if getattr(args, "rate_card", None):
        settings, rate_identity = load_rate_card(settings, args.rate_card)
    custom_variants: dict[str, Variant] | None = None
    if getattr(args, "variants", None):
        custom_variants = load_variants(args.variants, settings, root_directory())
        custom_variants = variants_with_cli_tiers(custom_variants, args)
    if getattr(args, "schedule_file", None):
        identity = experiment_identity(
            cases,
            settings,
            args.reps,
            args.seed,
            args.concurrency,
            args.timeout,
            getattr(args, "provider_timeout", None),
        )
        replay_blocks(
            args,
            cases,
            configured_conditions(args, settings, custom_variants),
            execution=schedule_run_identity(args, identity),
        )
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
    await execute_live(args, cases, settings, rate_identity, custom_variants, directory)


def require_replay_directory(directory: Path, stop_file: Path) -> None:
    """Permit only the declared stop flag in an otherwise new, unidentified output."""
    if not (directory / "identity.json").exists():
        names = {path.name for path in directory.iterdir() if path.name != "runner.lock"}
        if (
            stop_file.parent.resolve() == directory.resolve()
            and names == {stop_file.name}
            and stop_file.is_file()
        ):
            return
    require_async_directory(directory)


async def execute_live(
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    settings: Settings,
    rate_identity: dict[str, Any] | None,
    custom_variants: dict[str, Variant] | None,
    directory: Path,
) -> None:
    stop_file = (getattr(args, "stop_file", None) or directory / "stop.request").resolve()
    writer = DurableWriter(
        directory, initialization_guard=lambda path: require_replay_directory(path, stop_file)
    )
    try:
        await writer.ready()
        await execute_live_owned(
            args,
            cases,
            settings,
            rate_identity,
            custom_variants,
            directory,
            BufferedRecordStore(writer),
        )
    finally:
        await writer.close(timeout_s=10)


async def execute_live_owned(
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    settings: Settings,
    rate_identity: dict[str, Any] | None,
    custom_variants: dict[str, Variant] | None,
    directory: Path,
    buffered_store: BufferedRecordStore,
) -> None:
    from copilot_evals.live_answer_schedule import FailureCircuit, classify_failure

    conditions = configured_conditions(args, settings, custom_variants)
    strategies = {variant.label: condition for condition, variant in conditions.items()}
    circuit = FailureCircuit(
        getattr(args, "transport_failure_limit", 3),
        zero_output_timeout_threshold=getattr(args, "zero_output_timeout_limit", 8),
    )
    graceful_policy = stop_policy(args)
    stop_event = asyncio.Event()

    admission_stop_path = directory / "provider-admission-stop.json"

    def stop_dispatch(reason: str) -> None:
        stop_event.set()
        if circuit.reason is None:
            circuit.reason = reason
        if not buffered_store.writer.failed:
            buffered_store.snapshot_file(
                admission_stop_path.name,
                {"schema": 1, "reason": circuit.reason, "failure_circuit": circuit.snapshot()},
            )

    admission = ProviderAdmission(getattr(args, "max_provider_inflight", 8), stop_dispatch)
    if admission_stop_path.exists():
        previous_stop = json.loads(admission_stop_path.read_text("utf-8"))
        if previous_stop.get("schema") != 1 or not isinstance(previous_stop.get("reason"), str):
            raise ValueError("Invalid persisted provider admission stop")
        admission.stop(previous_stop["reason"])
    identity = experiment_identity(
        cases,
        settings,
        args.reps,
        args.seed,
        args.concurrency,
        args.timeout,
        getattr(args, "provider_timeout", None),
    )
    blocks, explicit_schedule = replay_blocks(
        args,
        cases,
        conditions,
        execution=(
            schedule_run_identity(args, identity) if getattr(args, "schedule_file", None) else None
        ),
    )
    planned = [item for block in blocks for item in block]
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
    if explicit_schedule is not None:
        identity["order_design"] = "explicit_case_blocks"
        identity["explicit_schedule"] = explicit_schedule
    identity["transport_failure_limit"] = circuit.threshold
    identity["zero_output_timeout_limit"] = circuit.zero_output_timeout_threshold
    identity["graceful_stop"] = graceful_policy
    identity["max_provider_inflight"] = admission.limit
    identity["provider_admission_policy"] = "bounded-v1-stop-on-rate-auth-config"
    identity["connectivity_check"] = not getattr(args, "skip_connectivity_check", False)
    identity["answer_style"] = getattr(args, "answer_style", "bullets")
    identity["response_length"] = getattr(args, "response_length", "default")
    identity_path = directory / "identity.json"
    use_records = (
        directory / "checkpoint-records" / "manifest.json"
    ).exists() or not identity_path.exists()
    if use_records:
        identity["checkpoint_policy"] = ASYNC_POLICY
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
    store = buffered_store if use_records else None
    if store is not None:
        store.on_error = lambda: admission.stop("checkpoint_error")
    budget = AuditBudget(
        directory,
        settings,
        limit=math.inf if args.max_cost is None else args.max_cost,
        store=store,
    )
    initial_cost = budget.total
    request_path = directory / "request-identities.json"
    requests: list[dict[str, Any]] = (
        store.read("requests")
        if store is not None
        else json.loads(request_path.read_text("utf-8"))
        if request_path.exists()
        else []
    )
    fault_path = directory / "synthetic-faults.json"
    prior_faults: list[dict[str, Any]] = (
        store.read("faults")
        if store is not None
        else json.loads(fault_path.read_text("utf-8"))
        if fault_path.exists()
        else []
    )
    fault_records = {row["fault_id"]: row for row in prior_faults}

    def record_fault(entry: dict[str, Any]) -> None:
        fault_records[entry["fault_id"]] = dict(entry)
        if store is not None:
            store.put("faults", entry["fault_id"], entry)
        else:
            save(fault_path, list(fault_records.values()))

    raw_providers = make_providers(
        settings,
        timeout_s=effective_provider_timeout(args.timeout, getattr(args, "provider_timeout", None)),
    )
    providers = [
        AuditProvider(provider, budget, requests, directory, admission=admission, store=store)
        for provider in raw_providers
    ]
    attempt_path = directory / "attempt-events.json"
    attempt_events: list[dict[str, Any]] = (
        store.read("attempts")
        if store is not None
        else json.loads(attempt_path.read_text("utf-8"))
        if attempt_path.exists()
        else []
    )

    def on_attempt(event: AttemptEvent) -> None:
        trial_id = TRIAL.get()
        entry = {"trial_id": trial_id, **asdict(event)}
        condition = conditions.get(trial_id.rsplit(":", 2)[1]) if trial_id else None
        if condition and condition.synthetic_fault is not None and event.job == "fast":
            entry.update(synthetic_fault=True, actual_provider_dispatched=False)
        attempt_events.append(entry)
        if store is not None:
            store.put("attempts", f"{event.attempt_id}:{event.status}", entry)
        else:
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
        apply_synthetic_fault(router, conditions[condition], record_fault)
    target = directory / "trials.json"
    results: list[dict[str, Any]] = (
        store.read("trials")
        if store is not None
        else json.loads(target.read_text("utf-8"))
        if target.exists()
        else []
    )
    if store is None:
        save(target, results)
    have = {(result["id"], result["architecture"], result["rep"]) for result in results}
    if len(have) != len(results):
        raise ValueError("Duplicate completed trial identity")
    journal_arguments = (
        [f"{case['id']}:{strategy}:{rep}" for case, strategy, rep in planned],
        {f"{identifier}:{strategy}:{rep}" for identifier, strategy, rep in have},
        {str(row["trial_id"]) for row in requests if row.get("trial_id")}
        | {str(row["trial_id"]) for row in budget.entries if row.get("trial_id")}
        | {str(row["trial_id"]) for row in fault_records.values() if row.get("trial_id")},
    )
    journal = (
        RecordDispatchJournal(store, *journal_arguments)
        if store is not None
        else DispatchJournal(directory, *journal_arguments)
    )

    progress = BufferedProgressSnapshots(directory, buffered_store)
    stop = ExternalStop(
        (getattr(args, "stop_file", None) or directory / "stop.request").resolve(),
        admission.stop,
        lambda value: buffered_store.snapshot_file("stop-control.json", value),
    )

    def progress_value() -> dict[str, Any]:
        return {
            "completed": len(results),
            "total": len(planned),
            "cost_start_usd": initial_cost,
            **budget_reporting(budget),
            "failures": sum(row.get("status") == "provider_or_engine_failure" for row in results),
            "budget_blocked_trials": sum(row.get("status") == "budget_blocked" for row in results),
            "not_started": sum(state == "not_started" for state in journal.states.values()),
            "in_flight": sum(state == "in_flight" for state in journal.states.values()),
            "interrupted_outcome_unknown": sum(
                state == "ambiguous" for state in journal.states.values()
            ),
            "circuit_reason": circuit.reason,
            "failure_circuit": circuit.snapshot(),
            "external_stop": stop.snapshot(),
            "provider_admission": admission.snapshot(),
            "progress_snapshot_errors": progress.diagnostics(),
        }

    async def run_one(case: dict[str, Any], strategy: str, rep: int) -> None:
        if (case["id"], strategy, rep) in have:
            return
        if budget.blocked or circuit.reason:
            return
        trial_id = f"{case['id']}:{strategy}:{rep}"
        if not journal.start(trial_id):
            return
        # Keep journal setup before the generation clock, as in the synchronous runner.
        if isinstance(store, BufferedRecordStore):
            await store.flush()
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
        annotate_synthetic_faults(
            result,
            conditions[strategy],
            [row for row in fault_records.values() if row["trial_id"] == trial_id],
        )
        result["status"] = (
            "budget_blocked"
            if trial_id in budget.blocked_trials
            else "injected_failure"
            if result.get("expected_injected_failure")
            else "provider_or_engine_failure"
            if result["failure"]
            else "completed"
        )
        attempts = [record for record in requests if record["trial_id"] == trial_id]
        result["request_attempts"] = len(attempts)
        if buffered_store.writer.failed:
            result["instrumentation_failure"] = buffered_store.writer.failure_reason
            if result["failure"]:
                result["status"] = "checkpoint_failure"
        if buffered_store.writer.failed:
            result["failure_kind"] = "checkpoint_failure"
            admission.stop("checkpoint_error")
        elif attempts and all(
            not record.get("dispatch_admitted", True)
            or (record.get("provider_invoked") is False and record.get("admission_blocked_reason"))
            for record in attempts
        ):
            result["status"] = "admission_blocked"
            result["failure_kind"] = "admission_blocked"
        else:
            real_result = real_failure_evidence(result)
            result["failure_kind"] = (
                "synthetic_pre_dispatch"
                if result.get("expected_injected_failure")
                else classify_failure(real_result, attempts)
            )
            # An expected injection neither increments nor resets the real-outage circuit.
            if not result.get("expected_injected_failure"):
                circuit.observe(real_result, attempts)
        result["failure_circuit"] = circuit.snapshot()
        if circuit.reason:
            admission.stop(circuit.reason)
        results.append(result)
        if store is not None:
            store.put("trials", trial_id, result)
        else:
            save(target, results)
        journal.finish(trial_id)
        progress.active(progress_value())

    monitor = AnchoredLoopLagMonitor()
    if store is not None:
        store.phase = "active_workers"
    await monitor.start()
    try:
        await stop.start()
        if not circuit.reason and not getattr(args, "skip_connectivity_check", False):
            health = await connectivity_check(settings)
            save(directory / "connectivity.json", health)
            if not health["ready"]:
                admission.stop("connectivity_preflight")
        stop.drain = await drain_on_stop(
            bounded_blocks(
                blocks, args.concurrency, run_one, lambda: budget.blocked or bool(circuit.reason)
            ),
            stop_event,
            graceful_policy["drain_timeout_seconds"],
            state=stop.drain,
        )
    finally:
        try:
            try:
                if not stop.drain["workers_drained"]:
                    raise ReplayDrainTimeout("Trial drain unconfirmed; retain durable starts")
                for identifier, state in journal.states.items():
                    if state == "in_flight":
                        journal.states[identifier] = "ambiguous"
                        if isinstance(journal, RecordDispatchJournal):
                            journal.dirty.add(identifier)
                journal.checkpoint()
            finally:
                try:
                    await bounded_cleanup(close_providers(raw_providers), 10.0)
                finally:
                    await monitor.stop()
                    await stop.close()
            # No provider work remains: compatibility snapshots cannot stall a live answer.
            if store is not None:
                store.phase = "drained"
                await store.flush()
                for kind, filename in (
                    ("cost", "cost.json"),
                    ("requests", "request-identities.json"),
                    ("attempts", "attempt-events.json"),
                    ("faults", "synthetic-faults.json"),
                    ("trials", "trials.json"),
                ):
                    store.export(kind, filename)
                if isinstance(journal, RecordDispatchJournal):
                    journal.export()
                save(directory / "checkpoint-write-telemetry.json", store.telemetry())
            save(directory / "event-loop-lag.json", monitor.summary())
            save(directory / "stop-control.json", stop.snapshot())
        except BaseException as error:
            record_finalization_failure(directory, error)
            raise
        else:
            try:
                await progress.final(progress_value)
                save(directory / "progress-write-errors.json", progress.diagnostics())
            except BaseException as error:
                record_finalization_failure(directory, error)
                raise
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
            **budget_reporting(budget),
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
            "failure_circuit": circuit.snapshot(),
            "external_stop": stop.snapshot(),
            "successful_trials": sum(row.get("status") == "completed" for row in results),
            "expected_injected_failures": sum(
                bool(r.get("expected_injected_failure")) for r in results
            ),
            "injected_fault_attempts": sum(
                r["status"] == "injected" for r in fault_records.values()
            ),
            "synthetic_fault_scope": (
                "pre-dispatch only; zero API cost; not natural-outage prevalence"
            ),
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
                else "partial_stopped"
                if circuit.reason == "external_stop"
                else "partial_circuit"
                if circuit.reason
                else "partial_ambiguous"
                if any(state == "ambiguous" for state in journal.states.values())
                else "complete",
                "trials": len(results),
                **budget_reporting(budget),
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

    if any(("answer_style" in case) != ("response_length" in case) for case in cases):
        raise ValueError("Response style and length must both be recorded or both absent")
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
                    "synthetic_fault_condition": item.get("synthetic_fault_condition"),
                    "expected_injected_failure": item.get("expected_injected_failure", False),
                    "synthetic_fault_count": len(item.get("synthetic_faults", [])),
                    "failure_kind_reclassified": (
                        "synthetic_pre_dispatch"
                        if item.get("expected_injected_failure")
                        else "admission_blocked"
                        if item.get("status") == "admission_blocked"
                        else classify_failure(real_failure_evidence(item), [])
                    ),
                    "not_started_reason": item.get("reason"),
                    **(
                        {
                            "response_preferences": {
                                "answer_style": case["answer_style"],
                                "response_length": case["response_length"],
                            }
                        }
                        if "answer_style" in case or "response_length" in case
                        else {}
                    ),
                    "context_fidelity": context_fidelity(case),
                    "evidence": {
                        key: case[key]
                        for key in ("digest", "recent", "memory", "source", *CONTEXT_TEXT_FIELDS)
                        if key in case
                        and (
                            key not in {"company", "role", "company_digest"}
                            or bool(case.get("company") and case.get("mode") in prompts.GROUP_MODES)
                        )
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


def write_failure_report(directory: Path, error: BaseException) -> dict[str, Any]:
    """Keep diagnostic leaves even when the outer TaskGroup hides their types."""
    tree = sanitized_exception_tree(error)
    summary: dict[str, Any] = {"status": "failed", "error_type": type(error).__name__}
    leaves: list[dict[str, Any]] = []
    pending = [tree]
    while pending:
        item = pending.pop()
        children = [*item.get("children", [])]
        children.extend(item[name] for name in ("cause", "context") if name in item)
        if children:
            pending.extend(reversed(children))
        else:
            leaf = {
                key: item[key] for key in ("type", "errno", "winerror", "truncated") if key in item
            }
            if item.get("frames"):
                leaf["source"] = item["frames"][-1]
            leaves.append(leaf)
    summary["errors"] = leaves
    try:
        path = private_directory(directory) / "failure.json"
        save(path, {"schema": 1, "status": "failed", "error": tree})
        summary["failure_artifact_written"] = True
    except (OSError, ValueError) as artifact_error:
        summary["failure_artifact_written"] = False
        summary["artifact_error"] = sanitized_exception_tree(artifact_error)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument(
        "--variants",
        type=Path,
        help="Variant manifest; expert profiles and category policies opt in per condition",
    )
    parser.add_argument(
        "--schedule-file",
        type=Path,
        help="Finite exact case/condition order, bound to effective inputs and request settings",
    )
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
    parser.add_argument(
        "--zero-output-timeout-limit",
        type=int,
        default=8,
        help="Stop after1-100 timeout trials without response progress; independent of cost",
    )
    parser.add_argument(
        "--stop-file",
        type=Path,
        help="Creating this file stops admissions; default is OUT/stop.request; contents ignored",
    )
    parser.add_argument(
        "--stop-drain-timeout",
        type=float,
        default=30.0,
        help="Seconds to let active trials finish after a stop, then cancel (0-300)",
    )
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
    tiers = ("default", "priority", "fast", "ultrafast", "flex")
    parser.add_argument("--fast-tier", choices=tiers)
    parser.add_argument("--expert-tier", choices=tiers)
    parser.add_argument("--timeout", type=float, default=60, help="Overall per-trial deadline")
    parser.add_argument(
        "--provider-timeout",
        type=float,
        help="SDK read/write/pool timeout; omitted uses --timeout (connect stays configured)",
    )
    args = parser.parse_args(argv)
    previous_logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(execute(args))
    except Exception as error:
        print(json.dumps(write_failure_report(args.out, error)))
        return 1
    finally:
        logging.disable(previous_logging_threshold)
    if args.live:
        accounting = json.loads((args.out / "accounting.json").read_text("utf-8"))
        if accounting.get("circuit_reason"):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
