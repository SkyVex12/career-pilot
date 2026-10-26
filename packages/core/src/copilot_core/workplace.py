"""Company workspaces (ARCHITECTURE.md, "Memory system", company workspaces).

Workplace memory belongs to one employer (`memory_nodes.employer_id`). This module is shared
by the workers, which build packs ahead of time, and the gateway, which renders the same pack
itself when none is current at session start and searches the company's memory live:

- `memory_version`: changes whenever the company's active memory does; packs carry it.
- `standup_pack`, `meeting_pack`: rendered from the company's nodes with no model call, so
  the same memory always gives the same bytes (prompt caching) and a build takes milliseconds.
- `search`: the doc's hybrid search (full text, then vectors when the question's embedding is
  in, fused by reciprocal rank, one hop over edges), scoped to one user and one employer.

Node bodies for the workplace kinds:

| kind | body |
| --- | --- |
| work_item | state (done, in_progress, planned, blocked), external_id, project |
| commitment | due_date, state (open, done, dropped), session_id, closed_by |
| project, system | stack |
| person | role |
| decision | project |
| term | (the meaning is the summary) |

`valid_from` is the work log day (or the meeting's) a work item, decision or commitment is from.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import (
    EmployerRef,
    NodeRef,
    PackItem,
    PreparedAnswer,
    SessionPack,
    StandupBrief,
    WorkplaceBrief,
    assign_short_id,
    render_pack,
    short_ids,
)

# A node row: an asyncpg Record or a mapping with its columns.
type Row = Any

WORKPLACE_KINDS = ("work_item", "commitment", "project", "system", "person", "decision", "term")
SHORT_PREFIX = "w"

# The standup questions the pack prepares points for: they are the same every day, so their
# vectors are made once per embedding model and carried from pack to pack.
STANDUP_QUESTIONS: dict[str, str] = {
    "yesterday": "What did you work on yesterday?",
    "today": "What are you working on today?",
    "blockers": "Is anything blocking you?",
    "update": "What's your update?",
}

RECENT_DAYS = 7
MEETING_RECENT_DAYS = 14
# Caps per meeting brief section, and on the whole brief (about 3,000 tokens).
BRIEF_CAPS = {
    "projects": 12,
    "systems": 12,
    "people": 15,
    "decisions": 10,
    "terms": 20,
    "recent_work": 15,
    "open_commitments": 10,
}
BRIEF_MAX_CHARS = 12_000


def body_of(row: Row) -> dict[str, Any]:
    body = row["body"]
    if isinstance(body, str):
        body = json.loads(body)
    return cast(dict[str, Any], body or {})


def local_today(settings: Mapping[str, Any] | None, now: datetime | None = None) -> date:
    """Today in the user's time zone (`users.settings.timezone`), UTC without one."""
    now = now or datetime.now(UTC)
    tz_name = str((settings or {}).get("timezone") or "")
    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return now.astimezone(tz).date()


async def employer(conn: Conn, employer_id: UUID) -> EmployerRef | None:
    row = await conn.fetchrow(
        "select id, name, title, team from app.employers where id = $1", employer_id
    )
    if row is None:
        return None
    return EmployerRef(id=row["id"], name=row["name"], title=row["title"], team=row["team"])


async def memory_version(conn: Conn, user_id: UUID, employer_id: UUID) -> str:
    """Changes whenever an active node of this company is added, changed or archived."""
    rows = await conn.fetch(
        "select id, updated_at from app.memory_nodes"
        " where user_id = $1 and employer_id = $2 and status = 'active' order by id",
        user_id,
        employer_id,
    )
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}:{r['updated_at'].isoformat()};".encode())
    return h.hexdigest()[:16]


async def load_nodes(conn: Conn, user_id: UUID, employer_id: UUID) -> list[Row]:
    return list(
        await conn.fetch(
            "select id, kind, title, summary, body, valid_from, created_at, updated_at"
            " from app.memory_nodes"
            " where user_id = $1 and employer_id = $2 and status = 'active'"
            " and domain = 'workplace' order by id",
            user_id,
            employer_id,
        )
    )


