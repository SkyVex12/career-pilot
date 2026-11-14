"""A company's engineering context (ARCHITECTURE.md, "Memory system", engineering context).

A company document is kept two ways, because each loses what the other keeps
(docs/analysis/2026-11-14-standup-technical.md: extraction alone kept 6% of the words):

- **Sections** (`split`): the text cut at its headings, each piece carrying its heading path.
  Nothing is dropped, so a one-line rule or a name can be found by search. Stored sealed
  (`app.knowledge_chunks`); a worker opens a company's sections into Valkey for live calls
  (`LiveIndex`, `kb:{uid}:{employer}`).
- **Facts**: components, decisions with their reason and rejected alternatives, flows,
  constraints, open risks and terms, extracted into workplace memory. They give the answer
  its shape, and the **company digest** (`render_digest`): a compact system map and decision
  list that sits in every standup and meeting prompt, so common questions need no search.

Keyword search over sections runs in memory (`Index`, BM25), in the gateway and in the
worker's "test a question" job alike. It needs no database round trip and no key: the
gateway cannot open sealed text. The vector half is a Postgres query that returns ids.
"""

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from copilot_core.packs import NodeRef, assign_short_id

# --- Sections --------------------------------------------------------------------------------

SECTION_WORDS = 220  # a section longer than this is cut at paragraph ends
SECTION_MAX_WORDS = 320  # and a single paragraph (or diagram) longer than this at line ends
HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Section:
    ord: int
    path: str  # "Design > 4. Decisions", "" for text before the first heading
    text: str


def _pieces(lines: Sequence[str]) -> list[str]:
    """Paragraphs of a section; a fenced block (code, a Mermaid or PlantUML diagram) is one
    piece however many blank lines it has."""
    out: list[str] = []
    buf: list[str] = []
    fenced = False
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
            buf.append(line)
            continue
        if not fenced and not line.strip():
            if buf:
                out.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


def _cut(piece: str) -> list[str]:
    """A piece over the limit, cut at line ends (a long table or diagram)."""
    if len(piece.split()) <= SECTION_MAX_WORDS:
        return [piece]
    out: list[str] = []
    buf: list[str] = []
    words = 0
    for line in piece.splitlines():
        n = len(line.split())
        if buf and words + n > SECTION_WORDS:
            out.append("\n".join(buf))
            buf, words = [], 0
        buf.append(line)
        words += n
    if buf:
        out.append("\n".join(buf))
    return out


def split(text: str) -> list[Section]:
    """Cut a Markdown or plain-text document into sections. Headings (`#` to `####`) set the
    path; a `#` inside a fenced block is not a heading; a document with no headings is cut at
    its paragraphs."""
    groups: list[tuple[str, list[str]]] = []
    path: list[str] = []
    lines: list[str] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        found = None if fenced else HEADING.match(line)
        if found is None:
            lines.append(line)
            continue
        groups.append((" > ".join(path), lines))
        level = len(found.group(1))
        path = [*path[: level - 1], found.group(2).strip()]
        lines = []
    groups.append((" > ".join(path), lines))

    out: list[Section] = []
    for where, body in groups:
        buf: list[str] = []
        words = 0
        for piece in (part for p in _pieces(body) for part in _cut(p)):
            n = len(piece.split())
            if buf and words + n > SECTION_WORDS:
                out.append(Section(len(out), where, "\n\n".join(buf)))
                buf, words = [], 0
            buf.append(piece)
            words += n
        if buf:
            out.append(Section(len(out), where, "\n\n".join(buf)))
    return out


def seal_body(doc: str, section: Section) -> str:
    """What `knowledge_chunks.body_enc` seals: the file's name, the heading path, the text."""
    return json.dumps({"doc": doc, "path": section.path, "text": section.text})


# --- The live index --------------------------------------------------------------------------


class LiveChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    document_id: UUID
    doc: str
    path: str = ""
    text: str

    def label(self) -> str:
        return f"{self.doc} > {self.path}" if self.path else self.doc


class LiveIndex(BaseModel):
    """`kb:{uid}:{employer}`: a company's sections opened for live calls, as a worker wrote
    them. `version` changes with the set of sections."""

    model_config = ConfigDict(extra="ignore")
    version: str = ""
    chunks: list[LiveChunk] = []
    total: int = 0  # sections the company has; more than len(chunks) when capped


