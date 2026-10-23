"""Resume versions (ARCHITECTURE.md, "Resume versions"): each version's facts stay its own.

An application's digest and interview pack come from its JD and its paired resume version
only; changing the pairing, or a version's content, rebuilds the packs that use it, and only
those."""

import json
from uuid import UUID

import pytest

from copilot_core import jobs, keys, resumes
from copilot_core.ids import uuid7
from copilot_core.packs import Digest, SessionPack
from copilot_worker.testing import FIXTURES, Stack

pytestmark = pytest.mark.stack

INPUTS = FIXTURES / "inputs"
# Facts only the tailored versions have. The base resume (the fixture) has none of them.
TAILORED_STORY = "Kubernetes autoscaler rewrite"
TAILORED_METRIC = "cut cloud spend 55%"
OTHER_STORY = "Ledger sharding at Acme"


async def digest_for(stack: Stack, uid: UUID, version: UUID) -> Digest:
    raw = await stack.ctx.valkey.get(keys.resume_digest(uid, version))
    assert raw is not None, f"no digest for version {version}"
    return Digest.model_validate_json(raw)


async def indexed_pack(stack: Stack, uid: UUID, field: str) -> SessionPack:
    pack_id = await stack.ctx.valkey.hget(keys.pack_index(uid), field)
    assert pack_id is not None, f"no {field} in the pack index"
    raw = await stack.ctx.valkey.get(keys.pack(uid, pack_id.decode()))
    assert raw is not None
    return SessionPack.model_validate_json(raw)


async def base_resume(stack: Stack, uid: UUID) -> UUID:
    doc = await stack.upload(uid, "resume", INPUTS / "resume.md", "text/markdown")
    await stack.run(
        jobs.envelope(
            "memory.extract", f"memory.extract:{uid}:{doc}", uid, {"document_id": str(doc)}
        )
    )
    await stack.drain("digest.rebuild", "pack.build")
    version = await stack.admin.fetchval(
        "select id from app.resume_versions where user_id = $1 and kind = 'base'", uid
    )
    assert version is not None
    return version


async def application(stack: Stack, uid: UUID, company: str) -> UUID:
    app_id = uuid7()
    await stack.admin.execute(
        "insert into app.applications (id, user_id, company, role, jd_text)"
        " values ($1, $2, $3, 'Backend Engineer', $4)",
        app_id, uid, company, (INPUTS / "job.md").read_text("utf-8"),
    )  # fmt: skip
    return app_id


async def tailored(stack: Stack, uid: UUID, app_id: UUID, story: str, metric: str) -> UUID:
    """A tailored version whose facts differ from the base: seeded as extracted nodes, so the
    test controls exactly what each version knows."""
    version = uuid7()
    await stack.admin.execute(
        "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
        " text_enc, key_version, content_sha256, extracted_sha256, status)"
        " values ($1, $2, 'tailored', $3, $4, 'text', '\\x00', 1, $5, $5, 'extracted')",
        version, uid, app_id, f"For {story}", "a" * 64,
    )  # fmt: skip
    source = json.dumps({"type": "resume", "ref": str(version)})
    role, story_id = uuid7(), uuid7()
    for node_id, kind, title, summary, body in (
        (role, "role", "Platform Engineer, Initech", "Runs the platform team.",
         {"company": "Initech", "title": "Platform Engineer", "start": "2022"}),
        (story_id, "story", story, metric,
         {"result": metric, "competencies": ["ownership"]}),
    ):  # fmt: skip
        await stack.admin.execute(
            "insert into app.memory_nodes (id, user_id, kind, domain, title, summary, body,"
            " source, resume_version_id) values ($1, $2, $3, 'career', $4, $5, $6, $7, $8)",
            node_id, uid, kind, title, summary, json.dumps(body), source, version,
        )  # fmt: skip
    await stack.run(jobs.digest_rebuild(uid, version, "seed"))
    return version