def normalize(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title.lower())).strip()


def item_text(row: Row) -> str:
    """How a node reads in a pack or the <memory> block."""
    title, summary = str(row["title"]), str(row["summary"])
    b = body_of(row)
    kind = row["kind"]
    text = title if not summary or normalize(summary) == normalize(title) else f"{title}: {summary}"
    if kind == "person" and b.get("role") and str(b["role"]).lower() not in text.lower():
        text = (
            f"{title} ({b['role']}): {summary}"
            if summary and summary != title
            else (f"{title} ({b['role']})")
        )
    if kind in ("project", "system") and b.get("stack"):
        stack = ", ".join(str(s) for s in cast(list[Any], b["stack"]))
        if stack.lower() not in text.lower():
            text += f". Stack: {stack}"
    if kind == "work_item" and b.get("external_id") and str(b["external_id"]) not in text:
        text = f"{text} ({b['external_id']})"
    return text


def _day(row: Row) -> date | None:
    value = row.get("valid_from")
    return value if isinstance(value, date) else None


def _item(row: Row, sids: Mapping[UUID, str], with_date: bool = False) -> PackItem:
    b = body_of(row)
    day = _day(row)
    state = str(b.get("state") or "") or None
    if row["kind"] == "commitment" and state == "open":
        state = None
    return PackItem(
        node_id=row["id"],
        short_id=sids.get(row["id"], ""),
        text=item_text(row),
        title=str(row["title"]),
        external_id=str(b.get("external_id") or ""),
        date=day.isoformat() if (with_date and day) else None,
        due_date=str(b["due_date"]) if row["kind"] == "commitment" and b.get("due_date") else None,
        state=state if row["kind"] in ("work_item", "commitment") else None,
    )


def _state(row: Row) -> str:
    return str(body_of(row).get("state") or "")


def open_commitments(nodes: Sequence[Row]) -> list[Row]:
    rows = [n for n in nodes if n["kind"] == "commitment" and _state(n) in ("", "open")]
    return sorted(rows, key=lambda n: (str(body_of(n).get("due_date") or "9999"), n["id"]))


