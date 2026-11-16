"""Question and addressee check (ARCHITECTURE.md, "Question and addressee check").

Stage 0 has no local classifier yet, so every utterance goes to a model. Results are cached by
text hash for the session.

Two ways to ask (`detector_api`):
- "decisions" (default since 2026-11-16, OpenAI only): the Decisions API, which returns typed
  choices instead of generated text. Two questions, whether the utterance needs a card now
  (answer, wait or none) and its type (`prompts.detector_decision`). On the 119 labelled
  lines of the evals it decided in 0.42 to 0.5 s at p50 against 1.2 s for the same rules on
  the Responses API, and the detector's ~1.5 s was what 27 of 35 benchmark questions waited
  for (docs/analysis/2026-11-14-speed-baseline.md). A second request starts alongside after
  `decisions_hedge_ms`, or at once when the first fails (it answered 503 now and then).
- "responses": the detector prompt on the job's routes (the Responses API on OpenAI, the
  Messages API on Claude), one JSON field. Used when no OpenAI provider is configured.

Every decision, cached or not, goes to `on_decision` as a log entry: the text's hash, the
decision, its latency, tokens and cost. The session adds the text only with transcript consent.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from copilot_core import prompts
from copilot_core.llm import (
    AllRoutesFailedError,
    Decided,
    LLMDone,
    LLMUsage,
    ModelRouter,
    RefusedError,
)
from copilot_gateway.turn import Detection

log = logging.getLogger(__name__)

NO_QUESTION = Detection("none", "other")
NAMED = Detection("answer", "respond")
_running: "set[asyncio.Future[Detection]]" = set()


def parse_detection(text: str) -> Detection:
    """Parse the detector's JSON; anything malformed counts as not a question."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in detector output: {text!r}")
    answer = str(json.loads(text[start : end + 1]).get("answer", "none"))
    if answer in prompts.QUESTION_TYPES or answer in prompts.WORK_TYPES:
        return Detection(decision="answer", qtype=answer)
    return Detection(decision="wait" if answer == "wait" else "none", qtype="other")


def detection_from(answers: Sequence[Mapping[str, Any]], mode: str) -> tuple[Detection, float]:
    """A Decisions API response as the detector's decision, and the confidence of its first
    question. A type the mode does not offer is "other"."""
    got = prompts.read_decision([dict(a) for a in answers])
    if got["decision"] == "wait":
        return Detection("wait", "other"), got["confidence"]
    if got["decision"] != "answer":
        return NO_QUESTION, got["confidence"]
    qtype = got["qtype"]
    allowed = set(prompts.QUESTION_TYPES) - {"small_talk"}
    if mode in prompts.GROUP_MODES:
        allowed |= set(prompts.WORK_TYPES)
    return (
        Detection("answer", qtype if qtype in allowed else "other", depth=got["depth"]),
        got["confidence"],
    )


class Decider(Protocol):
    name: str

    async def decide(
        self,
        model: str,
        input: Sequence[Mapping[str, Any]],
        questions: Sequence[Mapping[str, Any]],
    ) -> Decided: ...


@dataclass(frozen=True)
class DecisionsRoute:
    """Where the detector asks when it uses the Decisions API."""

    provider: Decider
    model: str
    hedge_s: float | None
    timeout_s: float


# The log entry and the utterance (the session stores the text only with consent).
type DecisionHook = Callable[[dict[str, Any], str], None]
type CostFn = Callable[[LLMDone], float | None]


def text_hash(utterance: str) -> str:
    return hashlib.sha256(utterance.strip().lower().encode()).hexdigest()


