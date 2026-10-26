"""Company workspace jobs against the local stack: work log extraction (idempotent, edits
kept), commitments closed and reopened, document proposals, node embeddings, company packs,
search scoped to one company, per-company purge, and standup commitments from post-call."""

import hashlib
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from copilot_core import jobs, keys
from copilot_core.db import create_pool, user_tx
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack, render_pack
from copilot_core.storage import company_document_key, employer_prefix
from copilot_core.teststack import as_role
from copilot_core.workplace import memory_version, search
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


async def test_company_document_becomes_proposals_for_what_is_new(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = await new_employer(stack, uid)
    other = await new_employer(stack, uid, "Old Corp")
    for e, title in ((eid, "Label service"), (other, "Dispatch service")):
        await stack.admin.execute(
            "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title,"
            " summary, source) values ($1, $2, $3, 'system', 'workplace', $4, $4,"
            ' \'{"type": "worklog"}\')',
            uuid7(),
            uid,
            e,
            title,
        )
    body = (INPUTS / "company_doc.md").read_bytes()
    doc = await seal_file(stack, uid, eid, "dispatch.md", body, None)
    job = jobs.envelope(
        "company_doc.extract", f"company_doc.extract:{uid}:{doc}", uid, {"document_id": str(doc)}
    )
    await stack.run(job)
    name, request = stack.claude.requests[-1]
    assert name == "company_doc"
    sent = json.dumps(request["messages"])
    assert "Label service" in sent  # in <known>
    assert "Dispatch service" not in sent.split("<known>")[1].split("</known>")[0]
    rows = await stack.admin.fetch(
        "select payload, status, employer_id, company_document_id from app.memory_proposals"
        " where company_document_id = $1",
        doc,
    )
    titles = sorted(json.loads(r["payload"])["title"] for r in rows)
    # The label service is known here; the other company's dispatch service is not.
    assert titles == [
        "Dispatch service",
        "Keep SQS for the dispatch queue",
        "Linehaul",
        "Priya Raman",
        "Sam Okafor",
    ]
    assert {(r["status"], r["employer_id"]) for r in rows} == {("pending", eid)}
    assert all(json.loads(r["payload"])["evidence"] for r in rows)
    calls = len(stack.claude.requests)
    await stack.run(job)  # ingested: a redelivery does nothing
    assert len(stack.claude.requests) == calls
    count = await stack.admin.fetchval(
        "select count(*) from app.memory_proposals where company_document_id = $1", doc
    )
    assert count == 5


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
