"""Hot digest and session pack formats (ARCHITECTURE.md, "Session pack and hot digest formats").

Workers build both and write them to Valkey; the gateway reads them at session start. The
`render_*` functions produce the exact prompt blocks, so they must stay deterministic: the same
digest or pack always renders to the same bytes, or prompt caching misses.
"""

import base64
import hashlib
import math
import re
import struct
from collections.abc import Iterable, Mapping
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SHORT_ID = re.compile(r"\[(n:[0-9a-z]{2,8})\]")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NodeRef(_Model):
    node_id: UUID
    label: str


class Digest(_Model):
    """`digest:{uid}`: the hot digest text plus the map from its short ids to node ids."""

    schema_: Literal["digest.v1"] = Field("digest.v1", alias="schema")
    version: str  # "sha256:<hex>" of text
    text: str
    refs: dict[str, NodeRef] = {}
    built_at: str | None = None  # when the worker wrote it; not part of the prompt

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @staticmethod
    def version_of(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class PackContext(_Model):
    company: str | None = None
    role_title: str | None = None
    round: str | None = None
    job_summary: str | None = None
    # The job description's key requirements (platforms, tools, duties), so an interview
    # answer's suggestions can aim at what the job asks for. The live prompts carry the pack,
    # not the description itself.
    requirements: list[str] = []


class RankedStory(_Model):
    node_id: UUID
    short_id: str
    title: str
    one_liner: str
    competencies: list[str] = []


class PreparedAnswer(_Model):
    question: str
    embedding_b64: str | None = None
    talking_points: list[str]
    node_ids: list[UUID] = []

    def embedding(self) -> list[float] | None:
        if not self.embedding_b64:
            return None
        raw = base64.b64decode(self.embedding_b64)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))


class PackItem(_Model):
    """One line of a standup or meeting pack: a workplace memory node as the prompt shows it."""

    node_id: UUID
    short_id: str = ""
    text: str
    title: str = ""  # the node's short name, for prepared talking points
    external_id: str = ""  # a ticket or PR id, when the log gave one
    date: str | None = None  # the work log day it came from
    due_date: str | None = None  # commitments
    state: str | None = None


class StandupBrief(_Model):
    """A company's standup: the last working day's log, today's plans, blockers and open
    commitments, all from that company's memory (copilot_core.workplace)."""

    day: str = ""  # the standup's date, in the user's time zone
    yesterday_day: str | None = None  # the log day "yesterday" is from (the last one before)
    yesterday: list[PackItem] = []
    today: list[PackItem] = []
    blockers: list[PackItem] = []
    open_commitments: list[PackItem] = []
    recent: list[PackItem] = []  # the rest of the last week, for "what about X?"


# The meeting brief's sections, in prompt order, with their headings.
BRIEF_SECTIONS: dict[str, str] = {
    "projects": "Projects",
    "systems": "Systems and stack",
    "people": "People",
    "decisions": "Decisions",
    "terms": "Terms used here",
    "recent_work": "Recent work",
    "open_commitments": "Open commitments",
}


class WorkplaceBrief(_Model):
    """A company's meeting pack: what the user's work there is about, from its memory."""

    sections: dict[str, list[PackItem]] = {}


class MeetingContext(_Model):
    """What the user typed for this meeting, and the company's memory that matches it. Set
    by the gateway at session start on its own copy of the pack, never stored by workers."""

    title: str | None = None
    agenda: str | None = None
    related: list[PackItem] = []


class FocusMaterial(_Model):
    """A company document chosen for this call, and its approved facts."""

    document_id: UUID
    items: list[PackItem] = []


class EmployerRef(_Model):
    id: UUID
    name: str
    title: str = ""
    team: str = ""


