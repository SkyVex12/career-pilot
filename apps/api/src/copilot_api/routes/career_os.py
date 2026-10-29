"""The Career-OS link (ARCHITECTURE.md, "Career-OS link"): connect, search, pull on demand.

One Career Pilot account links to one Career-OS account: a user, or an admin or super admin.
The Career-OS account approves the link itself: signed in to Career-OS, it generates a one-time
code, and Career Pilot exchanges it for a read-only token bound to that account
(`copilot_core.careeros`). A user's token reads that user's applications; an admin's reads
those of the users linked to that admin in Career-OS (`/career-os/users` lists them, and search
can narrow to one). The link is the account's (`AccountDep`), whichever profile the app has
switched to: the token is sealed with the account's data key in `app.integrations`, like
every tool token. Disconnecting revokes it on Career-OS first, then deletes it here.

Nothing syncs on its own. The user searches Career-OS applications and pulls one: a copy of
the application with its JD and its resume version (or base resume), recorded with the
Career-OS id, its content hash and the pull time. A pulled application lives with its
Career-OS user (ARCHITECTURE.md, "Profiles"): in the account itself for the linked user's own
(a user link), else in that user's profile, made on the first pull. Search, switched to a
profile, shows that profile's Career-OS user only. Search marks applications that changed in
Career-OS since they were pulled; pulling again replaces the pulled fields, JD and resume.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep
from copilot_api.routes.applications import application_out, enqueue_packs
from copilot_core import careeros, jobs, profiles, resumes
from copilot_core.auth import Principal
from copilot_core.careeros import CareerOsClient, CareerOsError, ResumeFile
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.storage import resume_file_key

router = APIRouter()

PROVIDER = careeros.PROVIDER
COS_ID = r"^[A-Za-z0-9_.:-]{1,128}$"
STAGES = ("applied", "interview", "offer", "rejected")
ACCOUNT_TYPES = ("user", "admin", "super_admin")
FILE_TYPES = ("application/pdf", "text/plain", "text/markdown",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "application/msword")  # fmt: skip


class ConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=64)


class PulledApplication(BaseModel):
    """The application as Career-OS sends it; lengths as Career Pilot stores them."""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=COS_ID)
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    source_site: str | None = Field(default=None, max_length=200)
    stage: str = "applied"
    note: str | None = Field(default=None, max_length=10_000)


def integration_row_id(uid: UUID) -> str:
    return f"{uid}:{PROVIDER}"


def client_of(svc: Services) -> CareerOsClient:
    if svc.career_os is None:
        raise HTTPException(503, "Career-OS is not set up on this server")
    return svc.career_os


async def token_of(svc: Services, conn: Conn, uid: UUID) -> str:
    row = await conn.fetchrow(
        "select token_enc, key_version, status from app.integrations where provider = $1",
        PROVIDER,
    )
    if row is None:
        raise HTTPException(409, "Career-OS is not connected")
    if row["status"] != "active":
        raise HTTPException(409, "Career-OS no longer accepts this link; connect again")
    sealed = await svc.crypto.decrypt(
        conn, uid, "integrations", integration_row_id(uid), bytes(row["token_enc"]),
        int(row["key_version"]),
    )  # fmt: skip
    return str(json.loads(sealed)["token"])


async def remote_failed(svc: Services, user: Principal, e: CareerOsError) -> HTTPException:
    """Career-OS refused: a revoked link is marked here, so the app asks to connect again."""
    if e.unauthorized:
        async with svc.tx(user) as conn:
            await conn.execute(
                "update app.integrations set status = 'revoked' where provider = $1", PROVIDER
            )
        return HTTPException(409, "Career-OS no longer accepts this link; connect again")
    if e.status in (404, 429):
        return HTTPException(e.status, e.message)
    return HTTPException(502, e.message)


async def kind_of(svc: Services, account: Principal, row: Any | None) -> str:
    """The link's Career-OS account type: `user`, `admin` or `super_admin`. A link made before
    Career Pilot kept it (migration 0009) has none: Career-OS is asked once (`GET /me`) and the
    answer kept. Until Career-OS answers, such a link counts as a user link."""
    if row is None:
        return "user"
    kind = row["external_kind"]
    if kind in ACCOUNT_TYPES:
        return str(kind)
    if row["status"] != "active" or svc.career_os is None:
        return "user"
    try:
        async with svc.tx(account) as conn:
            token = await token_of(svc, conn, account.user_id)
        me = await svc.career_os.me(token)
    except (HTTPException, CareerOsError):
        return "user"
    found = me.get("account_type")
    kind = found if found in ACCOUNT_TYPES else "user"
    async with svc.tx(account) as conn:
        await conn.execute(
            "update app.integrations set external_kind = $2"
            " where provider = $1 and external_kind is null",
            PROVIDER, kind,
        )  # fmt: skip
    return kind


def connection_out(
    row: Any | None, available: bool, kind: str = "user", required: bool = False
) -> dict[str, Any]:
    # required: the stage needs an active link before the app does anything else.
    if row is None:
        return {"connected": False, "available": available, "required": required}
    return {
        "connected": row["status"] == "active",
        "available": available,
        "required": required,
        "status": row["status"],
        "user": {"id": row["external_id"], "name": row["external_label"]},
        "account_type": kind,
        "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
        "last_pull_at": row["last_sync_at"].isoformat() if row["last_sync_at"] else None,
    }


@router.get("/career-os/connection")
async def get_connection(svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select status, external_id, external_label, external_kind, connected_at,"
            " last_sync_at from app.integrations where provider = $1",
            PROVIDER,
        )
    return connection_out(
        row,
        svc.career_os is not None,
        await kind_of(svc, user, row),
        svc.settings.require_career_os,
    )


@router.post("/career-os/connect", status_code=201)
async def connect(body: ConnectIn, svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    client = client_of(svc)
    uid = user.user_id
    async with svc.tx(user) as conn:
        current = await conn.fetchval(
            "select status from app.integrations where provider = $1", PROVIDER
        )
        email = await conn.fetchval("select email from app.users where id = $1", uid)
    if current == "active":
        raise HTTPException(409, "Career-OS is already connected; disconnect it first")
    code = body.code.strip().upper().replace(" ", "")
    try:
        linked = await client.link(code, f"Career Pilot ({email})")
    except CareerOsError as e:
        if e.status in (400, 401, 404, 410):
            raise HTTPException(400, "that code is not valid or has expired; make a new one") from e
        if e.status == 429:
            raise HTTPException(429, e.message) from e
        raise HTTPException(502, e.message) from e
    cos_user = linked["user"]
    kind = linked.get("account_type")
    kind = kind if kind in ACCOUNT_TYPES else "user"
    secret = json.dumps({"token": linked["token"], "link_id": linked.get("link_id")})
    try:
        async with svc.tx(user) as conn:
            sealed, key_version = await svc.crypto.encrypt(
                conn, uid, "integrations", integration_row_id(uid), secret
            )
            await conn.execute(
                "insert into app.integrations (user_id, provider, token_enc, key_version, scopes,"
                " status, external_id, external_label, external_kind, connected_at)"
                " values ($1, $2, $3, $4, $5, 'active', $6, $7, $8, now())"
                " on conflict (user_id, provider) do update set token_enc = excluded.token_enc,"
                " key_version = excluded.key_version, scopes = excluded.scopes,"
                " status = 'active', external_id = excluded.external_id,"
                " external_label = excluded.external_label,"
                " external_kind = excluded.external_kind, connected_at = now(),"
                " last_sync_at = null",
                uid, PROVIDER, sealed, key_version,
                [str(s) for s in linked.get("scopes") or ["applications:read"]],
                str(cos_user.get("id")), str(cos_user.get("name") or "")[:200], kind,
            )  # fmt: skip
            row = await conn.fetchrow(
                "select status, external_id, external_label, external_kind, connected_at,"
                " last_sync_at from app.integrations where provider = $1",
                PROVIDER,
            )
    except Exception:
        # Never leave a token on Career-OS that this account does not hold.
        await client.revoke(str(linked["token"]))
        raise
    return connection_out(row, True, kind, svc.settings.require_career_os)


@router.delete("/career-os/connection", status_code=204)
async def disconnect(svc: SvcDep, user: AccountDep) -> Response:
    """Revoke the token on Career-OS, then delete it here. If Career-OS cannot be reached the
    link stays, so it is never left valid there while gone here."""
    uid = user.user_id
    async with svc.tx(user) as conn:
        exists = await conn.fetchval("select 1 from app.integrations where provider = $1", PROVIDER)
        if exists is None:
            return Response(status_code=204)
        token: str | None
        try:
            token = await token_of(svc, conn, uid)
        except HTTPException:
            token = None  # revoked on Career-OS already
    if token is not None:
        try:
            await client_of(svc).revoke(token)
        except CareerOsError as e:
            raise HTTPException(502, f"could not revoke the link on Career-OS: {e.message}") from e
    async with svc.tx(user) as conn:
        await conn.execute("delete from app.integrations where provider = $1", PROVIDER)
    return Response(status_code=204)


@router.get("/career-os/users")
async def users(svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    """The Career-OS users the link reads: the linked user, or an admin's linked users. Their
    ids narrow search (`user_id`)."""
    client = client_of(svc)
    async with svc.tx(user) as conn:
        token = await token_of(svc, conn, user.user_id)
    try:
        found = await client.users(token)
    except CareerOsError as e:
        raise await remote_failed(svc, user, e) from e
    items = [user_out(obj(entry)) for entry in cast(list[object], found.get("items") or [])]
    return {"items": [i for i in items if i["id"]]}


def user_out(raw: dict[str, Any]) -> dict[str, str]:
    """A Career-OS user as `{id, name}` (empty strings when Career-OS sent none)."""
    return {"id": str(raw.get("id") or ""), "name": str(raw.get("name") or "")[:200]}


class Homes:
    """Where each Career-OS user's applications live here: the account for the linked user
    itself (a user link), else that user's profile."""

    def __init__(self, svc: Services, account: Principal, linked_id: str | None, kind: str):
        self.svc, self.account = svc, account
        self.self_id = linked_id if kind == "user" else None
        self.by_cos: dict[str, Principal] = {}
        self.cos_of: dict[UUID, str] = {}

    @classmethod
    async def load(cls, svc: Services, account: Principal) -> "Homes":
        async with svc.tx(account) as conn:
            row = await conn.fetchrow(
                "select status, external_id, external_kind from app.integrations"
                " where provider = $1",
                PROVIDER,
            )
        linked_id = None if row is None else cast(str | None, row["external_id"])
        homes = cls(svc, account, linked_id, await kind_of(svc, account, row))
        if homes.self_id:
            homes.by_cos[homes.self_id] = account
            homes.cos_of[account.user_id] = homes.self_id
        for p in await profiles.listed(svc.pool, account):
            pid = UUID(p["id"])
            homes.by_cos.setdefault(
                p["career_os_user_id"], Principal(pid, p["name"], account.expires_at)
            )
            homes.cos_of[pid] = p["career_os_user_id"]
        return homes

    def of(self, cos_user_id: str | None) -> Principal | None:
        """The home of a Career-OS user's applications, if it exists yet."""
        if not cos_user_id:
            return self.account
        return self.by_cos.get(cos_user_id)

    async def make(self, cos_user: dict[str, Any]) -> Principal:
        """The home of a Career-OS user's applications, its profile made on first use."""
        cos_id = str(cos_user.get("id") or "")
        found = self.of(cos_id)
        if found is not None:
            return found
        name = str(cos_user.get("name") or "")
        pid = await profiles.ensure(self.svc.pool, self.account, cos_id, name)
        made = Principal(pid, name or cos_id, self.account.expires_at)
        self.by_cos[cos_id], self.cos_of[pid] = made, cos_id
        return made


