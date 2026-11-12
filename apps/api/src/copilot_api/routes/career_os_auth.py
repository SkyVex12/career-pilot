"""Career-OS sign-in (ARCHITECTURE.md, "Career-OS sign-in"; copilot_api.careeros_signin), and
the API's own tokens whichever sign-in made them (copilot_api.tokens):

- `POST /auth/career-os/sign-in {code, code_verifier, redirect_uri}`: the authorization code
  the app got from Career-OS in the browser. With no token, the answer is one of
  - `{status: "signed_in", account: "existing" | "new", access_token, refresh_token, ...}`;
  - `{status: "choose_account", ticket, accounts: [{id, email, methods, created_at}]}`:
    several accounts are linked to that Career-OS account;
  - `{status: "existing_or_new", ticket, verified, account}`: an account here has the same
    email.
  With a token (the caller is signed in another way) it adds Career-OS sign-in to that
  account, which is the same as linking it: `{status: "added"}`.
- `POST /auth/career-os/complete {ticket, choice, account_id?}`: the person's answer.
  `choice: "existing"` carries the token of the account they chose, as proof they can sign in
  to it with its own method: `{status: "added"}`, and the app stays signed in that way.
  `choice: "new"` (after `existing_or_new` only) makes a separate account and signs in to it.
- `POST /auth/token/refresh {refresh_token}` and `POST /auth/token/signout {refresh_token}`:
  for any of the API's refresh tokens. `/auth/email/refresh` and `/auth/email/signout` are the
  same two calls under the names apps from before Career-OS sign-in know.

Errors carry a `code`: `career_os_unavailable`, `invalid_grant`, `invalid_assertion`,
`account_disabled`, `linked_to_another`, `identity_elsewhere`, `wrong_account`,
`proof_required`, `ticket_expired`, `not_a_choice`, `account_gone`, `rate_limited`,
`invalid_refresh_token`, `unavailable`. The Career-OS routes are 404 on a stage without it.
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.careeros_signin import CareerOsSignIn, SignInError
from copilot_api.deps import Services, SvcDep, current_account, services
from copilot_api.emailauth import RateLimited
from copilot_api.routes.email_auth import (
    UNAVAILABLE,
    RefreshBody,
    client_of,
    limited,
    problem,
    tokens_out,
    unavailable,
)
from copilot_api.tokens import RefreshError, TokenService
from copilot_core.auth import Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


class SignInBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=200)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=200)


class CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=20, max_length=200)
    choice: Literal["existing", "new"]
    account_id: str | None = Field(default=None, max_length=64)


def feature(svc: Services) -> CareerOsSignIn:
    if svc.cos_sign_in is None:
        raise HTTPException(404, "this server has no Career-OS sign-in")
    return svc.cos_sign_in


def tokens_of(svc: Services) -> TokenService:
    if svc.tokens is None:
        raise HTTPException(404, "this server issues no tokens of its own")
    return svc.tokens


async def maybe_account(
    svc: Annotated[Services, Depends(services)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """The signed-in account when the request carries a token; a bad token is still a 401."""
    if not authorization:
        return None
    return await current_account(svc, authorization)


MaybeAccount = Annotated[Principal | None, Depends(maybe_account)]


def refused(e: SignInError) -> HTTPException:
    extra = {"retry_after_s": e.retry_after_s} if e.retry_after_s else {}
    return problem(e.status, e.code, e.message, **extra)


@router.post("/career-os/sign-in")
async def sign_in(
    body: SignInBody, request: Request, svc: SvcDep, account: MaybeAccount
) -> dict[str, Any]:
    cos = feature(svc)
    try:
        return await cos.sign_in(
            body.code, body.code_verifier, body.redirect_uri, client_of(request), account
        )
    except SignInError as e:
        raise refused(e) from e
    except RateLimited as e:
        raise limited(e) from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e


@router.post("/career-os/complete")
async def complete(
    body: CompleteBody, request: Request, svc: SvcDep, account: MaybeAccount
) -> dict[str, Any]:
    cos = feature(svc)
    try:
        return await cos.complete(
            body.ticket, body.choice, body.account_id, client_of(request), account
        )
    except SignInError as e:
        raise refused(e) from e
    except RateLimited as e:
        raise limited(e) from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e


@router.post("/token/refresh")
async def refresh(body: RefreshBody, svc: SvcDep) -> dict[str, Any]:
    tokens = tokens_of(svc)
    try:
        fresh = await tokens.refresh(body.refresh_token)
    except RefreshError as e:
        raise problem(401, "invalid_refresh_token", "The sign-in has ended. Sign in again.") from e
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    return {**tokens_out(fresh), "name": fresh.name}


@router.post("/token/signout", status_code=204)
async def sign_out(body: RefreshBody, svc: SvcDep) -> Response:
    tokens = tokens_of(svc)
    try:
        await tokens.sign_out(body.refresh_token)
    except UNAVAILABLE as e:
        raise unavailable(e) from e
    return Response(status_code=204)
