"""Conversation memory for one live session (ARCHITECTURE.md, "Conversation memory").

The detector and both answer passes see the call through this, inside a hard token budget
(`conversation_budget_tokens`, counted as about 4 characters a token):

- recent: the latest lines word for word, both speakers labeled ("Them", the user's name), as
  many as fit in what the other two parts leave;
- summary: everything older, folded in by the cheapest model in the background, one chunk at
  a time, and only when lines overflow the recent window: never per line, never on the path
  of an answer;
- answers: earlier questions paired with what the user actually said after each (their own
  transcribed words, never the suggested answers), once the question has left the window.

The user's lines come from the live STT of their microphone. Without it (the setting is off,
or an older client sends speech flags only) a "(name spoke for N s)" line keeps their turns
visible, as before.

Echo: when the other side's voice leaks into the mic past echo cancellation, the user's STT
transcribes the other side's words. `echo_share` measures how many of a "me" line's words the
other side said around the same time; the session drops a line over the threshold.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from copilot_core.prompts import CallContext

log = logging.getLogger(__name__)

QUESTION_WORDS = 40  # of a question, in the earlier answers
SAID_WORDS = 60  # of what the user said after it
ASKED_KEEP = 50
# Without a summary model, or while it keeps failing, lines are still capped: this many times
# the recent window, oldest first.
LINES_CAP_FACTOR = 4


def tokens(text: str) -> int:
    """About 4 characters a token: the budget's unit (no tokenizer on the live path)."""
    return (len(text) + 3) // 4


def stamp(ms: int) -> str:
    return f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}]"


def _clip_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def _clip_tokens(text: str, max_tokens: int) -> str:
    """Cut at the last line (or word) boundary within the budget."""
    limit = max_tokens * 4
    if len(text) <= limit:
        return text
    cut = text[:limit]
    at = cut.rfind("\n")
    if at < limit // 2:
        at = cut.rfind(" ")
    return cut[: max(at, 0)].rstrip()


_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def echo_share(me_text: str, them_texts: Iterable[str]) -> float:
    """The share of a "me" line's words found among the other side's words of the same time
    (each of their words counts once). Near 1: the mic heard the other side."""
    mine = words(me_text)
    if not mine:
        return 0.0
    pool = Counter(w for t in them_texts for w in words(t))
    hit = 0
    for w in mine:
        if pool[w] > 0:
            pool[w] -= 1
            hit += 1
    return hit / len(mine)


@dataclass
class Line:
    channel: str  # "them" or "me"
    start_ms: int
    end_ms: int
    text: str  # "" on the me channel without live STT: only the timing is known


@dataclass
class Asked:
    """A confirmed question and what the user said after it, until the next one."""

    qid: str
    text: str
    at_ms: int  # capture ms when the question ended
    said: list[str] = field(default_factory=list[str])

    def said_words(self) -> int:
        return sum(len(s.split()) for s in self.said)


@dataclass(frozen=True)
class Budget:
    total: int = 3000
    summary: int = 500
    answers: int = 600
    chunk: int = 500  # how much leaves the recent window per summary call

    @property
    def recent(self) -> int:
        return max(0, self.total - self.summary - self.answers)


# (summary so far, the lines leaving the window) -> the new summary, or None on a failure.
type Summarize = Callable[[str, str], Awaitable[str | None]]


