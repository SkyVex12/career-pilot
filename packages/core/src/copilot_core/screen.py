"""Coding mode: answering the tasks on the user's screen from screenshots.

ARCHITECTURE.md, "Coding mode". The desktop sends 1 to 6 shots of one problem (a long page
scrolled, other files or tabs) in one `screenshot` message. Two passes answer them:

- The fast pass reads the screen and answers what needs no code. Its first line is `READ` and a
  JSON object: each task in screen order with its kind, language, starter signature and a
  one-line approach. The gateway takes that line out of the stream (`ReadSplitter`), shows the
  reading, and starts the deep pass at once when a task needs code (`needs_code`). Choice,
  short, output and SQL answers are final from the fast pass alone.
- The deep pass writes the code for the code, fix and complete tasks, from the same shots and
  the reading, so its code follows the approach the fast pass already showed. It also traces
  "what does this print" tasks: on 2026-11-18 the fast model (no reasoning) got 0 of 2 exact
  outputs, the deep model got them right.

The answer mode sets the order and tone: `interview` (a call is on: what to say first, then
code to type while talking, the interviewer's words before the screen) or `assessment` (an
online test: the answer or the complete code first, ready to paste).

Nothing here is guessed: a screen with no readable task gets a one-line request for another
shot, never an invented problem.
"""

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from copilot_core.llm import LLMRequest, SystemBlock
from copilot_core.prompts import CallContext, call_blocks, data_block

TASK_KINDS = ("code", "choice", "short", "output", "fix", "sql", "complete", "design")
# The kinds whose answer is code the deep pass writes.
CODE_KINDS = frozenset({"code", "fix", "complete"})
# The kinds the deep pass answers: the code, and exact outputs, which need reasoning.
DEEP_KINDS = CODE_KINDS | {"output"}
ANSWER_MODES = ("interview", "assessment")
MAX_IMAGES = 6

# The first bytes of each image type the protocol allows.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}


@dataclass(frozen=True)
class Shot:
    """One screenshot: base64 data, its media type and size, and its window's title."""

    data: str
    media_type: str
    width: int
    height: int
    title: str | None = None

    @property
    def size_bytes(self) -> int:
        """The decoded size, from the base64 length (no decoding)."""
        n = len(self.data)
        return n * 3 // 4 - self.data[-2:].count("=")


