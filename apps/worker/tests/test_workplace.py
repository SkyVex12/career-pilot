"""Company workspace jobs against the local stack: work log extraction (idempotent, edits
kept), commitments closed and reopened, node embeddings, company packs, search scoped to one
company, per-company purge, standup commitments from post-call, and engineering context: a
document indexed into sealed sections and facts, published for live calls, replaced by a
newer upload, and asked a test question."""

import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from copilot_core import jobs, keys, knowledge
from copilot_core.db import create_pool, user_tx
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack, render_pack
from copilot_core.storage import company_document_key, employer_prefix
from copilot_core.teststack import as_role
from copilot_core.workplace import memory_version, search
from copilot_worker.sessionlog import replace_session_commitments
from copilot_worker.testing import FIXTURES, STARTED, GatewayLog, Stack
from copilot_worker.workplace import write_worklog_nodes

pytestmark = pytest.mark.stack

INPUTS = FIXTURES / "inputs"
DAY = date(2026, 10, 19)  # a Monday: the log the Tuesday standup calls "yesterday"


def content_sha(text: str, shas: list[str]) -> str:
    h = hashlib.sha256(text.encode("utf-8"))
    for sha in sorted(shas):
        h.update(b"\n" + sha.encode("ascii"))
    return h.hexdigest()


async def new_employer(stack: Stack, uid: UUID, name: str = "Fernhill Logistics") -> UUID:
    eid = uuid7()
    await stack.admin.execute(
        "insert into app.employers (id, user_id, name, title) values ($1, $2, $3, 'Engineer')",
        eid,
        uid,
        name,
    )
    return eid


async def seal_file(
    stack: Stack, uid: UUID, eid: UUID, name: str, body: bytes, log_id: UUID | None
) -> UUID:
    crypto, doc_id = stack.ctx.crypto, uuid7()
    async with stack.ctx.user(uid) as conn:
        sealed, version = await crypto.encrypt_bytes(
            conn, uid, "company_documents", str(doc_id), body
        )
        name_enc, _ = await crypto.encrypt(conn, uid, "company_documents", str(doc_id), name)
        key = company_document_key(uid, eid, doc_id)
        await stack.ctx.store.put(key, sealed, "application/octet-stream")
        await conn.execute(
            "insert into app.company_documents (id, user_id, employer_id, work_log_id, name_enc,"
            " key_version, s3_key, sha256, content_type, size_bytes, status)"
            " values ($1, $2, $3, $4, $5, $6, $7, $8, 'text/markdown', $9, 'ingesting')",
            doc_id,
            uid,
            eid,
            log_id,
            name_enc,
            version,
            key,
            hashlib.sha256(body).hexdigest(),
            len(body),
        )
    return doc_id


async def seed_log(
    stack: Stack,
    uid: UUID,
    eid: UUID,
    text: str,
    day: date = DAY,
    attachments: tuple[tuple[str, bytes], ...] = (),
) -> tuple[UUID, str]:
    log_id = uuid7()
    async with stack.ctx.user(uid) as conn:
        enc, version = await stack.ctx.crypto.encrypt(conn, uid, "work_logs", str(log_id), text)
        await conn.execute(
            "insert into app.work_logs (id, user_id, employer_id, day, text_enc, key_version,"
            " content_sha256, status) values ($1, $2, $3, $4, $5, $6, '', 'extracting')",
            log_id,
            uid,
            eid,
            day,
            enc,
            version,
        )
    shas = []
    for name, body in attachments:
        await seal_file(stack, uid, eid, name, body, log_id)
        shas.append(hashlib.sha256(body).hexdigest())
    sha = content_sha(text, shas)
    await stack.admin.execute(
        "update app.work_logs set content_sha256 = $2 where id = $1", log_id, sha
    )
    return log_id, sha


def extract_job(uid: UUID, log_id: UUID, sha: str) -> dict[str, Any]:
    return jobs.envelope(
        "worklog.extract",
        f"worklog.extract:{uid}:{log_id}:{sha}",
        uid,
        {"work_log_id": str(log_id), "content_sha256": sha},
    )


