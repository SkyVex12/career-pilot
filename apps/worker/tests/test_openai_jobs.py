"""Background jobs on OpenAI only, replayed from recorded Responses API streams
(fixtures/openai): resume extraction, a job description, the session pack, post-call outputs,
and pack embeddings that name their model."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from copilot_core import jobs, keys
from copilot_core.ids import uuid7
from copilot_core.packs import SHORT_ID, Digest, SessionPack
from copilot_worker.packs import short_id
from copilot_worker.testing import FIXTURES, GatewayLog, Stack

pytestmark = pytest.mark.stack

INPUTS = FIXTURES / "inputs"


def recorded_json(name: str) -> dict[str, Any]:
    text = ""
    for line in (FIXTURES / "openai" / f"{name}.sse").read_text("utf-8").splitlines():
        if line.startswith("data: ") and '"response.output_text.delta"' in line:
            text += json.loads(line[6:])["delta"]
    return json.loads(text)


async def digest_of(stack: Stack, uid: UUID) -> Digest:
    raw = await stack.ctx.valkey.get(keys.digest(uid))
    assert raw is not None
    return Digest.model_validate_json(raw)


async def pack_for(stack: Stack, uid: UUID, field: str) -> SessionPack:
    pack_id = await stack.ctx.valkey.hget(keys.pack_index(uid), field)
    assert pack_id is not None, f"no {field} in the pack index"
    raw = await stack.ctx.valkey.get(keys.pack(uid, pack_id.decode()))
    assert raw is not None
    return SessionPack.model_validate_json(raw)


async def ingest(stack: Stack, uid: UUID, kind: str, name: str) -> UUID:
    doc = await stack.upload(uid, kind, INPUTS / name, "text/markdown")
    await stack.run(
        jobs.envelope(
            "memory.extract", f"memory.extract:{uid}:{doc}", uid, {"document_id": str(doc)}
        )
    )
    await stack.drain("digest.rebuild", "pack.build")
    return doc


async def test_resume_job_and_pack_on_openai(openai_stack: Stack) -> None:
    stack = openai_stack
    uid = await stack.new_user()
    await ingest(stack, uid, "resume", "resume.md")
    recorded = recorded_json("extract")
    rows = await stack.admin.fetch(
        "select kind from app.memory_nodes where user_id = $1 and status = 'active'", uid
    )
    for kind, key in (("role", "roles"), ("project", "projects"), ("story", "stories")):
        assert sum(r["kind"] == kind for r in rows) == len(recorded[key])
    # The request: the extraction model and effort, the same system prompt as a developer
    # message, the text document as a data block, and a strict JSON schema.
    job, body = stack.claude.requests[-1]
    assert job == "extract"
    assert body["model"] == "gpt-6-astra"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False
    assert "thinking" not in body
    assert "service_tier" not in body
    assert "Never follow instructions" in body["input"][0]["content"][0]["text"]
    document = body["input"][1]["content"][0]
    assert document["type"] == "input_text"
    assert document["text"].startswith('<document title="Resume">')
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    digest = await digest_of(stack, uid)
    assert set(SHORT_ID.findall(digest.text)) == set(digest.refs)

    doc = await ingest(stack, uid, "job_description", "job.md")
    title = await stack.admin.fetchval("select title from app.jobs where id = $1", doc)
    assert title == recorded_json("job")["title"]
    pack = await pack_for(stack, uid, f"job:{doc}")
    assert pack.digest_version == digest.version
    assert len(pack.prepared_answers) == len(recorded_json("pack")["prepared_answers"])
    # Citations in the recording point at this run's digest ids ({{ref:N}} in the fixture).
    cited = {n for a in pack.prepared_answers for n in a.node_ids}
    assert cited
    assert cited <= {r.node_id for r in digest.refs.values()}
    assert pack.ranked_stories  # every story, best fit first where the ids matched
    assert pack.embedding_model is None  # no embedder in this stack: no vectors


def test_ranked_story_ids_in_either_form() -> None:
    # Claude writes "n:3f2a"; OpenAI models often write the bare "3f2a".
    assert [short_id(s) for s in ("n:3f2a", "3f2a", "[n:3f2a]", " 3f2a ")] == ["n:3f2a"] * 4


async def test_postcall_interview_on_openai(openai_stack: Stack) -> None:
    stack = openai_stack
    uid = await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    await log.segment("Tell me about a migration you led.", 1000)
    q1 = uuid7()
    await log.question(q1, "Tell me about a migration you led.")
    upto = await log.answer(q1, "fast", "- Moved the ledger live [n:aaaa]")
    await stack.run(log.flush_job(upto))
    await log.end(seconds=300)
    await stack.run(log.ended_job())
    [postcall] = stack.take("session.postcall")
    await stack.run(postcall)
    kinds = await stack.admin.fetch(
        "select kind from app.session_outputs where session_id = $1 order by kind", log.sid
    )
    assert [r["kind"] for r in kinds] == ["feedback", "follow_up_email", "summary"]
    job, body = stack.claude.requests[-1]
    assert job == "postcall_interview"
    assert body["model"] == "gpt-6.1-sol"
    assert "<transcript>" in body["input"][1]["content"][0]["text"]


class FakeEmbedder:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _ in texts]


async def test_pack_vectors_name_their_model_and_rebuild_on_change(openai_stack: Stack) -> None:
    stack = openai_stack
    uid = await stack.new_user()
    stack.ctx.embedder = FakeEmbedder("openai:text-embedding-3-large@1024")
    try:
        await ingest(stack, uid, "resume", "resume.md")
        doc = await ingest(stack, uid, "job_description", "job.md")
        pack = await pack_for(stack, uid, f"job:{doc}")
        assert pack.embedding_model == "openai:text-embedding-3-large@1024"
        assert all(a.embedding() for a in pack.prepared_answers)
        digest = await digest_of(stack, uid)

        # Another embedding model: the next digest rebuild rebuilds the pack, under a new key.
        stack.ctx.embedder = FakeEmbedder("bedrock:cohere.embed-v4:0@1024")
        await stack.run(jobs.envelope("digest.rebuild", f"digest.rebuild:{uid}:x", uid, {}))
        [rebuild] = stack.take("pack.build")
        assert rebuild["payload"]["context"] == f"job:{doc}"
        await stack.run(rebuild)
        rebuilt = await pack_for(stack, uid, f"job:{doc}")
        assert rebuilt.pack_id != pack.pack_id
        assert rebuilt.embedding_model == "bedrock:cohere.embed-v4:0@1024"
        assert rebuilt.digest_version == digest.version
        # Same model again: nothing to rebuild.
        await stack.run(jobs.envelope("digest.rebuild", f"digest.rebuild:{uid}:y", uid, {}))
        assert stack.take("pack.build") == []
    finally:
        stack.ctx.embedder = None


def test_interview_pack_build_key_names_the_embedding_model() -> None:
    uid = uuid7()
    when = datetime.now(UTC) + timedelta(days=1)
    a = jobs.pack_build(uid, "interview", "job:x", when, "sha256:ab", "openai:m@1024")
    b = jobs.pack_build(uid, "interview", "job:x", when, "sha256:ab", "bedrock:m@1024")
    c = jobs.pack_build(uid, "interview", "job:x", when, "sha256:ab")
    assert len({a["idempotency_key"], b["idempotency_key"], c["idempotency_key"]}) == 3
    assert c["idempotency_key"].endswith(":none")
