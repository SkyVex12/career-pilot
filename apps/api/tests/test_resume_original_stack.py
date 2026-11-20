"""Original-file caches obey real migrated RLS, replacement, and deletion semantics."""

from uuid import uuid4

import pytest

from copilot_api.testing import Api
from copilot_core.auth import Principal

pytestmark = pytest.mark.stack


async def test_original_file_text_rls_replacement_and_delete(api: Api):
    uid, headers = api.user()
    _, other = api.user()
    assert (await api.client.get("/v1/profiles", headers=headers)).status_code == 200
    doc, version = uuid4(), uuid4()
    await api.admin.execute(
        "insert into app.documents(id,user_id,kind,s3_key,sha256) "
        "values($1,$2,'resume','fictional','old')",
        doc,
        uid,
    )
    await api.admin.execute(
        "insert into app.resume_versions(id,user_id,kind,label,source,document_id,content_sha256) "
        "values($1,$2,'base','Original upload','file',$3,'old')",
        version,
        uid,
        doc,
    )
    async with api.svc.tx(Principal(uid, "Fixture", None)) as conn:
        sealed, key = await api.svc.crypto.encrypt(
            conn, uid, "resume_versions:original", str(version), "Original private file text"
        )
        await conn.execute(
            "update app.resume_versions set original_text_enc=$2,original_text_key_version=$3,"
            "original_content_sha256='old' where id=$1",
            version,
            sealed,
            key,
        )
    path = f"/v1/ask/sources/resume/{version}"
    response = await api.client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Original private file text"
    assert (await api.client.get(path, headers=other)).status_code == 404
    await api.admin.execute(
        "update app.resume_versions set content_sha256='replacement' where id=$1", version
    )
    row = await api.admin.fetchrow(
        "select original_text_enc,original_text_key_version,original_content_sha256 "
        "from app.resume_versions where id=$1",
        version,
    )
    assert row is not None
    assert tuple(row.values()) == (None, None, None)
    assert (await api.client.get(path, headers=headers)).status_code == 404
    # A stale asynchronous worker cannot make an old hash readable, even if ciphertext exists.
    await api.admin.execute(
        "update app.resume_versions set original_text_enc=$2,original_text_key_version=$3,"
        "original_content_sha256='old' where id=$1",
        version,
        sealed,
        key,
    )
    assert (await api.client.get(path, headers=headers)).status_code == 404
    await api.admin.execute("delete from app.resume_versions where id=$1", version)
    assert (await api.client.get(path, headers=headers)).status_code == 404
