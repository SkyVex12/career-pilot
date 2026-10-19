"""The REST API against the local stack: auth and first login, settings, uploads, jobs and
interviews, sessions with decrypted outputs, the user's audio, usage and account deletion."""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx2
import pytest
from pydantic import SecretStr

from copilot_api.testing import Api
from copilot_core import jobs, keys
from copilot_core.auth import Principal
from copilot_core.crypto import segment_row_id
from copilot_core.logs import ContextFilter
from copilot_core.storage import me_audio_key

pytestmark = pytest.mark.stack


async def test_auth_is_required_and_first_login_creates_the_user(api: Api) -> None:
    assert (await api.client.get("/v1/me")).status_code == 401
    bad = await api.client.get("/v1/me", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    uid, h = api.user("Alex Doe")
    r = await api.client.get("/v1/me", headers=h)
    assert r.status_code == 200
    me = r.json()
    assert me["id"] == str(uid)
    assert me["display_name"] == "Alex Doe"
    assert me["email"] == f"{uid.hex}@example.com"
    assert me["plan"] == "free"
    assert await api.admin.fetchval("select count(*) from app.users where id = $1", uid) == 1
    await api.client.get("/v1/me", headers=h)  # the second request creates nothing
    assert await api.admin.fetchval("select count(*) from app.users where id = $1", uid) == 1


async def test_first_login_without_email_is_refused(api: Api) -> None:
    uid = uuid.uuid4()
    api.verifier.principals["no-email"] = Principal(uid, "x", None, email=None)
    r = await api.client.get("/v1/me", headers={"Authorization": "Bearer no-email"})
    assert r.status_code == 403


async def test_the_api_connects_as_app_api_under_rls(api: Api) -> None:
    async with api.svc.pool.acquire() as conn:
        assert await conn.fetchval("select current_user") == "app_api"
        # No user set (missing, or empty on a reused pooled connection): fails closed.
        with pytest.raises(asyncpg.PostgresError):
            await conn.fetch("select * from app.users")


async def test_patch_settings(api: Api) -> None:
    _, h = api.user()
    r = await api.client.patch("/v1/me", headers=h, json={"display_name": "Alex D."})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Alex D."
    r = await api.client.patch(
        "/v1/me/settings",
        headers=h,
        json={"standup_time": "09:30", "timezone": "Europe/Berlin",
              "consent": {"store_transcript": True}},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    r = await api.client.patch("/v1/me/settings", headers=h, json={"retention_days": 30})
    assert r.json()["settings"] == {
        "standup_time": "09:30",
        "timezone": "Europe/Berlin",
        "consent": {"store_transcript": True},
        "retention_days": 30,
    }
    r = await api.client.patch("/v1/me/settings", headers=h, json={"standup_time": None})
    assert "standup_time" not in r.json()["settings"]
    for bad in ({"timezone": "Mars/Base"}, {"standup_time": "9:30"}, {"retention_days": 0},
                {"unknown": 1}):  # fmt: skip
        assert (await api.client.patch("/v1/me/settings", headers=h, json=bad)).status_code == 422
    # Each path takes only its own fields.
    r = await api.client.patch("/v1/me", headers=h, json={"settings": {"retention_days": 7}})
    assert r.status_code == 422
    r = await api.client.patch("/v1/me", headers=h, json={"retention_days": 7})
    assert r.status_code == 422
    r = await api.client.patch("/v1/me/settings", headers=h, json={"display_name": "X"})
    assert r.status_code == 422
    assert (await api.client.get("/v1/me", headers=h)).json()["display_name"] == "Alex D."


async def upload_document(api: Api, h: dict[str, str], body: bytes, kind: str = "resume") -> str:
    r = await api.client.post(
        "/v1/documents",
        headers=h,
        json={"kind": kind, "content_type": "text/markdown", "size_bytes": len(body),
              "sha256": hashlib.sha256(body).hexdigest()},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    data = r.json()
    upload = data["upload"]
    assert upload["method"] == "PUT"
    async with httpx2.AsyncClient() as s3:  # straight to S3 with the presigned URL
        put = await s3.put(upload["url"], content=body, headers=upload["headers"])
        assert put.status_code == 200, put.text
    return data["document_id"]


async def test_document_upload_and_ingest(api: Api) -> None:
    uid, h = api.user()
    body = b"# Alex Doe\nSenior Backend Engineer at Acme, 2021 to now.\n"
    r = await api.client.post(
        "/v1/documents",
        headers=h,
        json={"kind": "resume", "content_type": "text/markdown", "size_bytes": len(body),
              "sha256": hashlib.sha256(body).hexdigest()},
    )  # fmt: skip
    early = r.json()["document_id"]
    assert (await api.client.post(f"/v1/documents/{early}/ingest", headers=h)).status_code == 409
    doc = await upload_document(api, h, body)
    key = await api.admin.fetchval("select s3_key from app.documents where id = $1", uuid.UUID(doc))
    assert key.startswith(f"uploads/{uid}/documents/")
    r = await api.client.post(f"/v1/documents/{doc}/ingest", headers=h)
    assert r.status_code == 202
    assert r.json()["status"] == "ingesting"
    [job] = api.take("memory.extract")
    assert job["payload"] == {"document_id": doc}
    assert job["user_id"] == str(uid)
    listed = (await api.client.get("/v1/documents", headers=h)).json()
    assert {d["id"]: d["status"] for d in listed["documents"]} == {
        doc: "ingesting",  # until memory.extract ends
        early: "uploaded",
    }
    assert listed["digest"] is None
    # Asking again changes nothing and queues nothing.
    assert (await api.client.post(f"/v1/documents/{doc}/ingest", headers=h)).status_code == 202
    assert api.take("memory.extract") == []
    # A document the worker could not read is uploaded again, not re-ingested.
    await api.admin.execute(
        "update app.documents set status = 'failed' where id = $1", uuid.UUID(doc)
    )
    assert (await api.client.post(f"/v1/documents/{doc}/ingest", headers=h)).status_code == 409
    # Once the worker wrote the digest, the list says so.
    await api.svc.valkey.set(
        keys.digest(uid),
        json.dumps({"schema": "digest.v1", "version": "sha256:" + "cd" * 32, "text": "x",
                    "refs": {}, "built_at": "2026-10-06T10:00:00Z"}),
    )  # fmt: skip
    listed = (await api.client.get("/v1/documents", headers=h)).json()
    assert listed["digest"] == {
        "version": "sha256:" + "cd" * 32, "built_at": "2026-10-06T10:00:00Z", "facts": 0,
    }  # fmt: skip
    # Another user cannot see or ingest it.
    _, other = api.user()
    assert (await api.client.post(f"/v1/documents/{doc}/ingest", headers=other)).status_code == 404
    # Limits.
    too_big = {
        "kind": "resume",
        "content_type": "text/plain",
        "size_bytes": 10**9,
        "sha256": "0" * 64,
    }
    assert (await api.client.post("/v1/documents", headers=h, json=too_big)).status_code == 413
    exe = {**too_big, "size_bytes": 10, "content_type": "application/x-msdownload"}
    assert (await api.client.post("/v1/documents", headers=h, json=exe)).status_code == 422


async def test_tampered_presigned_url_is_refused_by_s3(api: Api) -> None:
    _, h = api.user()
    body = b"resume"
    r = await api.client.post(
        "/v1/documents",
        headers=h,
        json={"kind": "resume", "content_type": "text/plain", "size_bytes": 6,
              "sha256": hashlib.sha256(body).hexdigest()},
    )  # fmt: skip
    url = r.json()["upload"]["url"].replace("/documents/", "/documents/other-")
    async with httpx2.AsyncClient() as s3:
        put = await s3.put(url, content=body, headers={"Content-Type": "text/plain"})
    assert put.status_code == 403


async def test_jobs_interviews_and_their_packs(api: Api) -> None:
    uid, h = api.user()
    await api.svc.valkey.set(
        keys.digest(uid),
        json.dumps(
            {"schema": "digest.v1", "version": "sha256:" + "ab" * 32, "text": "x", "refs": {}}
        ),
    )
    r = await api.client.post(
        "/v1/jobs",
        headers=h,
        json={"company": "Globex", "title": "Staff Engineer", "description": "Payments."},
    )
    assert r.status_code == 201
    job = r.json()
    [build] = api.take("pack.build")
    assert build["payload"]["context"] == f"job:{job['id']}"
    tag = jobs.embedding_tag(api.svc.settings.embedding_model_id)
    assert build["idempotency_key"] == f"pack.build:{uid}:job:{job['id']}:{'ab' * 8}:{tag}"

    listed = (await api.client.get("/v1/jobs", headers=h)).json()["jobs"]
    assert [j["pack"] for j in listed] == [None]
    pack_id = str(uuid.uuid4())
    pack = {"schema": "session_pack.v1", "pack_id": pack_id, "user_id": str(uid),
            "mode": "interview", "built_at": "2026-10-06T10:00:00Z", "digest_version": "x",
            "context": {"round": "technical"}, "prepared_answers": [
                {"question": "Q?", "talking_points": ["a"]}]}  # fmt: skip
    await api.svc.valkey.set(keys.pack(uid, pack_id), json.dumps(pack))
    await api.svc.valkey.hset(keys.pack_index(uid), mapping={f"job:{job['id']}": pack_id})
    listed = (await api.client.get("/v1/jobs", headers=h)).json()["jobs"]
    assert listed[0]["pack"]["available"] is True
    assert listed[0]["pack"]["prepared_answers"] == 1

    starts = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    r = await api.client.post(
        "/v1/interviews", headers=h,
        json={"job_id": job["id"], "round": "technical", "starts_at": starts},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    interview = r.json()
    [build] = api.take("pack.build")
    assert build["payload"]["context"] == f"interview:{interview['id']}"
    assert build["payload"]["due_at"].startswith(starts[:16].replace("+00:00", ""))
    assert (interview["company"], interview["title"]) == (job["company"], job["title"])
    [listed_interview] = (await api.client.get("/v1/interviews", headers=h)).json()["interviews"]
    assert listed_interview["company"] == job["company"]
    assert listed_interview["pack"] is None  # not built yet
    # A job of another user is not found; a naive start time is refused.
    _, other = api.user()
    r = await api.client.post(
        "/v1/interviews", headers=other,
        json={"job_id": job["id"], "round": "technical", "starts_at": starts},
    )  # fmt: skip
    assert r.status_code == 404
    r = await api.client.post(
        "/v1/interviews", headers=h,
        json={"job_id": job["id"], "round": "technical", "starts_at": "2026-10-08T10:00:00"},
    )  # fmt: skip
    assert r.status_code == 422


async def seed_session(api: Api, uid: uuid.UUID, *, ended: bool = True, outputs: bool = True,
                       store_transcript: bool = True, minutes: int = 10) -> uuid.UUID:  # fmt: skip
    sid = uuid.uuid4()
    started = datetime.now(UTC) - timedelta(hours=1)
    await api.admin.execute(
        "insert into app.sessions (id, user_id, mode, store_transcript, started_at, ended_at,"
        " billed_seconds, platform) values ($1, $2, 'interview', $3, $4, $5, $6, 'zoom')",
        sid,
        uid,
        store_transcript,
        started,
        started + timedelta(minutes=minutes) if ended else None,
        minutes * 60 if ended else 0,
    )
    async with api.svc.tx(Principal(uid, "x", None)) as conn:
        if store_transcript:
            for seg_id, (channel, text) in enumerate(
                [("them", "Why Globex?"), ("me", "Payments.")]
            ):
                enc, v = await api.svc.crypto.encrypt(
                    conn, uid, "transcript_segments", segment_row_id(sid, channel, seg_id), text
                )
                await conn.execute(
                    "insert into app.transcript_segments (session_id, channel, seg_id, user_id,"
                    " start_ms, end_ms, text_enc, key_version) values ($1,$2,$3,$4,$5,$6,$7,$8)",
                    sid, channel, seg_id, uid, seg_id * 1000, seg_id * 1000 + 900, enc, v,
                )  # fmt: skip
        if outputs:
            oid = uuid.uuid4()
            enc, v = await api.svc.crypto.encrypt(
                conn, uid, "session_outputs", str(oid), "Went well."
            )
            await conn.execute(
                "insert into app.session_outputs (id, session_id, user_id, kind, content_enc,"
                " key_version) values ($1, $2, $3, 'summary', $4, $5)",
                oid, sid, uid, enc, v,
            )  # fmt: skip
            await conn.execute(
                "update app.sessions set outputs_ready_at = now() where id = $1", sid
            )
    return sid


async def test_sessions_list_detail_transcript_and_delete(api: Api) -> None:
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    ready = await seed_session(api, uid)
    processing = await seed_session(api, uid, outputs=False)
    listed = (await api.client.get("/v1/sessions", headers=h)).json()["sessions"]
    assert {s["id"]: s["status"] for s in listed} == {
        str(ready): "ready",
        str(processing): "processing",
    }
    detail = (await api.client.get(f"/v1/sessions/{ready}", headers=h)).json()
    assert detail["outputs"] == {"summary": "Went well."}
    assert detail["status"] == "ready"
    transcript = (await api.client.get(f"/v1/sessions/{ready}/transcript", headers=h)).json()
    assert [(s["channel"], s["text"]) for s in transcript["segments"]] == [
        ("them", "Why Globex?"), ("me", "Payments."),
    ]  # fmt: skip
    _, other = api.user()
    assert (await api.client.get(f"/v1/sessions/{ready}", headers=other)).status_code == 404
    assert (await api.client.delete(f"/v1/sessions/{ready}", headers=other)).status_code == 404

    await api.svc.store.put(me_audio_key(uid, ready), b"audio", "audio/wav")
    assert (await api.client.delete(f"/v1/sessions/{ready}", headers=h)).status_code == 204
    for table in ("sessions", "transcript_segments", "session_outputs"):
        column = "id" if table == "sessions" else "session_id"
        n = await api.admin.fetchval(f"select count(*) from app.{table} where {column} = $1", ready)
        assert n == 0, table
    assert await api.svc.store.head(me_audio_key(uid, ready)) is None


async def test_a_live_session_is_reported_and_cannot_be_deleted(api: Api) -> None:
    uid, h = api.user()
    sid = uuid.uuid4()
    await api.svc.valkey.hset(keys.sess_state(sid), mapping={
        "user_id": str(uid), "status": "active", "mode": "standup", "store_transcript": "1",
        "started_at": datetime.now(UTC).isoformat(),
    })  # fmt: skip
    detail = (await api.client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert detail["status"] == "live"
    assert (await api.client.delete(f"/v1/sessions/{sid}", headers=h)).status_code == 409


async def test_me_audio_upload_and_complete(api: Api) -> None:
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    sid = await seed_session(api, uid, outputs=False)
    no_consent = await seed_session(api, uid, outputs=False, store_transcript=False)
    r = await api.client.post(f"/v1/sessions/{no_consent}/me-audio", headers=h, json={})
    assert r.status_code == 409
    r = await api.client.post(f"/v1/sessions/{sid}/me-audio/complete", headers=h)
    assert r.status_code == 409  # nothing uploaded yet
    # While the gateway still holds the session (live, or resumable), the upload waits.
    state = keys.sess_state(sid)
    await api.svc.valkey.hset(state, mapping={"user_id": str(uid), "status": "reconnecting"})
    for path in ("me-audio", "me-audio/complete"):
        r = await api.client.post(f"/v1/sessions/{sid}/{path}", headers=h, json={})
        assert r.status_code == 409
        assert r.json()["detail"].startswith("live:")
    await api.svc.valkey.hset(state, "status", "ended")
    r = await api.client.post(
        f"/v1/sessions/{sid}/me-audio", headers=h, json={"content_type": "audio/wav"}
    )
    assert r.status_code == 201
    upload = r.json()["upload"]
    async with httpx2.AsyncClient() as s3:
        put = await s3.put(upload["url"], content=b"RIFF....", headers=upload["headers"])
        assert put.status_code == 200
    r = await api.client.post(f"/v1/sessions/{sid}/me-audio/complete", headers=h)
    assert r.status_code == 202
    status = await api.admin.fetchval("select me_audio_status from app.sessions where id = $1", sid)
    assert status == "uploaded"
    [job] = api.take("me_audio.transcribe")
    assert job["idempotency_key"] == f"me_audio.transcribe:{sid}"
    again = await api.client.post(f"/v1/sessions/{sid}/me-audio", headers=h, json={})
    assert again.status_code == 409


async def test_usage_and_account_deletion(api: Api) -> None:
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    await seed_session(api, uid, minutes=10)
    await seed_session(api, uid, minutes=5, outputs=False)
    await seed_session(api, uid, ended=False)  # live: not billed yet
    usage = (await api.client.get("/v1/usage", headers=h)).json()
    assert usage["live_seconds"] == 900
    assert usage["live_minutes"] == 15
    assert usage["sessions"] == 2
    assert (await api.client.get("/v1/usage?month=2026-13", headers=h)).status_code == 422

    r = await api.client.delete("/v1/me", headers=h)
    assert r.status_code == 202
    [job] = api.take("user.delete")
    assert job["idempotency_key"] == f"user.delete:{uid}"


async def test_cognito_tokens_end_to_end(api: Api) -> None:
    """The Cognito verifier behind the same interface, with a locally generated key pair."""
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from copilot_core.auth import CognitoVerifier, JwksCache

    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Local"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k1", "alg": "RS256"}

    async def fetch(url: str) -> dict[str, Any]:
        return {"keys": [jwk]}

    verifier = CognitoVerifier(issuer, ["desktop"], JwksCache(f"{issuer}/jwks", fetch=fetch))
    uid = uuid.uuid4()
    now = int(time.time())
    claims = {"sub": str(uid), "iss": issuer, "client_id": "desktop", "token_use": "access",
              "iat": now, "exp": now + 600, "username": "sam",
              "email": "sam@example.com"}  # fmt: skip
    token = jwt.encode(claims, key, "RS256", headers={"kid": "k1"})
    expired = jwt.encode({**claims, "exp": now - 120}, key, "RS256", headers={"kid": "k1"})
    previous = api.svc.verifier
    api.svc.verifier = verifier
    try:
        r = await api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "sam@example.com"
        assert r.json()["id"] == str(uid)
        r = await api.client.get("/v1/me", headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401
        assert "expired" in r.json()["detail"]
    finally:
        api.svc.verifier = previous


async def test_request_lines_name_the_session_and_user(api: Api, caplog) -> None:
    # CloudWatch filters on session_id: requests under /v1/sessions/{id} carry it, and every
    # authenticated request carries the user id.
    uid, h = api.user()
    await api.client.get("/v1/me", headers=h)
    sid = await seed_session(api, uid)
    caplog.set_level(logging.INFO, logger="copilot_api")
    caplog.handler.addFilter(ContextFilter())
    await api.client.get(f"/v1/sessions/{sid}", headers=h)
    await api.client.get("/v1/sessions", headers=h)
    lines = {r.getMessage().rsplit(" ", 1)[0]: r for r in caplog.records}
    one = lines[f"GET /v1/sessions/{sid} 200"]
    assert (getattr(one, "session_id", None), getattr(one, "user_id", None)) == (str(sid), str(uid))
    listing = lines["GET /v1/sessions 200"]
    assert getattr(listing, "session_id", None) is None


async def test_auth_config_tells_the_app_how_to_sign_in(api: Api) -> None:
    # No token needed: the desktop app asks before it has one.
    saved = api.svc.settings
    try:
        api.svc.settings = saved.model_copy(update={
            "auth": "cognito", "cognito_client_ids": ["client123"],
            "cognito_domain": "https://copilot-x.auth.us-east-1.amazoncognito.com/",
        })  # fmt: skip
        body = (await api.client.get("/v1/auth/config")).json()
        assert body == {
            "provider": "cognito", "client_id": "client123",
            "domain": "https://copilot-x.auth.us-east-1.amazoncognito.com",
            "scopes": ["openid", "email", "profile"],
        }  # fmt: skip
        api.svc.settings = saved.model_copy(update={
            "auth": "google", "google_client_ids": ["123-abc.apps.googleusercontent.com"],
            "google_client_secret": SecretStr("GOCSPX-desktop"), "google_allowed_emails": ["a@b.c"],
        })  # fmt: skip
        body = (await api.client.get("/v1/auth/config")).json()
        assert body == {
            "provider": "google", "client_id": "123-abc.apps.googleusercontent.com",
            "client_secret": "GOCSPX-desktop", "scopes": ["openid", "email", "profile"],
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "revoke_url": "https://oauth2.googleapis.com/revoke",
            "bearer": "id_token",
            "authorize_params": {"access_type": "offline", "prompt": "consent"},
        }  # fmt: skip
        assert "a@b.c" not in str(body)  # the allow-list stays on the server
        api.svc.settings = saved.model_copy(update={"auth": "dev"})
        assert (await api.client.get("/v1/auth/config")).json() == {"provider": "dev"}
    finally:
        api.svc.settings = saved
