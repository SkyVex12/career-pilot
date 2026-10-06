"""Load a resume and a job description for the dev user through the Copilot API, schedule an
interview, and wait until the workers have built its session pack.

    uv run python scripts/dev_profile.py --resume resume.pdf --job job.md --round technical
    uv run python scripts/dev_profile.py --resume resume.md --job job.md --starts-in 30

This is the real path (ARCHITECTURE.md, "Write path"): `POST /documents` and a presigned upload
for each file, `POST /documents/{id}/ingest`, then `POST /interviews` for the job the
description became. The workers extract the resume (memory nodes, then the hot digest), read
the job description, and build the pack the gateway loads (`pack:{uid}:index`). So the API
(`uv run copilot-api`) and a worker (`uv run copilot-worker`) must be running.

Inputs may be PDF (Claude reads it directly), Markdown or plain text. The token is
COPILOT_DEV_TOKEN from .env unless --token is given.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from copilot_core.dotenv import load_dotenv

CONTENT_TYPES = {".pdf": "application/pdf", ".md": "text/markdown", ".markdown": "text/markdown"}


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "text/plain")


class ProfileError(Exception):
    pass


async def upload(api: httpx2.AsyncClient, kind: str, path: Path) -> str:
    body = await asyncio.to_thread(path.read_bytes)
    r = await api.post(
        "/v1/documents",
        json={
            "kind": kind,
            "content_type": content_type(path),
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
    )
    if r.status_code != 201:
        raise ProfileError(f"POST /documents for {path.name}: {r.status_code} {r.text}")
    target = r.json()["upload"]
    async with httpx2.AsyncClient(timeout=60) as s3:
        put = await s3.put(target["url"], content=body, headers=target["headers"])
    if put.status_code != 200:
        raise ProfileError(f"upload of {path.name} failed: {put.status_code} {put.text}")
    doc = str(r.json()["document_id"])
    r = await api.post(f"/v1/documents/{doc}/ingest")
    if r.status_code not in (200, 202):
        raise ProfileError(f"ingest of {path.name}: {r.status_code} {r.text}")
    return doc


async def poll(what: str, check: Callable[[], Any], interval_s: float, timeout_s: float) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        result = await check()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise ProfileError(f"timed out waiting for {what}; is copilot-worker running?")
        await asyncio.sleep(interval_s)


async def document_status(api: httpx2.AsyncClient, doc: str) -> str:
    docs = (await api.get("/v1/documents")).json()["documents"]
    return next((d["status"] for d in docs if d["id"] == doc), "missing")


async def build_profile(
    api: httpx2.AsyncClient,
    resume: Path,
    job: Path,
    round_: str,
    starts_at: datetime,
    interval_s: float = 2.0,
    timeout_s: float = 600.0,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    me = await api.get("/v1/me")
    if me.status_code != 200:
        raise ProfileError(f"GET /me: {me.status_code} {me.text}")
    say(f"user {me.json()['id']} ({me.json()['display_name']})")
    resume_doc = await upload(api, "resume", resume)
    job_doc = await upload(api, "job_description", job)
    say("uploaded; waiting for the workers to read both documents")

    async def ingested() -> bool:
        statuses = [await document_status(api, d) for d in (resume_doc, job_doc)]
        if "failed" in statuses:
            raise ProfileError("a document failed to ingest; see the worker log")
        return statuses == ["ingested", "ingested"]

    await poll("document ingest", ingested, interval_s, timeout_s)
    r = await api.post(
        "/v1/interviews",
        json={"job_id": job_doc, "round": round_, "starts_at": starts_at.isoformat()},
    )
    if r.status_code != 201:
        raise ProfileError(f"POST /interviews: {r.status_code} {r.text}")
    interview = r.json()
    say(f"interview {interview['id']} ({round_}) at {starts_at:%Y-%m-%d %H:%M} UTC")

    async def pack_ready() -> dict[str, Any] | None:
        items = (await api.get("/v1/interviews")).json()["interviews"]
        found = next((i for i in items if i["id"] == interview["id"]), None)
        pack = found and found.get("pack")
        return pack if pack and pack.get("available") else None

    pack: dict[str, Any] = await poll("the session pack", pack_ready, interval_s, timeout_s)
    jobs = (await api.get("/v1/jobs")).json()["jobs"]
    title = next((f"{j['title']} at {j['company']}" for j in jobs if j["id"] == job_doc), "?")
    say(
        f"pack {pack['pack_id']}: {pack['ranked_stories']} ranked stories, "
        f"{pack['prepared_answers']} prepared answers"
    )
    say(f"job id {job_doc} ({title}); pick it on the desktop start screen")
    return {"job_id": job_doc, "interview": interview, "pack": pack}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Load a resume and a job through the API.")
    parser.add_argument("--resume", type=Path, required=True)
    parser.add_argument("--job", type=Path, required=True, help="job description file")
    parser.add_argument(
        "--round",
        default="technical",
        choices=["phone_screen", "technical", "system_design", "final"],
    )
    parser.add_argument("--starts-in", type=int, default=60, help="minutes until the interview")
    parser.add_argument("--api", default=os.environ.get("COPILOT_API_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token", help="bearer token (default: COPILOT_DEV_TOKEN)")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for workers")
    args = parser.parse_args()
    load_dotenv()
    token = args.token or os.environ.get("COPILOT_DEV_TOKEN")
    if not token:
        sys.exit("no token: set COPILOT_DEV_TOKEN in .env or pass --token")
    starts_at = datetime.now(UTC) + timedelta(minutes=args.starts_in)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(base_url=args.api, headers=headers, timeout=30) as api:
        try:
            await build_profile(api, args.resume, args.job, args.round, starts_at,
                                timeout_s=args.timeout)  # fmt: skip
        except (ProfileError, httpx2.HTTPError) as e:
            sys.exit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
