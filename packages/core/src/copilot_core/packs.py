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

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @staticmethod
    def version_of(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class PackContext(_Model):
    company: str | None = None
    role_title: str | None = None
    round: str | None = None
    job_summary: str | None = None


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


class SessionPack(_Model):
    """`pack:{uid}:{pack_id}`: `session_pack.v1`."""

    schema_: Literal["session_pack.v1"] = Field("session_pack.v1", alias="schema")
    pack_id: UUID
    user_id: UUID
    mode: Literal["interview", "standup", "meeting"]
    built_at: str
    digest_version: str
    context: PackContext = PackContext()
    ranked_stories: list[RankedStory] = []
    prepared_answers: list[PreparedAnswer] = []
    standup: dict[str, object] | None = None
    token_count: int = 0
    # Short ids used in prepared talking points that are not ranked stories.
    refs: dict[str, NodeRef] = {}

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


def ref_map(digest: Digest | None, pack: SessionPack | None) -> dict[str, NodeRef]:
    """Short id to node, from the digest and the pack (the pack wins on a clash)."""
    refs: dict[str, NodeRef] = {}
    if digest is not None:
        refs.update(digest.refs)
    if pack is not None:
        refs.update(pack.refs)
        for story in pack.ranked_stories:
            refs[story.short_id] = NodeRef(node_id=story.node_id, label=story.title)
    return refs


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
    if pack.standup:
        lines.append("## Standup")
        for key in ("yesterday", "today", "blockers", "open_commitments"):
            if key in pack.standup:
                lines.append(f"{key}: {pack.standup[key]}")
    return "\n".join(lines)