def image_ok(shot: Shot) -> bool:
    """The data is base64 and its bytes start as its media type says."""
    try:
        head = base64.b64decode(shot.data[:64] + "=" * (-len(shot.data[:64]) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    if shot.media_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return any(head.startswith(m) for m in _MAGIC.get(shot.media_type, ()))


def needs_code(reading: Mapping[str, Any] | None) -> bool:
    """Whether the deep pass runs: a task needs code or an exact output, or the screen could
    not be typed (no reading at all, so the deep pass covers what the fast pass may have
    missed)."""
    if reading is None:
        return True
    tasks: list[dict[str, Any]] = reading.get("tasks") or []
    return any(t.get("kind") in DEEP_KINDS for t in tasks)


def summary(reading: Mapping[str, Any] | None) -> str:
    """The question's text: what the screen holds, in a few words."""
    if reading is None:
        return "Screen"
    tasks: list[dict[str, Any]] = reading.get("tasks") or []
    if not tasks:
        return "Screen: nothing readable"
    kinds = ", ".join(str(t.get("kind")) for t in tasks)
    lang = reading.get("language")
    label = "1 task" if len(tasks) == 1 else f"{len(tasks)} tasks"
    return f"Screen: {label} ({kinds})" + (f", {lang}" if lang else "")


def clean_reading(raw: Any) -> dict[str, Any] | None:
    """The `READ` object, kept to what the protocol's ScreenReading holds plus the signature
    and approach the deep pass uses; None when it is not a reading at all."""
    if not isinstance(raw, dict):
        return None
    data: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    tasks_in = data.get("tasks")
    if not isinstance(tasks_in, list):
        return None
    tasks: list[dict[str, Any]] = []
    for t in tasks_in:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(t, dict) or t.get("kind") not in TASK_KINDS:  # pyright: ignore[reportUnknownMemberType]
            continue
        task: dict[str, Any] = {"n": len(tasks) + 1, "kind": t["kind"]}
        for key in ("title", "language", "signature", "approach"):
            value = t.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(value, str) and value.strip():
                task[key] = value.strip()
        tasks.append(task)
    out: dict[str, Any] = {"tasks": tasks}
    lang = data.get("language")
    if isinstance(lang, str) and lang.strip():
        out["language"] = lang.strip()
    why = data.get("unreadable")
    if not tasks:
        out["unreadable"] = why.strip() if isinstance(why, str) and why.strip() else "no task"
    return out


def wire_reading(reading: Mapping[str, Any]) -> dict[str, Any]:
    """The reading as question.detected's `screen` (no signature or approach)."""
    tasks = [
        {k: v for k, v in t.items() if k in ("n", "kind", "title", "language")}
        for t in reading.get("tasks", [])
    ]
    out: dict[str, Any] = {"tasks": tasks}
    for key in ("language", "unreadable"):
        if key in reading:
            out[key] = reading[key]
    return out


_READ_HOLD_MAX = 8000  # characters held while the READ object is still arriving


class ReadSplitter:
    """Takes the fast pass's `READ {...}` header out of its stream.

    `feed` returns the text to show. Text is held until the header is complete (a few dozen
    tokens); then `reading` holds the parsed object, `done` is True and the rest flows through.
    An answer without the header (the model left it out) flows through whole, with `reading`
    None."""

    def __init__(self) -> None:
        self.buf = ""
        self.done = False
        self.reading: dict[str, Any] | None = None
        self._shown = False

    def feed(self, text: str) -> str:
        if self.done:
            if not self._shown:  # the blank lines after the header never show
                text = text.lstrip()
                self._shown = bool(text)
            return text
        self.buf += text
        head = self.buf.lstrip().removeprefix("```").lstrip()
        if len(head) < 4:
            return ""
        if not head.startswith("READ"):
            return self._give_up()
        rest = head[4:].lstrip(" :\t")
        start = rest.find("{")
        if start < 0:
            return "" if len(rest) < 40 else self._give_up()
        try:
            raw, end = json.JSONDecoder().raw_decode(rest[start:])
        except json.JSONDecodeError:
            return "" if len(self.buf) < _READ_HOLD_MAX else self._give_up()
        self.done = True
        self.reading = clean_reading(raw)
        after = rest[start + end :].lstrip()
        if self.buf.lstrip().startswith("```") and after.startswith("```"):
            after = after[3:].lstrip()  # the fence the header came in
        self.buf = ""
        self._shown = bool(after)
        return after

    def flush(self) -> str:
        """The stream ended: what is still held goes out (a header that never closed)."""
        return "" if self.done else self._give_up()

    def _give_up(self) -> str:
        self.done = True
        self._shown = True
        out, self.buf = self.buf, ""
        return out


# Prompts. The system block is the same for every shot of a session (cached); the shots, the
# call and the reading go in the user turn.

_SITUATION = {
    "interview": " during a live technical interview",
    "assessment": " during an online coding assessment",
}

FAST_SYSTEM = """You help {user_name} with the tasks on their screen{situation}. The user \
turn holds 1 to 6 screenshots of one problem, in order: shots taken while scrolling may \
overlap (read the overlap once), and shots of other files or tabs are told apart by their \
window titles.

Your first line is READ and one JSON object, on that line alone, for example:
READ {{"tasks":[{{"n":1,"kind":"code","title":"Merge intervals","language":"python",\
"signature":"def merge(self, intervals: List[List[int]]) -> List[List[int]]:",\
"approach":"sort by start, then extend or append to the last kept interval"}}],\
"language":"python"}}
- tasks: every task on the screen, in screen order (top to bottom, then the next shot). kind \
is one of: code (implement a function, class or program, with or without starter code, also \
when the task says "complete" or "implement" it), choice (pick the option or options), short \
(a short or conceptual answer), output (what given code prints or returns), fix (find and fix \
the bug in given code), sql (write a query), complete (extend or refactor code that already \
works: add a feature, change its style), design (system design).
- language: the language the code is in: the editor's language picker, else the starter \
code, else the language the task names. {language_rule}
- signature: for code, fix and complete only, the starter code's function or class header \
exactly as shown; null without starter code.
- approach: for code, fix and complete only, the approach in one line of at most 20 words (the \
algorithm and data structure). The code, written next by a second pass, follows it, so make \
it right: the complexity the constraints need.
- Other tasks get only n, kind and title: no language, signature or approach.
- If the shots show no task you can read (blurred, cut off, not a task at all): \
READ {{"tasks":[],"unreadable":"<what you see, in a few words>"}} and then one line asking \
for another shot. Never guess or invent a task you cannot read.

Then the answers, one per task, in screen order. With several tasks, start each with \
"### <n>. <title>"; with one task, no heading.
{mode_rule}
- choice: "**Answer:** " and the chosen option's letter and text (every correct option when \
several may be chosen), then one line why.
- short: the answer in 1 to 3 sentences.
- output: one line on what the code does; a second pass traces it for the exact output.
- sql: the query itself in a ```sql block, in the dialect shown (else standard SQL), then one \
line on the approach. The query is the whole answer, in either mode: no second pass writes it.
- design: talking points, one short line each, at most 12 lines and 160 words: 2 \
clarifying questions, then components, data flow, storage, scaling, tradeoffs. No code.
- code and complete: no code here; a second pass writes it. {code_rule}
- fix: "**Bug:** " and the bug in one line, then {fix_rule}
Markdown: **bold** labels, backticks for names in text. Nothing else: no preamble, no \
closing line."""

_FAST_MODE = {
    "interview": (
        "A call is on: {user_name} talks the interviewer through each task. What the "
        "interviewer said in <transcript> about this problem (a constraint, the language, "
        '"no extra memory", an expected complexity) comes before what the screen shows.'
    ),
    "assessment": (
        "An online assessment with no call: each answer first, ready to pick or paste; any "
        "explanation after it and short. No talking points."
    ),
}
_FAST_CODE = {
    "interview": (
        "**Say first:** 1 or 2 clarifying questions or assumptions to state. **Approach:** 2 "
        "to 4 short lines, as {user_name} would explain it. **Complexity:** time and space. "
        "**Edge cases:** one line."
    ),
    "assessment": (
        "**Approach:** one line. **Complexity:** time and space. **Edge cases:** one line."
    ),
}
_FAST_FIX = "**Fix:** one line on the change; the corrected code comes from the second pass."

CODE_SYSTEM = """You write the code for the coding tasks on {user_name}'s screen{situation}. \
The user turn holds the screenshots (in order; shots taken while scrolling may overlap, and \
other files are told apart by their window titles) and <reading>, a first pass's reading of \
them: each task's kind, language, starter signature and approach.

For each task in <reading> whose kind is code, fix, complete or output, in order:
- code: one ```<language> block with the complete solution in the task's language, using the \
approach in <reading> (another one only if that one is wrong or too slow for the \
constraints). Fill the starter code exactly as shown: the same class, function name, \
parameters, their order and types, and its imports. No main, no input reading, no printing, \
no example calls, unless the starter code reads input or the task asks for a full program.
- fix: the corrected code. When the code is over about 30 lines, only the changed lines, with \
a line of context around them and a comment on what changed.
- complete: the code, in the given code's style and names.
- output: "**Output:**" and the exact output in a ```text block, then the key steps of a \
careful trace in 2 to 4 short lines. Trace every line in order; aliasing, copies, \
integer division and mutation decide the answer.
{mode_rule}
{language_rule}
With several such tasks, start each with "### <n>. <title>"; with one, no heading. Nothing \
for the other tasks, no preamble, no closing line."""

_CODE_MODE = {
    "interview": (
        "A call is on: {user_name} types this while talking. A short comment at each step "
        "says what it does, in the order they would explain it. After the block, "
        "**Complexity:** time and space in one line. What the interviewer asked in "
        "<transcript> (a constraint, the language, no extra memory) comes before the screen."
    ),
    "assessment": (
        "An online assessment: the code first, ready to paste, with few comments. After the "
        "block, **Complexity:** time and space in one line. No other explanation."
    ),
}


def _language_rule(language: str | None, default_language: str) -> str:
    if language:
        return (
            f"{{user_name}} chose {language}: all code is in {language}, whatever the screen shows."
        )
    return f"With no language anywhere, {default_language}."


def _system(
    template: str, user_name: str, answer_mode: str, rules: Mapping[str, str]
) -> list[SystemBlock]:
    mode = answer_mode if answer_mode in ANSWER_MODES else "interview"
    text = template.format(user_name="{user_name}", situation=_SITUATION[mode], **rules).replace(
        "{user_name}", user_name
    )
    return [SystemBlock(text, cache=True)]


def _shot_blocks(shots: Sequence[Shot]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for i, shot in enumerate(shots, start=1):
        label = f"Shot {i} of {len(shots)}"
        if shot.title:
            label += f", window: {shot.title}"
        blocks.append({"type": "text", "text": label})
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": shot.media_type, "data": shot.data},
            }
        )
    return blocks


def fast_pass(
    user_name: str,
    shots: Sequence[Shot],
    *,
    answer_mode: str,
    call: "CallContext | str | None",
    language: str | None,
    default_language: str,
    max_tokens: int,
) -> LLMRequest:
    """Read the screen and answer what needs no code. `call` is the conversation so far in an
    interview (None in an assessment)."""
    mode = answer_mode if answer_mode in ANSWER_MODES else "interview"
    rules = {
        "language_rule": _language_rule(language, default_language),
        "mode_rule": _FAST_MODE[mode],
        "code_rule": _FAST_CODE[mode],
        "fix_rule": _FAST_FIX,
    }
    content = _shot_blocks(shots)
    tail: list[str] = []
    if call is not None and mode == "interview":
        tail += call_blocks(call)
    tail.append("Read the screen and answer.")
    content.append({"type": "text", "text": "\n\n".join(tail)})
    return LLMRequest(
        system=_system(FAST_SYSTEM, user_name, mode, rules), content=content, max_tokens=max_tokens
    )


def code_pass(
    user_name: str,
    shots: Sequence[Shot],
    reading: Mapping[str, Any] | None,
    *,
    answer_mode: str,
    call: "CallContext | str | None",
    language: str | None,
    default_language: str,
    max_tokens: int,
) -> LLMRequest:
    """Write the code for the code, fix and complete tasks. Without a reading (the fast pass
    left out its header) the pass reads the tasks itself."""
    mode = answer_mode if answer_mode in ANSWER_MODES else "interview"
    rules = {
        "language_rule": _language_rule(language, default_language),
        "mode_rule": _CODE_MODE[mode],
    }
    content = _shot_blocks(shots)
    tail: list[str] = []
    if call is not None and mode == "interview":
        tail += call_blocks(call)
    body = (
        json.dumps(reading, ensure_ascii=False)
        if reading is not None
        else ("(no reading: read the tasks from the shots yourself)")
    )
    tail.append(data_block("reading", body))
    tail.append("Write the code.")
    content.append({"type": "text", "text": "\n\n".join(tail)})
    return LLMRequest(
        system=_system(CODE_SYSTEM, user_name, mode, rules), content=content, max_tokens=max_tokens
    )
