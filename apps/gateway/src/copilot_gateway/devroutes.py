"""Dev-only REST routes, for the desktop app until the Copilot API serves them (build step 4).

`GET /v1/dev/jobs` lists the jobs in the caller's pack index (`pack:{uid}:index`, which
scripts/dev_profile.py writes), so the desktop start screen can offer them. The API's
`GET /jobs` replaces it. The routes are mounted only with COPILOT_ENV dev or test, and use the
same bearer token as the WebSocket.
"""

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from copilot_gateway.auth import AuthError, bearer_token

router = APIRouter(prefix="/v1/dev")


@router.get("/jobs")
async def jobs(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    svc = request.app.state.manager.svc
    try:
        principal = await svc.verifier.verify(bearer_token(authorization) or "")
    except AuthError as e:
        raise HTTPException(401, str(e)) from e
    uid = principal.user_id
    index = await svc.store.pack_index(uid)
    out: list[dict[str, Any]] = []
    for field, pack_id in index.items():
        kind, _, job_id = field.partition(":")
        if kind != "job":
            continue
        pack = await svc.store.get_pack_by_id(uid, pack_id)
        ctx = pack.context if pack is not None else None
        out.append(
            {
                "job_id": job_id,
                "pack_id": pack_id,
                "pack_available": pack is not None,  # packs expire after 24 h
                "company": ctx.company if ctx else None,
                "role_title": ctx.role_title if ctx else None,
                "round": ctx.round if ctx else None,
                "built_at": pack.built_at if pack is not None else None,
            }
        )
    out.sort(key=lambda j: j["built_at"] or "", reverse=True)
    modes = sorted(f.partition(":")[2] for f in index if f.startswith("mode:"))
    return {"user_name": principal.display_name, "jobs": out, "mode_packs": modes}
