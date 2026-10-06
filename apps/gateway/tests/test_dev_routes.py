"""GET /v1/dev/jobs, the desktop start screen's job list, and the offline runner's options."""

import json
from typing import Any
from uuid import UUID

import httpx2

from copilot_core import keys
from copilot_gateway.offline import parse_token
from copilot_gateway.testing import GatewayServer

PACK_A = "0192f3d0-0a11-7e2b-8c3d-2f1e0b9a7c44"
PACK_B = "0192f3d0-0a11-7e2b-8c3d-2f1e0b9a7c45"
JOB_A = "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b33"
JOB_B = "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b34"
JOB_EXPIRED = "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b35"


def pack_json(pack_id: str, uid: UUID, company: str, built_at: str) -> str:
    return json.dumps(
        {
            "schema": "session_pack.v1",
            "pack_id": pack_id,
            "user_id": str(uid),
            "mode": "interview",
            "built_at": built_at,
            "digest_version": "sha256:0",
            "context": {"company": company, "role_title": "Backend Engineer", "round": "technical"},
        }
    )


async def test_jobs_lists_the_pack_index(env: Any) -> None:
    uid = env.user_a
    client = env.client()
    await client.set(keys.pack(uid, PACK_A), pack_json(PACK_A, uid, "Acme", "2026-10-04T14:00:00Z"))
    await client.set(
        keys.pack(uid, PACK_B), pack_json(PACK_B, uid, "Globex", "2026-10-05T14:00:00Z")
    )
    await client.hset(
        keys.pack_index(uid),
        mapping={
            "mode:interview": PACK_B,
            f"job:{JOB_A}": PACK_A,
            f"job:{JOB_B}": PACK_B,
            f"job:{JOB_EXPIRED}": "0192f3d0-0000-7000-8000-000000000000",
        },
    )
    async with GatewayServer(env.services()) as server:
        base = f"http://127.0.0.1:{server.port}/v1/dev/jobs"
        async with httpx2.AsyncClient() as http:
            assert (await http.get(base)).status_code == 401
            bad = await http.get(base, headers={"Authorization": "Bearer nope"})
            assert bad.status_code == 401
            body = (await http.get(base, headers={"Authorization": "Bearer token-a"})).json()
            other = (await http.get(base, headers={"Authorization": "Bearer token-b"})).json()

    assert body["user_name"] == "Alex"
    assert body["mode_packs"] == ["interview"]
    jobs = body["jobs"]
    assert [j["job_id"] for j in jobs] == [JOB_B, JOB_A, JOB_EXPIRED]  # newest pack first
    assert jobs[0] == {
        "job_id": JOB_B,
        "pack_id": PACK_B,
        "pack_available": True,
        "company": "Globex",
        "role_title": "Backend Engineer",
        "round": "technical",
        "built_at": "2026-10-05T14:00:00Z",
    }
    assert jobs[2]["pack_available"] is False  # packs expire after 24 h; the index stays
    assert other["jobs"] == []  # another user sees none of it


async def test_dev_routes_are_absent_outside_dev_and_test(env: Any) -> None:
    services = env.services(env.settings(env="prod"))
    async with GatewayServer(services) as server, httpx2.AsyncClient() as http:
        response = await http.get(
            f"http://127.0.0.1:{server.port}/v1/dev/jobs",
            headers={"Authorization": "Bearer token-a"},
        )
    assert response.status_code == 404


def test_offline_token_specs() -> None:
    assert parse_token("abc") == ("abc", None)
    token, expires = parse_token("abc:30")
    assert token == "abc"
    assert expires is not None