async def build_packs(stack: Stack, uid: UUID, app_id: UUID) -> None:
    async with stack.ctx.user(uid) as conn:
        messages = await resumes.pack_messages(
            conn, stack.ctx.valkey, uid, app_id, stack.ctx.settings.embedding_model_id
        )
    for m in messages:
        await stack.run(m)
    stack.take("pack.build")


async def nodes_of(stack: Stack, version: UUID) -> set[UUID]:
    rows = await stack.admin.fetch(
        "select id from app.memory_nodes where resume_version_id = $1", version
    )
    return {r["id"] for r in rows}


async def test_pasted_text_becomes_that_versions_facts(stack: Stack) -> None:
    uid = await stack.new_user()
    base = await base_resume(stack, uid)
    app_id = await application(stack, uid, "Globex")
    text = (INPUTS / "resume.md").read_text("utf-8")
    version = uuid7()
    async with stack.ctx.user(uid) as conn:
        sealed, key_version = await stack.ctx.crypto.encrypt(
            conn, uid, "resume_versions", str(version), text
        )
        await conn.execute(
            "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
            " text_enc, key_version, content_sha256, status)"
            " values ($1, $2, 'tailored', $3, 'Globex cut', 'text', $4, $5, $6, 'extracting')",
            version, uid, app_id, sealed, key_version, resumes.sha256_text(text),
        )  # fmt: skip
    # A job for older content is skipped; the current one runs.
    await stack.run(jobs.resume_extract(uid, version, "0" * 64))
    assert await nodes_of(stack, version) == set()
    await stack.run(jobs.resume_extract(uid, version, resumes.sha256_text(text)))
    row = await stack.admin.fetchrow(
        "select status, extracted_sha256 from app.resume_versions where id = $1", version
    )
    assert row is not None
    assert (row["status"], row["extracted_sha256"]) == ("extracted", resumes.sha256_text(text))
    assert len(await nodes_of(stack, version)) == 13
    # The base keeps its own facts: a new version archives nothing of another.
    active_base = await stack.admin.fetchval(
        "select count(*) from app.memory_nodes where resume_version_id = $1 and status = 'active'",
        base,
    )
    assert active_base == 13
    [rebuild] = stack.take("digest.rebuild")
    await stack.run(rebuild)
    digest = await digest_for(stack, uid, version)
    assert {r.node_id for r in digest.refs.values()} <= await nodes_of(stack, version)
    # The application now pairs with its tailored version, so its pack is rebuilt.
    [build] = stack.take("pack.build")
    assert build["payload"]["context"] == f"job:{app_id}"
    assert f":{version.hex[:12]}:" in build["idempotency_key"]


async def test_no_facts_leak_between_resume_versions(stack: Stack) -> None:
    uid = await stack.new_user()
    base = await base_resume(stack, uid)
    on_base = await application(stack, uid, "Globex")
    on_tailored = await application(stack, uid, "Initech")
    version = await tailored(stack, uid, on_tailored, TAILORED_STORY, TAILORED_METRIC)
    stack.take("pack.build")
    await build_packs(stack, uid, on_base)
    await build_packs(stack, uid, on_tailored)

    base_digest = await digest_for(stack, uid, base)
    tailored_digest = await digest_for(stack, uid, version)
    assert TAILORED_STORY in tailored_digest.text
    assert TAILORED_METRIC in tailored_digest.text
    assert "Zero-downtime ledger migration" not in tailored_digest.text
    assert TAILORED_STORY not in base_digest.text
    assert "55%" not in base_digest.text
    assert await stack.ctx.valkey.get(keys.digest(uid)) == await stack.ctx.valkey.get(
        keys.resume_digest(uid, base)
    )  # the account's career digest is the base's, never a tailored one

    for app_id, own, digest in ((on_base, base, base_digest),
                                (on_tailored, version, tailored_digest)):  # fmt: skip
        pack = await indexed_pack(stack, uid, f"job:{app_id}")
        assert pack.resume_version_id == own
        assert pack.digest_version == digest.version
        mine = await nodes_of(stack, own)
        assert {s.node_id for s in pack.ranked_stories} <= mine
        assert {n for a in pack.prepared_answers for n in a.node_ids} <= mine
    assert [s.title for s in (await indexed_pack(stack, uid, f"job:{on_tailored}")).ranked_stories
            ] == [TAILORED_STORY]  # fmt: skip

    # What the pack model was sent: each request holds one version's digest only.
    packs_sent = [body for job, body in stack.claude.requests if job == "pack"][-2:]
    base_prompt, tailored_prompt = (json.dumps(b) for b in packs_sent)
    assert TAILORED_STORY not in base_prompt
    assert "55%" not in base_prompt
    assert "Zero-downtime ledger migration" not in tailored_prompt


