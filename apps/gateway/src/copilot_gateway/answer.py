"""Fast and deep passes, cancellation, prepared answers and the exact-repeat cache.

ARCHITECTURE.md, "Realtime pipeline" steps 5 and 6:
- The fast pass (Haiku) starts at the speculative point. If the question matches a prepared
  answer in the session pack (a similarity and a margin over the runner-up calibrated per
  embedding model, `Settings.prepared_answer_thresholds`), those points are sent at once and
  no model is called. An exact repeat of a normalized question is served from `qa:{uid}:{qhash}`.
- The deep pass (Sonnet) starts on its own once the question is confirmed and its fast pass is
  done, for every type in `deep_pass_auto_types` (all but small talk by default), otherwise on
  expand. Starting after the fast pass, not with it, means a quick follow-up that is confirmed
  while the fast pass still streams costs no deep tokens at all.
- A new confirmed question stops generation for the old one, deep pass included; the old
  answer stays on screen (the client keeps what it has; the server sends answer.error with
  code `cancelled`), and a deep pass not started yet never starts.
- Both passes see the conversation so far (`AnswerHost.call_context`, the session's
  conversation memory) after the cached blocks.
- Without transcript consent the session log keeps question and answer metadata only, and
  nothing goes into the exact-repeat cache (it holds answer text).
- A screenshot (coding mode, copilot_core.screen) is a question too: its fast pass reads the
  screen (the `READ` header, taken out of the stream) and answers what needs no code, and its
  deep pass writes the code, started as soon as the reading shows a task that needs it. No
  prepared answer, cache or memory search, and no citation filter (it would eat `a[n:]`).

Each pass runs as its own task outside the session's task group, one per question and pass.
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from copilot_core import prompts, screen
from copilot_core.embed import Embedder
from copilot_core.languages import language_instruction
from copilot_core.llm import (
    AllRoutesFailedError,
    AttemptEvent,
    LLMDone,
    LLMRequest,
    LLMUsage,
    ModelRouter,
    Route,
    SystemBlock,
)
from copilot_core.packs import NodeRef, PreparedAnswer, cited_refs, cosine
from copilot_core.pricing import cost_usd
from copilot_gateway.adaptive import (
    AnswerPlan,
    Architecture,
    SentenceBoundary,
    bounded_chunks,
    is_explicit_coding_request,
    plan_answer,
)
from copilot_gateway.config import PreparedThreshold, Settings
from copilot_protocol import models

log = logging.getLogger(__name__)

CACHE_READ_TIMEOUT_S = 0.1
# The types whose detailed answer goes to the deep model when the detector gave no depth.
DEEP_BY_TYPE = frozenset({"technical", "system_design", "coding", "other"})
# Shorter questions ("why?", "and then?") mean something different each time they are asked,
# so the exact-repeat cache never serves or keeps them.
CACHE_MIN_WORDS = 4
# Recent displayed cues help a follow-up add detail without retelling its opening. They
# remain suggestions, separate from the user's spoken answers and the factual sources.
PRIOR_CUES_COUNT = 3
PRIOR_CUE_QUESTION_CHARS = 160
PRIOR_CUE_TEXT_CHARS = 600
REPEATED_CUE_MIN_WORDS = 20

# Words of a typed instruction to answer what was just said ("answer", "answer every question
# now"), as opposed to a question. The 2026-10-26 standup typed these into the Ask box.
_COMMAND_VERBS = {"answer", "answers", "reply", "respond", "help", "suggest"}
_COMMAND_WORDS = _COMMAND_VERBS | {
    "please", "pls", "now", "it", "that", "this", "them", "all", "every", "each", "the", "to",
    "me", "question", "questions", "what", "was", "just", "said", "asked", "him", "her", "for",
}  # fmt: skip


# Validate only citation shape here, not whether an ID belongs to the supplied sources.
_CITATION_ID_MAX = 8


class CitationFilter:
    """Drop malformed citation markup with bounded, chunk-independent state.

    A malformed ``[n:`` consumes through ``]`` or the next newline, never the next
    paragraph. At most an 11-character possible tag is held; surrounding text and
    whitespace stream immediately. Code delimiters, escapes, simple index slices
    and ordinary link destinations protect literal uses of the reserved syntax.
    """

    def __init__(self) -> None:
        self.buf = ""
        self._discard = False
        self._last = ""
        self._index_prefix = False
        self._escaped = False
        self._code_marker: str = ""
        self._code_length = 0
        self._fenced_code = False
        self._closing_fence = False
        self._marker: str = ""
        self._marker_length = 0
        self._marker_leading = False
        self._indent: int | None = 0
        self._link_depth = 0

    def _emit(self, text: str, out: list[str]) -> None:
        out.append(text)
        self._last = text[-1:]
        for char in text:
            if char in "\r\n":
                self._indent = 0
            elif self._indent is not None:
                self._indent = self._indent + 1 if char == " " and self._indent < 3 else None

    def _start_marker(self, char: str, out: list[str]) -> None:
        self._marker, self._marker_length = char, 1
        self._marker_leading = self._indent is not None
        self._emit(char, out)

    def _finish_marker(self) -> None:
        if self._code_marker:
            if self._fenced_code:
                self._closing_fence = (
                    self._marker_leading and self._marker_length >= self._code_length
                )
            elif self._marker_length == self._code_length:
                self._code_marker = ""
        elif self._marker == "`" or (self._marker_leading and self._marker_length >= 3):
            self._code_marker = self._marker
            self._code_length = self._marker_length
            self._fenced_code = self._marker_leading and self._marker_length >= 3
        self._marker = ""
        self._marker_length = 0

    def feed(self, text: str) -> str:
        out: list[str] = []
        for char in text:
            if self._discard:
                if char == "]":
                    self._discard = False
                elif char in "\r\n":
                    self._discard = False
                    self._emit(char, out)
                continue
            if self.buf:
                if (self.buf == "[" and char.lower() == "n") or (
                    self.buf.lower() == "[n" and char == ":"
                ):
                    self.buf += char
                    continue
                if self.buf.lower().startswith("[n:"):
                    if char == "]":
                        id_length = len(self.buf) - 3
                        if 2 <= id_length <= _CITATION_ID_MAX or (
                            self._index_prefix and id_length <= 1
                        ):
                            self._emit(self.buf + char, out)
                        self.buf = ""
                    elif char.isascii() and char.isalnum() and len(self.buf) < 3 + _CITATION_ID_MAX:
                        self.buf += char
                    else:
                        self.buf = ""
                        if char in "\r\n":
                            self._emit(char, out)
                        else:
                            self._discard = True
                    continue
                self._emit(self.buf, out)
                self.buf = ""
            if self._marker:
                if char == self._marker:
                    self._marker_length += 1
                    self._emit(char, out)
                    continue
                self._finish_marker()
            if self._code_marker:
                if self._closing_fence:
                    if char in "\r\n":
                        self._code_marker = ""
                        self._closing_fence = False
                    elif char not in " \t":
                        self._closing_fence = False
                if char == self._code_marker:
                    self._start_marker(char, out)
                else:
                    self._emit(char, out)
                continue
            if self._escaped:
                self._emit(char, out)
                self._escaped = False
                continue
            if self._link_depth:
                self._emit(char, out)
                if char == "\\":
                    self._escaped = True
                elif char == "(":
                    self._link_depth += 1
                elif char == ")":
                    self._link_depth -= 1
                continue
            if char in "`~":
                self._start_marker(char, out)
            elif char == "\\":
                self._emit(char, out)
                self._escaped = True
            elif char == "[":
                self._index_prefix = self._last.isalnum() or self._last in ("_", "]", ")")
                self.buf = char
            else:
                if char == "(" and self._last == "]":
                    self._link_depth = 1
                self._emit(char, out)
        return "".join(out)

    def flush(self) -> str:
        out = "" if self._discard or self.buf.lower().startswith("[n:") else self.buf
        self.__init__()
        return out


def normalize_question(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode()).hexdigest()[:32]


def cacheable(text: str) -> bool:
    return len(normalize_question(text).split()) >= CACHE_MIN_WORDS


def is_answer_command(text: str) -> bool:
    """A typed instruction to answer what was just said, rather than a question of its own:
    a few words, no question mark, one of them a verb such as "answer" and the rest filler."""
    if "?" in text:
        return False
    words = normalize_question(text).split()
    return (
        0 < len(words) <= 6
        and any(w in _COMMAND_VERBS for w in words)
        and all(w in _COMMAND_WORDS for w in words)
    )


@dataclass(frozen=True)
class RequestSnapshot:
    """One generation's immutable question and prompt evidence before async lookup."""

    text: str
    context: prompts.PromptContext
    transcript: prompts.CallContext


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
    decided_ms: int | None = None  # t0 to the detector's decision (turn.Detection.decided_ms)
    depth: str | None = None  # "deep" or "light" from the detector (turn.Detection.depth)
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turn_end_ms: int | None = None
    final_ms: int | None = None  # t0 to the STT final (None: no final, or a forced question)
    early_ms: int | None = None  # its passes started this long before the speech end (turn.py)
    first_shown: bool = False  # shown before the detector had decided (turn.py)
    expanded_at: float | None = None  # monotonic time of the expand tap
    was_speculative: bool = False
    tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict[str, "asyncio.Task[None]"])
    started_passes: set[str] = field(default_factory=set[str])
    superseded: bool = False
    cancelled: bool = False
    # Timings that come late (the client's latency.report), per pass, merged into the
    # answer's log entry when they arrive first; `logged` holds the passes already logged.
    client: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    logged: set[str] = field(default_factory=set[str])
    # A held question (speculative fast pass started before the detector decided): its fast
    # pass's text is buffered here and nothing is sent until `release`; `discard` drops it.
    held: bool = False
    held_text: list[str] = field(default_factory=list[str])
    released: asyncio.Event = field(default_factory=asyncio.Event)
    released_at: float | None = None
    discarded: bool = False
    # Confirmed (not speculative any more), and the fast pass has ended: together they start
    # the automatic deep pass.
    confirmed: bool = False
    fast_ended: bool = False
    # The talking points as shown, so the deep pass goes beyond them instead of repeating.
    fast_text: str = ""
    delta_counts: dict[str, int] = field(default_factory=dict[str, int])
    plans: dict[str, AnswerPlan] = field(default_factory=dict[str, AnswerPlan])
    answer_strategy: Architecture | None = None  # frozen before scheduling this generation
    exposed: bool = False  # QuestionDetected metadata has reached the client
    answer_exposed: bool = False  # any delta (even whitespace), done or error is irreversible
    snapshots: dict[str, RequestSnapshot] = field(default_factory=dict[str, RequestSnapshot])
    retained_strategy: Architecture | None = None  # last late policy choice not applied
    preferences: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    # A screenshot question (coding mode): its shots, and the fast pass's reading of them.
    screen: "ScreenAsk | None" = None
    reading: dict[str, Any] | None = None
    code_unneeded: bool = False  # its code pass was stopped: the screen needs no code