# The live copy holds at most this many sections (about 400,000 words), newest documents
# first, so one company's key stays a few megabytes.
LIVE_MAX_CHUNKS = 2000


def live_version(chunk_ids: Iterable[UUID]) -> str:
    h = hashlib.sha256()
    for i in sorted(chunk_ids):
        h.update(i.bytes)
    return h.hexdigest()[:16]


_WORD = re.compile(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*")
# Spoken questions are mostly these; they match every section and say nothing about it.
_STOP_WORDS = (
    "a an and are as at be been but by can could did do does for from had has have he her his"
    " how i if in is it its just like me my no not of on or our she so some than that the"
    " their them then there these they this those to up us was we were what when where which"
    " who why will with would yeah yes you your also about any all one two thing things right"
    " okay ok mean know think going gonna really basically actually kind sort something"
    " anything maybe well now here very much more"
)
STOP = frozenset(_STOP_WORDS.split())


def tokens(text: str) -> list[str]:
    """Lowercase words for keyword search. A dotted or dashed name (`reports-engine`,
    `form_record`) counts whole and by its parts, so either way of saying it matches."""
    out: list[str] = []
    for word in _WORD.findall(text.lower()):
        parts = re.split(r"[_.-]", word)
        for w in (word, *parts) if len(parts) > 1 else (word,):
            if len(w) > 1 and w not in STOP:
                out.append(_stem(w))
    return out


def _stem(word: str) -> str:
    """A light plural and tense stem: enough for "reports" to find "report" and "indexed" to
    find "index", without a stemmer's surprises on names."""
    for suffix, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""),
                         ("s", "")):  # fmt: skip
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: len(word) - len(suffix)] + keep
    return word


@dataclass
class Index:
    """BM25 over a company's sections, built once per session (or per test question)."""

    chunks: Sequence[LiveChunk]
    postings: dict[str, dict[int, int]] = field(default_factory=dict[str, dict[int, int]])
    lengths: list[int] = field(default_factory=list[int])
    by_id: dict[UUID, int] = field(default_factory=dict[UUID, int])

    @classmethod
    def build(cls, chunks: Sequence[LiveChunk]) -> "Index":
        index = cls(chunks)
        for n, c in enumerate(chunks):
            # The heading path is the section's own summary: its words count twice.
            words = tokens(c.path) * 2 + tokens(c.text)
            index.lengths.append(len(words))
            index.by_id[c.id] = n
            for w in words:
                at = index.postings.setdefault(w, {})
                at[n] = at.get(n, 0) + 1
        return index

    def search(self, query: str, limit: int) -> list[tuple[LiveChunk, float]]:
        """The best sections for `query`, best first, with their scores. A section must
        share MIN_SHARED of the question's words (or its only one): a single shared word,
        however rare, is a section on another subject more often than not."""
        n = len(self.chunks)
        if not n:
            return []
        avg = sum(self.lengths) / n or 1.0
        scores: dict[int, float] = {}
        shared: dict[int, int] = {}
        words = set(tokens(query))
        for w in words:
            at = self.postings.get(w)
            if not at:
                continue
            idf = math.log(1 + (n - len(at) + 0.5) / (len(at) + 0.5))
            for i, tf in at.items():
                norm = tf + 1.2 * (0.25 + 0.75 * self.lengths[i] / avg)
                scores[i] = scores.get(i, 0.0) + idf * tf * 2.2 / norm
                shared[i] = shared.get(i, 0) + 1
        need = min(MIN_SHARED, len(words))
        ranked = sorted(
            ((i, s) for i, s in scores.items() if shared[i] >= need),
            key=lambda s: (-s[1], s[0]),
        )
        return [(self.chunks[i], score) for i, score in ranked[:limit]]

    def get(self, chunk_id: UUID) -> LiveChunk | None:
        at = self.by_id.get(chunk_id)
        return None if at is None else self.chunks[at]


MIN_SHARED = 2

# $1 user, $2 employer, $3 query vector (text), $4 its model id, $5 limit
VECTOR_SQL = """
select id from app.knowledge_chunks
where user_id = $1 and employer_id = $2 and embedding_model = $4
order by embedding <=> $3::vector
limit $5
"""


