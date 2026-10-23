"""Phase 1 memory: career facts from the resume, and the hot digest built from them.

Each resume version seeds its own part of the memory graph (ARCHITECTURE.md, "Write path",
step 1, and "Resume versions"): roles, projects, stories and skills become `memory_nodes` with
`source.type = "resume"` and the version's id, linked by `part_of` edges. New content for a
version archives that version's earlier nodes, and only those. Each extracted item is
validated with a Pydantic model for its kind; items that fail are dropped.

A version's digest is rendered from its own active nodes without a model call, so the same
memory always gives the same bytes (prompt caching) and a rebuild costs nothing, and no fact
of another version can reach it. Short ids come from a hash of the node id, so they stay the
same across rebuilds and match the session packs.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import Digest, NodeRef, short_ids

DIGEST_MAX_CHARS = 14_000  # about 3,500 tokens; the doc's limit is 4,000
MAX_STORIES = 12
MAX_PROJECTS = 12


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RoleItem(_Item):
    key: str
    company: str
    title: str
    start: str = ""
    end: str = ""
    team: str = ""
    summary: str = ""


class ProjectItem(_Item):
    key: str
    role_key: str = ""
    title: str
    summary: str
    contribution: str = ""
    stack: list[str] = []
    outcome: str = ""
    metrics: list[str] = []


class StoryItem(_Item):
    key: str
    project_key: str = ""
    title: str
    one_liner: str
    situation: str = ""
    task: str = ""
    action: str = ""
    result: str = ""
    metrics: list[str] = []
    competencies: list[str] = []


class SkillItem(_Item):
    name: str
    years: float = 0
    level: str = ""


def _valid[T: _Item](model: type[T], items: Any) -> list[T]:
    out: list[T] = []
    for raw in cast(list[Any], items or []):
        try:
            item = model.model_validate(raw)
        except ValidationError:
            continue
        if all(v != "" for k, v in item.model_dump().items() if k in ("key", "title", "name")):
            out.append(item)
    return out


async def replace_resume_nodes(
    conn: Conn, user_id: UUID, resume_version_id: UUID, data: dict[str, Any]
) -> int:
    """Archive this version's earlier nodes and write the new ones; returns the node count."""
    roles = _valid(RoleItem, data.get("roles"))
    projects = _valid(ProjectItem, data.get("projects"))
    stories = _valid(StoryItem, data.get("stories"))
    skills = _valid(SkillItem, data.get("skills"))
    source = {"type": "resume", "ref": str(resume_version_id)}
    await conn.execute(
        "update app.memory_nodes set status = 'archived', updated_at = now()"
        " where user_id = $1 and status = 'active' and resume_version_id = $2",
        user_id,
        resume_version_id,
    )
    ids: dict[str, UUID] = {}
    rows: list[tuple[Any, ...]] = []

    def node(kind: str, key: str | None, title: str, summary: str, body: dict[str, Any]) -> UUID:
        node_id = uuid7()
        if key:
            ids[key] = node_id
        rows.append((node_id, user_id, kind, title, summary, body, source, resume_version_id))
        return node_id

    for r in roles:
        label = f"{r.title}, {r.company}"
        body = {"company": r.company, "title": r.title, "start": r.start, "end": r.end,
                "team": r.team, "manager_name": ""}  # fmt: skip
        node("role", r.key, label, r.summary or label, body)
    for p in projects:
        body = {"goal": p.summary, "contribution": p.contribution, "stack": p.stack,
                "outcome": p.outcome, "metrics": p.metrics}  # fmt: skip
        node("project", p.key, p.title, p.summary, body)
    for s in stories:
        body = {"situation": s.situation, "task": s.task, "action": s.action,
                "result": s.result, "metrics": s.metrics, "competencies": s.competencies,
                "one_liner": s.one_liner}  # fmt: skip
        node("story", s.key, s.title, s.one_liner, body)
    for k in skills:
        node("skill", None, k.name, k.level or k.name,
             {"level": k.level, "years": k.years, "evidence_node_ids": []})  # fmt: skip
    await conn.executemany(
        "insert into app.memory_nodes (id, user_id, kind, domain, title, summary, body, source,"
        " resume_version_id) values ($1, $2, $3, 'career', $4, $5, $6, $7, $8)",
        rows,
    )
    edges: list[tuple[UUID, UUID, UUID]] = []
    for p in projects:
        if p.role_key in ids:
            edges.append((user_id, ids[p.key], ids[p.role_key]))
    for s in stories:
        if s.project_key in ids:
            edges.append((user_id, ids[s.key], ids[s.project_key]))
    await conn.executemany(
        "insert into app.memory_edges (user_id, src, rel, dst) values ($1, $2, 'part_of', $3)"
        " on conflict do nothing",
        edges,
    )
    return len(rows)