def _pack(mode: str, emp: EmployerRef, version: str, built_at: datetime | None) -> dict[str, Any]:
    return {
        "pack_id": str(uuid7()),
        "user_id": "00000000-0000-0000-0000-000000000000",  # set by the caller
        "mode": mode,
        "built_at": (built_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "digest_version": "",
        "context": {"company": emp.name, "role_title": emp.title or None},
        "employer": emp.model_dump(mode="json"),
        "memory_version": version,
    }


def standup_pack(
    user_id: UUID,
    emp: EmployerRef,
    nodes: Sequence[Row],
    today: date,
    version: str,
    built_at: datetime | None = None,
) -> SessionPack:
    """The standup pack for `today`: the last log day before it is "yesterday" (a Monday
    standup gets Friday's log), today's items plus the plans of that day are "today"."""
    sids = short_ids([n["id"] for n in nodes], SHORT_PREFIX)
    items = [n for n in nodes if n["kind"] in ("work_item", "decision") and _day(n) is not None]
    days = sorted({d for n in items if (d := _day(n)) is not None and d < today})
    last = days[-1] if days else None
    since = today - timedelta(days=RECENT_DAYS)

    yesterday = [n for n in items if _day(n) == last and _state(n) != "planned"]
    yesterday = [n for n in yesterday if not (n["kind"] == "work_item" and _state(n) == "blocked")]
    today_rows = [n for n in items if _day(n) == today and _state(n) != "blocked"]
    today_rows += [n for n in items if _day(n) == last and _state(n) == "planned"]
    # A blocker stays one until a later log says the same item moved on.
    later_done: dict[str, date] = {}
    for n in items:
        d = _day(n)
        if d is not None and n["kind"] == "work_item" and _state(n) in ("done", "in_progress"):
            key = normalize(str(n["title"]))
            later_done[key] = max(later_done.get(key, d), d)
    blockers = [
        n
        for n in items
        if n["kind"] == "work_item"
        and _state(n) == "blocked"
        and (d := _day(n)) is not None
        and d >= since
        and later_done.get(normalize(str(n["title"])), date.min) <= d
    ]
    shown = {n["id"] for n in (*yesterday, *today_rows, *blockers)}
    recent = [
        n
        for n in items
        if n["id"] not in shown and (d := _day(n)) is not None and since <= d < today
    ]
    recent.sort(key=lambda n: (_day(n), n["id"]), reverse=True)

    def dedupe(rows: Sequence[Row]) -> list[Row]:
        seen: set[str] = set()
        out: list[Row] = []
        for n in rows:
            key = normalize(str(n["title"]))
            if key not in seen:
                seen.add(key)
                out.append(n)
        return out

    brief = StandupBrief(
        day=today.isoformat(),
        yesterday_day=last.isoformat() if last else None,
        yesterday=[_item(n, sids) for n in dedupe(yesterday)],
        today=[_item(n, sids) for n in dedupe(today_rows)],
        blockers=[_item(n, sids, with_date=True) for n in dedupe(blockers)],
        open_commitments=[_item(n, sids) for n in open_commitments(nodes)],
        recent=[_item(n, sids, with_date=True) for n in dedupe(recent)[:10]],
    )
    data = _pack("standup", emp, version, built_at)
    data["user_id"] = str(user_id)
    pack = SessionPack.model_validate({**data, "standup": brief.model_dump(mode="json")})
    pack.prepared_answers = standup_answers(brief)
    pack.token_count = len(render_pack(pack)) // 4
    return pack


# Prepared standup points are read at a glance while the user talks: the item's name and
# state, never its whole summary (the 2026-10-26 standup showed four 200-300 character
# summaries). The deep pass still gets the full items from the pack.
POINT_MAX_WORDS = 12
POINTS_PER_SECTION = 3
STATE_LABELS = {"done": "Done", "in_progress": "In progress", "planned": "Next",
                "blocked": "Blocked"}  # fmt: skip


def _ranked(items: Sequence[PackItem]) -> list[PackItem]:
    """Items with a ticket or PR id first (what the team tracks), otherwise as listed."""
    return sorted(items, key=lambda i: not i.external_id)


def _point(i: PackItem) -> str:
    name = i.title or i.text
    words = name.split()
    if len(words) > POINT_MAX_WORDS:
        name = " ".join(words[:POINT_MAX_WORDS]) + "..."
    if i.external_id and i.external_id.lower() not in name.lower():
        name = f"{name} ({i.external_id})"
    label = STATE_LABELS.get(i.state or "")
    line = f"{label}: {name}" if label else name
    return f"{line} [{i.short_id}]" if i.short_id else line


def _points(items: Sequence[PackItem], n: int) -> list[str]:
    return [_point(i) for i in _ranked(items)[:n]]


def standup_answers(brief: StandupBrief) -> list[PreparedAnswer]:
    """Points for the fixed standup questions, straight from the brief (no model call).
    Vectors are added by the pack builder (`carry_vectors`, or the worker's embedder)."""
    answers: dict[str, list[str]] = {}
    if brief.yesterday:
        answers["yesterday"] = _points(brief.yesterday, POINTS_PER_SECTION)
    if brief.today:
        answers["today"] = _points(brief.today, POINTS_PER_SECTION)
    if brief.yesterday or brief.today:
        blockers = _points(brief.blockers, 2) or ["Nothing blocking me right now"]
        answers["blockers"] = blockers
        answers["update"] = (
            _points(brief.yesterday, 2) + _points(brief.today, 2) + _points(brief.blockers, 1)
        )
    return [
        PreparedAnswer(
            question=STANDUP_QUESTIONS[k],
            talking_points=v,
            node_ids=[i.node_id for i in _source(brief, k)],
        )
        for k, v in answers.items()
    ]


def _source(brief: StandupBrief, key: str) -> list[PackItem]:
    if key == "yesterday":
        return _ranked(brief.yesterday)[:POINTS_PER_SECTION]
    if key == "today":
        return _ranked(brief.today)[:POINTS_PER_SECTION]
    if key == "blockers":
        return _ranked(brief.blockers)[:2]
    return _ranked(brief.yesterday)[:2] + _ranked(brief.today)[:2] + _ranked(brief.blockers)[:1]


def carry_vectors(pack: SessionPack, previous: SessionPack | None) -> None:
    """Prepared standup questions never change, so their vectors carry over from an earlier
    pack made with the same embedding model; the gateway's own render uses this."""
    if previous is None or not previous.embedding_model:
        return
    known = {a.question: a.embedding_b64 for a in previous.prepared_answers if a.embedding_b64}
    if not known:
        return
    for a in pack.prepared_answers:
        a.embedding_b64 = known.get(a.question)
    if pack.prepared_answers and all(a.embedding_b64 for a in pack.prepared_answers):
        pack.embedding_model = previous.embedding_model
    else:
        for a in pack.prepared_answers:
            a.embedding_b64 = None


def meeting_pack(
    user_id: UUID,
    emp: EmployerRef,
    nodes: Sequence[Row],
    today: date,
    version: str,
    built_at: datetime | None = None,
) -> SessionPack:
    """The meeting brief: projects, systems and stack, people, recent decisions, terms, the
    last two weeks of work and open commitments. Live search covers the rest."""
    sids = short_ids([n["id"] for n in nodes], SHORT_PREFIX)
    by_kind: dict[str, list[Row]] = {}
    for n in nodes:
        by_kind.setdefault(str(n["kind"]), []).append(n)

    def newest(rows: Sequence[Row]) -> list[Row]:
        return sorted(rows, key=lambda n: (_day(n) or date.min, n["id"]), reverse=True)

    since = today - timedelta(days=MEETING_RECENT_DAYS)
    recent = [
        n
        for n in by_kind.get("work_item", [])
        if (d := _day(n)) is not None and since <= d <= today and _state(n) != "planned"
    ]
    rows: dict[str, list[Row]] = {
        "projects": by_kind.get("project", []),
        "systems": by_kind.get("system", []),
        "people": by_kind.get("person", []),
        "decisions": newest(by_kind.get("decision", [])),
        "terms": by_kind.get("term", []),
        "recent_work": newest(recent),
        "open_commitments": open_commitments(nodes),
    }
    sections: dict[str, list[PackItem]] = {}
    used = 0
    for key, cap in BRIEF_CAPS.items():
        dated = key in ("decisions", "recent_work")
        out: list[PackItem] = []
        for n in rows[key][:cap]:
            item = _item(n, sids, with_date=dated)
            if used + len(item.text) + 40 > BRIEF_MAX_CHARS:
                break
            used += len(item.text) + 40
            out.append(item)
        if out:
            sections[key] = out
    data = _pack("meeting", emp, version, built_at)
    data["user_id"] = str(user_id)
    pack = SessionPack.model_validate(
        {**data, "workplace": WorkplaceBrief(sections=sections).model_dump(mode="json")}
    )
    pack.token_count = len(render_pack(pack)) // 4
    return pack


async def render_employer_pack(
    conn: Conn,
    user_id: UUID,
    employer_id: UUID,
    mode: str,
    today: date,
    built_at: datetime | None = None,
) -> SessionPack | None:
    """Load a company's memory and render its standup or meeting pack; None if the company
    is gone. Runs as any role that may read employers and memory_nodes."""
    emp = await employer(conn, employer_id)
    if emp is None:
        return None
    nodes = await load_nodes(conn, user_id, employer_id)
    version = await memory_version(conn, user_id, employer_id)
    build = standup_pack if mode == "standup" else meeting_pack
    pack = build(user_id, emp, nodes, today, version, built_at)
    return pack.model_copy(update={"keyterms": keyterms(emp.name, nodes)})


KEYTERM_KINDS = ("project", "system", "term", "person")
KEYTERMS_MAX = 40
KEYTERM_MAX_WORDS = 4


def keyterms(company: str, nodes: Sequence[Row]) -> list[str]:
    """The names speech to text should expect at this company: the company, then its
    projects, systems, terms and people, in that order and by title (the same memory gives
    the same list), short ones only, at most KEYTERMS_MAX."""
    out: list[str] = []
    seen: set[str] = set()
    ranked = sorted(
        (n for n in nodes if n["kind"] in KEYTERM_KINDS),
        key=lambda n: (KEYTERM_KINDS.index(n["kind"]), str(n["title"]).lower()),
    )
    for title in [company, *(str(n["title"]) for n in ranked)]:
        title = " ".join(title.split())
        key = title.lower()
        if not title or key in seen or len(title.split()) > KEYTERM_MAX_WORDS:
            continue
        seen.add(key)
        out.append(title)
        if len(out) == KEYTERMS_MAX:
            break
    return out


# Live search -------------------------------------------------------------------------------

# Questions are long and spoken, so full text matches any of their words (an OR of the
# question's lexemes), ranked by how many and how well, instead of websearch's AND of all.
_TSQUERY = (
    "(select string_agg(quote_literal(l), ' | ')"
    " from unnest(tsvector_to_array(to_tsvector('english', $2))) l)::tsquery"
)

# $1 user, $2 query text, $3 employer, $4 limit
FTS_SQL = f"""
with q as (select {_TSQUERY} as tsq)
select n.id, n.kind, n.title, n.summary, n.body, n.valid_from,
       ts_rank(n.search, q.tsq) as rank
from app.memory_nodes n, q
where n.user_id = $1 and n.employer_id = $3 and n.status = 'active'
  and n.domain = 'workplace' and q.tsq is not null and n.search @@ q.tsq
order by rank desc, n.id
limit $4
"""

# $1 user, $2 query text, $3 employer, $4 limit, $5 query vector (text), $6 its model id
HYBRID_SQL = f"""
with q as (select {_TSQUERY} as tsq),
vec as (
  select id, row_number() over (order by embedding <=> $5::vector) as r
  from app.memory_nodes
  where user_id = $1 and employer_id = $3 and status = 'active' and domain = 'workplace'
    and embedding_model = $6
  order by embedding <=> $5::vector
  limit 40
),
fts as (
  select n.id, row_number() over (order by ts_rank(n.search, q.tsq) desc, n.id) as r
  from app.memory_nodes n, q
  where n.user_id = $1 and n.employer_id = $3 and n.status = 'active'
    and n.domain = 'workplace' and q.tsq is not null and n.search @@ q.tsq
  order by ts_rank(n.search, q.tsq) desc, n.id
  limit 40
),
fused as (
  select id, sum(1.0 / (60 + r)) as score
  from (select * from vec union all select * from fts) s
  group by id
  order by score desc, id
  limit $4
)
select n.id, n.kind, n.title, n.summary, n.body, n.valid_from, f.score as rank
from fused f join app.memory_nodes n using (id)
order by f.score desc, n.id
"""

# $1 uuid[] of the hits, $2 employer, $3 limit
HOPS_SQL = """
select distinct on (n.id) e.rel, n.id, n.kind, n.title, n.summary, n.body, n.valid_from
from app.memory_edges e
join app.memory_nodes n on n.id = case when e.src = any($1) then e.dst else e.src end
where (e.src = any($1) or e.dst = any($1)) and n.status = 'active'
  and n.employer_id = $2 and not (n.id = any($1))
order by n.id
limit $3
"""


@dataclass(frozen=True)
class Hit:
    node_id: UUID
    kind: str
    text: str
    day: str | None
    score: float
    hop: bool = False


# Ticket and PR ids that tie a work item's entries on different days together.
TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b|\bPR #\d+\b")


def thread_key(h: Hit) -> str:
    """The work item a hit is about: its first ticket or PR id, else its title."""
    found = TICKET.search(h.text)
    return found.group(0) if found else normalize(h.text.split(":", 1)[0])


def latest_only(hits: list[Hit]) -> list[Hit]:
    """One hit per work item: a ticket logged on several days ("PSP-412" in progress, then
    deployed) keeps only its newest entry, in the place of the best-ranked one, so an answer
    never reports an outdated status."""
    newest: dict[str, Hit] = {}
    for h in hits:
        if h.kind != "work_item":
            continue
        key = thread_key(h)
        if key not in newest or (h.day or "") > (newest[key].day or ""):
            newest[key] = h
    out: list[Hit] = []
    placed: set[str] = set()
    for h in hits:
        if h.kind != "work_item":
            out.append(h)
            continue
        key = thread_key(h)
        if key not in placed:
            placed.add(key)
            out.append(newest[key])
    return out


# $1 user, $2 employer, $3 normalized titles, $4 a regex of ticket ids (or null)
THREAD_SQL = """
select id, kind, title, summary, body, valid_from, 0.0 as rank
from app.memory_nodes
where user_id = $1 and employer_id = $2 and status = 'active' and kind = 'work_item'
  and (lower(title) = any($3::text[])
       or ($4::text is not null and (title ~ $4 or summary ~ $4)))
order by valid_from desc nulls last, id
limit 30
"""


async def newest_in_threads(
    conn: Conn, user_id: UUID, employer_id: UUID, hits: list[Hit]
) -> list[Hit]:
    """The newest entry of every work item the hits touch, found by ticket id or title even
    when it shares no word with the question: "did the refund idempotency change go out?"
    matched the ticket's earlier entries but not "PSP-412: deployed to production". In the
    eval, without this, every answer gave the outdated status."""
    items = [h for h in hits if h.kind == "work_item"]
    if not items:
        return hits
    tickets = sorted({t for h in items for t in TICKET.findall(h.text)})
    titles = sorted({h.text.split(":", 1)[0].strip().lower() for h in items})
    pattern = "|".join(re.escape(t) for t in tickets) if tickets else None
    rows = await conn.fetch(THREAD_SQL, user_id, employer_id, titles, pattern)
    return latest_only(hits + [_hit(r) for r in rows])


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def _hit(row: Row, hop: bool = False) -> Hit:
    day = _day(row)
    return Hit(
        node_id=row["id"],
        kind=str(row["kind"]),
        text=item_text(row),
        day=day.isoformat()
        if day and row["kind"] in ("work_item", "decision", "commitment")
        else None,
        score=float(row.get("rank") or 0.0),
        hop=hop,
    )


async def search(
    conn: Conn,
    user_id: UUID,
    employer_id: UUID,
    text: str,
    vector: Sequence[float] | None = None,
    embedding_model: str | None = None,
    limit: int = 6,
    hops: int = 4,
) -> list[Hit]:
    """Hybrid search over one company's memory: full text alone without a vector, fused with
    the vector half when there is one. Matches first, then up to `hops` neighbours."""
    if vector is not None and embedding_model:
        rows = await conn.fetch(
            HYBRID_SQL, user_id, text, employer_id, limit, vector_literal(vector), embedding_model
        )
    else:
        rows = await conn.fetch(FTS_SQL, user_id, text, employer_id, limit)
    hits = await newest_in_threads(conn, user_id, employer_id, [_hit(r) for r in rows])
    if hits and hops:
        extra = await conn.fetch(HOPS_SQL, [h.node_id for h in hits], employer_id, hops)
        hits += [_hit(r, hop=True) for r in extra]
    return hits


def memory_lines(
    hits: Sequence[Hit], refs: dict[str, NodeRef], skip: set[UUID] | None = None
) -> list[str]:
    """The `<memory>` block's lines for search hits, minus those already shown (`skip`). Each
    gets a short id the session's `refs` now map, so an answer's citation is a memory ref."""
    lines: list[str] = []
    for h in hits:
        if skip and h.node_id in skip:
            continue
        sid = assign_short_id(h.node_id, refs)
        refs[sid] = NodeRef(node_id=h.node_id, label=h.text[:120])
        when = f", {h.day}" if h.day else ""
        kind = h.kind.replace("_", " ")
        lines.append(f"[{sid}] ({kind}{when}) {h.text}")
    return lines
