"""Fast and deep passes, cancellation, prepared answers and the exact-repeat cache.

ARCHITECTURE.md, "Realtime pipeline" steps 5 and 6:
- The fast pass (Haiku) starts at the speculative point. If the question matches a prepared
  answer in the session pack (similarity 0.85 or more), those points are sent at once and no
  model is called. An exact repeat of a normalized question is served from `qa:{uid}:{qhash}`.
- The deep pass (Sonnet) starts at confirmation for the auto types, otherwise on expand.
- A new confirmed question stops generation for the old one; the old answer stays on screen
  (the client keeps what it has; the server sends answer.error with code `cancelled`).
- Without transcript consent the session log keeps question and answer metadata only, and
  nothing goes into the exact-repeat cache (it holds answer text).

Each pass runs as its own task outside the session's task group, one per question and pass.
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from copilot_core import prompts
from copilot_core.embed import Embedder
from copilot_core.llm import (
    AllRoutesFailedError,
    LLMDone,
    LLMRequest,
    LLMUsage,
    ModelRouter,
    Route,
)
from copilot_core.packs import NodeRef, PreparedAnswer, cited_refs, cosine
from copilot_core.pricing import cost_usd
from copilot_gateway.config import Settings
from copilot_protocol import models

log = logging.getLogger(__name__)

PREPARED_WAIT_S = 0.15  # extra wait for an in-flight match before calling the model anyway
CACHE_READ_TIMEOUT_S = 0.1


def normalize_question(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode()).hexdigest()[:32]


@dataclass
class Question:
    qid: UUID
    text: str
    qtype: str
    confidence: float
    addressed: bool
    speculative: bool
    forced: bool
    t0: float  # monotonic time the question ended (speech end, or the force message)
    detector_ms: int | None
    match: "asyncio.Task[object] | None"
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turn_end_ms: int | None = None
    final_ms: int | None = None  # t0 to the STT final (None: no final, or a forced question)
    expanded_at: float | None = None  # monotonic time of the expand tap
    was_speculative: bool = False
    tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict[str, "asyncio.Task[None]"])
    superseded: bool = False
    cancelled: bool = False
    # Timings that come late (the client's latency.report), per pass, merged into the
    # answer's log entry when they arrive first; `logged` holds the passes already logged.
    client: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    logged: set[str] = field(default_factory=set[str])


@dataclass(frozen=True)
class MatchResult:
    """A prepared-answer lookup: the best match over the threshold, if any, plus the best
    similarity and the time the lookup took (both logged, hit or miss)."""

    answer: PreparedAnswer | None
    similarity: float | None
    ms: int


class PreparedMatcher:
    """Compares the live question with the ~30 prepared questions of the session pack.

    Only vectors from the same embedding model compare: when the pack's `embedding_model` is
    not the gateway embedder's, matching is off for the session (`stale` asks for a rebuild).
    """

    def __init__(
        self,
        embedder: Embedder | None,
        answers: list[PreparedAnswer],
        min_similarity: float,
        embedding_model: str | None = None,
    ) -> None:
        self.embedder = embedder
        self.min_similarity = min_similarity
        self.vectors: list[tuple[PreparedAnswer, list[float]]] = []
        # The pack's vectors were made by another model, or it has none to match against.
        self.stale = embedder is not None and bool(answers) and embedding_model != embedder.model_id
        if embedder is None or self.stale:
            return
        for a in answers:
            vec = a.embedding()
            if vec:
                self.vectors.append((a, vec))

    @property
    def enabled(self) -> bool:
        return self.embedder is not None and bool(self.vectors)

    async def match(self, text: str) -> MatchResult | None:
        if self.embedder is None or not self.vectors:
            return None
        started = time.monotonic()
        try:
            [query] = await self.embedder.embed([text])
        except Exception as e:
            log.warning("prepared-answer embedding failed: %s", e)
            return None
        best: tuple[PreparedAnswer, float] | None = None
        for answer, vec in self.vectors:
            sim = cosine(query, vec)
            if best is None or sim > best[1]:
                best = (answer, sim)
        ms = int((time.monotonic() - started) * 1000)
        if best is None:
            return MatchResult(None, None, ms)
        hit = best[0] if best[1] >= self.min_similarity else None
        return MatchResult(hit, best[1], ms)


class AnswerHost(Protocol):
    settings: Settings
    router: ModelRouter

    def emit(self, cls: type[Any], **fields: Any) -> int: ...

    def log_entry(self, entry: Mapping[str, Any]) -> None: ...

    def store_text(self) -> bool:
        """Transcript consent: without it, question and answer text are never stored."""
        ...

    def transcript(self, seconds: float) -> str: ...

    def prompt_context(self) -> prompts.PromptContext: ...

    def refs(self) -> dict[str, NodeRef]: ...

    def llm_status(self, ok: bool) -> None: ...

    def cache_version(self) -> str: ...

    async def qa_get(self, qhash: str) -> dict[str, Any] | None: ...

    async def qa_put(self, qhash: str, value: Mapping[str, Any]) -> None: ...


class AnswerEngine:
    def __init__(self, host: AnswerHost) -> None:
        self.host = host
        self.questions: OrderedDict[UUID, Question] = OrderedDict()
        self.stats = {"prepared_hits": 0, "cache_hits": 0, "model_passes": 0, "cancelled": 0}
        self._background: set[asyncio.Task[None]] = set()

    # Question lifecycle, driven by the turn detector and client messages.

    def add(self, q: Question) -> None:
        self.questions[q.qid] = q
        while len(self.questions) > 100:
            _, old = self.questions.popitem(last=False)
            self._cancel_tasks(old)
        self.start_pass(q, "fast")

    def confirm(
        self, qid: UUID, text: str, turn_end_ms: int | None, final_ms: int | None = None
    ) -> None:
        q = self.questions.get(qid)
        if q is None:
            return
        q.final_ms = final_ms
        if q.speculative:
            q.speculative = False
            q.was_speculative = True
            q.text = text
            self.host.emit(
                models.QuestionDetected,
                question_id=q.qid,
                text=q.text,
                question_type=q.qtype,
                confidence=q.confidence,
                speculative=False,
            )
        q.turn_end_ms = turn_end_ms
        # A speculative fast pass can finish before its question is confirmed; its stored
        # latency gets the confirmation's timings afterwards.
        late = {"turn_end_ms": turn_end_ms, "final_ms": final_ms}
        for pass_ in q.logged:
            self._late_latency(q, pass_, {k: v for k, v in late.items() if v is not None})
        self.supersede(keep=qid)
        self._log_question(q, kept=True)
        if q.qtype in self.host.settings.deep_pass_auto_types:
            self.start_pass(q, "deep")

    def cancel_speculative(self, qid: UUID) -> None:
        q = self.questions.pop(qid, None)
        if q is None:
            return
        q.cancelled = True
        self._cancel_tasks(q)
        self.host.emit(models.QuestionCancelled, question_id=qid)
        self._log_question(q, kept=False)

    def supersede(self, keep: UUID | None) -> None:
        """A new question stops generation for every older one; their text stays shown."""
        for qid, q in self.questions.items():
            if qid != keep and any(not t.done() for t in q.tasks.values()):
                q.superseded = True
                self.stats["cancelled"] += 1
                self._cancel_tasks(q)

    def expand(self, qid: UUID) -> bool:
        q = self.questions.get(qid)
        if q is None:
            return False
        self.host.log_entry({"kind": "expand", "question_id": str(qid)})
        if "deep" not in q.tasks:
            q.expanded_at = time.monotonic()
            self.start_pass(q, "deep")
        return True

    def client_latency(self, qid: UUID, pass_: str, first_word_ms: int, anchor: str) -> bool:
        """latency.report: the client's first word for a pass, stored with the answer."""
        q = self.questions.get(qid)
        if q is None:
            return False
        self._late_latency(
            q, pass_, {"client_first_word_ms": first_word_ms, "client_anchor": anchor}
        )
        return True

    def _late_latency(self, q: Question, pass_: str, fields: dict[str, Any]) -> None:
        """Timings that arrive after a pass may already be logged: merged into its entry if
        it is not, else logged as a `latency_report` entry the flush merges into the row."""
        if not fields:
            return
        if pass_ in q.logged:
            self.host.log_entry(
                {"kind": "latency_report", "question_id": str(q.qid), "pass": pass_, **fields}
            )
        else:
            q.client.setdefault(pass_, {}).update(fields)

    def cancel_all(self) -> None:
        for q in self.questions.values():
            self._cancel_tasks(q)

    def _cost(self, done: LLMDone | None) -> float | None:
        """The answer's cost in USD for `answers.cost_usd`; None without a model call or a
        price for the model."""
        if done is None:
            return None
        s = self.host.settings
        u = done.usage
        cost = cost_usd(
            done.model, done.service_tier, u.input_tokens, u.cached_tokens, u.output_tokens,
            s.model_prices, s.tier_price_multipliers,
        )  # fmt: skip
        return None if cost is None else float(cost)

    def _cancel_tasks(self, q: Question) -> None:
        for task in q.tasks.values():
            if not task.done():
                task.cancel()

    def start_pass(self, q: Question, pass_: str) -> None:
        if pass_ in q.tasks:
            return
        q.tasks[pass_] = asyncio.create_task(self._run_pass(q, pass_))

    async def wait_idle(self) -> None:
        tasks = [t for q in self.questions.values() for t in q.tasks.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    # One pass.

    async def _run_pass(self, q: Question, pass_: str) -> None:
        host = self.host
        started = time.monotonic()
        state: dict[str, Any] = {"first_delta_at": None, "parts": [], "start_at": None}

        def delta(text: str) -> None:
            if state["first_delta_at"] is None:
                state["first_delta_at"] = time.monotonic()
            state["parts"].append(text)
            host.emit(models.AnswerDelta, question_id=q.qid, **{"pass": pass_}, text=text)

        def latency(first_token_ms: int | None) -> dict[str, int]:
            out: dict[str, int] = {}
            if q.turn_end_ms is not None:
                out["turn_end_ms"] = q.turn_end_ms
            if q.detector_ms is not None:
                out["detector_ms"] = q.detector_ms
            if first_token_ms is not None:
                out["first_token_ms"] = first_token_ms
            if state["first_delta_at"] is not None:
                out["first_delta_ms"] = max(0, int((state["first_delta_at"] - q.t0) * 1000))
            out["total_ms"] = max(0, int((time.monotonic() - q.t0) * 1000))
            if q.final_ms is not None:
                out["final_ms"] = q.final_ms
            if state["start_at"] is not None:
                out["start_ms"] = max(0, int((state["start_at"] - q.t0) * 1000))
            if pass_ == "deep" and q.expanded_at is not None:
                out["expand_ms"] = max(0, int((q.expanded_at - q.t0) * 1000))
            if pass_ == "fast" and (m := self._match_result(q)) is not None:
                out["prepared_ms"] = m.ms
            return out

        def done(
            source: str, refs: list[NodeRef], done_info: LLMDone | None, first_token_ms: int | None
        ) -> None:
            usage = done_info.usage if done_info else None
            tier = done_info.service_tier if done_info else "standard"
            lat: dict[str, Any] = latency(first_token_ms)
            m = self._match_result(q) if pass_ == "fast" else None
            if m is not None and m.similarity is not None:
                lat["prepared_similarity"] = round(m.similarity, 4)
            host.emit(
                models.AnswerDone,
                question_id=q.qid,
                **{"pass": pass_},
                memory_refs=[{"node_id": r.node_id, "label": r.label} for r in refs],
                usage={
                    "input_tokens": usage.input_tokens if usage else 0,
                    "cached_tokens": usage.cached_tokens if usage else 0,
                    "output_tokens": usage.output_tokens if usage else 0,
                },
                source=source,
                latency_ms=lat,
            )
            # Stored with the answer only: which route answered, and the client's first word
            # if its latency.report came before this.
            stored = {
                **lat,
                "provider": done_info.provider if done_info else source,
                "fallback_attempt": done_info.attempt if done_info else 0,
                **q.client.pop(pass_, {}),
            }
            q.logged.add(pass_)
            log.info(
                "answer %s %s: source=%s model=%s tokens=%s/%s/%s latency=%s",
                q.qid, pass_, source, done_info.model if done_info else source,
                usage.input_tokens if usage else 0, usage.cached_tokens if usage else 0,
                usage.output_tokens if usage else 0, stored,
            )  # fmt: skip
            host.log_entry(
                {
                    "kind": "answer",
                    "question_id": str(q.qid),
                    "pass": pass_,
                    "source": source,
                    "provider": done_info.provider if done_info else source,
                    "model": done_info.model if done_info else source,
                    "fallback_attempt": done_info.attempt if done_info else 0,
                    "tier": tier,
                    **({"text": "".join(state["parts"])} if host.store_text() else {}),
                    "input_tokens": usage.input_tokens if usage else 0,
                    "cached_tokens": usage.cached_tokens if usage else 0,
                    "output_tokens": usage.output_tokens if usage else 0,
                    "cost_usd": self._cost(done_info),
                    "latency_ms": stored,
                    "memory_node_ids": [str(r.node_id) for r in refs],
                }
            )

        def error(code: str) -> None:
            host.emit(models.AnswerError, question_id=q.qid, **{"pass": pass_}, code=code)
            host.log_entry(
                {"kind": "answer_error", "question_id": str(q.qid), "pass": pass_, "code": code}
            )

        try:
            if pass_ == "fast":
                state["start_at"] = time.monotonic()
                answer = await self._prepared(q)
                if answer is not None:
                    self.stats["prepared_hits"] += 1
                    delta("\n".join(f"- {p}" for p in answer.talking_points) + "\n")
                    done("prepared", self._refs_for_nodes(answer), None, None)
                    return
            qhash = question_hash(q.text)
            cached = await self._cached(qhash, pass_)
            if cached is not None:
                self.stats["cache_hits"] += 1
                delta(cached["text"])
                refs = [NodeRef.model_validate(r) for r in cached.get("refs", [])]
                done("cache", refs, None, None)
                return
            self.stats["model_passes"] += 1
            state["start_at"] = time.monotonic()
            done_info: LLMDone | None = None
            job = pass_
            req = self._request(q, pass_)
            state["req"] = req

            def on_fallback(job: str, route: Route, reason: str) -> None:
                host.llm_status(False)

            async for chunk in host.router.stream(job, req, on_fallback):
                if isinstance(chunk, LLMDone):
                    done_info = chunk
                else:
                    delta(chunk)
            assert done_info is not None
            if done_info.attempt == 0:
                host.llm_status(True)
            if done_info.stop_reason == "refusal":
                error("refused")
                return
            text = "".join(state["parts"])
            refs = cited_refs(text, host.refs())
            done("model", refs, done_info, done_info.first_token_ms)
            if not q.cancelled and not q.superseded and host.store_text():
                task = asyncio.create_task(self._cache_put(qhash, pass_, text, refs))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        except AllRoutesFailedError as e:
            log.warning("%s pass failed for %s: %s", pass_, q.qid, e)
            if e.refused:  # every model that ran declined: not an outage
                error("refused")
                return
            host.llm_status(False)
            error("llm_unavailable")
        except asyncio.CancelledError:
            if q.superseded:
                error("cancelled")
            self._log_cancelled(q, pass_, state)
            raise
        except Exception:
            log.exception("%s pass crashed for %s", pass_, q.qid)
            error("internal")
        finally:
            log.debug(
                "%s pass for %s took %.0f ms", pass_, q.qid, (time.monotonic() - started) * 1000
            )

    def _log_cancelled(self, q: Question, pass_: str, state: Mapping[str, Any]) -> None:
        """A pass stopped before its end still costs what the provider read and wrote, but the
        stream ends without usage: logged with an estimate (about 4 characters a token, the
        system blocks counted as cached), so speculative waste and its cost can be measured."""
        if q.superseded:
            reason = "superseded"
        elif q.cancelled:
            reason = "speculative"
        else:
            reason = "stopped"
        entry: dict[str, Any] = {
            "kind": "pass_cancelled",
            "question_id": str(q.qid),
            "pass": pass_,
            "reason": reason,
            "model_started": "req" in state,
            "streamed_chars": sum(len(p) for p in state["parts"]),
        }
        req = cast(LLMRequest | None, state.get("req"))
        routes = self.host.router.routes.get(pass_) or []
        if req is not None and routes:
            system = sum(len(b.text) for b in req.system) // 4
            user = len(str(req.content)) // 4
            output = entry["streamed_chars"] // 4
            done = LLMDone(
                usage=LLMUsage(
                    input_tokens=system + user, cached_tokens=system, output_tokens=output
                ),
                stop_reason=None,
                model=routes[0].model,
            )
            entry.update(
                model=routes[0].model,
                input_tokens_est=system + user,
                output_tokens_est=output,
                cost_usd_est=self._cost(done),
            )
        self.host.log_entry(entry)

    def _request(self, q: Question, pass_: str) -> LLMRequest:
        s = self.host.settings
        ctx = self.host.prompt_context()
        transcript = self.host.transcript(s.transcript_context_s)
        if pass_ == "fast":
            return prompts.fast_pass(ctx, transcript, q.text, q.qtype, s.fast_pass_max_tokens)
        return prompts.deep_pass(ctx, transcript, q.text, q.qtype, s.deep_pass_max_tokens)

    async def _prepared(self, q: Question) -> PreparedAnswer | None:
        match = q.match
        if match is None:
            return None
        await asyncio.wait({match}, timeout=PREPARED_WAIT_S)
        result = self._match_result(q)
        return result.answer if result is not None else None

    @staticmethod
    def _match_result(q: Question) -> MatchResult | None:
        """The question's prepared-answer lookup, once it has finished."""
        match = q.match
        if match is None or not match.done() or match.cancelled() or match.exception():
            return None
        result = match.result()
        return result if isinstance(result, MatchResult) else None

    def _refs_for_nodes(self, answer: PreparedAnswer) -> list[NodeRef]:
        by_id = {r.node_id: r for r in self.host.refs().values()}
        return [by_id.get(n) or NodeRef(node_id=n, label=answer.question) for n in answer.node_ids]

    async def _cached(self, qhash: str, pass_: str) -> dict[str, Any] | None:
        try:
            value = await asyncio.wait_for(self.host.qa_get(qhash), CACHE_READ_TIMEOUT_S)
        except Exception:
            return None
        if not value or value.get("v") != self.host.cache_version():
            return None
        entry: Any = value.get(pass_)
        if not isinstance(entry, dict):
            return None
        found = cast(dict[str, Any], entry)
        return found if found.get("text") else None

    async def _cache_put(self, qhash: str, pass_: str, text: str, refs: list[NodeRef]) -> None:
        with contextlib.suppress(Exception):
            version = self.host.cache_version()
            current: dict[str, Any] = await self.host.qa_get(qhash) or {}
            if current.get("v") != version:
                current = {"v": version}
            current[pass_] = {"text": text, "refs": [r.model_dump(mode="json") for r in refs]}
            await self.host.qa_put(qhash, current)

    def _log_question(self, q: Question, kept: bool) -> None:
        self.host.log_entry(
            {
                "kind": "question",
                "question_id": str(q.qid),
                **({"text": q.text} if self.host.store_text() else {}),
                "type": q.qtype,
                "confidence": q.confidence,
                "addressed_to_user": q.addressed,
                "speculative_kept": kept,
                "forced_by_hotkey": q.forced,
                "detected_at": q.detected_at.isoformat(),
            }
        )