@dataclass(frozen=True)
class ScreenAsk:
    """What a `screenshot` message asks: its shots, the answer mode, the user's language."""

    shots: tuple[screen.Shot, ...]
    answer_mode: str
    language: str | None
    default_language: str


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
        threshold: PreparedThreshold,
        embedding_model: str | None = None,
    ) -> None:
        self.embedder = embedder
        self.min_similarity = threshold.min_similarity
        # A wrong match shows points for another question: the best must also clearly beat
        # the runner-up, or two prepared questions are too alike to choose between.
        self.min_margin = threshold.min_margin
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

    async def match(
        self, text: str, embedding: "Awaitable[list[float] | None] | None" = None
    ) -> MatchResult | None:
        """`embedding` is the text's vector when the session already asked for it (memory
        search shares it), else the matcher embeds the text itself."""
        if self.embedder is None or not self.vectors:
            return None
        started = time.monotonic()
        if embedding is not None:
            vec = await asyncio.shield(embedding)
            if vec is None:
                return None
            query = vec
        else:
            try:
                [query] = await self.embedder.embed([text])
            except Exception as e:
                log.warning("prepared-answer embedding failed: %s", e)
                return None
        scored = sorted(
            ((cosine(query, vec), i) for i, (_, vec) in enumerate(self.vectors)), reverse=True
        )
        ms = int((time.monotonic() - started) * 1000)
        if not scored:
            return MatchResult(None, None, ms)
        best_sim, best_i = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        ok = best_sim >= self.min_similarity and best_sim - runner_up >= self.min_margin
        return MatchResult(self.vectors[best_i][0] if ok else None, best_sim, ms)


class AnswerHost(Protocol):
    settings: Settings
    router: ModelRouter

    def emit(self, cls: type[Any], **fields: Any) -> int: ...

    def log_entry(self, entry: Mapping[str, Any]) -> None: ...

    def store_text(self) -> bool:
        """Transcript consent: without it, question and answer text are never stored."""
        ...

    def call_context(self) -> prompts.CallContext:
        """The conversation so far: recent lines verbatim, the summary, earlier answers."""
        ...

    def prompt_context(self) -> prompts.PromptContext: ...

    def refs(self) -> dict[str, NodeRef]: ...

    def prepared_source_version(self, policy: str) -> str | None:
        """Current prepared source, or None when its freshness cannot be established."""
        return None

    def llm_status(self, ok: bool) -> None: ...

    def cache_version(self) -> str: ...

    def answer_style(self) -> str:
        return "bullets"

    def response_length(self) -> str:
        return "default"

    def cache_extra(self) -> dict[str, str]:
        """Fields stored with an exact-repeat answer (the company it was given at)."""
        ...

    async def memory_lines(self, text: str, pass_: str) -> tuple[list[str], dict[str, int]]:
        """Live memory search results for this pass, as `<memory>` lines, and their timings."""
        ...

    async def qa_get(self, qhash: str) -> dict[str, Any] | None: ...

    async def qa_put(self, qhash: str, value: Mapping[str, Any]) -> None: ...


def _transient_before_text(events: list[AttemptEvent]) -> bool:
    """Recovery needs complete typed evidence; mixed refusals and unknowns fail closed."""
    if not events or any(event.job != "fast" for event in events):
        return False
    # A refusal is a completed first chunk, even when another route later fails.
    # Reject any first chunk, completion, hedge cancellation, or incomplete lifecycle.
    if any(event.status not in {"started", "failed"} for event in events):
        return False
    started = [event.attempt_id for event in events if event.status == "started"]
    failures = [event for event in events if event.status == "failed"]
    if (
        not started
        or len(started) != len(set(started))
        or len(failures) != len(started)
        or {event.attempt_id for event in failures} != set(started)
    ):
        return False
    categories = {
        "connect_error",
        "connect_timeout",
        "pool_timeout",
        "read_timeout",
        "write_timeout",
        "provider_timeout",
        "first_token_timeout",
    }
    # SDK connection_error alone includes permanent TLS/configuration failures. Only
    # explicit abrupt-close/read/write/reset causes justify this bounded experiment.
    closure_causes = {
        "SSLZeroReturnError",
        "ConnectionResetError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
    }
    certificate_causes = {"SSLCertVerificationError", "CertificateError"}
    return all(
        event.stage == "first_chunk"
        and not certificate_causes.intersection(event.cause_types)
        and (
            event.http_status in {500, 502, 503, 504, 529} and event.error_category == "http_error"
            if event.http_status is not None
            else event.error_category in categories
            or (
                event.error_category == "connection_error"
                and bool(closure_causes.intersection(event.cause_types))
            )
        )
        for event in failures
    )


