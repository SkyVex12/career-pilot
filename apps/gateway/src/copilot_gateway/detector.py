"""Question and addressee check (ARCHITECTURE.md, "Question and addressee check").

Stage 0 has no local classifier yet, so every utterance goes to Haiku (Rollout stages,
"Detector: Haiku on every utterance"). Results are cached by text hash for the session.
"""

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import replace

from copilot_core import prompts
from copilot_core.llm import AllRoutesFailedError, ModelRouter, RefusedError
from copilot_gateway.turn import Detection

log = logging.getLogger(__name__)

NO_QUESTION = Detection(False, False, False, "other", 0.0)


def parse_detection(text: str) -> Detection:
    """Parse the detector's JSON; anything malformed counts as not a question."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in detector output: {text!r}")
    data = json.loads(text[start : end + 1])
    qtype = str(data.get("type", "other"))
    if qtype not in prompts.QUESTION_TYPES:
        qtype = "other"
    return Detection(
        is_question=bool(data.get("is_question")),
        complete=bool(data.get("complete")),
        addressed_to_user=bool(data.get("addressed_to_user")),
        qtype=qtype,
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
    )


class Detector:
    def __init__(self, router: ModelRouter, user_name: str, max_tokens: int) -> None:
        self.router = router
        self.user_name = user_name
        self.max_tokens = max_tokens
        self.cache: OrderedDict[str, Detection] = OrderedDict()
        self.calls = 0

    async def detect(self, utterance: str, transcript: str) -> Detection:
        key = hashlib.sha256(utterance.strip().lower().encode()).hexdigest()
        if (hit := self.cache.get(key)) is not None:
            return replace(hit, latency_ms=0)
        started = time.monotonic()
        self.calls += 1
        req = prompts.detector(self.user_name, transcript, utterance, self.max_tokens)
        try:
            text, _ = await self.router.complete("detector", req)
            detection = parse_detection(text)
        except (AllRoutesFailedError, RefusedError, ValueError) as e:
            log.warning("detector failed: %s", e)
            return replace(NO_QUESTION, error=str(e))
        result = replace(detection, latency_ms=int((time.monotonic() - started) * 1000))
        self.cache[key] = result
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        return result