class Detector:
    def __init__(
        self,
        router: ModelRouter,
        user_name: str,
        max_tokens: int,
        on_decision: DecisionHook | None = None,
        cost: CostFn | None = None,
        decisions: DecisionsRoute | None = None,
    ) -> None:
        self.router = router
        self.user_name = user_name
        self.max_tokens = max_tokens
        self.on_decision = on_decision
        self.cost = cost
        self.decisions = decisions
        self.cache: OrderedDict[str, Detection] = OrderedDict()
        self.calls = 0
        # Totals for the session's end log line, so an analysis needs no per-call lines:
        # decisions, cache hits, calls cancelled by newer text, failures, hedges and tokens
        # (cancelled calls' input estimated).
        self.stats: dict[str, int] = dict.fromkeys(
            ("decided", "cached", "cancelled", "failed", "hedged", "input_tokens",
             "output_tokens"), 0,
        )  # fmt: skip

    def _report(
        self,
        utterance: str,
        key: str,
        detection: Detection,
        done: LLMDone | None,
        cached: bool,
        **extra: Any,
    ) -> None:
        st = self.stats
        if cached:
            st["cached"] += 1
        elif extra.get("cancelled"):
            st["cancelled"] += 1
        elif detection.error:
            st["failed"] += 1
        else:
            st["decided"] += 1
        if done is not None:
            st["input_tokens"] += done.usage.input_tokens
            st["output_tokens"] += done.usage.output_tokens
            st["hedged"] += int(done.hedged)
        if self.on_decision is None:
            return
        entry: dict[str, Any] = {
            "kind": "detector",
            "text_hash": key[:32],
            "decision": detection.decision,
            "type": detection.qtype,
            "latency_ms": detection.latency_ms,
            "cached": cached,
        }
        if detection.error:
            entry["error"] = detection.error[:200]
        if done is not None:
            entry.update(
                model=done.model,
                input_tokens=done.usage.input_tokens,
                cached_tokens=done.usage.cached_tokens,
                output_tokens=done.usage.output_tokens,
                cost_usd=self.cost(done) if self.cost else None,
                first_token_ms=done.first_token_ms,
                attempt=done.attempt,
            )
            if done.hedged:
                # The losing request is billed for at least its input.
                loser = LLMDone(
                    LLMUsage(input_tokens=done.usage.input_tokens), None, model=done.model
                )
                entry.update(
                    hedged=True, hedge_cost_usd_est=self.cost(loser) if self.cost else None
                )
        entry.update(extra)
        self.on_decision(entry, utterance)

    async def detect(
        self, utterance: str, transcript: str, mode: str = "interview", turns: str = ""
    ) -> Detection:
        key = text_hash(utterance)
        # The call type and the turn-taking block are part of the prompt.
        cache_key = f"{mode}:{turns}:{key}"
        if (hit := self.cache.get(cache_key)) is not None:
            result = replace(hit, latency_ms=0)
            self._report(utterance, key, result, None, cached=True)
            return result
        started = time.monotonic()
        self.calls += 1
        if self.decisions is not None:
            return await self._decide(utterance, key, cache_key, transcript, mode, turns, started)
        req = prompts.detector(self.user_name, transcript, utterance, self.max_tokens, mode, turns)
        try:
            text, done = await self.router.complete("detector", req)
            detection = parse_detection(text)
        except asyncio.CancelledError:
            # The turn moved on (the speaker kept talking). The provider still bills the input
            # it read, so the call is logged with an estimate (about 4 characters a token).
            routes = self.router.routes.get("detector") or []
            chars = sum(len(b.text) for b in req.system) + len(str(req.content))
            est = LLMDone(
                usage=LLMUsage(input_tokens=chars // 4),
                stop_reason=None,
                model=routes[0].model if routes else "",
            )
            ms = int((time.monotonic() - started) * 1000)
            self._report(
                utterance, key, replace(NO_QUESTION, latency_ms=ms), est, cached=False,
                cancelled=True, estimated=True,
            )  # fmt: skip
            raise
        except (AllRoutesFailedError, RefusedError, ValueError) as e:
            log.warning("detector failed: %s", e)
            failed = replace(
                NO_QUESTION, error=str(e), latency_ms=int((time.monotonic() - started) * 1000)
            )
            self._report(utterance, key, failed, None, cached=False)
            return replace(NO_QUESTION, error=str(e))
        result = replace(detection, latency_ms=int((time.monotonic() - started) * 1000))
        self.cache[cache_key] = result
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        self._report(utterance, key, result, done, cached=False)
        return result

    async def _decide(
        self,
        utterance: str,
        key: str,
        cache_key: str,
        transcript: str,
        mode: str,
        turns: str,
        started: float,
    ) -> Detection:
        """The decision on the Decisions API, hedged: a second request starts alongside after
        `hedge_s`, or at once when the first fails; the first answer wins."""
        route = self.decisions
        assert route is not None
        req = prompts.detector_decision(self.user_name, transcript, utterance, mode, turns)

        async def one() -> Decided:
            return await asyncio.wait_for(
                route.provider.decide(route.model, req.input, req.questions), route.timeout_s
            )

        racing: set[asyncio.Task[Decided]] = {asyncio.ensure_future(one())}
        attempts, hedged = 1, False
        errors: list[str] = []
        won: Decided | None = None
        try:
            while racing and won is None:
                wait_s = route.hedge_s if attempts == 1 else None
                done, _ = await asyncio.wait(
                    racing, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:  # no answer by the hedge time: a second request alongside
                    attempts, hedged = 2, True
                    racing.add(asyncio.ensure_future(one()))
                    continue
                for task in done:
                    racing.discard(task)
                    if task.exception() is None:
                        won = task.result()
                        break
                    e = task.exception()
                    errors.append(f"{type(e).__name__}: {e}"[:200])
                if won is None and not racing and attempts < 2:
                    attempts = 2
                    racing.add(asyncio.ensure_future(one()))
        except asyncio.CancelledError:
            # The turn moved on. The provider bills what it read: estimated, 4 characters a
            # token, as for a cancelled call on the Responses API.
            chars = sum(
                len(str(p.get("text", ""))) for m in req.input for p in m.get("content", [])
            )
            est = LLMDone(LLMUsage(input_tokens=chars // 4), None, model=route.model)
            ms = int((time.monotonic() - started) * 1000)
            self._report(
                utterance, key, replace(NO_QUESTION, latency_ms=ms), est, cached=False,
                cancelled=True, estimated=True, api="decisions",
            )  # fmt: skip
            raise
        finally:
            for task in racing:
                task.cancel()
        ms = int((time.monotonic() - started) * 1000)
        if won is None:
            log.warning("detector failed: %s", "; ".join(errors))
            failed = replace(NO_QUESTION, error="; ".join(errors), latency_ms=ms)
            self._report(utterance, key, failed, None, cached=False, api="decisions")
            return replace(NO_QUESTION, error="; ".join(errors))
        detection, confidence = detection_from(won.answers, mode)
        result = replace(detection, latency_ms=ms)
        done = LLMDone(
            won.usage, "end_turn", won.provider, won.model, attempt=int(hedged or bool(errors)),
            first_token_ms=ms, hedged=hedged,
        )  # fmt: skip
        self.cache[cache_key] = result
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        self._report(
            utterance, key, result, done, cached=False, api="decisions",
            confidence=round(confidence, 3),
        )  # fmt: skip
        return result


async def named_or_model(call: "asyncio.Future[Detection]", grace_s: float) -> Detection:
    """For an utterance that addresses the user by name (`turn.addressed_by_name`): the
    model's decision if it comes within `grace_s`, else `respond` at once. A failed call
    counts as `respond` too. In the 2026-10-27 standup a question ending in the user's name
    waited 2.8 s for a hedged detector, and the answer showed after the user had begun to
    reply. The call itself runs on after the grace period, so its decision is still logged
    and cached."""
    started = time.monotonic()
    try:
        await asyncio.wait({call}, timeout=grace_s)
    except asyncio.CancelledError:
        call.cancel()
        raise
    if call.done() and not call.cancelled() and call.result().error is None:
        return call.result()
    if not call.done():  # it runs on, unreferenced by the turn: keep it alive
        _running.add(call)
        call.add_done_callback(_running.discard)
    ms = int((time.monotonic() - started) * 1000)
    log.info("detector: addressed by name, shown as respond after %d ms", ms)
    return replace(NAMED, latency_ms=ms)
