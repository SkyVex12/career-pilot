"""Test support for the API (apps/api/tests): settings for the local stack and `Api`, the
client and checks the tests use."""

import base64
import copy
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from copilot_api.config import ApiSettings
from copilot_api.deps import Services
from copilot_api.mailer import MemoryMailer
from copilot_core.auth import Principal, StaticVerifier
from copilot_core.careeros import CareerOsClient
from copilot_core.jobs import MemoryJobQueue
from copilot_core.teststack import (
    S3_ENDPOINT,
    VALKEY_URL,
)


def api_settings(**over: Any) -> ApiSettings:
    base: dict[str, Any] = {
        "env": "test",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "valkey_url": VALKEY_URL,
        "max_document_bytes": 64 * 1024,
    }
    base.update(over)
    return ApiSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@dataclass
class Api:
    svc: Services
    client: httpx2.AsyncClient
    admin: asyncpg.Connection
    jobs: MemoryJobQueue
    verifier: StaticVerifier
    mailer: MemoryMailer = field(default_factory=MemoryMailer)

    def user(self, name: str = "Alex") -> tuple[uuid.UUID, dict[str, str]]:
        """A new user's id and auth headers (their row is created on first request)."""
        uid = uuid.uuid4()
        token = f"token-{uid}"
        self.verifier.principals[token] = Principal(uid, name, None, email=f"{uid.hex}@example.com")
        return uid, {"Authorization": f"Bearer {token}"}

    def take(self, job_type: str) -> list[dict[str, Any]]:
        out = [m for m in self.jobs.sent if m["type"] == job_type]
        self.jobs.sent = [m for m in self.jobs.sent if m not in out]
        return out


CONTRACT = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "career_os_contract.json"


