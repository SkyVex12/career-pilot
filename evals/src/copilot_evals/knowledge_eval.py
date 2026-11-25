"""Engineering-context eval: do standup answers handle technical questions from a company's
documents? (evals/README.md, "Engineering context".)

`knowledge/questions.toml` holds the technical turns of the standups analysed in
docs/analysis/2026-11-14-standup-technical.md, moved to a fictional company whose documents
are in `knowledge/docs/`. Each question runs the live path's own pieces, in order:

- detect: the detector prompt on the configured route, with the call's lines before it.
  Scored: fired, and the type is one of the question's `types`.
- retrieve: the company's pack and digest, memory search and section search. Scored: the
  question's `fact` is in the short answer's prompt, and in the detailed answer's.
- answer: the short and the detailed answer's prompts on their routes. Scored: every `must`
  pattern and no `must_not`.

A question the detector does not fire on gets no answer, as in a call, and counts as wrong.

The same file runs against another checkout's code (`PYTHONPATH` pointing at its packages),
which is how the "before" numbers are made from main: where `copilot_core.knowledge` does not
exist, documents go through that checkout's own extraction, with every proposal approved,
and there are no sections and no digest.

    uv run copilot-knowledge-eval run --label after --database-url postgresql://...
    uv run copilot-knowledge-eval compare before after

`run` extracts the documents once per label (`results/<label>/prepared.json`, reused on the
next run) in a scratch database that is dropped afterwards. About $0.30 to $0.60 a label.
"""

import argparse
import asyncio
import importlib
import importlib.util
import json
import re
import statistics
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from copilot_evals.answer_eval import add_cost, call_cost, summarize_costs
from copilot_evals.benchdata import BENCH_DIR, load_profile
from copilot_evals.company_eval import (
    EVAL_EMPLOYER,
    EVAL_USER,
    connect,
    scratch_database,
    seed_company,
)

KNOWLEDGE_DIR = BENCH_DIR.parent / "knowledge"
DOCS = KNOWLEDGE_DIR / "docs"
QUESTIONS = KNOWLEDGE_DIR / "questions.toml"
RESULTS = KNOWLEDGE_DIR / "results"
HAS_KNOWLEDGE = importlib.util.find_spec("copilot_core.knowledge") is not None
# Extraction token counts remain visible even when missing billing details leave cost unknown.
TOKENS: dict[str, Any] = {"model": "", "input": 0, "output": 0}
SAFE = re.compile(r"confirm|follow up|check|get back|look (it|that) up", re.I)


@dataclass
class Question:
    id: str
    turn: int
    text: str
    types: list[str]
    source: str
    context: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fact: str = ""
    must: list[str] = field(default_factory=list[str])
    must_not: list[str] = field(default_factory=list[str])


def load_questions(only: list[str] | None = None) -> tuple[dict[str, Any], list[Question]]:
    data = tomllib.loads(QUESTIONS.read_text("utf-8"))
    questions = [
        Question(
            id=str(q["id"]),
            turn=int(q.get("turn", 0)),
            text=str(q["text"]),
            types=[str(t) for t in q["types"]],
            source=str(q["source"]),
            context=[(str(s), str(t)) for s, t in q.get("context", [])],
            fact=str(q.get("fact", "")),
            must=[str(m) for m in q.get("must", [])],
            must_not=[str(m) for m in q.get("must_not", [])],
        )
        for q in data["question"]
    ]
    if only:
        questions = [q for q in questions if q.id in only]
    return data, questions


def documents() -> list[tuple[str, str]]:
    return [(p.name, p.read_text("utf-8")) for p in sorted(DOCS.glob("*.md"))]


def doc_id(name: str) -> uuid.UUID:
    return uuid.uuid5(EVAL_EMPLOYER, f"doc:{name}")


# --- prepare --------------------------------------------------------------------------------


async def seed_items(conn: Any, items: list[dict[str, Any]], today: date) -> None:
    """The user's recent work, as a work log's extraction would have left it."""
    for n, item in enumerate(items):
        body: dict[str, Any] = {}
        if item["kind"] == "work_item":
            body = {"state": item.get("state", "in_progress"),
                    "external_id": item.get("external_id", ""), "project": ""}  # fmt: skip
        await conn.execute(
            "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
            " summary, body, source, valid_from)"
            " values ($1, $2, $3, $4, 'workplace', $5, $6, $7, $8, $9)",
            uuid.uuid5(EVAL_EMPLOYER, f"item:{n}"),
            EVAL_USER,
            EVAL_EMPLOYER,
            item["kind"],
            item["title"],
            item["summary"],
            body,
            {"type": "worklog", "ref": "eval"},
            today - timedelta(days=int(item.get("day", 1))),
        )