async def nodes_of(stack: Stack, eid: UUID) -> list[asyncpg.Record]:
    return await stack.admin.fetch(
        "select id, kind, title, summary, body, source, valid_from, employer_id, domain"
        " from app.memory_nodes where employer_id = $1 and status = 'active' order by kind, title",
        eid,
    )


def kinds(rows: list[asyncpg.Record]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return out


async def test_work_log_becomes_company_memory_once(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    text = (INPUTS / "worklog.md").read_text("utf-8")
    log_id, sha = await seed_log(
        stack, uid, eid, text, attachments=(("incident.md", b"# DHL sandbox outage"),)
    )
    stack.take()
    calls = len(stack.claude.requests)
    await stack.run(extract_job(uid, log_id, sha))
    job, body = stack.claude.requests[-1]
    assert job == "worklog"
    sent = json.dumps(body["messages"])
    assert "<work_log>" in sent
    assert "Fixed the label retry bug" in sent
    assert "Attachment: incident.md" in sent  # the attachment went in as a document
    assert "Log day: 2026-10-19" in sent
    # Logs written from coding-assistant sessions kept "the user ... Claude ..." narration.
    assert "never name an assistant or tool" in json.dumps(body["system"])

    rows = await nodes_of(stack, eid)
    assert kinds(rows) == {"commitment": 1, "decision": 1, "person": 2, "system": 3, "work_item": 4}
    assert {(r["domain"], r["employer_id"]) for r in rows} == {("workplace", eid)}
    assert {json.loads(r["source"])["ref"] for r in rows} == {str(log_id)}
    assert {r["valid_from"] for r in rows} == {DAY}
    fix = next(r for r in rows if r["title"] == "Label retry bug fix")
    assert json.loads(fix["body"])["state"] == "done"
    edge = await stack.admin.fetchrow(
        "select n.title from app.memory_edges e join app.memory_nodes n on n.id = e.dst"
        " where e.src = $1 and e.rel = 'part_of'",
        fix["id"],
    )
    assert edge is not None
    assert edge["title"] == "Label service"
    status = await stack.admin.fetchrow(
        "select status, extracted_sha256 from app.work_logs where id = $1", log_id
    )
    assert status is not None
    assert (status["status"], status["extracted_sha256"]) == ("extracted", sha)
    [refresh] = stack.take("workplace.refresh")
    assert refresh["payload"] == {"employer_id": str(eid)}

    # The same job again (a redelivery) calls no model and changes nothing.
    await stack.run(extract_job(uid, log_id, sha))
    assert len(stack.claude.requests) == calls + 1
    assert [r["id"] for r in await nodes_of(stack, eid)] == [r["id"] for r in rows]
    # A job for content the log no longer has does nothing either.
    await stack.run(extract_job(uid, log_id, "0" * 64))
    assert len(stack.claude.requests) == calls + 1


async def test_reextraction_replaces_the_logs_facts_but_keeps_edits(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    text = (INPUTS / "worklog.md").read_text("utf-8")
    log_id, sha = await seed_log(stack, uid, eid, text)
    await stack.run(extract_job(uid, log_id, sha))
    before = await nodes_of(stack, eid)
    fix = next(r for r in before if r["title"] == "Label retry bug fix")
    # The user renamed and edited one item (PATCH /memory/{id}).
    await stack.admin.execute(
        "update app.memory_nodes set title = 'Label retries fixed', summary = 'My words',"
        ' body = body || \'{"edited": true, "extracted_as": "Label retry bug fix"}\''
        " where id = $1",
        fix["id"],
    )
    new_sha = content_sha(text + "\n- Paired with Sam.", [])
    await stack.admin.execute(
        "update app.work_logs set content_sha256 = $2 where id = $1", log_id, new_sha
    )
    await stack.run(extract_job(uid, log_id, new_sha))
    after = await nodes_of(stack, eid)
    assert kinds(after) == kinds(before)  # replaced, not added to
    assert not ({r["id"] for r in after} - {fix["id"]}) & {r["id"] for r in before}
    kept = next(r for r in after if r["id"] == fix["id"])
    assert (kept["title"], kept["summary"]) == ("Label retries fixed", "My words")
    assert not any(r["title"] == "Label retry bug fix" for r in after)


async def test_a_log_closes_commitments_and_reopens_them_when_it_changes(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    promise = uuid7()
    await stack.admin.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source) values ($1, $2, $3, 'commitment', 'workplace', 'Send the plan',"
        ' \'Send the plan\', \'{"state": "open", "due_date": "2026-10-23"}\','
        ' \'{"type": "session", "ref": "s"}\')',
        promise,
        uid,
        eid,
    )
    log_id, _ = await seed_log(stack, uid, eid, "Sent the plan to Priya.")
    async with stack.ctx.user(uid) as conn:
        await write_worklog_nodes(
            conn, uid, eid, log_id, DAY, {"closed_commitments": ["n:wabc"]}, {"n:wabc": promise}
        )
    body = json.loads(
        await stack.admin.fetchval("select body from app.memory_nodes where id = $1", promise)
    )
    assert (body["state"], body["closed_by"]) == ("done", str(log_id))
    async with stack.ctx.user(uid) as conn:  # the log changed and no longer says so
        await write_worklog_nodes(conn, uid, eid, log_id, DAY, {}, {})
    body = json.loads(
        await stack.admin.fetchval("select body from app.memory_nodes where id = $1", promise)
    )
    assert body["state"] == "open"
    assert "closed_by" not in body


class FakeEmbedder:
    model_id = "test:fake@1024"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector(t) for t in texts]


def vector(text: str) -> list[float]:
    """Similar texts share words, so they share dimensions."""
    v = [0.0] * 1024
    for word in text.lower().split():
        v[int(hashlib.sha256(word.strip(".,:;()").encode()).hexdigest(), 16) % 1024] += 1.0
    return v


async def test_refresh_embeds_nodes_and_packs_render_one_company(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    other = await new_employer(stack, uid, "Old Corp")
    text = (INPUTS / "worklog.md").read_text("utf-8")
    log_id, sha = await seed_log(stack, uid, eid, text)
    await stack.run(extract_job(uid, log_id, sha))
    await stack.admin.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " source) values ($1, $2, $3, 'project', 'workplace', 'Secret merger', 'Secret merger',"
        ' \'{"type": "worklog"}\')',
        uuid7(),
        uid,
        other,
    )
    stack.take()
    stack.ctx.embedder = FakeEmbedder()  # pyright: ignore[reportAttributeAccessIssue]
    try:
        [refresh] = [jobs.workplace_refresh(uid, eid, "v")]
        await stack.run(refresh)
    finally:
        stack.ctx.embedder = None
    missing = await stack.admin.fetchval(
        "select count(*) from app.memory_nodes where employer_id = $1 and embedding is null", eid
    )
    assert missing == 0
    built = stack.take("pack.build")
    assert sorted(b["payload"]["mode"] for b in built) == ["meeting", "standup"]

    async with stack.ctx.user(uid) as conn:
        version = await memory_version(conn, uid, eid)
    for mode in ("standup", "meeting"):
        await stack.run(jobs.employer_pack_build(uid, eid, mode, version, "2026-10-20"))
    index = await stack.ctx.valkey.hgetall(keys.pack_index(uid))
    fields = {k.decode(): v.decode() for k, v in index.items()}
    standup = SessionPack.model_validate_json(
        await stack.ctx.valkey.get(keys.pack(uid, fields[f"employer:{eid}:standup"]))
    )
    meeting = SessionPack.model_validate_json(
        await stack.ctx.valkey.get(keys.pack(uid, fields[f"employer:{eid}:meeting"]))
    )
    assert standup.memory_version == version
    assert standup.employer is not None
    assert standup.employer.name == "Fernhill Logistics"
    brief = standup.standup
    assert brief is not None
    assert brief.yesterday_day == "2026-10-19"
    assert any("Label retry bug fix" in i.text for i in brief.yesterday)
    [today] = brief.today
    assert today.text.startswith("Rate cache warm-up job")
    assert today.text.endswith("(RATE-88)")
    assert [i.text.split(":")[0] for i in brief.blockers] == ["Tracking webhook testing"]
    assert [i.due_date for i in brief.open_commitments] == ["2026-10-23"]
    assert all(i.short_id.startswith("n:w") for i in brief.yesterday)
    assert {a.question for a in standup.prepared_answers} >= {"What did you work on yesterday?"}
    text_ = render_pack(standup)
    assert "Yesterday (2026-10-19):" in text_
    assert "Secret merger" not in text_ + render_pack(meeting)
    assert meeting.workplace is not None
    systems = [i.text for i in meeting.workplace.sections["systems"]]
    assert any(s.startswith("Label service") for s in systems)
    assert "## Systems and stack" in render_pack(meeting)


async def test_search_finds_only_the_chosen_companys_memory(stack: Stack) -> None:
    a = await stack.new_user("A")
    b = await stack.new_user("B")
    ea = await new_employer(stack, a)
    eo = await new_employer(stack, a, "Old Corp")
    eb = await new_employer(stack, b, "B Corp")
    rows = [
        (a, ea, "system", "Label service", "Prints carrier labels; retries back off 1s 4s 16s"),
        (a, ea, "decision", "Keep SQS", "Keep SQS for the dispatch queue this quarter"),
        (a, eo, "system", "Label printer", "Old label retries with backoff"),
        (b, eb, "system", "Label API", "Carrier label retries with backoff"),
    ]
    for uid, eid, kind, title, summary in rows:
        await stack.admin.execute(
            "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
            " summary, source, embedding, embedding_model) values ($1, $2, $3, $4, 'workplace',"
            ' $5, $6, \'{"type": "worklog"}\', $7::vector, $8)',
            uuid7(),
            uid,
            eid,
            kind,
            title,
            summary,
            "[" + ",".join(str(x) for x in vector(f"{title} {summary}")) + "]",
            FakeEmbedder.model_id,
        )
    pool = await create_pool(as_role(stack.url, "app_gateway"), 1, 2)
    try:
        q = "How do label retries back off when a carrier call fails?"
        async with user_tx(pool, a) as conn:
            fts = await search(conn, a, ea, q)
            hybrid = await search(conn, a, ea, q, vector(q), FakeEmbedder.model_id)
            none = await search(conn, a, eb, q)  # B's company, asked as A
        assert [h.text.split(":")[0] for h in fts] == ["Label service"]
        assert {h.text.split(":")[0] for h in hybrid} == {"Label service", "Keep SQS"}
        assert hybrid[0].text.startswith("Label service")
        assert none == []
        async with user_tx(pool, b) as conn:
            mine = await search(conn, b, eb, q, vector(q), FakeEmbedder.model_id)
        assert [h.text.split(":")[0] for h in mine] == ["Label API"]
    finally:
        await pool.close()


async def test_purge_deletes_one_companys_files_packs_and_cached_answers(stack: Stack) -> None:
    uid = await stack.new_user()
    gone, keep = uuid.uuid4(), uuid.uuid4()
    for e in (gone, keep):
        await stack.ctx.store.put(f"{employer_prefix(uid, e)}x.sealed", b"x", "text/plain")
        await stack.ctx.valkey.set(keys.pack(uid, f"p-{e}"), "{}")
        await stack.ctx.valkey.hset(
            keys.pack_index(uid), mapping={keys.employer_pack_field(e, "meeting"): f"p-{e}"}
        )
        await stack.ctx.valkey.set(keys.qa(uid, f"q-{e}"), json.dumps({"employer": str(e)}))
    await stack.run(
        jobs.envelope("employer.purge", f"employer.purge:{gone}", uid, {"employer_id": str(gone)})
    )
    assert await stack.ctx.store.list_keys(employer_prefix(uid, gone)) == []
    assert await stack.ctx.store.list_keys(employer_prefix(uid, keep)) != []
    fields = {k.decode() for k in await stack.ctx.valkey.hkeys(keys.pack_index(uid))}
    assert fields == {f"employer:{keep}:meeting"}
    assert await stack.ctx.valkey.get(keys.pack(uid, f"p-{gone}")) is None
    assert await stack.ctx.valkey.get(keys.qa(uid, f"q-{gone}")) is None
    assert await stack.ctx.valkey.get(keys.qa(uid, f"q-{keep}")) is not None


async def test_standup_commitments_go_back_to_the_company(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    log = GatewayLog(stack, uid, mode="standup", employer_id=eid)
    await log.start()
    upto = await log.segment("What's your update?", 1000)
    await stack.run(log.flush_job(upto))
    assert (
        await stack.admin.fetchval("select employer_id from app.sessions where id = $1", log.sid)
        == eid
    )
    await log.end(seconds=300)
    await stack.run(log.ended_job())
    stack.take()
    postcall = jobs.envelope(
        "session.postcall", f"session.postcall:{log.sid}", uid, {"session_id": str(log.sid)}
    )
    await stack.run(postcall)
    # 2026-10-26: without the day, "today or tomorrow" had no due date and the model wrote
    # "the session date was not provided" into the commitment.
    job, body = stack.claude.requests[-1]
    assert job == "postcall_standup"
    assert f"Date: {STARTED:%A} {STARTED.date().isoformat()}" in json.dumps(body["messages"])
    await stack.run(postcall)  # a retry replaces them
    rows = await nodes_of(stack, eid)
    assert [(r["kind"], r["title"]) for r in rows] == [
        ("commitment", "Send the migration plan to the team")
    ]
    body = json.loads(rows[0]["body"])
    assert (body["due_date"], body["state"], body["session_id"]) == (
        "2026-10-07",
        "open",
        str(log.sid),
    )
    assert len(stack.take("workplace.refresh")) == 2


async def test_a_standup_report_sees_open_commitments_and_closes_them(stack: Stack) -> None:
    # 2026-10-27: the report stored 6 commitments, mostly steps of one task and repeats of the
    # 10-26 ones, and closed none; the next pack listed 8 open commitments.
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    earlier = uuid7()
    await stack.admin.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source, valid_from) values ($1, $2, $3, 'commitment', 'workplace',"
        " 'Finish the refund report', 'Finish the refund report', $4, $5, $6)",
        earlier,
        uid,
        eid,
        json.dumps({"state": "open", "due_date": "2026-10-06"}),
        json.dumps({"type": "session", "ref": str(uuid.uuid4())}),
        DAY,
    )
    log = GatewayLog(stack, uid, mode="standup", employer_id=eid)
    await log.start()
    upto = await log.segment("What's your update?", 1000)
    await stack.run(log.flush_job(upto))
    await log.end(seconds=300)
    await stack.run(log.ended_job())
    await stack.run(
        jobs.envelope(
            "session.postcall", f"session.postcall:{log.sid}", uid, {"session_id": str(log.sid)}
        )
    )
    _, body = stack.claude.requests[-1]
    sent = json.dumps(body["messages"])
    assert "<open_commitments>" in sent
    found = re.search(r"\[(n:w[0-9a-z]+)\] Finish the refund report \(due 2026-10-06\)", sent)
    assert found is not None
    short = found.group(1)

    async def run(new: list[dict[str, str]], closed: list[str]) -> tuple[int, int]:
        async with stack.ctx.user(uid) as conn:
            from copilot_worker.workplace import open_commitments_block

            _, closable = await open_commitments_block(conn, uid, eid, str(log.sid))
            return await replace_session_commitments(
                conn, uid, eid, log.sid, DAY, new, closed, closable
            )

    five = [{"text": f"Step {i} of the refund fix", "due_date": ""} for i in range(5)]
    assert await run(five, [short]) == (3, 1)  # at most three new ones
    rows = {r["title"]: json.loads(r["body"]) for r in await nodes_of(stack, eid)}
    assert rows["Finish the refund report"]["state"] == "done"
    assert rows["Finish the refund report"]["closed_by"] == str(log.sid)
    assert sum(1 for t in rows if t.startswith("Step")) == 3
    # A rerun that closes nothing opens it again (the short id still resolves).
    assert await run([], []) == (0, 0)
    rows = {r["title"]: json.loads(r["body"]) for r in await nodes_of(stack, eid)}
    assert rows == {"Finish the refund report": {"state": "open", "due_date": "2026-10-06"}}


def test_fixture_inputs_exist() -> None:
    assert Path(INPUTS / "worklog.md").is_file()


async def test_search_reports_a_tickets_newest_entry(stack: Stack) -> None:
    """The question matches a ticket's early entries only; the newest one, which shares no
    word with it, is what search returns."""
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    for day, summary in (
        (date(2026, 10, 7), "Refund idempotency keys implemented; PR planned"),
        (date(2026, 10, 8), "Refund idempotency; PR #881 opened, Priya reviewing"),
        (date(2026, 10, 13), "Deployed to production at 10:00"),
    ):
        await stack.admin.execute(
            "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
            " summary, body, source, valid_from) values ($1, $2, $3, 'work_item', 'workplace',"
            ' \'PSP-412\', $4, \'{"state": "done"}\', \'{"type": "worklog"}\', $5)',
            uuid7(),
            uid,
            eid,
            summary,
            day,
        )
    async with stack.ctx.user(uid) as conn:
        hits = await search(conn, uid, eid, "Did the refund idempotency change go out?")
    assert [(h.day, h.text) for h in hits if h.kind == "work_item"] == [
        ("2026-10-13", "PSP-412: Deployed to production at 10:00")
    ]


async def test_a_provider_outage_marks_the_log_failed_and_it_is_retried(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-22 on Stage 0: every route failed (no credits) and the log said "Reading" for
    # four days, so that day's work never reached a standup pack.
    from copilot_core.llm import AllRoutesFailedError
    from copilot_worker import workplace

    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    log_id, sha = await seed_log(stack, uid, eid, (INPUTS / "worklog.md").read_text("utf-8"))

    async def outage(*_: Any, **__: Any) -> Any:
        raise AllRoutesFailedError("worklog", ["openai:gpt-6.1-sol: no credits remaining"])

    with monkeypatch.context() as m:
        m.setattr(workplace, "complete_json", outage)
        with pytest.raises(AllRoutesFailedError):  # raised again, so the queue retries it
            await stack.run(extract_job(uid, log_id, sha))
    status = await stack.admin.fetchval("select status from app.work_logs where id = $1", log_id)
    assert status == "failed"
    await stack.run(extract_job(uid, log_id, sha))  # the provider is back
    status = await stack.admin.fetchval("select status from app.work_logs where id = $1", log_id)
    assert status == "extracted"
    assert await nodes_of(stack, eid)


# --- engineering context -------------------------------------------------------------------

DOC = (INPUTS / "engineering_doc.md").read_bytes()


async def index(stack: Stack, uid: UUID, doc: UUID) -> None:
    await stack.run(jobs.knowledge_index(uid, doc, hashlib.sha256(DOC).hexdigest()))


async def test_a_company_document_becomes_sealed_sections_and_facts_at_once(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    label = uuid7()  # named in a work log before the document came
    await stack.admin.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " source) values ($1, $2, $3, 'system', 'workplace', 'Label service',"
        """ 'Prints labels', '{"type": "worklog"}')""",
        label,
        uid,
        eid,
    )
    doc = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)
    stack.take()
    await index(stack, uid, doc)
    name, request = stack.claude.requests[-1]
    assert name == "knowledge"
    sent = json.dumps(request["messages"])
    assert "[section 1] Dispatch platform: architecture notes > How a parcel is dispatched" in sent
    assert "Label service" in sent.split("<known>")[1].split("</known>")[0]

    # Sections: the whole text, cut at its headings, sealed. The diagram's "#" is no heading.
    chunks = await stack.admin.fetch(
        "select id, ord, body_enc, key_version from app.knowledge_chunks where document_id = $1"
        " order by ord",
        doc,
    )
    assert len(chunks) == 6
    assert all(b"dispatch" not in bytes(c["body_enc"]).lower() for c in chunks)
    async with stack.ctx.user(uid) as conn:
        opened = [
            json.loads(await stack.ctx.crypto.decrypt(
                conn, uid, "knowledge_chunks", str(c["id"]), c["body_enc"], c["key_version"]
            ))
            for c in chunks
        ]  # fmt: skip
    assert opened[1]["doc"] == "dispatch.md"
    assert opened[1]["path"].endswith("> How a parcel is dispatched")
    assert "this line is a comment in the diagram" in opened[1]["text"]
    assert "at most 5 times" in opened[0]["text"]  # a detail no fact needs to carry

    # Facts: kept at once, with what an architecture question is about. Nothing to approve.
    rows = await nodes_of(stack, eid)
    by_title = {r["title"]: r for r in rows}
    assert kinds(rows) == {
        "constraint": 1, "decision": 1, "flow": 1, "person": 2, "risk": 1, "system": 3, "term": 1,
    }  # fmt: skip
    decision = json.loads(by_title["Keep SQS for the dispatch queue"]["body"])
    assert decision["why"] == "Ordering per route is all the team needs"
    assert decision["alternatives"] == ["Kafka: nobody on the team runs it"]
    assert decision["tradeoff"].startswith("The FIFO limit of 300 messages")
    assert decision["section"].endswith("Keep SQS for the dispatch queue (2026-09-14)")
    assert by_title["Keep SQS for the dispatch queue"]["valid_from"] == date(2026, 9, 14)
    assert len(json.loads(by_title["Parcel dispatch"]["body"])["steps"]) == 3
    assert json.loads(by_title["Dispatch service"]["body"])["talks_to"] == [
        "Dispatch queue", "Rate service", "Label service",
    ]  # fmt: skip
    # The system a work log named first is one node still: its own words, plus the stack
    # the document adds.
    assert by_title["Label service"]["id"] == label
    assert by_title["Label service"]["summary"] == "Prints labels"
    assert json.loads(by_title["Label service"]["body"])["stack"] == ["Carrier APIs"]
    assert (
        await stack.admin.fetchval(
            "select count(*) from app.memory_proposals where employer_id = $1", eid
        )
        == 0
    )
    row = await stack.admin.fetchrow(
        "select status, chunk_count, error, indexed_sha256 = sha256 as indexed"
        " from app.company_documents where id = $1",
        doc,
    )
    assert row is not None
    assert dict(row) == {"status": "ingested", "chunk_count": 6, "error": None, "indexed": True}
    assert {m["type"] for m in stack.take()} == {"workplace.refresh", "knowledge.publish"}

    calls = len(stack.claude.requests)
    await index(stack, uid, doc)  # indexed: a redelivery does nothing
    await stack.run(
        jobs.envelope("company_doc.extract", f"old:{doc}", uid, {"document_id": str(doc)})
    )  # nor does the job an API from before engineering context sends
    assert len(stack.claude.requests) == calls

    # The digest every standup prompt carries is rendered from these facts, the same bytes
    # each time, and cites them.
    async with stack.ctx.user(uid) as conn:
        from copilot_core.workplace import render_employer_pack

        packs = [await render_employer_pack(conn, uid, eid, "standup", DAY) for _ in range(2)]
    first, second = (p.knowledge for p in packs if p is not None)
    assert first is not None
    assert second is not None
    assert first.text == second.text
    assert "## System map" in first.text
    assert "## Decisions" in first.text
    assert "Why: Ordering per route is all the team needs." in first.text
    assert "Rejected: Kafka: nobody on the team runs it." in first.text
    assert "1) The intake API writes one job per parcel" in first.text
    cited = re.findall(r"\[(n:w[0-9a-f]+)\]", first.text)
    assert cited
    assert set(cited) <= set(first.refs)
    assert "Dispatch service" in first.names


async def test_sections_are_published_for_live_calls_with_vectors(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    doc = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)
    await index(stack, uid, doc)
    publish = stack.take("knowledge.publish")[-1]
    stack.ctx.embedder = FakeEmbedder()  # pyright: ignore[reportAttributeAccessIssue]
    try:
        await stack.run(publish)
    finally:
        stack.ctx.embedder = None
    raw = await stack.ctx.valkey.get(keys.knowledge(uid, eid))
    live = knowledge.LiveIndex.model_validate_json(raw)
    assert len(live.chunks) == live.total == 6
    assert {c.doc for c in live.chunks} == {"dispatch.md"}
    found = knowledge.Index.build(live.chunks).search("how many parcels does a route take", 2)
    assert found[0][0].path.endswith("> Limits")
    models = await stack.admin.fetch(
        "select embedding_model from app.knowledge_chunks where document_id = $1", doc
    )
    assert {r["embedding_model"] for r in models} == {FakeEmbedder.model_id}
    # Deleting the last document leaves no live copy behind.
    await stack.admin.execute("delete from app.company_documents where id = $1", doc)
    await stack.run(jobs.knowledge_publish(uid, eid, "empty"))
    assert await stack.ctx.valkey.get(keys.knowledge(uid, eid)) is None


async def test_a_new_upload_of_the_same_file_replaces_the_old_one(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    old = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)
    await index(stack, uid, old)
    # The user corrected one fact; it must survive the replacement.
    await stack.admin.execute(
        "update app.memory_nodes set summary = 'A route takes 150 parcels',"
        " body = body || '{\"edited\": true}' where employer_id = $1 and kind = 'constraint'",
        eid,
    )
    old_key = company_document_key(uid, eid, old)
    new = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)
    await stack.admin.execute(
        "update app.company_documents set replaces = $2 where id = $1", new, old
    )
    await index(stack, uid, new)
    docs = await stack.admin.fetch(
        "select id from app.company_documents where employer_id = $1", eid
    )
    assert [r["id"] for r in docs] == [new]
    assert await stack.ctx.store.get(old_key) is None
    owners = await stack.admin.fetch(
        "select distinct document_id from app.knowledge_chunks where employer_id = $1", eid
    )
    assert [r["document_id"] for r in owners] == [new]
    rows = await nodes_of(stack, eid)
    titles = [r["title"] for r in rows]
    assert len(titles) == len(set(titles))  # every fact once
    capacity = next(r for r in rows if r["kind"] == "constraint")
    assert capacity["summary"] == "A route takes 150 parcels"
    assert {json.loads(r["source"])["ref"] for r in rows} == {str(new)}


async def test_a_failed_extraction_keeps_the_sections_and_says_so(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copilot_core.llm import AllRoutesFailedError
    from copilot_worker import knowledge as jobs_module

    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    doc = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)

    async def outage(*_: Any, **__: Any) -> Any:
        raise AllRoutesFailedError("knowledge", ["openai: no credits remaining"])

    with monkeypatch.context() as m:
        m.setattr(jobs_module, "complete_json", outage)
        with pytest.raises(AllRoutesFailedError):  # raised again, so the queue retries it
            await index(stack, uid, doc)
    row = await stack.admin.fetchrow(
        "select status, error, chunk_count from app.company_documents where id = $1", doc
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["chunk_count"] == 6
    assert "searchable" in row["error"]
    assert "knowledge.publish" in {m["type"] for m in stack.take()}
    await index(stack, uid, doc)  # the provider is back
    assert (
        await stack.admin.fetchval("select status from app.company_documents where id = $1", doc)
        == "ingested"
    )

    pdf = await seal_file(stack, uid, eid, "scan.pdf", b"%PDF-1.7", None)
    await stack.admin.execute(
        "update app.company_documents set content_type = 'application/pdf' where id = $1", pdf
    )
    await stack.run(jobs.knowledge_index(uid, pdf, hashlib.sha256(b"%PDF-1.7").hexdigest()))
    error = await stack.admin.fetchval("select error from app.company_documents where id = $1", pdf)
    assert error == "Only Markdown and text files are read for now."


async def test_a_test_question_gets_an_answer_with_its_type_and_sources(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    doc = await seal_file(stack, uid, eid, "dispatch.md", DOC, None)
    await index(stack, uid, doc)
    ask = uuid7()
    question = "Why did we keep SQS for the dispatch queue?"
    await stack.run(jobs.knowledge_ask(uid, eid, ask, question, "standup"))
    result = json.loads(await stack.ctx.valkey.get(keys.knowledge_ask(uid, ask)))
    assert result["status"] == "done"
    assert result["type"] == "decision"
    assert result["answer"].startswith("- We kept SQS FIFO")
    assert result["searched"]["sections"] >= 1
    assert result["sources"]
    assert {s["kind"] for s in result["sources"]} <= {"fact", "section"}
    (_, typed), (_, answered) = stack.claude.requests[-2:]
    # The same prompt a standup gets: the digest as its own block, the decision shape, and
    # passages of the document named by file and section.
    system = json.dumps(answered["system"])
    assert "<company_digest>" in system
    assert "Keep SQS for the dispatch queue" in system
    asked = json.dumps(answered["messages"])
    assert "Question (decision): " + question in asked
    assert "what was rejected and why not" in asked
    assert "(doc: dispatch.md > Dispatch platform: architecture notes > Decisions" in asked
    assert "decision|how_it_works" not in json.dumps(typed["messages"])