class FakeCareerOs:
    """Career-OS's /v1/career-pilot API as the contract fixture shapes it, in memory, for an
    httpx2 MockTransport. Three Career-OS users and an admin, `a1`, linked to `u1` and `u2`
    (`admin_users`); a user's token reads that user's applications, the admin's reads its
    users'. Tests change `apps` to make an application change in Career-OS, and `admin_users`
    to link or unlink a user."""

    def __init__(self) -> None:
        contract = json.loads(CONTRACT.read_text("utf-8"))
        self.contract = contract
        detail = contract["application"]["response"]
        self.users = {"u1": "Alex Doe", "u2": "Blair Roe", "u3": "Casey Poe"}
        self.admins = {"a1": "Dana Admin"}
        self.admin_users: dict[str, list[str]] = {"a1": ["u1", "u2"]}
        self.codes: dict[str, str] = {}
        self.tokens: dict[str, str] = {}  # token -> Career-OS account (user or admin)
        self.revoked: set[str] = set()
        self.down = False
        self.calls: list[str] = []
        mine = copy.deepcopy(detail)
        mine["user"] = {"id": "u1", "name": "Alex Doe"}
        other = copy.deepcopy(detail)
        other["application"].update(id="app_other_1", company="Initech", role="SRE")
        other["user"] = {"id": "u2", "name": "Blair Roe"}
        other["resume_version"] = None
        other["base_resume"] = {"text": "Blair Roe\nSRE\n- Ran the on-call rota\n",
                                "updated_at": "2026-09-21T08:00:00"}  # fmt: skip
        third = copy.deepcopy(detail)
        third["application"].update(id="app_hooli_1", company="Hooli", role="Data Engineer")
        third["user"] = {"id": "u3", "name": "Casey Poe"}
        self.apps: dict[str, dict[str, dict[str, Any]]] = {
            "u1": {"app_acme_1": mine},
            "u2": {"app_other_1": other},
            "u3": {"app_hooli_1": third},
        }
        self.pdf = b"%PDF-1.4 fake resume for the test\n"
        # Sign-in (/oauth/*): the address Career-OS has for each account (never verified,
        # unless a test says so), who is disabled, the signing key, and the codes a browser
        # approval made. `assert_with` lets a test put anything in place of the assertion.
        self.emails: dict[str, str] = {
            "u1": "alex@example.com", "u2": "blair@example.com", "u3": "casey@example.com",
            "a1": "dana@example.com",
        }  # fmt: skip
        self.email_verified = False
        self.disabled: set[str] = set()
        self.sign_in_key = Ed25519PrivateKey.generate()
        self.sign_in_codes: dict[str, tuple[str, str, str]] = {}
        self.assert_with: Callable[[str], str] | None = None

    # --- Sign-in ------------------------------------------------------------------------------

    REDIRECT = "http://127.0.0.1:53682/callback"

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def public_key(self, key: Ed25519PrivateKey | None = None) -> tuple[str, str]:
        """(kid, public key in base64url) of the signing key, as `/oauth/keys` gives them."""
        raw = (
            (key or self.sign_in_key)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        return hashlib.sha256(raw).hexdigest()[:16], self._b64(raw)

    def approve(self, account: str, redirect: str = REDIRECT) -> tuple[str, str]:
        """The person signed in on Career-OS's site as `account` and approved: the
        authorization code the app gets on its redirect, and the app's PKCE verifier."""
        verifier = self._b64(os.urandom(48))
        challenge = self._b64(hashlib.sha256(verifier.encode()).digest())
        code = f"cos-code-{uuid.uuid4().hex}"
        self.sign_in_codes[code] = (account, challenge, redirect)
        return code, verifier

    def assertion(
        self, account: str, *, key: Ed25519PrivateKey | None = None, kid: str | None = None,
        now: float | None = None, **claims: Any,
    ) -> str:  # fmt: skip
        """A sign-in assertion as Career-OS signs it; the keyword arguments change it."""
        is_admin = account in self.admins
        kind = "admin" if is_admin else "user"
        issued = int(time.time() if now is None else now)
        body: dict[str, Any] = {
            "iss": "career-os", "aud": "career-pilot", "sub": f"{kind}:{account}",
            "iat": issued, "exp": issued + 60, "jti": uuid.uuid4().hex,
            "principal_kind": kind, "principal_id": account,
            "account_type": "admin" if is_admin else "user",
            "name": (self.admins if is_admin else self.users).get(account, ""),
            "email": self.emails.get(account, ""), "email_verified": self.email_verified,
            **claims,
        }  # fmt: skip
        return jwt.encode(
            body, key or self.sign_in_key, algorithm="EdDSA",
            headers={"kid": kid or self.public_key()[0]},
        )  # fmt: skip

    def _sign_in(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if path == "/oauth/keys":
            kid, public = self.public_key()
            return httpx2.Response(
                200, json={"keys": [{"kid": kid, "alg": "EdDSA", "public_key": public}]}
            )
        body = json.loads(request.content)
        found = self.sign_in_codes.pop(body.get("code", ""), None)  # single use
        if found is None or body.get("client_id") != "career-pilot":
            return httpx2.Response(400, json={"detail": "invalid_grant"})
        account, challenge, redirect = found
        verifier = str(body.get("code_verifier", ""))
        if (
            self._b64(hashlib.sha256(verifier.encode()).digest()) != challenge
            or body.get("redirect_uri") != redirect
        ):
            return httpx2.Response(400, json={"detail": "invalid_grant"})
        if account in self.disabled:
            return httpx2.Response(403, json={"detail": "account_disabled"})
        out = copy.deepcopy(self.contract["sign_in"]["token"]["response"])
        make = self.assert_with or self.assertion
        out.update(assertion=make(account), link_code=self.code_for(account))
        return httpx2.Response(200, json=out)

    def scope(self, account: str) -> list[str]:
        """The users an account's token reads, as Career-OS's `applications_query` does."""
        return list(self.admin_users.get(account, [])) if account in self.admins else [account]

    def code_for(self, user: str) -> str:
        code = f"CODE-{user.upper()}-{len(self.codes):02d}"
        self.codes[code] = user
        return code

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def client(self) -> "CareerOsClient":
        return CareerOsClient("http://career-os.test", transport=self.transport())

    @staticmethod
    def hash_of(detail: dict[str, Any]) -> str:
        body = {k: v for k, v in detail.items() if k != "content_hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("career-os is down")
        path = request.url.path.removeprefix("/v1/career-pilot")
        self.calls.append(f"{request.method} {path}")
        if path in ("/oauth/keys", "/oauth/token"):
            return self._sign_in(request, path)
        if request.method == "POST" and path == "/link":
            body = json.loads(request.content)
            user = self.codes.pop(body["code"], None)
            if user is None or user in self.disabled:
                return httpx2.Response(401, json={"detail": "invalid or expired code"})
            token = f"cpt_{uuid.uuid4().hex}"
            self.tokens[token] = user
            out = copy.deepcopy(self.contract["link"]["response"])
            is_admin = user in self.admins
            out.update(
                token=token,
                account_type="admin" if is_admin else "user",
                user={"id": user, "name": (self.admins if is_admin else self.users)[user]},
            )
            return httpx2.Response(201, json=out)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        account = self.tokens.get(token)
        if account is None or token in self.revoked:
            return httpx2.Response(401, json={"detail": "invalid token"})
        if account in self.disabled:
            return httpx2.Response(401, json={"detail": "Account disabled"})
        if request.method == "DELETE" and path == "/link":
            self.revoked.add(token)
            return httpx2.Response(204)
        scope = self.scope(account)
        if path == "/me":
            is_admin = account in self.admins
            me = copy.deepcopy(self.contract["me"])
            me.update(user_id=account, account_type="admin" if is_admin else "user",
                      name=(self.admins if is_admin else self.users)[account])  # fmt: skip
            return httpx2.Response(200, json=me)
        if path == "/users":
            users = sorted(({"id": u, "name": self.users[u]} for u in scope),
                           key=lambda u: u["name"])  # fmt: skip
            return httpx2.Response(200, json={"items": users})
        apps = {k: v for u in scope for k, v in self.apps.get(u, {}).items()}
        if path == "/applications":
            wanted = request.url.params.get("user_id")
            if wanted is not None:
                if wanted not in scope:
                    return httpx2.Response(404, json={"detail": "User not found"})
                apps = dict(self.apps.get(wanted, {}))
            q = (request.url.params.get("q") or "").lower()
            items: list[dict[str, Any]] = []
            for d in apps.values():
                a = d["application"]
                if q and q not in (a["company"] + " " + a["role"]).lower():
                    continue
                items.append({
                    **{k: a[k] for k in ("id", "company", "role", "stage", "source_site", "url",
                                         "created_at", "updated_at")},
                    "user": d["user"],
                    "resume": "tailored" if d["resume_version"] else (
                        "base" if d["base_resume"] else "none"),
                    "content_hash": self.hash_of(d),
                })  # fmt: skip
            return httpx2.Response(200, json={"items": items, "next_cursor": None})
        parts = path.strip("/").split("/")
        if parts[0] == "applications" and len(parts) >= 2:
            d = apps.get(parts[1])
            if d is None:
                return httpx2.Response(404, json={"detail": "Application not found"})
            if len(parts) == 3 and parts[2] == "resume-file":
                if not d["resume_version"] or not d["resume_version"].get("file"):
                    return httpx2.Response(404, json={"detail": "no file"})
                return httpx2.Response(200, content=self.pdf, headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="resume.pdf"'})  # fmt: skip
            return httpx2.Response(200, json={**d, "content_hash": self.hash_of(d)})
        return httpx2.Response(404, json={"detail": "Not Found"})