async def memory_version(conn: Conn, user_id: UUID, resume_version_id: UUID | None = None) -> str:
    """Changes whenever an active node (of the version, if given) is added, changed or
    archived."""
    rows = await conn.fetch(
        "select id, updated_at from app.memory_nodes where user_id = $1 and status = 'active'"
        " and ($2::uuid is null or resume_version_id = $2) order by id",
        user_id,
        resume_version_id,
    )
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}:{r['updated_at'].isoformat()};".encode())
    return h.hexdigest()[:16]


def _body(row: Any) -> dict[str, Any]:
    body = row["body"]
    return cast(dict[str, Any], json.loads(body) if isinstance(body, str) else body)


def render_digest(
    display_name: str, nodes: Sequence[Any], today: str | None = None
) -> Digest | None:
    """The hot digest (ARCHITECTURE.md, "Session pack and hot digest formats") from active
    nodes in creation order; None when there is nothing to say yet."""
    if not nodes:
        return None
    sids = short_ids([n["id"] for n in nodes])
    by_kind: dict[str, list[Any]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind"], []).append(n)
    day = today or datetime.now(UTC).date().isoformat()
    lines = [f"# Career digest (version {day})", f"Name: {display_name}"]
    refs: dict[str, NodeRef] = {}

    def cite(n: Any) -> str:
        refs[sids[n["id"]]] = NodeRef(node_id=n["id"], label=n["title"])
        return f"[{sids[n['id']]}]"

    if by_kind.get("role"):
        lines.append("## Roles")
        for n in by_kind["role"]:
            b = _body(n)
            when = f", {b.get('start')} to {b.get('end') or 'now'}" if b.get("start") else ""
            team = f" Team: {b['team']}." if b.get("team") else ""
            summary = f" {n['summary']}" if n["summary"] != n["title"] else ""
            lines.append(f"{cite(n)} {n['title']}{when}.{team}{summary}".rstrip())
    if by_kind.get("project"):
        lines.append("## Projects")
        for n in by_kind["project"][:MAX_PROJECTS]:
            b = _body(n)
            extra = ""
            if b.get("contribution"):
                extra += f" Contribution: {b['contribution']}"
            if b.get("stack"):
                extra += f" Stack: {', '.join(b['stack'])}."
            if b.get("metrics"):
                extra += f" Metrics: {'; '.join(b['metrics'])}."
            lines.append(f"{cite(n)} {n['title']}: {n['summary']}{extra}")
    if by_kind.get("story"):
        lines.append("## Top stories")
        for n in by_kind["story"][:MAX_STORIES]:
            b = _body(n)
            parts = [f"{cite(n)} {n['title']}: {n['summary']}"]
            for label, key in (("Situation", "situation"), ("Action", "action"),
                               ("Result", "result")):  # fmt: skip
                if b.get(key):
                    parts.append(f"{label}: {b[key]}")
            if b.get("competencies"):
                parts.append(f"Competencies: {', '.join(b['competencies'])}.")
            lines.append(" ".join(parts))
    if by_kind.get("skill"):
        lines.append("## Skills")
        skills: list[str] = []
        for n in by_kind["skill"]:
            years = float(_body(n).get("years") or 0)
            skills.append(f"{n['title']} ({years:g} y)" if years else n["title"])
        lines.append(", ".join(skills))
    text = ""
    for line in lines:  # keep whole lines within the size limit
        if len(text) + len(line) + 1 > DIGEST_MAX_CHARS:
            break
        text += line + "\n"
    kept = {k: v for k, v in refs.items() if f"[{k}]" in text}
    return Digest.model_validate({"version": Digest.version_of(text), "text": text, "refs": kept})


async def load_digest_nodes(conn: Conn, user_id: UUID, resume_version_id: UUID) -> list[Any]:
    """The digest's nodes: one resume version's career facts, and nothing else."""
    return list(
        await conn.fetch(
            "select id, kind, title, summary, body from app.memory_nodes"
            " where user_id = $1 and resume_version_id = $2 and status = 'active'"
            " and domain = 'career' and kind in ('role', 'project', 'story', 'skill') order by id",
            user_id,
            resume_version_id,
        )
    )