async def extract_documents(conn: Any, user: str, company: str) -> tuple[list[Any], dict[str, Any]]:
    """Run this checkout's own document extraction over the fixture documents and write its
    facts. Returns the sections (none before engineering context) and the cost."""
    from copilot_core.providers import make_providers
    from copilot_worker.config import WorkerSettings
    from copilot_worker.llm import build_router
    from copilot_worker.workplace import known_titles, render_known

    wprompts: Any = importlib.import_module("copilot_worker.prompts")
    settings = WorkerSettings()
    router = build_router(settings, make_providers(settings, settings.llm_timeout_s))
    cost = summarize_costs([])

    async def complete(job: str, req: Any) -> dict[str, Any]:
        text, done = await router.complete(job, req)
        u = done.usage
        TOKENS["model"] = done.model
        TOKENS["input"] += u.input_tokens
        TOKENS["output"] += u.output_tokens
        add_cost(cost, call_cost(settings, done))
        return cast(dict[str, Any], json.loads(text))

    chunks: list[Any] = []
    for name, text in documents():
        if HAS_KNOWLEDGE:
            knowledge: Any = importlib.import_module("copilot_core.knowledge")
            jobs: Any = importlib.import_module("copilot_worker.knowledge")
            sections = knowledge.split(text)
            merged: dict[str, list[dict[str, Any]]] = {f: [] for f in jobs.FIELDS}
            for part in jobs.parts(sections):
                known = render_known(await known_titles(conn, EVAL_USER, EVAL_EMPLOYER))
                req = wprompts.knowledge_request(user, company, jobs.render_sections(part), known)
                data = await complete("knowledge", req)
                for f in jobs.FIELDS:
                    merged[f] += list(data.get(f) or [])
            count = await jobs.write_facts(
                conn, EVAL_USER, EVAL_EMPLOYER, doc_id(name), merged, sections
            )
            chunks += [
                {"id": str(uuid.uuid5(doc_id(name), str(s.ord))), "document_id": str(doc_id(name)),
                 "doc": name, "path": s.path, "text": s.text}
                for s in sections
            ]  # fmt: skip
            print(f"document {name}: {len(sections)} sections, {count} facts", flush=True)
            continue
        documents_mod: Any = importlib.import_module("copilot_worker.documents")
        workplace_mod: Any = importlib.import_module("copilot_worker.workplace")
        known_rows = await known_titles(conn, EVAL_USER, EVAL_EMPLOYER)
        block = documents_mod.document_block(text.encode(), "text/markdown", name)
        req = wprompts.company_doc_request(user, company, block, render_known(known_rows))
        data = await complete("company_doc", req)
        proposals: list[dict[str, Any]] = workplace_mod.document_proposals(data, known_rows)
        for p in proposals:  # approved, every one, as the user did
            await conn.execute(
                "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
                " summary, body, source, valid_from)"
                " values ($1, $2, $3, $4, 'workplace', $5, $6, $7, $8, $9)",
                uuid.uuid4(),
                EVAL_USER,
                EVAL_EMPLOYER,
                p["kind"],
                p["title"],
                p["summary"],
                {**p["body"], "evidence": p["evidence"]},
                {"type": "company_document", "ref": str(doc_id(name))},
                date.fromisoformat(p["valid_from"]) if p.get("valid_from") else None,
            )
        print(f"document {name}: {len(proposals)} facts", flush=True)
    return chunks, cost


async def prepare(conn: Any, label: str, items: list[dict[str, Any]], user: str, company: str,
                  today: date) -> tuple[list[Any], dict[str, Any]]:  # fmt: skip
    """The company's memory for this label: extracted now, or loaded from the last run."""
    path = RESULTS / label / "prepared.json"
    await seed_items(conn, items, today)
    if path.exists():
        saved = json.loads(path.read_text("utf-8"))
        for n in saved["nodes"]:
            await conn.execute(
                "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
                " summary, body, source, valid_from)"
                " values ($1, $2, $3, $4, 'workplace', $5, $6, $7, $8, $9)"
                " on conflict (id) do nothing",
                uuid.UUID(n["id"]), EVAL_USER, EVAL_EMPLOYER, n["kind"], n["title"],
                n["summary"], n["body"], n["source"],
                date.fromisoformat(n["valid_from"]) if n["valid_from"] else None,
            )  # fmt: skip
        return saved["chunks"], summarize_costs([])
    chunks, cost = await extract_documents(conn, user, company)
    rows = await conn.fetch(
        "select id, kind, title, summary, body, source, valid_from from app.memory_nodes"
        " where employer_id = $1 and source->>'type' = 'company_document' order by kind, title",
        EVAL_EMPLOYER,
    )
    nodes = [
        {"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "summary": r["summary"],
         "body": r["body"], "source": r["source"],
         "valid_from": r["valid_from"].isoformat() if r["valid_from"] else None}
        for r in rows
    ]  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nodes": nodes, "chunks": chunks}, indent=1), "utf-8")
    return chunks, cost


