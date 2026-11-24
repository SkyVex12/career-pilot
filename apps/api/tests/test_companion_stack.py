"""Real account RLS and grant/revoke persistence; no capture or provider side effects."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from copilot_api.testing import Api
from copilot_core import companions, keys
from copilot_core.auth import AuthError, Principal
from copilot_core.ids import uuid7
from copilot_gateway.companionrelay import CompanionRelay

pytestmark = pytest.mark.stack


async def test_companion_attach_and_revoke_do_not_claim_capture(api: Api):
    owner, headers = api.user()
    _, other_headers = api.user()
    await api.client.get("/v1/companion/attachments", headers=headers)
    await api.client.get("/v1/companion/attachments", headers=other_headers)
    sid = uuid7()
    await api.admin.execute(
        "insert into app.sessions(id,user_id,mode,started_at,store_transcript,"
        "private_context_account_id)"
        " values($1,$2,'meeting',now(),true,$2)",
        sid,
        owner,
    )
    await api.svc.valkey.hset(
        keys.sess_state(sid),
        mapping={
            "account_id": str(owner),
            "user_id": str(owner),
            "status": "active",
            "private_context_account_id": str(owner),
            "title": "Private desktop call",
        },
    )
    await api.svc.valkey.set(keys.user_active_session(owner), str(sid))
    await api.svc.valkey.set(keys.sess_lease(sid), "desktop-task")
    listing = await api.client.get("/v1/companion/sessions", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"][0]["session_id"] == str(sid)
    denied = await api.client.post(
        f"/v1/companion/sessions/{sid}/attach",
        headers=other_headers,
        json={"device_name": "Intruder"},
    )
    assert denied.status_code == 404
    response = await api.client.post(
        f"/v1/companion/sessions/{sid}/attach", headers=headers, json={"device_name": "Phone"}
    )
    assert response.status_code == 200, response.text
    aid = response.json()["attachment_id"]
    assert await companions.read(api.svc.valkey, str(sid), aid) is not None
    principal = Principal(owner, "Owner", None)
    relay = CompanionRelay(
        cast(
            Any,
            SimpleNamespace(
                pool=api.svc.pool,
                store=SimpleNamespace(
                    client=api.svc.valkey,
                    load_state=AsyncMock(
                        return_value={
                            "account_id": str(owner),
                            "user_id": str(owner),
                            "status": "active",
                            "private_context_account_id": str(owner),
                        }
                    ),
                ),
            ),
        ),
        cast(Any, SimpleNamespace(as_profile=AsyncMock(return_value=principal))),
    )
    assert (await relay.authorize(principal, str(sid), aid)).account_id == str(owner)

    assert (await api.client.get("/v1/companion/attachments", headers=other_headers)).json()[
        "items"
    ] == []
    forbidden = await api.client.delete(f"/v1/companion/attachments/{aid}", headers=other_headers)
    assert forbidden.status_code == 404
    assert (
        await api.client.delete(f"/v1/companion/attachments/{aid}", headers=headers)
    ).status_code == 200
    assert await companions.read(api.svc.valkey, str(sid), aid) is None
    with pytest.raises(AuthError):
        await relay.authorize(principal, str(sid), aid)

    holder = await api.svc.valkey.get(keys.sess_lease(sid))
    assert holder in ("desktop-task", b"desktop-task")
    assert api.jobs.sent == []
    await api.svc.valkey.delete(keys.sess_state(sid), keys.sess_lease(sid))
    await api.svc.valkey.delete(keys.user_active_session(owner))