class SessionPack(_Model):
    """`pack:{uid}:{pack_id}`: `session_pack.v1`.

    `embedding_model` names the model that made the prepared answers' vectors
    (`CoreSettings.embedding_model_id`); None when they have none. The gateway compares them
    only with question vectors from the same model."""

    schema_: Literal["session_pack.v1"] = Field("session_pack.v1", alias="schema")
    pack_id: UUID
    user_id: UUID
    mode: Literal["interview", "standup", "meeting"]
    built_at: str
    digest_version: str
    context: PackContext = PackContext()
    ranked_stories: list[RankedStory] = []
    prepared_answers: list[PreparedAnswer] = []
    embedding_model: str | None = None
    standup: StandupBrief | None = None
    token_count: int = 0
    # Company workspaces: the employer a standup or meeting pack belongs to, the version of
    # that company's memory it was rendered from, and the meeting brief.
    employer: EmployerRef | None = None
    memory_version: str | None = None
    workplace: WorkplaceBrief | None = None
    meeting: MeetingContext | None = None
    # A standup or meeting: the company documents the user chose on the start screen, and
    # every approved fact from them, read before the rest. Set by the gateway on its own
    # copy of the pack, never stored by workers.
    focus: list[FocusMaterial] = []
    # Short ids used in prepared talking points that are not ranked stories.
    refs: dict[str, NodeRef] = {}
    # An application's interview pack: the resume version its digest and stories came from.
    # None on packs from before resume versions, which all came from the one (base) resume.
    resume_version_id: UUID | None = None
    # The pairing it was built for (copilot_core.resumes.pairing_tag), so a rebuild can tell
    # whether it is still current.
    pairing: str | None = None
    # A company pack's names for speech to text (Deepgram keyterms): the company, its
    # projects, systems, people and terms, so a product name is not misheard.
    keyterms: list[str] = []

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def encode_embedding(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def pack_items(pack: SessionPack) -> list[PackItem]:
    """Every standup and meeting item of a pack."""
    items: list[PackItem] = []
    if pack.standup is not None:
        b = pack.standup
        for group in (b.yesterday, b.today, b.blockers, b.open_commitments, b.recent):
            items.extend(group)
    if pack.workplace is not None:
        for group in pack.workplace.sections.values():
            items.extend(group)
    if pack.meeting is not None:
        items.extend(pack.meeting.related)
    for material in pack.focus:
        items.extend(material.items)
    return items


def ref_map(digest: Digest | None, pack: SessionPack | None) -> dict[str, NodeRef]:
    """Short id to node, from the digest and the pack (the pack wins on a clash)."""
    refs: dict[str, NodeRef] = {}
    if digest is not None:
        refs.update(digest.refs)
    if pack is not None:
        refs.update(pack.refs)
        for story in pack.ranked_stories:
            refs[story.short_id] = NodeRef(node_id=story.node_id, label=story.title)
        for item in pack_items(pack):
            if item.short_id:
                refs[item.short_id] = NodeRef(node_id=item.node_id, label=item.text[:120])
    return refs


def short_ids(node_ids: Iterable[UUID], prefix: str = "") -> dict[UUID, str]:
    """Short ids like n:3f2a from a hash of the node id, stable across rebuilds; longer only on
    a clash. Career memory uses plain hex; workplace memory starts with `w` (n:w3f2), a letter
    hex never has, so a company's ids never clash with the digest's."""
    out: dict[UUID, str] = {}
    used: set[str] = set()
    for node_id in sorted(set(node_ids)):
        out[node_id] = _short_id(node_id, prefix, used)
        used.add(out[node_id])
    return out


def _short_id(node_id: UUID, prefix: str, taken: Iterable[str]) -> str:
    used = set(taken)
    digest = hashlib.sha256(node_id.bytes).hexdigest()
    size = 4 - len(prefix)
    while f"n:{prefix}{digest[:size]}" in used:
        size += 1
    return f"n:{prefix}{digest[:size]}"


def assign_short_id(node_id: UUID, refs: Mapping[str, NodeRef], prefix: str = "w") -> str:
    """The short id for a node found during the call (live memory search): the one the session
    already uses for it, otherwise the stable one, longer if another node has that."""
    for short, ref in refs.items():
        if ref.node_id == node_id:
            return short
    return _short_id(node_id, prefix, refs.keys())


def cited_refs(text: str, refs: dict[str, NodeRef]) -> list[NodeRef]:
    """Memory refs for the short ids an answer cites, in first-cited order, without repeats."""
    out: list[NodeRef] = []
    seen: set[str] = set()
    for short in SHORT_ID.findall(text):
        if short in refs and short not in seen:
            seen.add(short)
            out.append(refs[short])
    return out


def render_pack(pack: SessionPack) -> str:
    """The `<session_pack>` prompt block body. Embeddings are left out."""
    lines: list[str] = [f"Mode: {pack.mode}"]
    ctx = pack.context
    for label, value in (
        ("Company", ctx.company),
        ("Role", ctx.role_title),
        ("Round", ctx.round),
        ("Job summary", ctx.job_summary),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if ctx.requirements:
        lines.append("Job requirements: " + "; ".join(ctx.requirements))
    if pack.ranked_stories:
        lines.append("## Stories ranked for this call")
        for s in pack.ranked_stories:
            comps = f" Competencies: {', '.join(s.competencies)}." if s.competencies else ""
            lines.append(f"[{s.short_id}] {s.title}: {s.one_liner}{comps}")
    if pack.prepared_answers:
        lines.append("## Prepared answers")
        for a in pack.prepared_answers:
            lines.append(f"Q: {a.question}")
            lines.extend(f"- {p}" for p in a.talking_points)
    if pack.standup is not None:
        lines.extend(_render_standup(pack.standup))
    focus = [m for m in pack.focus if m.items]
    if focus:
        lines.append("## Material chosen for this call (look here first)")
        for n, material in enumerate(focus, 1):
            if len(focus) > 1:
                lines.append(f"Document {n}:")
            lines.extend(render_item(i) for i in material.items)
    if pack.meeting is not None and (pack.meeting.title or pack.meeting.agenda):
        lines.append("## This meeting")
        if pack.meeting.title:
            lines.append(f"Title: {pack.meeting.title}")
        if pack.meeting.agenda:
            lines.append(f"Agenda: {pack.meeting.agenda}")
        if pack.meeting.related:
            lines.append("Related to the agenda:")
            lines.extend(render_item(i) for i in pack.meeting.related)
    if pack.workplace is not None:
        for key, heading in BRIEF_SECTIONS.items():
            items = pack.workplace.sections.get(key) or []
            if items:
                lines.append(f"## {heading}")
                lines.extend(render_item(i) for i in items)
    return "\n".join(lines)


def render_item(item: PackItem) -> str:
    cite = f"[{item.short_id}] " if item.short_id else ""
    extra: list[str] = []
    if item.state and item.state not in ("open", "active"):
        extra.append(item.state.replace("_", " "))
    if item.date:
        extra.append(item.date)
    if item.due_date:
        extra.append(f"due {item.due_date}")
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"- {cite}{item.text}{tail}"


def _render_standup(b: StandupBrief) -> list[str]:
    lines = [f"## Standup on {b.day}" if b.day else "## Standup"]
    yesterday = f"Yesterday ({b.yesterday_day})" if b.yesterday_day else "Yesterday"
    groups = (
        (yesterday, b.yesterday, "nothing logged"),
        ("Today", b.today, "nothing planned in the log"),
        ("Blockers", b.blockers, "none logged"),
        ("Open commitments", b.open_commitments, "none"),
    )
    for heading, items, empty in groups:
        lines.append(f"{heading}:")
        if items:
            lines.extend(render_item(i) for i in items)
        else:
            lines.append(f"- ({empty})")
    if b.recent:
        lines.append("Earlier this week:")
        lines.extend(render_item(i) for i in b.recent)
    return lines