class Conversation:
    def __init__(
        self,
        user_name: str,
        budget: Budget,
        summarize: Summarize | None = None,
        *,
        retry_s: float = 30.0,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.user_name = user_name
        self.budget = budget
        self.summarize = summarize
        self.retry_s = retry_s
        self.on_change = on_change
        self.lines: list[Line] = []
        self.summary = ""
        self.asked: list[Asked] = []
        self.summary_calls = 0
        self._task: asyncio.Task[None] | None = None
        self._failed_at: float | None = None

    # Building it.

    @property
    def capture_now(self) -> int:
        return max((ln.end_ms for ln in self.lines), default=0)

    def add(self, line: Line) -> None:
        """A final line from either channel. The two STT streams deliver their finals a little
        out of order, so lines are kept in order of their start."""
        i = len(self.lines)
        while i > 0 and self.lines[i - 1].start_ms > line.start_ms:
            i -= 1
        self.lines.insert(i, line)
        if line.channel == "me" and line.text:
            for asked in reversed(self.asked):
                if asked.at_ms <= line.end_ms:
                    self._said(asked, line.text)
                    break
        self._cap()
        self._changed()
        self._maybe_fold()

    def question(self, qid: str, text: str, at_ms: int) -> None:
        """A confirmed question; what the user says after it is paired with it."""
        if any(a.qid == qid for a in self.asked):
            return
        asked = Asked(qid, " ".join(text.split()), at_ms)
        for line in self.lines:
            if line.channel == "me" and line.text and line.end_ms > at_ms:
                self._said(asked, line.text)
        # The previous question's answer ends where this one starts.
        self.asked.append(asked)
        del self.asked[:-ASKED_KEEP]
        self._changed()

    @staticmethod
    def _said(asked: Asked, text: str) -> None:
        if asked.said_words() < SAID_WORDS * 2:
            asked.said.append(text)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _cap(self) -> None:
        cap = max(self.budget.recent, 1) * LINES_CAP_FACTOR
        while len(self.lines) > 1 and self._lines_tokens(self.lines) > cap:
            self.lines.pop(0)

    # What the prompts see.

    def render(self, line: Line) -> str:
        at = stamp(line.start_ms)
        if line.channel == "them":
            return f"{at} Them: {line.text}"
        if line.text:
            return f"{at} {self.user_name}: {line.text}"
        secs = max(1, (line.end_ms - line.start_ms) // 1000)
        return f"{at} ({self.user_name} spoke for {secs} s)"

    def _lines_tokens(self, lines: Iterable[Line]) -> int:
        return sum(tokens(self.render(ln)) + 1 for ln in lines)

    def recent_lines(self, max_tokens: int) -> list[Line]:
        """The newest lines that fit in `max_tokens`, oldest first."""
        out: list[Line] = []
        used = 0
        for line in reversed(self.lines):
            cost = tokens(self.render(line)) + 1
            if used + cost > max_tokens:
                break
            out.append(line)
            used += cost
        out.reverse()
        return out

    def recent(self, max_tokens: int | None = None) -> str:
        budget = self.budget.recent if max_tokens is None else max_tokens
        return "\n".join(self.render(ln) for ln in self.recent_lines(budget))

    def answers_text(self, since_ms: int | None) -> str:
        """Earlier questions that have left the recent window (asked before `since_ms`), with
        what the user said after each, newest first into the budget, shown oldest first."""
        blocks: list[str] = []
        used = 0
        for asked in reversed(self.asked):
            if since_ms is not None and asked.at_ms >= since_ms:
                continue
            if not asked.said:
                continue
            said = _clip_words(" ".join(asked.said), SAID_WORDS)
            block = (
                f"{stamp(asked.at_ms)} Q: {_clip_words(asked.text, QUESTION_WORDS)}\n"
                f"{self.user_name} said: {said}"
            )
            cost = tokens(block) + 1
            if used + cost > self.budget.answers:
                break
            blocks.append(block)
            used += cost
        blocks.reverse()
        return "\n".join(blocks)

    def context(self) -> CallContext:
        """The three parts, each within its share of the budget."""
        recent = self.recent_lines(self.budget.recent)
        since = recent[0].start_ms if recent else None
        return CallContext(
            recent="\n".join(self.render(ln) for ln in recent),
            summary=_clip_tokens(self.summary, self.budget.summary),
            answers=self.answers_text(since),
        )

    def them_text_since(self, ms: int) -> str:
        return " ".join(ln.text for ln in self.lines if ln.channel == "them" and ln.end_ms >= ms)

    def them_text_between(self, start_ms: int, end_ms: int) -> list[str]:
        return [
            ln.text
            for ln in self.lines
            if ln.channel == "them" and ln.end_ms >= start_ms and ln.start_ms <= end_ms
        ]

    # The rolling summary.

    def _maybe_fold(self) -> None:
        if self.summarize is None or self._task is not None:
            return
        if self._failed_at is not None and time.monotonic() - self._failed_at < self.retry_s:
            return
        if self._lines_tokens(self.lines) <= self.budget.recent:
            return
        take: list[Line] = []
        used = 0
        for line in self.lines[:-1]:  # the newest line always stays verbatim
            if used >= self.budget.chunk:
                break
            take.append(line)
            used += tokens(self.render(line)) + 1
        if not take:
            return
        self._task = asyncio.create_task(self._fold(take))

    async def _fold(self, take: list[Line]) -> None:
        assert self.summarize is not None
        new: str | None = None
        try:
            new = await self.summarize(self.summary, "\n".join(self.render(ln) for ln in take))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("conversation summary failed: %s", e)
        finally:
            self._task = None
        self.summary_calls += 1
        if not new or not new.strip():
            # The lines stay (the recent window trims what does not fit) and the next overflow
            # after `retry_s` tries again.
            self._failed_at = time.monotonic()
            return
        self._failed_at = None
        self.summary = _clip_tokens(new.strip(), self.budget.summary)
        gone = {id(ln) for ln in take}
        self.lines = [ln for ln in self.lines if id(ln) not in gone]
        self._changed()
        self._maybe_fold()  # more may have piled up meanwhile

    @property
    def folding(self) -> bool:
        return self._task is not None

    async def wait_folded(self) -> None:
        while self._task is not None:
            await asyncio.shield(self._task)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()

    # Session state, for a resume on another gateway task.

    def to_state(self) -> dict[str, Any]:
        return {
            "lines": [asdict(ln) for ln in self.lines],
            "summary": self.summary,
            "asked": [asdict(a) for a in self.asked[-20:]],
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        with contextlib.suppress(TypeError, ValueError, KeyError):
            self.lines = [Line(**ln) for ln in state.get("lines", [])]
            self.summary = str(state.get("summary", ""))
            self.asked = [Asked(**a) for a in state.get("asked", [])]