def merge(*lists: Sequence[LiveChunk], limit: int) -> list[LiveChunk]:
    """Result lists interleaved by rank (first of each, then second of each), without
    repeats: a keyword list, a list for the question with its context, a vector list."""
    out: list[LiveChunk] = []
    seen: set[UUID] = set()
    for rank in range(max((len(x) for x in lists), default=0)):
        for items in lists:
            if rank < len(items) and items[rank].id not in seen:
                seen.add(items[rank].id)
                out.append(items[rank])
                if len(out) == limit:
                    return out
    return out


# A short question, or one that points back ("which one?", "what's that called?"), carries
# too little to search with: in the analysed standups such turns had 4 to 10 words and no
# name. They get a second search with the names heard in the last minutes of the call.
POINTERS = frozenset(["that", "it", "this", "those", "these", "one", "there", "they", "them"])
CONTEXT_MIN_WORDS = 6
CONTEXT_MAX_NAMES = 6


def needs_context(question: str, names: Sequence[str]) -> bool:
    words = _WORD.findall(question.lower())
    if any(_mentions(question, n) for n in names):
        return False
    content = [w for w in words if w not in STOP]
    return len(content) < CONTEXT_MIN_WORDS or any(w in POINTERS for w in words)


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", text.lower()) is not None


def context_names(recent: str, names: Sequence[str]) -> list[str]:
    """The company's names (components, projects, terms) said in `recent` (the last lines of
    the call, oldest first), the latest said first."""
    found: list[tuple[int, str]] = []
    low = recent.lower()
    for name in names:
        if len(name) < 3:
            continue
        at = low.rfind(name.lower())
        if at >= 0 and _mentions(recent, name):
            found.append((at, name))
    return [name for _, name in sorted(found, reverse=True)[:CONTEXT_MAX_NAMES]]