@router.get("/career-os/applications")
async def search(
    svc: SvcDep,
    account: AccountDep,
    user: UserDep,
    user_id: str | None = Query(default=None, pattern=COS_ID),
    q: str | None = Query(default=None, max_length=200),
    stage: Literal["applied", "interview", "offer", "rejected"] | None = None,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, Any]:
    """The Career-OS applications the link reads (company, role, stage, whose), each marked
    with whether it was pulled here and whether it changed in Career-OS since. Switched to a
    profile: that profile's Career-OS user only. Otherwise `user_id` (from `/career-os/users`)
    narrows to one Career-OS user."""
    client = client_of(svc)
    async with svc.tx(account) as conn:
        token = await token_of(svc, conn, account.user_id)
    homes = await Homes.load(svc, account)
    if user is not account:
        user_id = homes.cos_of.get(user.user_id) or user_id
    params = {k: v for k, v in (("user_id", user_id), ("q", q), ("stage", stage),
                                ("cursor", cursor)) if v}  # fmt: skip
    params["limit"] = str(limit)
    try:
        found = await client.search(token, params)
    except CareerOsError as e:
        raise await remote_failed(svc, account, e) from e
    raws = [r for r in (obj(e) for e in cast(list[object], found.get("items") or [])) if r]
    # What was pulled, from each home the results belong to.
    pulled: dict[tuple[UUID, str], Any] = {}
    wanted = {h.user_id: h for r in raws if (h := homes.of(obj(r.get("user")).get("id")))}
    for home in wanted.values():
        async with svc.tx(home) as conn:
            for r in await conn.fetch(
                "select id, career_os_application_id, career_os_hash, pulled_at"
                " from app.applications where career_os_application_id is not null"
            ):
                pulled[(home.user_id, r["career_os_application_id"])] = r
    items: list[dict[str, Any]] = []
    for raw in raws:
        item: dict[str, Any] = {k: raw.get(k) for k in (
            "id", "company", "role", "stage", "source_site", "url", "created_at", "updated_at",
            "resume")}  # fmt: skip
        item["user"] = user_out(obj(raw.get("user"))) if raw.get("user") else None
        home = homes.of(obj(raw.get("user")).get("id"))
        item["profile_id"] = None if home is None else str(home.user_id)
        here = None if home is None else pulled.get((home.user_id, str(raw.get("id"))))
        item["pulled"] = (
            None
            if here is None
            else {
                "application_id": str(here["id"]),
                "pulled_at": here["pulled_at"].isoformat(),
                "changed": here["career_os_hash"] != raw.get("content_hash"),
            }
        )
        items.append(item)
    return {"items": items, "next_cursor": found.get("next_cursor")}


