"""Email sign-in, with no token (ARCHITECTURE.md, "Email sign-in"; copilot_api.emailauth):

- `POST /auth/email/start {email}`: mails a six-digit code. The answer is the same for every
  address, with or without an account.
- `POST /auth/email/verify {email, code}`: the code for an access token and a refresh token.
- `POST /auth/email/refresh {refresh_token}`: new tokens; the refresh token shown is spent.
- `POST /auth/email/signout {refresh_token}`: revokes the sign-in.

Errors carry a `code` the app shows its own words for: `invalid_email`, `invalid_code`,
`code_expired`, `code_locked`, `rate_limited` (with `retry_after_s`), `invalid_refresh_token`,
`email_taken`, `unavailable`. All are 404 when the stage has no email sign-in.
"""

import logging
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from valkey.exceptions import ValkeyError

from copilot_api.deps import SvcDep
from copilot_api.emailauth import (
    CODE_DIGITS,
    CodeError,
    EmailSignIn,
    RateLimited,
    RefreshError,
    Tokens,
)
from copilot_core.identities import MAX_EMAIL, EmailTaken, normalize_email

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/email")

UNAVAILABLE = (OSError, ValkeyError, asyncpg.PostgresError)
CODE_MESSAGES = {
    "invalid": ("invalid_code", "That code is not right."),
    "expired": ("code_expired", "That code has expired or was already used. Ask for a new one."),
    "locked": ("code_locked", "Too many wrong tries. Ask for a new code."),
}


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=MAX_EMAIL + 10)


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=MAX_EMAIL + 10)
    code: str = Field(max_length=32)


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=512)


def problem(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    headers = {"Retry-After": str(extra["retry_after_s"])} if "retry_after_s" in extra else None
    return HTTPException(status, {"code": code, "message": message, **extra}, headers=headers)


def feature(svc: SvcDep) -> EmailSignIn:
    if svc.email is None:
        raise HTTPException(404, "this server has no email sign-in")
    return svc.email


def address(raw: str) -> str:
    try:
        return normalize_email(raw)
    except ValueError as e:
        raise problem(422, "invalid_email", "That is not an email address.") from e


def client_of(request: Request) -> str:
    """The caller's address, for the per-client limits. The stage's proxy sets
    X-Forwarded-For to the peer it saw (docker/proxy/nginx.conf), replacing anything the
    caller sent; without a proxy (the local stack) it is the peer itself."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def limited(e: RateLimited) -> HTTPException:
    return problem(
        429, "rate_limited", "Too many tries. Wait a while and try again.",
        retry_after_s=e.retry_after_s,
    )  # fmt: skip


def unavailable(e: BaseException) -> HTTPException:
    log.error("email sign-in unavailable: %s: %s", type(e).__name__, e)
    return problem(503, "unavailable", "Sign-in is not available right now. Try again.")


def tokens_out(t: Tokens) -> dict[str, Any]:
    return {
        "access_token": t.access_token,
        "refresh_token": t.refresh_token,
        "token_type": "Bearer",
        "expires_in": t.expires_in,
        "email": t.email,
    }


@router.post("/start")
async def start(body: StartBody, request: Request, svc: SvcDep) -> dict[str, Any]:
    email_sign_in = feature(svc)
    email = address(body.email)
    try:
        await email_sign_in.start(email, client_of(request))
    except RateLimited as e:
        raise limited(e) from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    s = svc.settings
    return {
        "sent": True,
        "code_length": CODE_DIGITS,
        "expires_in_s": s.email_code_ttl_s,
        "resend_after_s": s.email_resend_after_s,
    }


@router.post("/verify")
async def verify(body: VerifyBody, request: Request, svc: SvcDep) -> dict[str, Any]:
    email_sign_in = feature(svc)
    email = address(body.email)
    code = re.sub(r"[\s-]", "", body.code)
    if not re.fullmatch(rf"\d{{{CODE_DIGITS}}}", code):
        raise problem(400, *CODE_MESSAGES["invalid"])
    try:
        tokens = await email_sign_in.verify(email, code, client_of(request))
    except RateLimited as e:
        raise limited(e) from e
    except CodeError as e:
        raise problem(400, *CODE_MESSAGES[e.reason]) from e
    except EmailTaken as e:
        raise problem(409, "email_taken", "This email belongs to another account.") from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    return tokens_out(tokens)


@router.post("/refresh")
async def refresh(body: RefreshBody, svc: SvcDep) -> dict[str, Any]:
    email_sign_in = feature(svc)
    try:
        tokens = await email_sign_in.refresh(body.refresh_token)
    except RefreshError as e:
        raise problem(401, "invalid_refresh_token", "The sign-in has ended. Sign in again.") from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    return tokens_out(tokens)


@router.post("/signout", status_code=204)
async def sign_out(body: RefreshBody, svc: SvcDep) -> Response:
    email_sign_in = feature(svc)
    try:
        await email_sign_in.sign_out(body.refresh_token)
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    return Response(status_code=204)
