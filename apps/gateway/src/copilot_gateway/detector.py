"""Question and addressee check (ARCHITECTURE.md, "Question and addressee check").

Stage 0 has no local classifier yet, so every utterance goes to Haiku (Rollout stages,
"Detector: Haiku on every utterance"). Results are cached by text hash for the session.

Every decision, cached or not, goes to `on_decision` as a log entry: the text's hash, the
decision, its latency, tokens and cost. The session adds the text only with transcript consent.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from copilot_core import prompts
from copilot_core.llm import AllRoutesFailedError, LLMDone, LLMUsage, ModelRouter, RefusedError
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
    if answer in prompts.QUESTION_TYPES:
        return Detection(decision="answer", qtype=answer)
    return Detection(decision="wait" if answer == "wait" else "none", qtype="other")


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
    ) -> None:
        self.router = router
        self.user_name = user_name
        self.max_tokens = max_tokens
        self.on_decision = on_decision
        self.cost = cost
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
