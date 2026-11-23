"""What a guest sees of a call (ARCHITECTURE.md, "Guest seats").

Two rules live here, so the host's session and the seat relay cannot disagree:

- `guest_message`: the forwarding allow-list. A host message reaches a guest only if its type
  is in FORWARDED, and only while sharing is on; `answer.done` loses its usage, source and
  timings. Everything else (session.ready, auth.expiring, errors, seat and sharing messages,
  and any type added later) stays with the host until it is added here on purpose.
- `ViewFold`: the call so far, folded into the messages the overlay needs (finals, questions,
  answers with their chunks merged, the latest status per component), so a guest who joins
  late, or falls behind, gets the whole call in one `seat.snapshot` however long it has run.
  The host's session keeps one and writes it to `sess:{sid}:view`; the relay folds the event
  stream itself only when that is missing.

Both work on the JSON the host was sent, so what a guest sees is always what the host saw.
"""

import json
from collections import OrderedDict
from typing import Any, cast

FORWARDED = frozenset(
    {
        "transcript.partial",
        "transcript.final",
        "question.detected",
        "question.cancelled",
        "answer.delta",
        "answer.done",
        "answer.error",
        "status",
        "session.language",
    }
)
SHARING = "sharing"
# At most this many messages in a snapshot; the oldest transcript lines go first.
MAX_ITEMS = 6000


def parse(data: str) -> dict[str, Any] | None:
    try:
        message: Any = json.loads(data)
    except ValueError:
        return None
    return cast(dict[str, Any], message) if isinstance(message, dict) else None


def guest_view(message: dict[str, Any]) -> dict[str, Any] | None:
    """The message as a guest may see it, or None."""
    kind = message.get("type")
    if kind not in FORWARDED:
        return None
    if kind == "session.language":
        return {key: message[key] for key in ("type", "seq", "spoken_language")}
    if kind == "answer.done":
        return {
            "type": "answer.done",
            "seq": message["seq"],
            "question_id": message["question_id"],
            "pass": message["pass"],
            "memory_refs": message.get("memory_refs", []),
            "usage": {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0},
        }
    return message


def guest_message(data: str) -> str | None:
    """The JSON a guest gets for a host message, or None when it is not theirs to see."""
    message = parse(data)
    if message is None or message.get("type") not in FORWARDED:
        return None
    if message.get("type") not in ("answer.done", "session.language"):
        return data
    view = guest_view(message)
    return None if view is None else json.dumps(view, separators=(",", ":"))


def sharing_of(message: dict[str, Any]) -> bool | None:
    """The new sharing state if this is a sharing marker."""
    if message.get("type") == SHARING:
        return bool(message.get("on"))
    return None


class ViewFold:
    """The overlay so far, as a guest sees it. Feed it every host message in seq order."""

    def __init__(self, seq: int = 0, sharing: bool = True) -> None:
        self.seq = seq
        self.sharing = sharing
        self.items: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self.dirty = False

    @classmethod
    def load(cls, raw: str | bytes | None) -> "ViewFold | None":
        if raw is None:
            return None
        try:
            d = json.loads(raw)
            fold = cls(int(d["seq"]), bool(d.get("sharing", True)))
            for m in d["messages"]:
                fold._put(m)
        except (ValueError, KeyError, TypeError):
            return None
        fold.dirty = False
        return fold

    def dump(self) -> str:
        return json.dumps(
            {"seq": self.seq, "sharing": self.sharing, "messages": self.messages()},
            separators=(",", ":"),
        )

    def messages(self) -> list[dict[str, Any]]:
        return list(self.items.values())

    def add(self, seq: int, data: str) -> None:
        if seq <= self.seq:
            return
        self.seq = seq
        message = parse(data)
        if message is None:
            return
        self.add_message(message)

    def add_message(self, message: dict[str, Any]) -> None:
        seq = int(message.get("seq", 0))
        self.seq = max(self.seq, seq)
        sharing = sharing_of(message)
        if sharing is not None:
            if sharing != self.sharing:
                self.sharing = sharing
                self.dirty = True
            return
        if not self.sharing:
            return
        view = guest_view(message)
        if view is not None:
            self._put(view)
            self.dirty = True

    def _put(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        if kind == "transcript.partial":
            return  # live partials follow; a snapshot holds finished lines
        if kind == "transcript.final":
            self.items[("t", m.get("channel"), m.get("seg_id"))] = m
        elif kind == "question.detected":
            self.items[("q", m.get("question_id"))] = m  # a confirmation keeps the card's place
        elif kind == "question.cancelled":
            self.items[("qc", m.get("question_id"))] = m
        elif kind == "answer.delta":
            key = ("a", m.get("question_id"), m.get("pass"))
            prev = self.items.get(key)
            if prev is None:
                self.items[key] = dict(m)
            else:
                prev["text"] = str(prev.get("text", "")) + str(m.get("text", ""))
                prev["seq"] = m.get("seq", prev.get("seq"))
        elif kind in ("answer.done", "answer.error"):
            self.items[("ad", m.get("question_id"), m.get("pass"), kind)] = m
        elif kind == "session.language":
            # Requests/errors are transient, not state to replay when another display joins.
            self.items[("language",)] = {key: m[key] for key in ("type", "seq", "spoken_language")}
        elif kind == "status":
            key = ("s", m.get("component"))
            self.items.pop(key, None)
            self.items[key] = m
        else:
            return
        while len(self.items) > MAX_ITEMS:
            self._evict()

    def _evict(self) -> None:
        for key in self.items:
            if key[0] == "t":
                del self.items[key]
                return
        self.items.popitem(last=False)