async def embed_nodes(conn: Any, embedder: Any) -> None:
    from copilot_core.workplace import item_text, vector_literal

    rows = await conn.fetch(
        "select id, kind, title, summary, body from app.memory_nodes where employer_id = $1",
        EVAL_EMPLOYER,
    )
    for start in range(0, len(rows), 64):
        batch = rows[start : start + 64]
        vectors = await embedder.embed([item_text(r) for r in batch])
        for r, v in zip(batch, vectors, strict=True):
            await conn.execute(
                "update app.memory_nodes set embedding = $2::vector, embedding_model = $3"
                " where id = $1",
                r["id"], vector_literal(v), embedder.model_id,
            )  # fmt: skip


# --- one question ---------------------------------------------------------------------------


def transcript(q: Question, first: str) -> str:
    return "\n".join(f"{'Them' if s == 'them' else first}: {t}" for s, t in q.context)


def turn_taking(q: Question, user: str) -> str:
    from copilot_core import prompts

    # Each line of the context took four seconds; the question starts two seconds later.
    spans = [(s, n * 4.0, n * 4.0 + 3.0) for n, (s, _) in enumerate(q.context)]
    return prompts.turn_taking(user, spans, len(q.context) * 4.0 + 1.0)


async def timed(router: Any, job: str, req: Any) -> tuple[str, Any, int]:
    """One call on a live route: the text, the result and the ms to its first text."""
    from copilot_core.llm import LLMDone

    started = time.monotonic()
    first: float | None = None
    parts: list[str] = []
    done: Any = None
    async for chunk in router.stream(job, req):
        if isinstance(chunk, LLMDone):
            done = chunk
        else:
            if first is None:
                first = time.monotonic()
            parts.append(chunk)
    ms = int(((first or time.monotonic()) - started) * 1000)
    return "".join(parts), done, ms


def section_lines(index: Any, pack: Any, q: Question, refs: Any, deep: bool, first: str,
                  settings: Any) -> list[str]:  # fmt: skip
    """The gateway's `Session.section_lines`, without the vector half (the eval's sections
    are not in the database)."""
    knowledge: Any = importlib.import_module("copilot_core.knowledge")
    want = settings.knowledge_deep_sections if deep else settings.knowledge_fast_sections
    lists = [[c for c, _ in index.search(q.text, want)]]
    digest = getattr(pack, "knowledge", None)
    names = list(dict.fromkeys([*pack.keyterms, *(digest.names if digest else [])]))
    if knowledge.needs_context(q.text, names):
        said = knowledge.context_names(transcript(q, first), names)
        if said:
            lists.append([c for c, _ in index.search(f"{q.text} {' '.join(said)}", want)])
    chunks = knowledge.merge(*lists, limit=want)
    words = None if deep else knowledge.FAST_EXCERPT_WORDS
    return cast(list[str], knowledge.memory_lines(chunks, refs, q.text, words))


def check(q: Question, answer: str) -> bool:
    must = q.must or ([SAFE.pattern] if q.source == "none" else [])
    if not answer.strip():
        return False
    ok = all(re.search(m, answer, re.I) for m in must)
    return ok and not any(re.search(m, answer, re.I) for m in q.must_not)