def excerpt(text: str, query: str, words: int) -> str:
    """The part of a section that best matches the question, for the short answer: the
    window of `words` words with the most query words in it."""
    parts = text.split()
    if len(parts) <= words:
        return " ".join(parts)
    wanted = set(tokens(query))
    hit = [1 if any(t in wanted for t in tokens(p)) else 0 for p in parts]
    best, starts, run = -1, [0], sum(hit[:words])
    for start in range(len(parts) - words + 1):
        if start:
            run += hit[start + words - 1] - hit[start - 1]
        if run > best:
            best, starts = run, [start]
        elif run == best:
            starts.append(start)
    at = starts[len(starts) // 2]  # of the equally good windows, the one around the match
    lead = "... " if at else ""
    tail = " ..." if at + words < len(parts) else ""
    return f"{lead}{' '.join(parts[at : at + words])}{tail}"


SHORT_PREFIX = "k"  # n:k3f2: a section of a document; hex never has a k, nor a w
FAST_EXCERPT_WORDS = 70


def memory_lines(
    chunks: Sequence[LiveChunk], refs: dict[str, NodeRef], query: str, words: int | None
) -> list[str]:
    """The `<memory>` lines for sections: each a passage of a named document and section,
    with a short id the session's `refs` now map, so its citation shows the source. `words`
    cuts each to its best-matching window (the short answer); None gives the section whole."""
    lines: list[str] = []
    for c in chunks:
        sid = assign_short_id(c.id, refs, SHORT_PREFIX)
        refs[sid] = NodeRef(node_id=c.id, label=c.label()[:120])
        body = excerpt(c.text, query, words) if words else " ".join(c.text.split())
        lines.append(f"[{sid}] (doc: {c.label()}) {body}")
    return lines


# --- The company digest ----------------------------------------------------------------------

DIGEST_KINDS = ("system", "decision", "flow", "constraint", "risk", "term")
DIGEST_SECTIONS: dict[str, tuple[str, int]] = {
    "system": ("System map", 24),
    "flow": ("How it works", 10),
    "decision": ("Decisions", 24),
    "constraint": ("Constraints and limits", 12),
    "risk": ("Open risks", 8),
    "term": ("Glossary", 24),
}
DIGEST_MAX_CHARS = 10_000  # about 2,500 tokens
DIGEST_LINE_CHARS = 420


class KnowledgeDigest(BaseModel):
    """The `<company_digest>` block and the short ids it cites. Rendered with no model call,
    so the same memory gives the same bytes and the prompt cache holds."""

    model_config = ConfigDict(extra="ignore")
    text: str = ""
    refs: dict[str, NodeRef] = {}
    names: list[str] = []  # components, flows and terms, for a question's context search


def _body(row: Any) -> dict[str, Any]:
    body = row["body"]
    if isinstance(body, str):
        body = json.loads(body)
    return cast(dict[str, Any], body or {})


def _strs(value: object) -> list[str]:
    items = cast(list[Any], value) if isinstance(value, list) else []
    return [str(v).strip() for v in items if str(v).strip()]


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if not text or text[-1] in ".!?" else text + "."


def digest_line(row: Any) -> str:
    """One fact as the digest and the `<memory>` block show it: what it is, then the fields
    its kind carries (a decision's reason, what was rejected and the tradeoff; a component's
    neighbours; a flow's steps)."""
    title, summary, body, kind = str(row["title"]), str(row["summary"]), _body(row), row["kind"]
    parts = [_sentence(title if not summary or summary == title else f"{title}: {summary}")]
    if kind == "system":
        if talks := _strs(body.get("talks_to")):
            parts.append(f"Talks to: {', '.join(talks)}.")
        if stack := _strs(body.get("stack")):
            parts.append(f"Stack: {', '.join(stack)}.")
    elif kind == "decision":
        if why := str(body.get("why") or "").strip():
            parts.append(_sentence(f"Why: {why}"))
        if rejected := _strs(body.get("alternatives")):
            parts.append(_sentence(f"Rejected: {'; '.join(rejected)}"))
        if tradeoff := str(body.get("tradeoff") or "").strip():
            parts.append(_sentence(f"Tradeoff: {tradeoff}"))
    elif kind == "flow":
        steps = _strs(body.get("steps"))
        if steps:
            parts.append(" ".join(f"{n}) {_sentence(s)}" for n, s in enumerate(steps, 1)))
    line = " ".join(parts)
    return line if len(line) <= DIGEST_LINE_CHARS else line[: DIGEST_LINE_CHARS - 3] + "..."


def _rank(row: Any) -> tuple[int, str, str]:
    """A document's facts before a work log's mention of the same kind; decisions newest
    first; otherwise by name. Stable for the same memory."""
    source = row["source"]
    if isinstance(source, str):
        source = json.loads(source)
    from_doc = 0 if cast(dict[str, Any], source or {}).get("type") == "company_document" else 1
    day = row.get("valid_from") or row.get("created_at")
    newest = "" if row["kind"] != "decision" or day is None else str(day)[:10]
    inverse = "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in newest)
    return from_doc, inverse, str(row["title"]).lower()


def render_digest(company: str, nodes: Sequence[Any], sids: Mapping[UUID, str]) -> KnowledgeDigest:
    """A company's digest from its active workplace nodes (rows with id, kind, title, summary,
    body, source, valid_from, created_at). Empty when the company has no component, decision
    or flow from a document: a work log's passing mentions alone are no system map."""
    by_kind: dict[str, list[Any]] = {}
    for n in nodes:
        if n["kind"] in DIGEST_KINDS:
            by_kind.setdefault(str(n["kind"]), []).append(n)
    documented = [
        n for n in nodes
        if n["kind"] in ("system", "decision", "flow") and _rank(n)[0] == 0
    ]  # fmt: skip
    if not documented:
        return KnowledgeDigest()
    lines = [f"What {company}'s systems are and why, from the documents uploaded for it."]
    refs: dict[str, NodeRef] = {}
    names: list[str] = []
    used = len(lines[0])
    for kind, (heading, cap) in DIGEST_SECTIONS.items():
        rows = sorted(by_kind.get(kind, []), key=_rank)[:cap]
        section: list[str] = []
        for n in rows:
            sid = sids.get(n["id"], "")
            line = f"- [{sid}] {digest_line(n)}" if sid else f"- {digest_line(n)}"
            if used + len(line) + 1 > DIGEST_MAX_CHARS:
                break
            used += len(line) + 1
            section.append(line)
            if sid:
                refs[sid] = NodeRef(node_id=n["id"], label=str(n["title"])[:120])
            if kind in ("system", "flow", "term"):
                names.append(str(n["title"]))
        if section:
            lines += [f"## {heading}", *section]
            used += len(heading) + 4
    return KnowledgeDigest(text="\n".join(lines), refs=refs, names=names)
