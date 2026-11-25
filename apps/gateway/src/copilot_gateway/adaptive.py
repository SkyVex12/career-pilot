"""Local answer-depth policy and streaming boundary instrumentation.

Routing reuses the turn detector's type/depth. Text rules cover forced and speculative
questions whose detector has not returned. They never call another model. A boundary is
an observable latency proxy, not a judgment that the sentence is correct or useful.
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal

from copilot_core.llm import LLMChunk

Architecture = Literal["legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"]
Depth = Literal["brief", "standard", "narrative", "expert"]

_BRIEF = re.compile(
    r"\b(where (?:are you|do you (?:live|reside))|where are you based|can you hear|"
    r"can you see.*screen|are you (?:available|authorized)|when can you start|"
    r"what(?:'s| is) your (?:notice|location))\b",
    re.I,
)
_DESIGN = re.compile(
    r"\b(?:design (?:a|an|the)|system design|architect (?:a|an)|"
    r"distributed (?:system|notification)|trade[- ]?offs|debug|diagnos)\w*\b",
    re.I,
)
_STORY = re.compile(
    r"\b(?:tell (?:me|us) about yourself|walk (?:me|us) through your (?:background|career)|"
    r"tell (?:me|us) about a time|describe.*(?:incident|conflict|challenge))\b",
    re.I,
)


@dataclass(frozen=True)
class AnswerPlan:
    depth: Depth
    job: str
    max_tokens: int
    auto_expert: bool = False


def plan_answer(
    text: str,
    qtype: str | None,
    detector_depth: str | None,
    architecture: Architecture,
    *,
    expert: bool = False,
) -> AnswerPlan:
    """Choose a bounded answer, without interpreting a generated answer as evidence."""
    if expert:
        return AnswerPlan("expert", "deep", 1200)
    if _BRIEF.search(text) and len(text.split()) < 20 and text.count("?") <= 1:
        return AnswerPlan("brief", "fast", 100)
    needs_expert = qtype in {"system_design", "coding"} or bool(_DESIGN.search(text))
    needs_expert |= detector_depth == "deep" and qtype in {
        "technical",
        "behavioral",
        "other",
        "decision",
        "how_it_works",
        "estimate_risk",
        "respond",
    }
    if needs_expert:
        return AnswerPlan(
            "expert",
            "deep" if architecture == "adaptive" else "fast",
            1200 if architecture == "adaptive" else 650,
            auto_expert=architecture == "adaptive_hybrid",
        )
    if qtype == "small_talk" or (
        qtype == "respond" and len(text.split()) <= 16 and detector_depth != "deep"
    ):
        return AnswerPlan("brief", "fast", 100)
    if qtype == "behavioral" or _STORY.search(text):
        return AnswerPlan("narrative", "fast", 600)
    return AnswerPlan("standard", "fast", 400)


_ABBREVIATION = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc)|\b[A-Z])\.$", re.I)
_FILLER = re.compile(
    r"^(?:that's (?:a )?(?:great|good|interesting) question|great question|"
    r"let me (?:think|explain)|sure|absolutely|of course)[.!:]?$",
    re.I,
)


class SentenceBoundary:
    """Find the first non-filler complete sentence across arbitrary token chunks.

    Punctuation requires following whitespace or end-of-stream: a split decimal
    ``3.`` + ``5`` cannot report a premature boundary. Completed bullet lines count;
    headings and citation-only lines do not. Usefulness still needs human review.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.found = False

    def feed(self, text: str, *, final: bool = False) -> bool:
        if self.found:
            return False
        self.buffer += text
        tail = r"(?=\s|$)" if final else r"(?=\s)"
        for match in re.finditer(
            r"[.!?\u3002\uff01\uff1f](?:[\"'\u201d\u2019)]*)" + tail + r"|\n", self.buffer
        ):
            candidate = self.buffer[: match.end()].strip()
            if match.group() == "\n" and not re.match(r"^\s*(?:[-*•]|\d+[.)])\s", candidate):
                continue
            clean = re.sub(r"\[n:[^\]]*\]", "", candidate)
            clean = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", clean).strip()
            if _ABBREVIATION.search(clean) or not re.search(r"\w", clean):
                continue
            if _FILLER.fullmatch(clean):
                self.buffer = self.buffer[match.end() :]
                return self.feed("", final=final)
            self.found = True
            return True
        return False


async def bounded_chunks(
    stream: AsyncGenerator[LLMChunk],
    idle_s: float,
    total_s: float,
) -> AsyncGenerator[LLMChunk]:
    """Close a stalled provider stream without retrying an already displayed answer."""
    async with contextlib.aclosing(stream), asyncio.timeout(total_s):
        while True:
            try:
                async with asyncio.timeout(idle_s):
                    chunk = await anext(stream)
            except StopAsyncIteration:
                return
            yield chunk