async def run_question(q: Question, env: dict[str, Any]) -> dict[str, Any]:
    from copilot_core import prompts, workplace
    from copilot_core.packs import pack_items, ref_map
    from copilot_gateway.detector import parse_detection

    conn, router, settings, pack = env["conn"], env["router"], env["settings"], env["pack"]
    user, first = env["user"], prompts.first_name(env["user"])
    row: dict[str, Any] = {"id": q.id, "turn": q.turn, "source": q.source, "cost": 0.0}
    lines = transcript(q, first)
    req = prompts.detector(user, lines, q.text, 80, "standup", turn_taking(q, user))
    text, done, ms = await timed(router, "detector", req)
    add_cost(row, call_cost(settings, done), "cost")
    found = parse_detection(text)
    row.update(detected=found.qtype if found.decision == "answer" else found.decision,
               fired=found.fires(), type_ok=found.fires() and found.qtype in q.types,
               detector_ms=ms)  # fmt: skip

    refs = ref_map(None, pack)
    shown = {i.node_id for i in pack_items(pack)}
    digest = getattr(pack, "knowledge", None)
    if digest is not None:
        shown |= {r.node_id for r in digest.refs.values()}
    [vec] = await env["embedder"].embed([q.text])
    model = env["embedder"].model_id
    fts = await workplace.search(conn, EVAL_USER, EVAL_EMPLOYER, q.text, limit=6)
    hybrid = await workplace.search(conn, EVAL_USER, EVAL_EMPLOYER, q.text, vec, model, limit=6)
    memory = {
        "fast": workplace.memory_lines(fts[: settings.memory_fast_hits], refs, shown),
        "deep": workplace.memory_lines(hybrid, refs, shown),
    }
    if env["index"] is not None:
        for pass_ in ("fast", "deep"):
            memory[pass_] += section_lines(
                env["index"], pack, q, refs, pass_ == "deep", first, settings
            )
    ctx = env["ctx"]
    prefix = "\n".join(b.text for b in prompts.fast_pass(ctx, "", "q", None, 10).system[1:])
    if q.fact:
        for pass_ in ("fast", "deep"):
            row[f"fact_in_{pass_}"] = bool(
                re.search(q.fact, prefix + "\n" + "\n".join(memory[pass_]), re.I)
            )
        row["fact_preloaded"] = bool(re.search(q.fact, prefix, re.I))
    if not found.fires():
        row.update(fast="", deep="", fast_ok=False, deep_ok=False)
        return row
    call = prompts.CallContext(recent=lines)
    req = prompts.fast_pass(ctx, call, q.text, None, settings.fast_pass_max_tokens, memory["fast"])
    fast, done, ms = await timed(router, "fast", req)
    add_cost(row, call_cost(settings, done), "cost")
    row.update(fast=fast.strip(), fast_first_ms=ms, fast_input_tokens=done.usage.input_tokens)
    req = prompts.deep_pass(
        ctx, call, q.text, found.qtype, settings.deep_pass_max_tokens, memory["deep"],
        shown=fast.strip(),
    )  # fmt: skip
    deep, done, ms = await timed(router, "deep", req)
    add_cost(row, call_cost(settings, done), "cost")
    row.update(deep=deep.strip(), deep_first_ms=ms)
    row.update(fast_ok=check(q, row["fast"]), deep_ok=check(q, row["deep"]))
    return row


# --- run ------------------------------------------------------------------------------------


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def count(key: str, among: list[dict[str, Any]] | None = None) -> list[int]:
        pool = rows if among is None else among
        return [sum(1 for r in pool if r.get(key)), len(pool)]

    def p50(key: str) -> int | None:
        values = [int(r[key]) for r in rows if r.get(key) is not None]
        return int(statistics.median(values)) if values else None

    with_fact = [r for r in rows if "fact_in_fast" in r]
    shown = [r for r in rows if r["fired"]]
    covered = [r for r in rows if r["source"] not in ("none", "opinion")]
    honesty = [r for r in rows if r["source"] == "none"]
    return {
        "questions": len(rows),
        "detected": count("fired"),
        "type_right": count("type_ok"),
        "fact_in_short_prompt": count("fact_in_fast", with_fact),
        "fact_in_detailed_prompt": count("fact_in_deep", with_fact),
        "fact_preloaded": count("fact_preloaded", with_fact),
        "short_answer_correct": count("fast_ok", covered),
        "detailed_answer_correct": count("deep_ok", covered),
        "detailed_correct_when_shown": count("deep_ok", [r for r in shown if r in covered]),
        "safe_answer_when_not_covered": count("deep_ok", honesty),
        "detector_ms_p50": p50("detector_ms"),
        "short_first_token_ms_p50": p50("fast_first_ms"),
        "detailed_first_token_ms_p50": p50("deep_first_ms"),
        "short_prompt_tokens_p50": p50("fast_input_tokens"),
        **summarize_costs(rows, "cost"),
    }


