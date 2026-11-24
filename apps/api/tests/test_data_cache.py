"""Conditional reads, committed delta ordering and scope/deletion boundaries."""

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from copilot_api.routes import applications, ask, employers, sessions, sync
from copilot_api.testing import Api
from copilot_core import keys

pytestmark = pytest.mark.stack


async def employer(api, headers):
    response = await api.client.post("/v1/employers", headers=headers, json={"name": "Cache test"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_work_validation_skips_decryption_and_worker_change_invalidates(
    api: Api, monkeypatch
):
    uid, h = api.user()
    eid = await employer(api, h)
    path = f"/v1/employers/{eid}/logs/2026-11-20"
    saved = await api.client.put(path, headers=h, json={"text": "Private work log"})
    assert saved.status_code == 200, saved.text
    first = await api.client.get(path, headers=h)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=30, must-revalidate"
    assert first.headers["x-copilot-account"] == str(uid)
    assert first.headers["x-copilot-workspace"] == str(uid)
    decrypt = AsyncMock(wraps=api.svc.crypto.decrypt)
    monkeypatch.setattr(api.svc.crypto, "decrypt", decrypt)
    same = await api.client.get(path, headers={**h, "If-None-Match": first.headers["etag"]})
    assert same.status_code == 304
    assert same.content == b""
    decrypt.assert_not_awaited()
    async with api.admin.transaction():
        await api.admin.execute("set local role app_worker")
        await api.admin.execute("select set_config('app.user_id',$1,true)", str(uid))
        # The old worker deliberately doesn't advance updated_at here.
        await api.admin.execute(
            "update app.work_logs set status='extracted' where id=$1", UUID(saved.json()["id"])
        )
    changed = await api.client.get(path, headers={**h, "If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200
    assert changed.json()["status"] == "extracted"
    assert changed.headers["etag"] != first.headers["etag"]
    assert decrypt.await_count == 1


async def test_sync_deletions_reset_scope_and_status_does_not_decrypt(api: Api, monkeypatch):
    uid, h = api.user()
    eid = await employer(api, h)
    initial = await api.client.get("/v1/sync?domain=work", headers=h)
    assert initial.json()["reset"] is True
    token = initial.json()["cursor"]
    path = f"/v1/employers/{eid}/logs/2026-11-21"
    created = (await api.client.put(path, headers=h, json={"text": "Text"})).json()
    delta = await api.client.get("/v1/sync", headers=h, params={"domain": "work", "cursor": token})
    assert delta.json()["changes"] == [
        {
            "resource": "work_logs",
            "id": created["id"],
            "revision": delta.json()["revision"],
            "deleted": False,
        }
    ]
    decrypt = AsyncMock(side_effect=AssertionError("status must not decrypt content"))
    monkeypatch.setattr(api.svc.crypto, "decrypt", decrypt)
    progress = await api.client.get(f"/v1/employers/{eid}/status", headers=h)
    assert progress.status_code == 200
    assert progress.json()["pending"] is True
    assert (await api.client.delete(path, headers=h)).status_code == 204
    removed = (
        await api.client.get(
            "/v1/sync", headers=h, params={"domain": "work", "cursor": delta.json()["cursor"]}
        )
    ).json()
    assert any(item["id"] == created["id"] and item["deleted"] for item in removed["changes"])
    _, other = api.user()
    foreign = await api.client.get(
        "/v1/sync", headers=other, params={"domain": "work", "cursor": token}
    )
    assert foreign.json()["reset"] is True
    assert foreign.json()["changes"] == []
    assert foreign.headers["x-copilot-account"] != str(uid)


async def test_revision_counter_cannot_skip_a_later_commit(api: Api):
    uid, h = api.user()
    first, second = await employer(api, h), await employer(api, h)
    initial = (await api.client.get("/v1/sync?domain=work", headers=h)).json()
    async with api.svc.pool.acquire() as a, api.svc.pool.acquire() as b:
        ta, tb = a.transaction(), b.transaction()
        await ta.start()
        await tb.start()
        await a.execute("select set_config('app.user_id',$1,true)", str(uid))
        await b.execute("select set_config('app.user_id',$1,true)", str(uid))
        await a.execute("update app.employers set title='First' where id=$1", UUID(first))
        later = asyncio.create_task(
            b.execute("update app.employers set title='Second' where id=$1", UUID(second))
        )
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(later), 0.05)
            during = (
                await api.client.get(
                    "/v1/sync", headers=h, params={"domain": "work", "cursor": initial["cursor"]}
                )
            ).json()
            assert during["changes"] == []
            await ta.commit()
            await later
            middle = (
                await api.client.get(
                    "/v1/sync", headers=h, params={"domain": "work", "cursor": during["cursor"]}
                )
            ).json()
            assert [r["id"] for r in middle["changes"]] == [first]
            await tb.commit()
            final = (
                await api.client.get(
                    "/v1/sync", headers=h, params={"domain": "work", "cursor": middle["cursor"]}
                )
            ).json()
            assert [r["id"] for r in final["changes"]] == [second]
        finally:
            if not later.done():
                later.cancel()


async def seed_session(api, uid, *, ended=True):
    sid = uuid4()
    at = datetime(2026, 11, 20, tzinfo=UTC)
    await api.admin.execute(
        "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at)"
        " values($1,$2,'interview',true,$3,$4)",
        sid,
        uid,
        at,
        at if ended else None,
    )
    return sid


async def test_history_ties_and_live_privacy_do_not_truncate_pagination(api: Api):
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    ids = [await seed_session(api, uid) for _ in range(5)]
    hidden = max(ids)
    await api.svc.valkey.hset(
        keys.sess_state(hidden),
        mapping={
            "user_id": str(uid),
            "private_context_account_id": str(uuid4()),
            "status": "ended",
        },
    )
    seen = []
    params = {"limit": 2}
    while True:
        response = await api.client.get("/v1/sessions", headers=h, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        seen += [UUID(row["id"]) for row in page["sessions"]]
        if not page["next_cursor"]:
            break
        params = {"limit": 2, "cursor": page["next_cursor"]}
    assert seen == sorted(set(ids) - {hidden}, reverse=True)


async def test_completed_session_etag_deletion_and_private_scope(api: Api):
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    sid = await seed_session(api, uid)
    url = f"/v1/sessions/{sid}/transcript"
    initial = await api.client.get(url, headers=h)
    cached = {**h, "If-None-Match": initial.headers["etag"]}
    assert (await api.client.get(url, headers=cached)).status_code == 304
    # Private marking changes the validator and denies a formerly authorized reader.
    other_uid, other = api.user()
    await api.client.get("/v1/me", headers=other)
    await api.admin.execute(
        "update app.sessions set private_context_account_id=$2 where id=$1", sid, other_uid
    )
    assert (await api.client.get(url, headers=cached)).status_code == 404
    await api.admin.execute("delete from app.sessions where id=$1", sid)
    assert (await api.client.get(url, headers=cached)).status_code == 404


async def test_catalog_validates_configuration_and_account(api: Api):
    _, h = api.user()
    first = await api.client.get("/v1/call-languages", headers=h)
    assert "max-age=86400" in first.headers["cache-control"]
    headers = {**h, "If-None-Match": first.headers["etag"]}
    assert (await api.client.get("/v1/call-languages", headers=headers)).status_code == 304
    old = api.svc.settings.deepgram_model
    try:
        api.svc.settings.deepgram_model = "nova-2"
        changed = await api.client.get("/v1/call-languages", headers=headers)
        assert changed.status_code == 200
        assert changed.headers["etag"] != first.headers["etag"]
    finally:
        api.svc.settings.deepgram_model = old
    _, other = api.user()
    assert (
        await api.client.get(
            "/v1/call-languages", headers={**other, "If-None-Match": first.headers["etag"]}
        )
    ).status_code == 200


async def test_pending_message_reads_only_one_answer_and_checks_account(api: Api, monkeypatch):
    _, h = api.user()
    monkeypatch.setattr(ask, "answer", AsyncMock(return_value="Answer"))
    conversation = await api.client.post(
        "/v1/ask/conversations", headers=h, json={"scope": {"kind": "general"}}
    )
    url = f"/v1/ask/conversations/{conversation.json()['id']}"
    ids = []
    for question in ("First", "Second"):
        msg = await api.client.post(
            url + "/messages", headers=h, json={"request_id": str(uuid4()), "question": question}
        )
        ids.append(msg.json()["id"])
    decrypt = AsyncMock(wraps=api.svc.crypto.decrypt)
    monkeypatch.setattr(api.svc.crypto, "decrypt", decrypt)
    response = await api.client.get(url + f"/messages/{ids[1]}", headers=h)
    assert response.status_code == 200
    assert response.json()["question"] == "Second"
    assert all(call.args[3] == ids[1] for call in decrypt.await_args_list)
    _, other = api.user()
    assert (await api.client.get(url + f"/messages/{ids[1]}", headers=other)).status_code == 404


async def test_sync_pages_catch_compacted_updates_and_expired_cursors(api: Api):
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    initial = (await api.client.get("/v1/sync?domain=work", headers=h)).json()
    ids = [await employer(api, h) for _ in range(3)]
    page = (
        await api.client.get(
            "/v1/sync",
            headers=h,
            params={"domain": "work", "cursor": initial["cursor"], "limit": 1},
        )
    ).json()
    assert page["has_more"] is True
    assert [r["id"] for r in page["changes"]] == ids[:1]
    # A record in the unseen part of the page is replaced above the page watermark.
    await api.client.patch(f"/v1/employers/{ids[1]}", headers=h, json={"title": "Changed"})
    tail = (
        await api.client.get(
            "/v1/sync", headers=h, params={"domain": "work", "cursor": page["cursor"]}
        )
    ).json()
    assert [r["id"] for r in tail["changes"]] == ids[2:]
    next_round = (
        await api.client.get(
            "/v1/sync", headers=h, params={"domain": "work", "cursor": tail["cursor"]}
        )
    ).json()
    assert [r["id"] for r in next_round["changes"]] == ids[1:2]
    expired = sync.encode_cursor(
        [str(uid), str(uid), "work"], 0, None, int(time.time()) - sync.MAX_AGE_S
    )
    assert (
        await api.client.get("/v1/sync", headers=h, params={"domain": "work", "cursor": expired})
    ).json()["reset"] is True


async def test_company_cascade_and_private_history_reset_do_not_leak_ids(api: Api):
    uid, h = api.user()
    eid = await employer(api, h)
    log = (
        await api.client.put(
            f"/v1/employers/{eid}/logs/2026-11-20", headers=h, json={"text": "Private"}
        )
    ).json()
    initial = (await api.client.get("/v1/sync?domain=work", headers=h)).json()
    await api.admin.execute("delete from app.employers where id=$1", UUID(eid))
    delta = (
        await api.client.get(
            "/v1/sync", headers=h, params={"domain": "work", "cursor": initial["cursor"]}
        )
    ).json()
    assert {(r["resource"], r["id"]) for r in delta["changes"] if r["deleted"]} == {
        ("employers", eid),
        ("work_logs", log["id"]),
    }
    sid = await seed_session(api, uid)
    history = (await api.client.get("/v1/sync?domain=history", headers=h)).json()
    other_uid, other = api.user()
    await api.client.get("/v1/me", headers=other)
    await api.admin.execute(
        "update app.sessions set private_context_account_id=$2 where id=$1", sid, other_uid
    )
    changed = (
        await api.client.get(
            "/v1/sync", headers=h, params={"domain": "history", "cursor": history["cursor"]}
        )
    ).json()
    assert changed["reset"] is True
    assert changed["changes"] == []
    # Cold list revalidation cannot hide a new, not-yet-flushed live privacy marker.
    assert "etag" not in (await api.client.get("/v1/sessions", headers=h)).headers


async def test_same_workspace_different_accounts_get_distinct_validators(api: Api):
    uid, h = api.user()
    await employer(api, h)
    other_uid, other = api.user()
    await api.client.get("/v1/me", headers=other)
    await api.admin.execute(
        "insert into app.profiles(id,user_id,career_os_user_id,name) values($1,$2,$3,'Shared')",
        uid,
        other_uid,
        f"shared-{uid}",
    )
    other = {**other, "X-Copilot-Profile": str(uid)}
    first = await api.client.get("/v1/employers", headers=h)
    shared = await api.client.get(
        "/v1/employers", headers={**other, "If-None-Match": first.headers["etag"]}
    )
    assert shared.status_code == 200
    assert shared.json() == first.json()
    assert shared.headers["etag"] != first.headers["etag"]
    assert shared.headers["x-copilot-workspace"] == str(uid)
    assert shared.headers["x-copilot-account"] == str(other_uid)
    discovery = await api.client.get("/v1/profiles", headers=other)
    assert discovery.headers["x-copilot-account"] == shared.headers["x-copilot-account"]
    assert discovery.headers["x-copilot-workspace"] == shared.headers["x-copilot-workspace"]
    stale = await api.client.get(
        "/v1/profiles", headers={**other, "X-Copilot-Profile": str(uuid4())}
    )
    assert stale.status_code == 200
    assert "x-copilot-workspace" not in stale.headers


async def test_catalog_status_batches_metadata_without_pack_bodies(api: Api, monkeypatch):
    uid, h = api.user()
    created = await api.client.post(
        "/v1/applications",
        headers=h,
        json={"company": "Example", "role": "Engineer", "jd_text": "Build software"},
    )
    aid = created.json()["id"]
    assert created.json()["pack"]["operation_state"] == "queued"
    # Invalid content is enough: status only checks availability and cannot parse it.
    await api.svc.valkey.set(keys.pack(uid, "test-pack"), "not JSON")
    await api.svc.valkey.hset(keys.pack_index(uid), mapping={f"job:{aid}": "test-pack"})
    await api.client.post(
        "/v1/applications",
        headers=h,
        json={"company": "Second", "role": "Engineer", "jd_text": "Another role"},
    )
    original_get, original_mget = api.svc.valkey.get, api.svc.valkey.mget

    async def read_key(*args, **kwargs):
        return await original_get(*args, **kwargs)

    async def read_many(*args, **kwargs):
        return await original_mget(*args, **kwargs)

    get = AsyncMock(side_effect=read_key)
    mget = AsyncMock(side_effect=read_many)
    monkeypatch.setattr(api.svc.valkey, "get", get)
    monkeypatch.setattr(api.svc.valkey, "mget", mget)
    status = await api.client.get("/v1/applications/status", headers=h)
    assert status.status_code == 200
    assert status.json()["applications"][0]["pack"]["available"] is True
    assert status.json()["applications"][1]["pack"]["availability"] == "missing"
    # Constant metadata round trips and no individual pack/operation GETs while polling.
    get.assert_not_awaited()
    assert mget.await_count == 2
    assert len(mget.await_args_list[0].args[0]) == 2
    assert "jd_text" not in status.text
    missing = await applications.pack_info(api.svc, uid, None, "job:never-queued", summary=False)
    assert missing is not None
    assert missing["operation_state"] == "idle"
    assert missing["availability"] == "missing"


async def test_edits_reject_outdated_revisions_and_preserve_saved_text(api: Api):
    uid, h = api.user()
    eid = await employer(api, h)
    path = f"/v1/employers/{eid}/logs/2026-11-22"
    first = (await api.client.put(path, headers=h, json={"text": "Original"})).json()
    second = await api.client.put(
        path, headers=h, json={"text": "Another device", "expected_revision": first["revision"]}
    )
    assert second.status_code == 200
    assert second.json()["revision"] > first["revision"]
    stale = await api.client.put(
        path, headers=h, json={"text": "Stale draft", "expected_revision": first["revision"]}
    )
    assert stale.status_code == 409
    assert (await api.client.get(path, headers=h)).json()["text"] == "Another device"
    listed = (await api.client.get(f"/v1/employers/{eid}/logs", headers=h)).json()["logs"]
    assert listed[0]["revision"] == second.json()["revision"]
    sid = await seed_session(api, uid)
    detail = (await api.client.get(f"/v1/sessions/{sid}", headers=h)).json()
    path = f"/v1/sessions/{sid}/notes"
    saved = await api.client.put(
        path, headers=h, json={"text": "Saved notes", "expected_revision": detail["revision"]}
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] > detail["revision"]
    assert (
        await api.client.put(
            path, headers=h, json={"text": "Stale notes", "expected_revision": detail["revision"]}
        )
    ).status_code == 409


async def test_concurrent_work_log_creation_preserves_first_draft(api: Api, monkeypatch):
    _, h = api.user()
    eid = await employer(api, h)
    path = f"/v1/employers/{eid}/logs/2026-11-23"
    entered, release = asyncio.Event(), asyncio.Event()
    encrypt = api.svc.crypto.encrypt

    async def hold_first_encrypt(*args, **kwargs):
        entered.set()
        await release.wait()
        return await encrypt(*args, **kwargs)

    monkeypatch.setattr(api.svc.crypto, "encrypt", hold_first_encrypt)
    first = asyncio.create_task(
        api.client.put(path, headers=h, json={"text": "First draft", "expected_revision": -1})
    )
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        api.client.put(path, headers=h, json={"text": "Other device", "expected_revision": -1})
    )
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.05)
    finally:
        release.set()
    saved, rejected = await asyncio.gather(first, second)
    assert saved.status_code == 200, saved.text
    assert rejected.status_code == 409, rejected.text
    assert (await api.client.get(path, headers=h)).json()["text"] == "First draft"


async def test_history_resource_validator_ignores_unrelated_calls_but_tracks_outputs(api: Api):
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    sid = await seed_session(api, uid)
    path = f"/v1/sessions/{sid}"
    initial = await api.client.get(path, headers=h)
    cached = {**h, "If-None-Match": initial.headers["etag"]}
    await seed_session(api, uid)
    assert (await api.client.get(path, headers=cached)).status_code == 304
    # A finished session's worker result changes the parent's content revision.
    async with api.svc.tx(
        api.verifier.principals[h["Authorization"].removeprefix("Bearer ")]
    ) as conn:
        oid = uuid4()
        body, key = await api.svc.crypto.encrypt(conn, uid, "session_outputs", str(oid), "Summary")
        await conn.execute(
            "insert into app.session_outputs(id,user_id,session_id,kind,content_enc,key_version)"
            " values($1,$2,$3,'summary',$4,$5)",
            oid,
            uid,
            sid,
            body,
            key,
        )
    changed = await api.client.get(path, headers=cached)
    assert changed.status_code == 200
    assert changed.json()["outputs"]["summary"] == "Summary"
    # Live transcript segments do not churn the change index on every partial flush.
    live = await seed_session(api, uid, ended=False)
    before = await api.admin.fetchval(
        "select revision from app.data_revisions where user_id=$1 and domain='history'", uid
    )
    await api.admin.execute(
        "insert into app.transcript_segments"
        "(session_id,channel,seg_id,user_id,start_ms,end_ms,text_enc,key_version)"
        " values($1,'them',1,$2,0,1000,$3,1)",
        live,
        uid,
        b"encrypted",
    )
    after = await api.admin.fetchval(
        "select revision from app.data_revisions where user_id=$1 and domain='history'", uid
    )
    assert after == before
    await api.admin.execute("update app.sessions set ended_at=now() where id=$1", live)
    published = await api.admin.fetchval(
        "select revision from app.data_revisions where user_id=$1 and domain='history'", uid
    )
    assert published > after


async def test_history_validation_retries_a_racing_row_update(api: Api, monkeypatch):
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    sid = await seed_session(api, uid)
    path = f"/v1/sessions/{sid}"
    first = await api.client.get(path, headers=h)
    original = sessions.session_privacy
    calls = 0

    async def update_between_snapshot_and_lock(*args, **kwargs):
        nonlocal calls
        await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            await api.admin.execute("update app.sessions set title='Changed' where id=$1", sid)

    monkeypatch.setattr(sessions, "session_privacy", update_between_snapshot_and_lock)
    changed = await api.client.get(path, headers={**h, "If-None-Match": first.headers["etag"]})
    assert calls == 2
    assert changed.status_code == 200
    assert changed.json()["title"] == "Changed"
    assert changed.headers["etag"] != first.headers["etag"]
    same = await api.client.get(path, headers={**h, "If-None-Match": changed.headers["etag"]})
    assert same.status_code == 304


async def test_conditional_read_layer_does_not_retry_mutations(api: Api, monkeypatch):
    _, h = api.user()
    eid = await employer(api, h)
    receipt = AsyncMock(side_effect=asyncpg.SerializationError("a racing write"))
    monkeypatch.setattr(employers, "_log_entry", receipt)
    with pytest.raises(asyncpg.SerializationError):
        await api.client.put(
            f"/v1/employers/{eid}/logs/2026-11-23",
            headers=h,
            json={"text": "New draft", "expected_revision": -1},
        )
    assert receipt.await_count == 1
    assert not await api.admin.fetchval(
        "select exists(select 1 from app.work_logs where employer_id=$1)", UUID(eid)
    )


async def test_tombstone_pruning_covers_every_workspace_domain(api: Api):
    uid, h = api.user()
    eid = await employer(api, h)
    await api.admin.execute("delete from app.employers where id=$1", UUID(eid))
    await api.admin.execute(
        "update app.data_changes set changed_at=now()-interval '32 days'"
        " where user_id=$1 and deleted",
        uid,
    )
    await api.admin.execute(
        "update app.data_revisions set revision=255 where user_id=$1 and domain='*'", uid
    )
    # The periodic boundary lands in history; old work tombstones must also be pruned.
    await seed_session(api, uid)
    assert not await api.admin.fetchval(
        "select exists(select 1 from app.data_changes where user_id=$1 and deleted)", uid
    )


async def test_opposite_domain_writes_acquire_one_workspace_clock(api: Api):
    uid, h = api.user()
    eid = await employer(api, h)
    sid = await seed_session(api, uid)
    async with api.svc.pool.acquire() as a, api.svc.pool.acquire() as b:
        ta, tb = a.transaction(), b.transaction()
        await ta.start()
        await tb.start()
        await a.execute("select set_config('app.user_id',$1,true)", str(uid))
        await b.execute("select set_config('app.user_id',$1,true)", str(uid))
        await a.execute("update app.employers set title='work-first' where id=$1", UUID(eid))
        # Opposite domain first. Under separate per-domain clocks this would hold
        # history while A held work, then deadlock when each touched the other.
        later = asyncio.create_task(
            b.execute("update app.sessions set title='history-first' where id=$1", sid)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(later), 0.05)
        await a.execute("select app.record_data_change($1,'history','references','*',false)", uid)
        await ta.commit()
        await later
        await b.execute("select app.record_data_change($1,'work','memory_nodes','*',false)", uid)
        await tb.commit()