async def test_changing_the_pairing_rebuilds_the_pack_and_back_again(stack: Stack) -> None:
    uid = await stack.new_user()
    base = await base_resume(stack, uid)
    app_id = await application(stack, uid, "Globex")
    await build_packs(stack, uid, app_id)
    assert (await indexed_pack(stack, uid, f"job:{app_id}")).resume_version_id == base

    # A tailored version of this application: the automatic pairing moves to it.
    version = await tailored(stack, uid, app_id, OTHER_STORY, "30% faster settlement")
    [build] = stack.take("pack.build")
    await stack.run(build)
    stack.take("pack.build")
    pack = await indexed_pack(stack, uid, f"job:{app_id}")
    assert pack.resume_version_id == version
    assert [s.title for s in pack.ranked_stories] == [OTHER_STORY]

    # The user pairs it with the base, then back with the tailored version: each change
    # bumps the pairing revision, so going back builds again instead of being skipped.
    for chosen, expected in ((base, base), (None, version)):
        await stack.admin.execute(
            "update app.applications set resume_version_id = $2, pairing_rev = pairing_rev + 1"
            " where id = $1",
            app_id, chosen,
        )  # fmt: skip
        before = (await indexed_pack(stack, uid, f"job:{app_id}")).pack_id
        await build_packs(stack, uid, app_id)
        pack = await indexed_pack(stack, uid, f"job:{app_id}")
        assert pack.pack_id != before
        assert pack.resume_version_id == expected
        assert {s.node_id for s in pack.ranked_stories} <= await nodes_of(stack, expected)


async def test_a_versions_new_content_rebuilds_only_the_packs_that_use_it(stack: Stack) -> None:
    uid = await stack.new_user()
    await base_resume(stack, uid)
    on_base = await application(stack, uid, "Globex")
    on_tailored = await application(stack, uid, "Initech")
    version = await tailored(stack, uid, on_tailored, TAILORED_STORY, TAILORED_METRIC)
    stack.take("pack.build")
    await build_packs(stack, uid, on_base)
    await build_packs(stack, uid, on_tailored)

    # The tailored version's facts change (as a new extraction would leave them).
    await stack.admin.execute(
        "update app.memory_nodes set summary = 'cut cloud spend 61%', updated_at = now()"
        " where resume_version_id = $1 and kind = 'story'",
        version,
    )
    await stack.run(jobs.digest_rebuild(uid, version, "changed"))
    builds = stack.take("pack.build")
    assert [b["payload"]["context"] for b in builds] == [f"job:{on_tailored}"]
    for b in builds:
        await stack.run(b)
    assert "61%" in (await digest_for(stack, uid, version)).text
    pack = await indexed_pack(stack, uid, f"job:{on_tailored}")
    assert pack.digest_version == (await digest_for(stack, uid, version)).version

    # A weekly rebuild of every version sends nothing: every pack is current.
    await stack.run(jobs.envelope("digest.rebuild", f"digest.rebuild:{uid}:weekly", uid, {}))
    assert stack.take("pack.build") == []