async def cmd_run(args: argparse.Namespace) -> int:
    from copilot_core import prompts
    from copilot_core.dotenv import load_dotenv
    from copilot_core.embed import make_embedder
    from copilot_core.packs import render_pack
    from copilot_core.workplace import render_employer_pack
    from copilot_gateway.config import Settings
    from copilot_gateway.router import build_router

    load_dotenv()
    data, questions = load_questions(args.only)
    company = {"employer": data["company"]}
    user = load_profile().user_name
    settings = Settings()
    embedder = make_embedder(settings)
    if embedder is None:
        print("an embedding model must be configured", file=sys.stderr)
        return 2
    router = build_router(settings)
    await router.warm()
    today = date.today()
    async with scratch_database(args.database_url) as url:
        conn = await connect(url)
        try:
            await seed_company(conn, company, user)
            chunks, prepared_cost = await prepare(
                conn, args.label, data["item"], user, str(data["company"]["name"]), today
            )
            await embed_nodes(conn, embedder)
            pack = await render_employer_pack(conn, EVAL_USER, EVAL_EMPLOYER, "standup", today)
            assert pack is not None
            assert pack.employer is not None
            digest = getattr(pack, "knowledge", None)
            fields: dict[str, Any] = {
                "user_name": user, "mode": "standup", "digest_text": None,
                "pack_text": render_pack(pack), "company": pack.employer.name,
                "role": pack.employer.title or None,
            }  # fmt: skip
            if HAS_KNOWLEDGE and digest is not None:
                fields["company_digest"] = digest.text
            index: Any = None
            if HAS_KNOWLEDGE and chunks:
                knowledge: Any = importlib.import_module("copilot_core.knowledge")
                index = knowledge.Index.build(
                    [knowledge.LiveChunk.model_validate(c) for c in chunks]
                )
            env = {
                "conn": conn, "router": router, "settings": settings, "pack": pack,
                "user": user, "embedder": embedder, "index": index,
                "ctx": prompts.PromptContext(**fields),
            }  # fmt: skip
            rows: list[dict[str, Any]] = []
            for q in questions:
                row = await run_question(q, env)
                rows.append(row)
                mark = "ok" if row["deep_ok"] else ("--" if row["fired"] else "no card")
                print(f"{q.id:28} {row['detected']:16} {mark}", flush=True)
        finally:
            await conn.close()
    summary = summarize(rows)
    summary.update({f"prepare_{key}": value for key, value in prepared_cost.items()})
    summary["prepare_tokens"] = dict(TOKENS)
    summary["engineering_context"] = HAS_KNOWLEDGE
    summary["digest_chars"] = len(digest.text) if digest is not None else 0
    summary["sections"] = len(chunks)
    out = RESULTS / args.label
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), "utf-8")
    (out / "answers.json").write_text(json.dumps(rows, indent=1), "utf-8")
    print(json.dumps(summary, indent=1))
    return 0


ROWS = [
    ("detected", "Detected (a card was due on every one)"),
    ("type_right", "Type right"),
    ("fact_in_short_prompt", "Fact in the short answer's prompt"),
    ("fact_in_detailed_prompt", "Fact in the detailed answer's prompt"),
    ("fact_preloaded", "of which preloaded (digest or pack, no search)"),
    ("short_answer_correct", "Short answer correct"),
    ("detailed_answer_correct", "Detailed answer correct"),
    ("detailed_correct_when_shown", "Detailed answer correct, of the cards shown"),
    ("safe_answer_when_not_covered", "Says it will confirm when nothing covers it"),
    ("detector_ms_p50", "Detector, ms (p50)"),
    ("short_first_token_ms_p50", "Short answer's first token, ms (p50)"),
    ("detailed_first_token_ms_p50", "Detailed answer's first token, ms (p50)"),
    ("short_prompt_tokens_p50", "Short answer's prompt, tokens (p50)"),
    ("cost_usd", "Cost of the run, USD"),
    ("known_cost_usd", "Known token-cost subtotal, USD"),
    ("unpriced_cost_entries", "Unpriced cost entries"),
]


def cell(value: Any) -> str:
    if isinstance(value, list):
        a, b = cast(list[int], value)
        return f"{a}/{b}"
    return "—" if value is None else str(value)


def compare(labels: list[str]) -> str:
    found = {
        label: json.loads((RESULTS / label / "summary.json").read_text("utf-8")) for label in labels
    }
    lines = ["| | " + " | ".join(labels) + " |", "| --- |" + " --- |" * len(labels)]
    for key, name in ROWS:
        lines.append(f"| {name} | " + " | ".join(cell(found[x].get(key)) for x in labels) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Engineering-context eval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="extract (once per label), then ask every question")
    run_p.add_argument("--label", required=True)
    run_p.add_argument("--database-url", required=True, help="a Postgres superuser URL")
    run_p.add_argument("--only", action="append", help="a question id (repeatable)")
    cmp_p = sub.add_parser("compare", help="the labels' summaries side by side")
    cmp_p.add_argument("labels", nargs="+")
    args = parser.parse_args(argv)
    if args.cmd == "compare":
        print(compare(args.labels))
        return 0
    return asyncio.run(cmd_run(args))


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