@router.get("/career-os/applications/{cos_id}/status")
async def status(cos_id: str, svc: SvcDep, account: AccountDep, user: UserDep) -> dict[str, Any]:
    """Whether a pulled application (of the switched profile) changed in Career-OS since."""
    _check_id(cos_id)
    client = client_of(svc)
    async with svc.tx(account) as conn:
        token = await token_of(svc, conn, account.user_id)
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select career_os_hash, pulled_at from app.applications"
            " where career_os_application_id = $1",
            cos_id,
        )
    if row is None:
        raise HTTPException(404, "that application was not pulled")
    try:
        detail = await client.application(token, cos_id)
    except CareerOsError as e:
        raise await remote_failed(svc, account, e) from e
    return {
        "changed": detail.get("content_hash") != row["career_os_hash"],
        "pulled_at": row["pulled_at"].isoformat(),
        "updated_at": obj(detail.get("application")).get("updated_at"),
    }


def obj(value: object) -> dict[str, Any]:
    """A JSON object from Career-OS, or an empty one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _check_id(cos_id: str) -> None:
    import re

    if not re.fullmatch(COS_ID, cos_id):
        raise HTTPException(422, "not a Career-OS application id")


@router.post("/career-os/applications/{cos_id}/pull")
async def pull(cos_id: str, svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    """Copy one Career-OS application here with its JD and its resume version (or base
    resume), or pull it again: the pulled fields, JD and resume are replaced, the application
    keeps its id, interviews and a still-valid pairing. It goes to its Career-OS user's home
    (the account, or that user's profile, made now if needed), whichever profile is switched
    to; the response names it (`profile_id`)."""
    _check_id(cos_id)
    client = client_of(svc)
    async with svc.tx(account) as conn:
        token = await token_of(svc, conn, account.user_id)
    try:
        detail = await client.application(token, cos_id)
        try:
            app = PulledApplication.model_validate(detail.get("application") or {})
        except ValidationError as e:
            raise CareerOsError(502, "Career-OS sent an application this app cannot store") from e
        version = (
            detail.get("resume_version") if isinstance(detail.get("resume_version"), dict) else None
        )
        base = detail.get("base_resume") if isinstance(detail.get("base_resume"), dict) else None
        file: ResumeFile | None = None
        if version is not None and version.get("file"):
            file = await client.resume_file(token, cos_id)
            if file is not None and file.content_type not in FILE_TYPES:
                file = None
    except CareerOsError as e:
        raise await remote_failed(svc, account, e) from e
    if app.id != cos_id:
        raise HTTPException(502, "Career-OS sent another application than the one asked for")
    homes = await Homes.load(svc, account)
    user = await homes.make(obj(detail.get("user")))
    uid = user.user_id
    jd = str(obj(detail.get("job_description")).get("text") or "")[:50_000]
    stage = app.stage if app.stage in STAGES else "applied"
    content_hash = str(detail.get("content_hash") or "")
    extract: tuple[UUID, str] | None = None
    async with svc.tx(user) as conn:
        existing = await conn.fetchval(
            "select id from app.applications where career_os_application_id = $1 for update",
            cos_id,
        )
        if existing is None:
            app_id = uuid7()
            await conn.execute(
                "insert into app.applications (id, user_id, company, role, url, source_site,"
                " stage, note, jd_text, career_os_application_id, career_os_hash, pulled_at)"
                " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())",
                app_id, uid, app.company, app.role, app.url, app.source_site, stage, app.note,
                jd, cos_id, content_hash,
            )  # fmt: skip
        else:
            app_id = existing
            await conn.execute(
                "update app.applications set company = $2, role = $3, url = $4,"
                " source_site = $5, stage = $6, note = $7, jd_text = $8, career_os_hash = $9,"
                " pulled_at = now(), updated_at = now() where id = $1",
                app_id, app.company, app.role, app.url, app.source_site, stage, app.note, jd,
                content_hash,
            )  # fmt: skip
        if version is not None:
            extract = await _pull_resume(
                svc, conn, uid, app_id, "tailored", f"resume_version:{version.get('id')}",
                _label("Career-OS resume", version.get("created_at")),
                str(version.get("text") or ""), file,
            )  # fmt: skip
        elif base is not None and str(base.get("text") or "").strip():
            # The Career-OS base resume: the home's base if it has none (or its base is an
            # earlier copy of the same), otherwise this application's own version, so a base
            # made here is never overwritten. Each Career-OS user has its own home, so one
            # user's base never meets another's.
            mine = await conn.fetchrow(
                "select id, career_os_ref from app.resume_versions where kind = 'base'"
            )
            ref = "base:career-os"
            kind = "base" if mine is None or mine["career_os_ref"] == ref else "tailored"
            extract = await _pull_resume(
                svc, conn, uid, app_id, kind, ref,
                _label("Career-OS base resume", base.get("updated_at")),
                str(base["text"]), None,
            )  # fmt: skip
        await enqueue_packs(svc, conn, uid, app_id)
        out = await application_out(svc, conn, uid, app_id, detail=True)
    async with svc.tx(account) as conn:
        await conn.execute(
            "update app.integrations set last_sync_at = now() where provider = $1", PROVIDER
        )
    if extract is not None:
        await svc.queue.send(jobs.resume_extract(uid, *extract))
    assert out is not None
    out["pulled"] = "new" if existing is None else "again"
    out["profile_id"] = str(uid)
    return out


def _label(prefix: str, when: object) -> str:
    day = str(when or "")[:10]
    return f"{prefix} ({day})" if day else prefix


async def _pull_resume(
    svc: Services, conn: Conn, uid: UUID, app_id: UUID, kind: str, ref: str, label: str,
    text: str, file: ResumeFile | None,
) -> tuple[UUID, str] | None:  # fmt: skip
    """Write the pulled resume into this application's pulled version (or the base), sealed.
    Returns what to extract, or None when nothing changed."""
    if not text.strip() and file is None:
        return None
    sha = (resumes.sha256_text(text) if text.strip()
           else hashlib.sha256(file.body).hexdigest())  # type: ignore[union-attr]  # fmt: skip
    if kind == "base":
        current = await conn.fetchrow(
            "select id, content_sha256 from app.resume_versions where kind = 'base'"
        )
    else:
        current = await conn.fetchrow(
            "select id, content_sha256 from app.resume_versions"
            " where application_id = $1 and career_os_ref is not null"
            " order by created_at desc limit 1",
            app_id,
        )
    version = current["id"] if current is not None else uuid7()
    sealed, key_version = await svc.crypto.encrypt(conn, uid, "resume_versions", str(version), text)
    file_key = name_enc = file_type = None
    if file is not None:
        body, _ = await svc.crypto.encrypt_bytes(
            conn, uid, "resume_versions", f"{version}:file", file.body
        )
        name_enc, _ = await svc.crypto.encrypt(
            conn, uid, "resume_versions", f"{version}:file-name", file.filename[:200]
        )
        file_key = resume_file_key(uid, version)
        file_type = file.content_type
        await svc.store.put(file_key, body, "application/octet-stream")
    elif current is not None:
        await svc.store.delete(resume_file_key(uid, version))  # an earlier pull's file
    now = datetime.now(UTC)
    if current is None:
        await conn.execute(
            "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
            " text_enc, key_version, file_s3_key, file_name_enc, file_content_type,"
            " content_sha256, status, career_os_ref, pulled_at)"
            " values ($1, $2, $3, $4, $5, 'career_os', $6, $7, $8, $9, $10, $11, 'extracting',"
            " $12, $13)",
            version, uid, kind, app_id if kind == "tailored" else None, label, sealed,
            key_version, file_key, name_enc, file_type, sha, ref, now,
        )  # fmt: skip
    else:
        await conn.execute(
            "update app.resume_versions set source = 'career_os', label = $2, text_enc = $3,"
            " key_version = $4, document_id = null, file_s3_key = $5, file_name_enc = $6,"
            " file_content_type = $7, content_sha256 = $8, career_os_ref = $9, pulled_at = $10,"
            " status = case when content_sha256 = $8 then status else 'extracting' end,"
            " updated_at = now() where id = $1",
            version, label, sealed, key_version, file_key, name_enc, file_type, sha, ref, now,
        )  # fmt: skip
        if current["content_sha256"] == sha:
            return None
    return version, sha