class AnswerEngine:
    def __init__(self, host: AnswerHost) -> None:
        self.host = host
        self.questions: OrderedDict[UUID, Question] = OrderedDict()
        self.stats = {
            "prepared_hits": 0, "cache_hits": 0, "model_passes": 0, "cancelled": 0,
            "deep_light": 0,
        }  # fmt: skip
        self._background: set[asyncio.Task[None]] = set()
        # Keep ownership after speculative cards are discarded or history is evicted.
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        # The last screen's problem, for the follow-ups after it (copilot_core.screen.Problem).
        self.problem: screen.Problem | None = None

    # Question lifecycle, driven by the turn detector and client messages.

    def add(self, q: Question) -> None:
        q.answer_strategy = q.answer_strategy or self._selected_strategy(q)
        q.exposed = q.exposed or not q.held
        self.questions[q.qid] = q
        while len(self.questions) > 100:
            _, old = self.questions.popitem(last=False)
            self._cancel_tasks(old)
        self.start_pass(q, "fast")
        if q.screen is not None and self.host.settings.screen_code_start == "with_fast":
            # The code pass at once, reading the screen itself: the reading takes 1 to 3 s,
            # and the deep model's first token 2 to 6 s more (2026-11-18).
            self.start_pass(q, "deep")

    def confirm(
        self,
        qid: UUID,
        text: str,
        turn_end_ms: int | None,
        final_ms: int | None = None,
        *,
        supersede: bool = True,
    ) -> None:
        """`supersede` False (a card that follows the user) leaves older answers running: the
        follow continues the answer the user is reading, it does not replace it."""
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
        q.confirmed = True
        if supersede:
            self.supersede(keep=qid)
        self._log_question(q, kept=True)
        self._auto_deep(q)

    def _auto_deep(self, q: Question) -> None:
        """The deep pass starts on its own once the question is confirmed and its fast pass
        has ended, for the auto types; never for a question a newer one has replaced."""
        if self.questions.get(q.qid) is not q:
            return
        s = self.host.settings
        if q.screen is not None:
            # The code pass: started from the reading (`_on_read`), or here when the fast pass
            # ended without one (it failed, or left the header out), unless nothing is readable.
            if q.fast_ended and screen.needs_code(q.reading) and not q.superseded:
                self.start_pass(q, "deep")
            return
        strategy = self.strategy(q)
        if strategy != "legacy_dual":
            # An independent hybrid expert can start at confirmation, without waiting
            # for the primary. B and D run a second request only on explicit expansion.
            if (
                q.confirmed
                and s.deep_pass_auto
                and not (q.superseded or q.cancelled or q.discarded)
                and plan_answer(q.text, q.qtype, q.depth, strategy).auto_expert
            ):
                self.start_pass(q, "deep")
            return
        if not s.deep_pass_auto or q.qtype not in s.deep_pass_auto_types:
            return
        if not (q.confirmed and q.fast_ended) or q.superseded or q.cancelled or q.discarded:
            return
        self.start_pass(q, "deep")

    def _on_read(self, q: Question, reading: dict[str, Any] | None) -> None:
        """The fast pass's reading of the screen arrived: shown with the question, kept for
        the code pass, which starts now when a task needs code."""
        q.reading = reading
        if reading is None:
            return
        q.text = screen.summary(reading)
        self.host.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=q.text,
            question_type=q.qtype,
            confidence=q.confidence,
            speculative=False,
            screen=screen.wire_reading(reading),
        )
        self.host.log_entry(
            {
                "kind": "screen_read",
                "question_id": str(q.qid),
                "kinds": [t["kind"] for t in reading["tasks"]],
                **({"language": reading["language"]} if "language" in reading else {}),
                # Titles, signatures and approaches are the problem's text: consent only.
                **({"reading": reading} if self.host.store_text() else {}),
            }
        )
        if screen.needs_code(reading):
            if not (q.superseded or q.cancelled):
                self.start_pass(q, "deep")
        elif (task := q.tasks.get("deep")) is not None and not task.done():
            # Started with the shot, and nothing on the screen needs it.
            q.code_unneeded = True
            task.cancel()

    def release(self, qid: UUID) -> None:
        """The detector confirmed a held question (the session has just sent it): what its
        fast pass buffered goes out now, the rest streams as it comes."""
        q = self.questions.get(qid)
        if q is None or not q.held:
            return
        q.exposed = True
        q.held = False
        q.released_at = time.monotonic()
        if q.held_text:
            self._emit_delta(q, "fast", "".join(q.held_text))
            q.held_text.clear()
        q.released.set()

    def discard(self, qid: UUID) -> None:
        """A held question the detector did not confirm, or whose speaker kept talking: its
        pass stops and nothing is sent, so the user never sees a false start."""
        q = self.questions.pop(qid, None)
        if q is None:
            return
        q.discarded = True
        self._cancel_tasks(q)

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
        if q is None or q.cancelled or q.discarded or q.superseded:
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
        for task in self._tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    async def shutdown(self, *, within_s: float = 5) -> bool:
        """Finish cancellation telemetry before the session releases its writer lease."""
        self._closing = True
        self.cancel_all()
        pending: set[asyncio.Task[Any]] = set(self._tasks) | set(self._background)
        pending.update(q.match for q in self.questions.values() if q.match is not None)
        for task in pending:
            if not task.done() and not task.cancelling():
                task.cancel()
        if not pending:
            return True
        # wait_for would inject a second cancellation into provider cleanup on timeout.
        _, unfinished = await asyncio.wait(pending, timeout=within_s)
        return not unfinished

    async def change_language(self) -> None:
        """Keep completed cards while stopping language-dependent pending generation."""
        matches: list[asyncio.Task[object]] = []
        unstarted: list[tuple[Question, str]] = []
        for q in self.questions.values():
            unstarted.extend(
                (q, pass_)
                for pass_, task in q.tasks.items()
                if not task.done() and pass_ not in q.started_passes
            )
            if any(not task.done() for task in q.tasks.values()):
                # Send cancellation for unfinished visible passes so their UI spinners stop.
                q.superseded = True
            if q.match is not None:
                q.match.cancel()
                matches.append(q.match)
                q.match = None
        self.cancel_all()
        # A task cancelled before its first coroutine step cannot run its error handler.
        for q, pass_ in unstarted:
            self.host.emit(
                models.AnswerError, question_id=q.qid, **{"pass": pass_}, code="cancelled"
            )
            self.host.log_entry(
                {
                    "kind": "answer_error",
                    "question_id": str(q.qid),
                    "pass": pass_,
                    "code": "cancelled",
                }
            )
        background = list(self._background)
        for task in background:
            task.cancel()
        await self.wait_idle()
        await asyncio.gather(*background, *matches, return_exceptions=True)

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
            cache_write_tokens=u.cache_write_tokens,
            cache_write_30m_tokens=u.cache_write_30m_tokens,
            cache_write_12h_tokens=u.cache_write_12h_tokens,
        )  # fmt: skip
        return None if cost is None else float(cost)

    def _cancel_tasks(self, q: Question) -> None:
        for task in q.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()

    def start_pass(self, q: Question, pass_: str) -> None:
        if self._closing or pass_ in q.tasks or q.cancelled or q.discarded or q.superseded:
            return
        q.answer_strategy = q.answer_strategy or self._selected_strategy(q)
        if self.strategy(q) != "legacy_dual" and pass_ not in q.plans:
            q.plans[pass_] = plan_answer(
                q.text, q.qtype, q.depth, self.strategy(q), expert=pass_ == "deep"
            )
        task = asyncio.create_task(self._run_pass(q, pass_))
        q.tasks[pass_] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _selected_strategy(self, q: Question) -> Architecture:
        # Screenshot reading/code and follow-ups retain their specialized prompts.
        if q.screen is not None or self.open_problem() is not None:
            return "legacy_dual"
        settings = self.host.settings
        strategy = settings.answer_architecture
        if (
            strategy != "legacy_dual"
            and settings.adaptive_technical_legacy
            and q.qtype == "technical"
            and plan_answer(q.text, q.qtype, q.depth, strategy).depth == "standard"
        ):
            return "legacy_dual"
        return strategy

    def strategy(self, q: Question) -> Architecture:
        return q.answer_strategy or self._selected_strategy(q)

    def reconcile_strategy(self, q: Question) -> Question:
        """Replace a generation before answer content or terminal state is exposed.

        Once any answer event or expert request exists, retain the frozen generation.
        An exposed legacy card cannot become adaptive: the client may already predict its
        dependent detail. No classifier, question cancellation or protocol reset is added.
        """
        if self.questions.get(q.qid) is not q or q.cancelled or q.discarded or q.superseded:
            return q
        desired = self._selected_strategy(q)
        current = self.strategy(q)
        primary = q.plans.get("fast")
        # A delayed classifier may identify an expert question after the provisional
        # standard request started. Upgrade only before exposure; C/D and all other
        # depth transitions retain their existing frozen-plan behavior.
        upgrade = (
            current == desired == "adaptive"
            and primary is not None
            and primary.depth == "standard"
            and plan_answer(q.text, q.qtype, q.depth, current).depth == "expert"
        )
        if desired == current and not upgrade:
            return q
        shown = q.exposed or not q.held
        if (
            q.answer_exposed
            or q.expanded_at is not None
            or "deep" in q.tasks
            or q.fast_ended
            or q.logged
            or (shown and not upgrade and (current == "legacy_dual" or desired != "legacy_dual"))
        ):
            if q.retained_strategy != desired:
                q.retained_strategy = desired
                self.host.log_entry(
                    {
                        "kind": "answer_strategy_retained",
                        "question_id": str(q.qid),
                        "answer_strategy": current,
                        "desired_strategy": desired,
                        "expert_upgrade": upgrade,
                        "reason": "answer_or_expert_exposed"
                        if q.answer_exposed or q.expanded_at is not None or "deep" in q.tasks
                        else "generation_stable",
                    }
                )
            return q
        # Shared matching and frozen context/preferences transfer; revised question text
        # belongs to the replacement request. Old tasks retain only their retired object.
        replacement = Question(
            qid=q.qid,
            text=q.text,
            qtype=q.qtype,
            confidence=q.confidence,
            addressed=q.addressed,
            speculative=q.speculative,
            forced=q.forced,
            t0=q.t0,
            detector_ms=q.detector_ms,
            match=q.match,
            decided_ms=q.decided_ms,
            depth=q.depth,
            detected_at=q.detected_at,
            turn_end_ms=q.turn_end_ms,
            final_ms=q.final_ms,
            early_ms=q.early_ms,
            first_shown=q.first_shown,
            held=q.held,
            confirmed=q.confirmed,
            released_at=q.released_at if shown else None,
            was_speculative=q.was_speculative,
            exposed=shown,
            answer_strategy=desired,
            preferences=dict(q.preferences),
            snapshots={
                key: value if value.text == q.text else replace(value, text=q.text)
                for key, value in q.snapshots.items()
            },
        )
        q.match = None
        q.discarded = True
        self._cancel_tasks(q)
        self.host.log_entry(
            {
                "kind": "answer_strategy_replaced",
                "question_id": str(q.qid),
                "previous_strategy": current,
                "answer_strategy": desired,
                "reason": "pre_answer_expert_upgrade"
                if upgrade
                else "empty_card"
                if shown
                else "unexposed_held",
            }
        )
        self.add(replacement)
        if shown:
            # Synchronous metadata update precedes every task step and replacement delta;
            # forced/non-speculative confirmation may otherwise emit no detection at all.
            self.host.emit(
                models.QuestionDetected,
                question_id=replacement.qid,
                text=replacement.text,
                question_type=replacement.qtype,
                confidence=replacement.confidence,
                speculative=replacement.speculative,
                answer_strategy=desired,
            )
        return replacement

    def _adaptive_evidence(self, q: Question) -> bool:
        # A category-specific legacy prompt must not bypass adaptive deployment freshness.
        # Explicit legacy rollback and specialized screenshot workflows stay unchanged.
        return (
            self.host.settings.answer_architecture != "legacy_dual"
            and q.screen is None
            and self.open_problem() is None
        )

    def _fresh_cache_slot(
        self,
        strategy: Architecture,
        plan: AnswerPlan | None,
        slot: str,
        style: str,
        version: str,
        request: LLMRequest | None,
    ) -> str:
        # A model/reasoning policy change must not keep serving the old model's answer.
        # Include fallback policy too; only hashes leave this scope, never private text.
        routing = tuple(
            (
                job,
                tuple(
                    (route.provider.name, route.model, route.params, route.refusal_only)
                    for route in self.host.router.routes.get(job, ())
                ),
            )
            for job in ("fast", "deep", "deep_light", "adaptive_expert")
        )
        identity = hashlib.sha256(
            (
                version
                + "\n"
                + prompts.ADAPTIVE_PROMPT_VERSION
                + "\n"
                + repr(request)
                + "\n"
                + repr(routing)
            ).encode()
        ).hexdigest()[:24]
        scope = f"{strategy}:{plan.depth}:{plan.job}" if plan is not None else "legacy-policy"
        return f"{scope}:{slot}:style={style}:{identity}"

    def _emit_delta(self, q: Question, pass_: str, text: str) -> None:
        if (
            not text
            or self.questions.get(q.qid) is not q
            or q.cancelled
            or q.discarded
            or q.superseded
        ):
            return
        q.answer_exposed = True
        sequence = q.delta_counts.get(pass_, 0)
        self.host.emit(
            models.AnswerDelta,
            question_id=q.qid,
            **{"pass": pass_},
            text=text,
            sequence=sequence,
        )
        q.delta_counts[pass_] = sequence + 1

    async def wait_idle(self) -> None:
        # Completion of a primary may enqueue a dependent legacy deep pass.
        while tasks := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*tasks, return_exceptions=True)

    # One pass.

    async def _run_pass(self, q: Question, pass_: str) -> None:
        q.started_passes.add(pass_)
        host = self.host
        style, response_length = self._preferences(q, pass_)
        q.preferences[pass_] = (style, response_length)
        started = time.monotonic()
        state: dict[str, Any] = {
            "first_delta_at": None, "parts": [], "start_at": None, "emitted": False,
            "stopped": False, "first_sentence_at": None,
            "stages": {}, "router_started_at": None, "raw_first_chunk_at": None,
            "detector_depth_at_start": q.depth, "question_type_at_start": q.qtype,
            "recovery": {},
        }  # fmt: skip
        cache_read: asyncio.Task[None] | None = None
        cache_value: dict[str, Any] | None = None
        boundary = SentenceBoundary()
        strategy = self.strategy(q)
        plan = None
        if strategy != "legacy_dual":
            plan = q.plans.get(pass_) or plan_answer(
                q.text, q.qtype, q.depth, strategy, expert=pass_ == "deep"
            )
            q.plans[pass_] = plan

        def current() -> bool:
            return self.questions.get(q.qid) is q and not (q.cancelled or q.discarded)

        def require_current() -> None:
            if not current():
                raise asyncio.CancelledError

        def delta(text: str) -> None:
            if not current() or q.superseded:
                return
            if state["first_delta_at"] is None:
                state["first_delta_at"] = time.monotonic()
            state["parts"].append(text)
            if boundary.feed(text):
                state["first_sentence_at"] = time.monotonic()
            if q.held:  # nothing leaves the gateway before the detector confirms
                q.held_text.append(text)
                return
            state["emitted"] = True
            self._emit_delta(q, pass_, text)

        def latency(first_token_ms: int | None) -> dict[str, int]:
            out: dict[str, int] = {}
            if q.turn_end_ms is not None:
                out["turn_end_ms"] = q.turn_end_ms
            if q.detector_ms is not None:
                out["detector_ms"] = q.detector_ms
            if q.decided_ms is not None:
                out["decided_ms"] = q.decided_ms
            if first_token_ms is not None:
                out["first_token_ms"] = first_token_ms
            if state["first_delta_at"] is not None:
                # A held pass's first words reach the client at its release, not earlier.
                shown = max(state["first_delta_at"], q.released_at or 0.0)
                out["first_delta_ms"] = max(0, int((shown - q.t0) * 1000))
            if q.released_at is not None:
                out["release_ms"] = max(0, int((q.released_at - q.t0) * 1000))
            if state["first_sentence_at"] is not None:
                sentence_shown = max(state["first_sentence_at"], q.released_at or 0.0)
                out["first_complete_sentence_ms"] = max(0, int((sentence_shown - q.t0) * 1000))
            out["total_ms"] = max(0, int((time.monotonic() - q.t0) * 1000))
            if q.final_ms is not None:
                out["final_ms"] = q.final_ms
            if q.early_ms is not None:
                out["early_ms"] = q.early_ms
            if q.first_shown:
                out["first_shown"] = 1
            if state["start_at"] is not None:
                out["start_ms"] = max(0, int((state["start_at"] - q.t0) * 1000))
            if pass_ == "deep" and q.expanded_at is not None:
                out["expand_ms"] = max(0, int((q.expanded_at - q.t0) * 1000))
            if pass_ == "fast" and (m := self._match_result(q)) is not None:
                out["prepared_ms"] = m.ms
            out.update(state.get("memory") or {})
            return out

        def stage_timings() -> dict[str, int]:
            # Router wait includes retries, not separately measured DNS/TLS or compute.
            # Keep these in stored metadata; client receipt and physical paint differ.
            out = dict(state["stages"])
            dispatched = state["router_started_at"]
            first = state["raw_first_chunk_at"]
            sentence = state["first_sentence_at"]
            if dispatched is not None:
                out["answer_prepare_ms"] = max(0, round((dispatched - started) * 1000))
                out["model_dispatch_ms"] = max(0, round((dispatched - q.t0) * 1000))
            if first is not None and dispatched is not None:
                out["router_first_chunk_ms"] = max(0, round((first - dispatched) * 1000))
            if sentence is not None and first is not None:
                out["stream_sentence_ms"] = max(0, round((sentence - first) * 1000))
            if sentence is not None and q.released_at is not None:
                out["sentence_release_wait_ms"] = max(0, round((q.released_at - sentence) * 1000))
            return out

        def done(
            source: str, refs: list[NodeRef], done_info: LLMDone | None, first_token_ms: int | None
        ) -> None:
            require_current()
            q.answer_exposed = True
            if boundary.feed("", final=True):
                state["first_sentence_at"] = time.monotonic()
            usage = done_info.usage if done_info else None
            tier = done_info.service_tier if done_info else "standard"
            if pass_ == "fast":
                q.fast_text = "".join(state["parts"])
            if q.screen is not None:
                self._keep_problem(q, pass_, "".join(state["parts"]))
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
                delta_count=q.delta_counts.get(pass_, 0),
            )
            # Stored with the answer only: which route answered, and the client's first word
            # if its latency.report came before this.
            configured_reasoning: dict[str, Any] = {}
            routes = host.router.routes.get(str(state.get("job")), [])
            if done_info is not None and 0 <= done_info.attempt < len(routes):
                params = routes[done_info.attempt].params
                configured_reasoning = {
                    key: params[key]
                    for key in ("reasoning", "thinking", "output_config")
                    if key in params
                }
            stored = {
                **lat,
                **stage_timings(),
                **state["recovery"],
                "provider": done_info.provider if done_info else source,
                "fallback_attempt": done_info.attempt if done_info else 0,
                "stop_reason": done_info.stop_reason if done_info else None,
                # Existing worker persistence keeps this metadata in answers.latency_ms.
                "answer_strategy": strategy,
                "prompt_version": prompts.ADAPTIVE_PROMPT_VERSION if plan is not None else None,
                "answer_style": style,
                "response_length": response_length,
                "answer_depth": plan.depth if plan is not None else None,
                "question_type_at_start": state["question_type_at_start"],
                "detector_depth_at_start": state["detector_depth_at_start"],
                "detector_depth_at_completion": q.depth,
                "configured_reasoning": configured_reasoning,
                "request_job": state.get("job"),
                "max_output_tokens": state["req"].max_tokens if "req" in state else None,
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
                    "reasoning_tokens": usage.reasoning_tokens if usage else None,
                    "cache_write_tokens": usage.cache_write_tokens if usage else None,
                    "cache_write_30m_tokens": usage.cache_write_30m_tokens if usage else None,
                    "cache_write_12h_tokens": usage.cache_write_12h_tokens if usage else None,
                    "cost_usd": self._cost(done_info),
                    "latency_ms": stored,
                    "memory_node_ids": [str(r.node_id) for r in refs],
                    # The deep pass ran because the user tapped expand, not on its own.
                    "expanded": pass_ == "deep" and q.expanded_at is not None,
                    "answer_strategy": strategy,
                    "answer_style": style,
                    "response_length": response_length,
                    "answer_depth": plan.depth if plan is not None else None,
                    "request_job": state.get("job"),
                    "max_output_tokens": state["req"].max_tokens if "req" in state else None,
                }
            )

        async def shown() -> None:
            """A held pass waits here for the detector before it ends; if the detector says
            no, `discard` cancels it here and it ends unseen."""
            require_current()
            if q.held:
                await q.released.wait()
            require_current()

        def error(code: str) -> None:
            if not current():
                return
            q.answer_exposed = True
            host.emit(models.AnswerError, question_id=q.qid, **{"pass": pass_}, code=code)
            host.log_entry(
                {
                    "kind": "answer_error",
                    "question_id": str(q.qid),
                    "pass": pass_,
                    "code": code,
                    **state["recovery"],
                }
            )

        async def prepared(answer: PreparedAnswer) -> bool:
            if self._adaptive_evidence(q):
                # Nothing prepared enters held_text: release must first expose the latest
                # transcript/source state. A changed fact falls through to fresh generation.
                await shown()
                if self._prepared(q) is not answer:
                    return False
            self.stats["prepared_hits"] += 1
            delta("\n".join(f"- {p}" for p in answer.talking_points) + "\n")
            await shown()
            done("prepared", self._refs_for_nodes(answer), None, None)
            return True

        try:
            if pass_ == "fast":
                state["start_at"] = time.monotonic()
                answer = self._prepared(q) if response_length == "default" else None
                if answer is not None and await prepared(answer):
                    return  # the match is current at display time: no model call
            # Prepared validation can wait for a final or a changed source. Snapshot
            # only when fresh generation begins, before retrieval/cache awaits.
            require_current()
            snapshot = q.snapshots.get(pass_) or self._request_snapshot(q)
            q.snapshots[pass_] = snapshot
            cache_version = host.cache_version()
            qhash = question_hash(snapshot.text)
            # A follow has the question's text but continues the user: never the cache. Nor
            # a screenshot: its text says what the screen holds, not what it asks; nor a
            # question while a problem is open: "optimize it" means that problem.
            reuse = (
                not host.settings.personal_ai_api_url
                and cacheable(snapshot.text)
                and q.qtype != "follow"
                and q.screen is None
                and self.open_problem() is None
            )
            # A paragraph deep answer is cached apart from a bulleted one.
            slot = pass_ if pass_ == "fast" or style == "bullets" else f"{pass_}_{style}"
            if response_length != "default":
                slot += f":length={response_length}"
            memory_pass = "deep" if plan is not None else pass_
            memory: list[str] | None = None
            candidate_req: LLMRequest | None = None
            fresh_cache = self._adaptive_evidence(q)
            if fresh_cache:
                if reuse:

                    async def prefetch_cache() -> None:
                        nonlocal cache_value
                        lookup_started = time.monotonic()
                        try:
                            cache_value = await self._cache_value(qhash)
                        finally:
                            # Full lookup duration, including work overlapped with retrieval.
                            state["stages"]["cache_lookup_ms"] = round(
                                (time.monotonic() - lookup_started) * 1000
                            )

                    cache_read = asyncio.create_task(prefetch_cache())
                    # Retain ownership even if this pass is cancelled again during cleanup.
                    self._tasks.add(cache_read)
                    cache_read.add_done_callback(self._tasks.discard)
                # A digest/pack version does not version live company memory. Resolve
                # the same bounded evidence we would generate with before reusing an
                # answer, and freeze the request so later speech cannot change its key.
                state["start_at"] = time.monotonic()
                context_started = time.monotonic()
                memory, state["memory"] = await host.memory_lines(snapshot.text, memory_pass)
                require_current()
                state["stages"]["context_wait_ms"] = round(
                    (time.monotonic() - context_started) * 1000
                )
                if not host.settings.personal_ai_api_url:
                    prompt_started = time.monotonic()
                    candidate_req = self._request(q, pass_, memory, snapshot=snapshot)
                    state["stages"]["prompt_build_ms"] = round(
                        (time.monotonic() - prompt_started) * 1000
                    )
                slot = self._fresh_cache_slot(
                    strategy, plan, slot, style, cache_version, candidate_req
                )
            if cache_read is not None:
                await asyncio.shield(cache_read)
                require_current()
                # A prefetched value is eligible only for the freshly resolved evidence.
                cached = self._cache_entry(cache_value, slot, cache_version)
            else:
                cache_started = time.monotonic()
                cached = await self._cached(qhash, slot, cache_version) if reuse else None
                if reuse:
                    state["stages"]["cache_lookup_ms"] = round(
                        (time.monotonic() - cache_started) * 1000
                    )
            require_current()
            if cached is not None and not self._repeats_cues(q, cached["text"]):
                self.stats["cache_hits"] += 1
                delta(cached["text"])
                refs = [NodeRef.model_validate(r) for r in cached.get("refs", [])]
                await shown()
                done("cache", refs, None, None)
                return
            self.stats["model_passes"] += 1
            if not fresh_cache:
                state["start_at"] = time.monotonic()
            if q.screen is not None:
                job = "screen" if pass_ == "fast" else "screen_code"
            else:
                job = (
                    plan.job
                    if plan is not None
                    else (self.deep_job(q) if pass_ == "deep" else pass_)
                )
            # Keep the frozen plan/public pass; isolate only the actual adaptive expert route.
            if (
                plan is not None
                and job == "deep"
                and host.settings.adaptive_expert_route is not None
            ):
                job = "adaptive_expert"
            state["job"] = job
            if job == "deep_light":
                self.stats["deep_light"] += 1
            if memory is None:
                context_started = time.monotonic()
                memory, state["memory"] = (
                    await host.memory_lines(snapshot.text, memory_pass)
                    if q.screen is None
                    else ([], {})
                )
                state["stages"]["context_wait_ms"] = round(
                    (time.monotonic() - context_started) * 1000
                )
            if host.settings.personal_ai_api_url:
                private = getattr(host, "private_memory", None)
                if private is not None:
                    context_started = time.monotonic()
                    memory = [*memory, *(await private())]
                    state["stages"]["private_context_wait_ms"] = round(
                        (time.monotonic() - context_started) * 1000
                    )
            require_current()
            if candidate_req is not None:
                req = candidate_req
            else:
                prompt_started = time.monotonic()
                req = self._request(q, pass_, memory, snapshot=snapshot)
                state["stages"]["prompt_build_ms"] = round(
                    (time.monotonic() - prompt_started) * 1000
                )
            state["req"] = req

            attempt_events: list[AttemptEvent] = []

            def on_attempt(event: AttemptEvent) -> None:
                attempt_events.append(event)
                # This ledger includes abandoned retries; do not sum it with answer costs.
                entry = {
                    **asdict(event),
                    "kind": "provider_attempt",
                    "question_id": str(q.qid),
                    "pass": pass_,
                    "answer_strategy": strategy,
                    "usage_observed": event.usage is not None,
                    **state["recovery"],
                }
                if event.usage is not None:
                    entry["cost_usd"] = self._cost(
                        LLMDone(
                            usage=event.usage,
                            stop_reason=None,
                            model=event.model,
                            service_tier=event.service_tier or "standard",
                        )
                    )
                host.log_entry(entry)

            def on_fallback(job: str, route: Route, reason: str) -> None:
                if current():
                    host.llm_status(False)

            async def routed_chunks() -> AsyncGenerator[str | LLMDone]:
                try:
                    async with contextlib.aclosing(
                        host.router.stream(job, req, on_fallback, on_attempt=on_attempt)
                    ) as primary:
                        async for chunk in primary:
                            yield chunk
                except AllRoutesFailedError as failure:
                    recovery_routes = host.router.routes.get("answer_recovery", [])
                    current = asyncio.current_task()
                    if (
                        not host.settings.adaptive_failure_recovery
                        or strategy not in {"adaptive", "adaptive_on_demand"}
                        or pass_ != "fast"
                        or plan is None
                        or plan.job != "fast"
                        or job != "fast"
                        or failure.refused
                        or self._closing
                        or q.cancelled
                        or q.discarded
                        or q.superseded
                        or (current is not None and current.cancelling())
                        or state["raw_first_chunk_at"] is not None
                        or state["parts"]
                        or state["emitted"]
                        or q.held_text
                        or q.fast_text
                        or q.delta_counts.get(pass_, 0)
                        or not _transient_before_text(attempt_events)
                        or len(recovery_routes) != 1
                        or recovery_routes[0].refusal_only
                        or host.router.hedge_after_s.get("answer_recovery")
                    ):
                        raise
                    route = recovery_routes[0]
                    recovery = {
                        "failure_recovery": True,
                        "failure_recovery_reason": "transient_primary_exhausted_before_text",
                        "failure_recovery_start_ms": max(0, int((time.monotonic() - q.t0) * 1000)),
                        "failure_recovery_primary_job": job,
                        "failure_recovery_job": "answer_recovery",
                        "failure_recovery_model": route.model,
                        "failure_recovery_provider": route.provider.name,
                        "failure_recovery_categories": sorted(
                            {
                                event.error_category
                                for event in attempt_events
                                if event.status == "failed" and event.error_category is not None
                            }
                        ),
                    }
                    state["recovery"] = recovery
                    state["job"] = "answer_recovery"
                    host.log_entry(
                        {
                            "kind": "answer_recovery",
                            "question_id": str(q.qid),
                            "pass": pass_,
                            "answer_strategy": strategy,
                            **recovery,
                        }
                    )
                    # Same original frozen question/evidence/depth/output cap, no quick
                    # answer expansion. The outer bounded_chunks still owns ONE deadline.
                    async with contextlib.aclosing(
                        host.router.stream(
                            "answer_recovery", req, on_fallback, on_attempt=on_attempt
                        )
                    ) as recovered:
                        async for chunk in recovered:
                            yield chunk

            async def stream() -> LLMDone:
                finished: LLMDone | None = None
                cites = CitationFilter()
                reader = screen.ReadSplitter() if q.screen is not None and pass_ == "fast" else None

                def shown_text(chunk: str) -> str:
                    if q.screen is None:
                        return cites.feed(chunk)
                    if reader is None or reader.done:
                        return chunk
                    text = reader.feed(chunk)
                    if reader.done:
                        self._on_read(q, reader.reading)
                    return text

                chunks = (
                    routed_chunks()
                    if host.settings.adaptive_failure_recovery
                    else host.router.stream(job, req, on_fallback, on_attempt=on_attempt)
                )
                if strategy != "legacy_dual":
                    chunks = bounded_chunks(
                        chunks,
                        host.settings.adaptive_stream_idle_timeout_s,
                        host.settings.adaptive_stream_total_timeout_s,
                    )
                state["router_started_at"] = time.monotonic()
                async for chunk in chunks:
                    if isinstance(chunk, LLMDone):
                        finished = chunk
                        state["done"] = chunk
                    else:
                        require_current()
                        if chunk and state["raw_first_chunk_at"] is None:
                            state["raw_first_chunk_at"] = time.monotonic()
                        if text := shown_text(chunk):
                            delta(text)
                require_current()
                rest = cites.flush() if q.screen is None else (reader.flush() if reader else "")
                if rest:
                    delta(rest)
                assert finished is not None
                return finished

            model = asyncio.ensure_future(stream())
            state["model"] = model
            match = q.match
            if (
                strategy == "legacy_dual"
                and pass_ == "fast"
                and match is not None
                and not match.done()
            ):
                # Candidates keep already-dispatched model progress rather than cancel it
                # for a late prepared hit that could become stale before held release.
                # The model does not wait for the prepared match (a forced question's match
                # starts with it; an OpenAI embedding takes 0.5 s or more). A hit that comes
                # before any of the model's text reached the client takes the slot over.
                await asyncio.wait({match, model}, return_when=asyncio.FIRST_COMPLETED)
                answer = self._prepared(q) if response_length == "default" else None
                if answer is not None and not state["emitted"]:
                    model.cancel()
                    await asyncio.gather(model, return_exceptions=True)
                    self._log_cancelled(q, pass_, state, reason="prepared")
                    state.update(first_delta_at=None, first_sentence_at=None, parts=[])
                    state.update(router_started_at=None, raw_first_chunk_at=None, stages={})
                    boundary = SentenceBoundary()
                    for key in ("req", "done", "model"):
                        state.pop(key, None)
                    q.held_text.clear()
                    self.stats["model_passes"] -= 1
                    await prepared(answer)
                    return
            done_info = await model
            await shown()
            if done_info.attempt == 0:
                host.llm_status(True)
            if done_info.stop_reason == "refusal":
                error("refused")
                return
            text = "".join(state["parts"])
            refs = cited_refs(text, host.refs()) if q.screen is None else []
            done("model", refs, done_info, done_info.first_token_ms)
            if (
                current()
                and not q.superseded
                and host.store_text()
                and reuse
                and not state["recovery"]
            ):
                task = asyncio.create_task(self._cache_put(qhash, slot, text, refs, cache_version))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        except TimeoutError:
            state["stopped"] = True
            self._log_cancelled(q, pass_, state, reason="timeout")
            if current():
                host.llm_status(False)
                await shown()
                error("timeout")
        except AllRoutesFailedError as e:
            if not current():
                state["stopped"] = True
                self._log_cancelled(q, pass_, state)
                return
            log.warning("%s pass failed for %s: %s", pass_, q.qid, e)
            await shown()
            if e.refused:  # every model that ran declined: not an outage
                error("refused")
                return
            host.llm_status(False)
            error("llm_unavailable")
        except asyncio.CancelledError:
            state["stopped"] = True
            if (model_task := state.get("model")) is not None:
                model_task.cancel()
                await asyncio.gather(model_task, return_exceptions=True)
            if q.superseded:
                error("cancelled")
            self._log_cancelled(q, pass_, state)
            raise
        except Exception:
            if not current():
                state["stopped"] = True
                self._log_cancelled(q, pass_, state)
                return
            log.exception("%s pass crashed for %s", pass_, q.qid)
            error("internal")
        finally:
            try:
                if cache_read is not None and not cache_read.done():
                    if not cache_read.cancelling():
                        cache_read.cancel()
                    # wait does not inject another cancellation into the read's cleanup.
                    # The engine retains the child if this pass is interrupted again.
                    await asyncio.wait({cache_read})
            except asyncio.CancelledError:
                state["stopped"] = True
                raise
            finally:
                log.debug(
                    "%s pass for %s took %.0f ms", pass_, q.qid, (time.monotonic() - started) * 1000
                )
                if pass_ == "fast" and not state["stopped"] and current():
                    # Done, failed or refused: the deep pass may start now.
                    q.fast_ended = True
                    self._auto_deep(q)

    def _log_cancelled(
        self, q: Question, pass_: str, state: Mapping[str, Any], reason: str | None = None
    ) -> None:
        """A pass stopped before its end still costs what the provider read and wrote, but the
        stream ends without usage: log a visible-text estimate (about 4 characters a token).
        Cache hits and unobserved output/reasoning are unknown. Assume no cache discount,
        label the estimate, and never mistake it for billed usage or an upper bound."""
        if reason is not None:
            pass  # the caller knows: a prepared answer took the slot over
        elif q.superseded:
            reason = "superseded"
        elif q.discarded:
            reason = "held_discarded"
        elif q.cancelled:
            reason = "speculative"
        elif q.code_unneeded:
            reason = "code_unneeded"
        else:
            reason = "stopped"
        entry: dict[str, Any] = {
            "kind": "pass_cancelled",
            "question_id": str(q.qid),
            "pass": pass_,
            "reason": reason,
            "model_started": "req" in state,
            "streamed_chars": sum(len(p) for p in state["parts"]),
            "answer_strategy": self.strategy(q),
            "response_length": self._preferences(q, pass_)[1],
            "request_job": state.get("job"),
            "question_type_at_start": state.get("question_type_at_start"),
            "detector_depth_at_start": state.get("detector_depth_at_start"),
            "question_type_at_cancellation": q.qtype,
            "detector_depth_at_cancellation": q.depth,
            **state.get("recovery", {}),
        }
        req = cast(LLMRequest | None, state.get("req"))
        routes = self.host.router.routes.get(str(state.get("job") or pass_)) or []
        finished = cast(LLMDone | None, state.get("done"))
        if finished is not None:
            # The pass ran to its end while held, so its real usage is known.
            u = finished.usage
            entry.update(
                model=finished.model,
                tier=finished.service_tier,
                stop_reason=finished.stop_reason,
                input_tokens=u.input_tokens,
                cached_tokens=u.cached_tokens,
                output_tokens=u.output_tokens,
                reasoning_tokens=u.reasoning_tokens,
                cache_write_tokens=u.cache_write_tokens,
                cache_write_30m_tokens=u.cache_write_30m_tokens,
                cache_write_12h_tokens=u.cache_write_12h_tokens,
                cost_usd=self._cost(finished),
            )
        elif req is not None and routes:
            system = sum(len(b.text) for b in req.system) // 4
            user = len(str(req.content)) // 4
            output = entry["streamed_chars"] // 4
            done = LLMDone(
                usage=LLMUsage(input_tokens=system + user, cached_tokens=0, output_tokens=output),
                stop_reason=None,
                model=routes[0].model,
            )
            entry.update(
                model=routes[0].model,
                input_tokens_est=system + user,
                cached_tokens_est=0,
                output_tokens_est=output,
                estimation_method="visible_chars_div_4_assume_uncached",
                estimated_route=routes[0].label,
                cost_tier_assumption="standard",
                cost_usd_est=self._cost(done),
            )
        self.host.log_entry(entry)

    def _request(
        self,
        q: Question,
        pass_: str,
        memory: list[str] | None = None,
        *,
        snapshot: RequestSnapshot | None = None,
    ) -> LLMRequest:
        snapshot = snapshot or self._request_snapshot(q)
        req = self._base_request(q, pass_, memory, snapshot=snapshot)
        _, length = self._preferences(q, pass_)
        if length == "default":
            return req
        # A preference changes the next request, never an in-flight stream. Screens keep
        # their code budget; Concise also preserves classified or explicit spoken coding.
        # Local recognition uses this request's snapshot and never changes detector state.
        limit = req.max_tokens
        if q.screen is None and self.open_problem() is None:
            coding = q.qtype == "coding" or (
                self.strategy(q) != "legacy_dual" and is_explicit_coding_request(snapshot.text)
            )
            if length == "concise" and not coding:
                limit = min(limit, max(100, round(limit * 0.6)))
            elif length == "detailed" and self.strategy(q) != "legacy_dual":
                limit = min(self.host.settings.adaptive_max_tokens, round(limit * 1.5))
        return prompts.with_response_length(
            req,
            length,
            max_tokens=limit,
            interview_drafting=(
                snapshot.context.mode == "interview"
                and self.strategy(q) != "legacy_dual"
                and q.screen is None
                and self.open_problem() is None
            ),
        )

    def _preferences(self, q: Question, pass_: str) -> tuple[str, str]:
        return q.preferences.get(pass_) or (
            self.host.answer_style(),
            getattr(self.host, "response_length", lambda: "default")(),
        )

    def _request_snapshot(self, q: Question) -> RequestSnapshot:
        transcript = self.host.call_context()
        if q.screen is None and self.open_problem() is None:
            transcript = replace(transcript, suggestions=self._earlier_suggestions(q))
        return RequestSnapshot(q.text, self.host.prompt_context(), transcript)

    def _base_request(
        self,
        q: Question,
        pass_: str,
        memory: list[str] | None = None,
        *,
        snapshot: RequestSnapshot | None = None,
    ) -> LLMRequest:
        s = self.host.settings
        snapshot = snapshot or self._request_snapshot(q)
        ctx, transcript, text = snapshot.context, snapshot.transcript, snapshot.text
        memory = memory or []
        if q.screen is not None:
            ask = q.screen
            common: dict[str, Any] = {
                "answer_mode": ask.answer_mode,
                "call": transcript if ask.answer_mode == "interview" else None,
                "language": ask.language,
                "default_language": ask.default_language,
            }
            if pass_ == "fast":
                req = screen.fast_pass(
                    ctx.user_name, ask.shots, max_tokens=s.screen_fast_max_tokens, **common
                )
            else:
                req = screen.code_pass(
                    ctx.user_name,
                    ask.shots,
                    q.reading,
                    max_tokens=s.screen_code_max_tokens,
                    **common,
                )
            return replace(
                req, system=[*req.system, SystemBlock(language_instruction(ctx.spoken_language))]
            )
        problem = self.open_problem()
        if self.strategy(q) != "legacy_dual":
            plan = q.plans.get(pass_) or plan_answer(
                text, q.qtype, q.depth, self.strategy(q), expert=pass_ == "deep"
            )
            return prompts.adaptive_pass(
                ctx,
                transcript,
                text,
                None if q.held else q.qtype,
                min(plan.max_tokens, s.adaptive_max_tokens),
                memory,
                depth=plan.depth,
                style=self._preferences(q, pass_)[0],
                expert=pass_ == "deep",
            )
        if pass_ == "fast":
            # A held pass starts before the detector has typed the question.
            qtype = None if q.held else q.qtype
            follow = screen.follow_up(problem, ctx.user_name, deep=False) if problem else None
            return prompts.fast_pass(
                ctx, transcript, text, qtype, s.fast_pass_max_tokens, memory, problem=follow
            )
        return prompts.deep_pass(
            ctx, transcript, text, q.qtype, s.deep_pass_max_tokens, memory,
            style=self._preferences(q, pass_)[0], shown=q.fast_text,
            problem=screen.follow_up(problem, ctx.user_name, deep=True) if problem else None,
            images=screen.shot_blocks(problem.shots) if problem else (),
        )  # fmt: skip

    def _prior_cues(self, q: Question) -> list[Question]:
        """Only completed cues that were visible before this question started. A newer
        question can supersede an old deep pass while its useful fast cues remain shown."""
        prior = [
            old
            for old in self.questions.values()
            if old.qid != q.qid
            and old.t0 < q.t0
            and old.confirmed
            and old.fast_ended
            and old.fast_text.strip()
            and old.screen is None
            and not (old.held or old.discarded or old.cancelled)
        ]
        return prior[-PRIOR_CUES_COUNT:]

    def _earlier_suggestions(self, q: Question) -> str:
        return "\n\n".join(
            f"Question: {old.text[:PRIOR_CUE_QUESTION_CHARS]}\n"
            f"Suggested cues: {old.fast_text[:PRIOR_CUE_TEXT_CHARS]}"
            for old in self._prior_cues(q)
        )

    def _repeats_cues(self, q: Question, text: str) -> bool:
        """Do not replay a whole prior answer for a distinct automatic follow-up. Brief
        acknowledgements can legitimately be identical, and a forced repeat is explicit."""
        if q.forced or q.screen is not None or self.open_problem() is not None:
            return False
        normalized = " ".join(text.split())
        return len(normalized.split()) >= REPEATED_CUE_MIN_WORDS and any(
            " ".join(old.fast_text.split()) == normalized for old in self._prior_cues(q)
        )

    def open_problem(self) -> "screen.Problem | None":
        """The last screen's problem while follow-ups may be about it (`screen_followup_s`)."""
        p = self.problem
        limit = self.host.settings.screen_followup_s
        if p is None or not limit or time.monotonic() - p.at > limit:
            return None
        return p

    def _keep_problem(self, q: Question, pass_: str, text: str) -> None:
        """A screenshot's answer is in: its problem is the one follow-ups are about. A screen
        with nothing readable keeps the problem before it."""
        if q.superseded or q.cancelled or q.screen is None:
            return
        if q.reading is not None and not q.reading.get("tasks"):
            return
        p = self.problem
        if pass_ == "fast" or p is None or p.qid != q.qid:
            p = screen.Problem(
                qid=q.qid, shots=q.screen.shots, reading=q.reading, answer="", code="", at=q.t0
            )
            self.problem = p
        p.reading = q.reading
        if pass_ == "fast":
            p.answer = text
        else:
            p.code = text

    def deep_job(self, q: Question) -> str:
        """Which route writes the detailed answer (`deep_routing`): "deep" (the deep model)
        when the detector says the answer needs expert reasoning, "deep_light" (the fast
        model, with the detailed answer's prompt) when it says a short or familiar answer is
        enough. With no word from the detector (the Responses API detector, a forced question,
        a follow card), the type decides: technical, system design, coding and the job and
        goals questions ("other") get the deep model."""
        if self.host.settings.deep_routing != "by_need":
            return "deep"
        if self.open_problem() is not None and q.qtype not in ("behavioral", "small_talk"):
            return "deep"  # a follow-up may need new code: the deep model writes it
        if q.depth in ("deep", "light"):
            return "deep" if q.depth == "deep" else "deep_light"
        return "deep" if q.qtype in DEEP_BY_TYPE else "deep_light"

    def _prepared(self, q: Question) -> PreparedAnswer | None:
        """The prepared answer, if the question's match has finished with a hit. A reply to a
        remark (respond) is never a prepared standup answer."""
        if self.host.settings.personal_ai_api_url:
            return None
        if q.qtype in ("respond", "follow", *prompts.WORK_TYPES):
            return None  # nor is a question about a decision, a mechanism or an estimate
        result = self._match_result(q)
        answer = result.answer if result is not None else None
        if answer is not None:
            if self._adaptive_evidence(q):
                # Citation IDs and a question match do not establish semantic grounding.
                # Versioned provenance narrows eligibility; it is not human verification.
                version_for = getattr(self.host, "prepared_source_version", None)
                if (
                    any("[~]" in p for p in answer.talking_points)
                    or not answer.grounding_policy
                    or not answer.evidence_version
                    or version_for is None
                    or version_for(answer.grounding_policy) != answer.evidence_version
                ):
                    return None
                # The pack predates this conversation. Any earlier evidence must be
                # reconciled by live generation, even if no lexical contradiction is found.
                call = self.host.call_context()
                if call.summary.strip() or call.answers.strip():
                    return None
                for line in call.recent.splitlines():
                    line = re.sub(r"^\[\d+:\d+\]\s*", "", line.strip())
                    if line and (
                        not line.startswith("Them: ")
                        or normalize_question(line[6:]) != normalize_question(q.text)
                    ):
                        return None
            text = "\n".join(f"- {p}" for p in answer.talking_points)
            if self._repeats_cues(q, text):
                return None
        return answer

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

    async def _cache_value(self, qhash: str) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.host.qa_get(qhash), CACHE_READ_TIMEOUT_S)
        except Exception:
            return None

    async def _cached(
        self, qhash: str, pass_: str, version: str | None = None
    ) -> dict[str, Any] | None:
        return self._cache_entry(await self._cache_value(qhash), pass_, version)

    def _cache_entry(
        self, value: dict[str, Any] | None, pass_: str, version: str | None = None
    ) -> dict[str, Any] | None:
        if not value or value.get("v") != (version or self.host.cache_version()):
            return None
        entry: Any = value.get(pass_)
        if not isinstance(entry, dict):
            return None
        found = cast(dict[str, Any], entry)
        return found if found.get("text") else None

    async def _cache_put(
        self, qhash: str, pass_: str, text: str, refs: list[NodeRef], version: str | None = None
    ) -> None:
        with contextlib.suppress(Exception):
            version = version or self.host.cache_version()
            current: dict[str, Any] = await self.host.qa_get(qhash) or {}
            if current.get("v") != version:
                current = {"v": version, **self.host.cache_extra()}
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
                "answer_strategy": self.strategy(q),
                "detector_depth": q.depth,
                "detected_at": q.detected_at.isoformat(),
            }
        )
