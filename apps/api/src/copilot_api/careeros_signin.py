"""Career-OS sign-in (ARCHITECTURE.md, "Career-OS sign-in"): a third way in, beside Google and
an emailed code.

The person signs in on Career-OS's own HTTPS site, in their browser, and approves "Sign in to
Career Pilot" there. The desktop app gets an authorization code on a loopback redirect and
hands it here with its PKCE verifier; neither the app nor this API ever sees a Career-OS
password. This API exchanges the code with Career-OS, server to server, for

- an **assertion**: who signed in, signed by Career-OS for this app, for a minute
  (`copilot_core.careeros.AssertionVerifier`), accepted once;
- a **link code**: the one-time code of today's Career-OS link, so the approval also links the
  account, with exactly a link's access.

Then it issues its own tokens, as email sign-in does (`copilot_api.tokens`).

**Which account.** The identity is `('career_os', '<kind>:<id>')` in `app.identities`. In
this order:

1. That identity has signed in before: its account.
2. Exactly one account has an active Career-OS link to that Career-OS account: that account,
   and the identity is recorded for it, whatever its email is.
3. Several accounts are linked to it: none is picked. The person chooses one and proves it by
   signing in to it with its own method (a *ticket*, below).
4. No link, but Career-OS's email is the verified address of an account here: nothing is
   attached silently. The person either proves that account the same way, or makes a separate
   one. Career-OS does not verify addresses, so in practice its email is a hint: the choice
   is offered, no detail of the account is shown, and whichever account the person proves is
   theirs to add the sign-in to.
5. Nothing matches: a new account, the identity and the link, in one step.

**Refused**, with words the app shows: an account already linked to a different Career-OS
account (or with another Career-OS sign-in), and a Career-OS account that is disabled or gone.

**Never**: two accounts are not merged, no data moves between accounts or profiles, and an
identity row is never pointed at another account (rows are only inserted, and removed when the
account unlinks Career-OS).

**Tickets.** Cases 3 and 4 wait for the person. What was learned (who signed in, the link
code) is kept for a few minutes in Valkey, sealed with a key made from the ticket itself, which
only the app holds: Valkey, or a dump of it, gives neither a link code nor a name.

Every decision is logged with ids only: no email, no name.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from copilot_api import links
from copilot_api.config import ApiSettings
from copilot_api.emailauth import Limit, hit
from copilot_api.tokens import RefreshError, Tokens, TokenService, sha
from copilot_core import profiles
from copilot_core.auth import CAREER_OS, Principal
from copilot_core.careeros import (
    ASSERTION_MAX_LIFE_S,
    SIGN_IN_CLIENT_ID,
    AssertionVerifier,
    CareerOsClient,
    CareerOsError,
    CareerOsPerson,
    InvalidAssertion,
    principal_kind,
)
from copilot_core.crypto import UserCrypto
from copilot_core.db import sign_in_tx, user_tx
from copilot_core.ids import uuid7
from copilot_core.valkey_client import Valkey, text

log = logging.getLogger(__name__)

# A link code works for ten minutes on Career-OS; a ticket ends before it does.
TICKET_TTL_S = 8 * 60
CLIENT_LIMIT = (Limit(20, 15 * 60),)
# The address of an account made by a Career-OS sign-in, until Career-OS's own replaces it
# (`own_address`). It stays only when Career-OS has none, or another account has it:
# `app.accounts.email` is unique.
PLACEHOLDER_DOMAIN = "career-os.invalid"
PLACEHOLDER_DOMAINS = (PLACEHOLDER_DOMAIN, "profiles.invalid")

MESSAGES = {
    "career_os_unavailable": (
        503, "HubRuby could not be reached. Try again, or sign in another way."),
    "invalid_grant": (
        400, "That HubRuby sign-in has expired or was already used. Start it again."),
    "invalid_assertion": (
        502, "HubRuby's answer could not be verified. Try again, or sign in another way."),
    "account_disabled": (403, "This HubRuby account is disabled or no longer exists."),
    "linked_to_another": (
        409,
        "That CoRuby account is already linked to a different HubRuby account. Disconnect it "
        "there first (Settings, HubRuby), or use another account.",
    ),
    "identity_elsewhere": (
        409, "This HubRuby account already signs in to another CoRuby account."),
    "wrong_account": (409, "That is not the account you chose. Sign in to the one you chose."),
    "proof_required": (
        401, "Sign in to that account with its own method first, then try again."),
    "ticket_expired": (400, "That took too long. Start the HubRuby sign-in again."),
    "not_a_choice": (400, "That is not one of the choices for this sign-in."),
    "account_gone": (409, "That CoRuby account no longer exists."),
}  # fmt: skip


class SignInError(Exception):
    """A sign-in step refused, with the code the app shows its own words for."""

    def __init__(self, code: str, retry_after_s: int | None = None) -> None:
        self.status, self.message = MESSAGES[code]
        super().__init__(self.message)
        self.code = code
        self.retry_after_s = retry_after_s


def mask_email(email: str | None) -> str | None:
    """`a•••@g•••.com`: enough to tell one's accounts apart, not enough to learn an address.
    None for an account without an address of its own."""
    if not email or "@" not in email or email.rsplit("@", 1)[1] in PLACEHOLDER_DOMAINS:
        return None
    local, domain = email.rsplit("@", 1)
    name, dot, tld = domain.rpartition(".")
    return f"{local[:1]}•••@{(name or tld)[:1]}•••{dot}{tld if name else ''}"


def placeholder_email(user_id: UUID) -> str:
    return f"career-os-{user_id}@{PLACEHOLDER_DOMAIN}"


async def own_address(conn: Any, user_id: UUID, email: str | None) -> bool:
    """Make Career-OS's address the account's own, in the account's transaction, when the
    account still has the placeholder: as `app.accounts.email`, and as an email sign-in, so a
    guest-seat invite finds the account and an email-code or Google sign-in with that address
    lands in it ("the same address is the same account"). Career-OS's addresses are trusted
    (the owner's decision, 2026-11-17). An account with a real address keeps it, and an
    address another account has is left to that account. True when the address was set."""
    address = (email or "").strip().lower()
    if not 3 <= len(address) <= 320 or "@" not in address or address.endswith(".invalid"):
        return False
    current = await conn.fetchval("select email from app.accounts where id = $1", user_id)
    if current is None or not str(current).endswith(f"@{PLACEHOLDER_DOMAIN}"):
        return False
    try:
        async with conn.transaction():  # a savepoint: another account may have the address
            await conn.execute("update app.accounts set email = $2 where id = $1", user_id, address)
            await conn.execute(
                "insert into app.account_identities (provider, subject, account_id, email)"
                " values ('email', $1, $2, $1)",
                address, user_id,
            )  # fmt: skip
    except asyncpg.UniqueViolationError:
        log.info("career-os sign-in: user %s keeps its placeholder; the address is taken", user_id)
        return False
    log.info("career-os sign-in: user %s has its HubRuby address now", user_id)
    return True


def tokens_out(t: Tokens) -> dict[str, Any]:
    return {
        "access_token": t.access_token,
        "refresh_token": t.refresh_token,
        "token_type": "Bearer",
        "expires_in": t.expires_in,
        "name": t.name,
    }


class CareerOsSignIn:
    def __init__(
        self, settings: ApiSettings, pool: asyncpg.Pool, valkey: Valkey, client: CareerOsClient,
        crypto: UserCrypto, tokens: TokenService, assertions: AssertionVerifier | None = None,
    ) -> None:  # fmt: skip
        if not settings.career_os_site_url:
            raise RuntimeError("COPILOT_CAREER_OS_SIGN_IN needs COPILOT_CAREER_OS_SITE_URL")
        site = settings.career_os_site_url.rstrip("/")
        if not site.startswith("https://") and settings.env not in ("dev", "test"):
            raise RuntimeError("COPILOT_CAREER_OS_SITE_URL must be an https:// URL")
        self.settings = settings
        self.pool = pool
        self.valkey = valkey
        self.client = client
        self.crypto = crypto
        self.tokens = tokens
        self.assertions = assertions or AssertionVerifier(client)
        self.authorize_url = f"{site}/career-pilot/authorize"
        self.limit = CLIENT_LIMIT
        # A refresh asks Career-OS whether the account was disabled since.
        tokens.checks[CAREER_OS] = self.still_enabled

    def option(self) -> dict[str, str]:
        """What `GET /auth/config` tells the app: where the person signs in."""
        return {"authorize_url": self.authorize_url, "client_id": SIGN_IN_CLIENT_ID}

    # --- Career-OS ----------------------------------------------------------------------------

    async def _exchange(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> tuple[CareerOsPerson, str]:
        """The authorization code for who signed in and their link code."""
        try:
            data = await self.client.sign_in_token(code, code_verifier, redirect_uri)
            person = await self.assertions.verify(str(data["assertion"]))
        except CareerOsError as e:
            if e.status in (400, 401, 404, 410, 422):
                raise SignInError("invalid_grant") from e
            if e.status == 403:
                log.info("career-os sign-in: refused, the HubRuby account is disabled")
                raise SignInError("account_disabled") from e
            if e.status == 429:
                raise SignInError("career_os_unavailable", retry_after_s=60) from e
            log.warning("career-os sign-in: HubRuby unavailable (%s)", e.status)
            raise SignInError("career_os_unavailable") from e
        except InvalidAssertion as e:
            log.warning("career-os sign-in: assertion refused: %s", e)
            raise SignInError("invalid_assertion") from e
        # An assertion is accepted once, however long it still has to live.
        fresh = await self.valkey.set(
            f"auth:cos:jti:{sha(person.jti)[:32]}", 1, nx=True, ex=ASSERTION_MAX_LIFE_S + 60
        )
        if not fresh:
            log.warning("career-os sign-in: assertion replayed (principal %s)", person.subject)
            raise SignInError("invalid_assertion")
        return person, str(data["link_code"])

    async def _link(self, link_code: str, label: str, person: CareerOsPerson) -> dict[str, Any]:
        """Exchange the link code: today's link, for the Career-OS account that signed in."""
        try:
            linked = await self.client.link(link_code, label)
        except CareerOsError as e:
            if e.status in (400, 401, 404, 410):
                raise SignInError("ticket_expired") from e
            raise SignInError("career_os_unavailable") from e
        user: dict[str, Any] = linked["user"]
        if str(user.get("id")) != person.id or (
            principal_kind(links.account_type(linked)) != person.kind
        ):
            # Not the account the assertion named: never store it.
            await self._revoke(str(linked["token"]))
            log.error("career-os sign-in: the link is not principal %s's", person.subject)
            raise SignInError("invalid_assertion")
        return linked

    async def _revoke(self, token: str) -> None:
        try:
            await self.client.revoke(token)
        except CareerOsError:
            log.warning("career-os sign-in: could not revoke an unused link token")

    async def still_enabled(self, user_id: UUID) -> bool:
        """False once Career-OS says the linked account is disabled. Anything else, Career-OS
        being down included, leaves the sign-in alone."""
        async with user_tx(self.pool, user_id) as conn:
            token = await links.token_of(self.crypto, conn, user_id)
        if token is None:
            return True
        try:
            await self.client.me(token)
        except CareerOsError as e:
            if e.status == 401 and e.message == "Account disabled":
                log.info("career-os sign-in: ended for user %s, the account is disabled", user_id)
                return False
        return True

    # --- Lookups before the user is known (migrations 0014, 0015) -----------------------------

    async def _identity(self, subject: str) -> UUID | None:
        async with sign_in_tx(self.pool, f"{CAREER_OS}:{subject}") as conn:
            return await conn.fetchval(
                "select user_id from app.identities where provider = $1 and subject = $2",
                CAREER_OS, subject,
            )  # fmt: skip

    async def _linked(self, person: CareerOsPerson) -> list[UUID]:
        """The accounts with an active link to this Career-OS account, oldest link first."""
        async with sign_in_tx(self.pool, f"career_os_link:{person.subject}") as conn:
            rows = await conn.fetch(
                "select user_id from app.integrations"
                " where provider = $1 and status = 'active' and external_id = $2"
                "   and (case when external_kind in ('admin', 'super_admin') then 'admin'"
                "        else 'user' end) = $3"
                " order by connected_at nulls first, user_id",
                links.PROVIDER, person.id, person.kind,
            )  # fmt: skip
        return [r["user_id"] for r in rows]

    async def _by_email(self, email: str) -> list[UUID]:
        """The accounts whose Google or email sign-in verified this address."""
        async with sign_in_tx(self.pool, f"email:{email}") as conn:
            rows = await conn.fetch(
                "select user_id, min(created_at) as first from app.identities"
                " where email = $1 and provider in ('google', 'email')"
                " group by user_id order by first, user_id",
                email,
            )
        return [r["user_id"] for r in rows]

    async def _summary(self, user_id: UUID) -> dict[str, Any] | None:
        """An account as the choice screen shows it: masked email, methods, created date."""
        async with user_tx(self.pool, user_id) as conn:
            row = await conn.fetchrow(
                "select email, created_at from app.accounts where id = $1",
                user_id,
            )
            if row is None:
                return None
            methods = await conn.fetch(
                "select distinct provider from app.identities where provider <> $1"
                " order by provider",
                CAREER_OS,
            )
        created: datetime = row["created_at"]
        return {
            "email": mask_email(row["email"]),
            "methods": [str(m["provider"]) for m in methods],
            "created_at": created.date().isoformat(),
        }

    # --- Attaching the sign-in to an account --------------------------------------------------

    async def _check(self, user_id: UUID, person: CareerOsPerson) -> tuple[str, str]:
        """Refuse an account that belongs to another Career-OS account, before anything is
        written or exchanged. Returns the account's (email, display name)."""
        async with user_tx(self.pool, user_id) as conn:
            account = await conn.fetchrow(
                "select email, display_name,status from app.accounts where id = $1",
                user_id,
            )
            if account is None:
                raise SignInError("account_gone")
            if account["status"] != "active":
                raise SignInError("account_disabled")
            mine = await conn.fetchval(
                "select subject from app.identities where provider = $1", CAREER_OS
            )
            link = await conn.fetchrow(
                "select status, external_id, external_kind from app.integrations"
                " where provider = $1",
                links.PROVIDER,
            )
        linked_to = (
            None
            if link is None or link["status"] != "active" or not link["external_id"]
            else f"{principal_kind(link['external_kind'])}:{link['external_id']}"
        )
        if (mine is not None and mine != person.subject) or (
            linked_to is not None and linked_to != person.subject
        ):
            log.info(
                "career-os sign-in: refused, user %s belongs to another principal than %s",
                user_id, person.subject,
            )  # fmt: skip
            raise SignInError("linked_to_another")
        owner = await self._identity(person.subject)
        if owner is not None and owner != user_id:
            log.info(
                "career-os sign-in: refused for user %s, principal %s signs in to user %s",
                user_id, person.subject, owner,
            )  # fmt: skip
            raise SignInError("identity_elsewhere")
        return str(account["email"]), str(account["display_name"] or "")

    async def _attach(self, user_id: UUID, person: CareerOsPerson, link_code: str) -> str:
        """Record the identity for an existing account and make its link. Nothing is written
        unless all of it is. Returns the account's display name."""
        email, name = await self._check(user_id, person)
        shown = mask_email(email)
        label = f"CoRuby ({email})" if shown else "CoRuby (HubRuby sign-in)"
        linked = await self._link(link_code, label, person)
        await self._write(user_id, person, linked, new_account=False)
        return name or person.name or "HubRuby account"

    async def _create(self, person: CareerOsPerson, link_code: str) -> tuple[UUID, str]:
        """A new account for this Career-OS account: the users row, the identity and the link
        together. Its address is Career-OS's (`own_address`)."""
        user_id = uuid7()
        linked = await self._link(link_code, "CoRuby (HubRuby sign-in)", person)
        await self._write(user_id, person, linked, new_account=True)
        return user_id, person.name or "HubRuby account"

    async def _write(
        self, user_id: UUID, person: CareerOsPerson, linked: dict[str, Any], new_account: bool
    ) -> None:
        try:
            async with user_tx(self.pool, user_id) as conn:
                if new_account:
                    await conn.execute(
                        "insert into app.accounts (id, email, display_name) values ($1, $2, $3)",
                        user_id, placeholder_email(user_id), person.name or "HubRuby account",
                    )  # fmt: skip
                # Inserted, never updated: an identity that exists keeps its account.
                await conn.execute(
                    "insert into app.account_identities (provider, subject, account_id, email)"
                    " values ($1, $2, $3, null) on conflict (provider, realm, subject) do nothing",
                    CAREER_OS, person.subject, user_id,
                )  # fmt: skip
                owner = await conn.fetchval(
                    "select user_id from app.identities where provider = $1 and subject = $2",
                    CAREER_OS, person.subject,
                )  # fmt: skip
                if owner != user_id:  # another account recorded it a moment ago
                    raise SignInError("identity_elsewhere")
                await links.write(self.crypto, conn, user_id, linked)
                await own_address(conn, user_id, person.email)
        except asyncpg.UniqueViolationError as e:
            # The account got another Career-OS sign-in a moment ago (identities_one_career_os).
            await self._revoke(str(linked["token"]))
            raise SignInError("linked_to_another") from e
        except Exception:
            # Never leave a token on Career-OS that no account holds.
            await self._revoke(str(linked["token"]))
            raise
        if person.kind == "user":
            # The person's workspace: this account's own row, unless another account has one.
            await profiles.claim_me(self.pool, Principal(user_id, person.name, None), person.id)

    async def _signed_in(
        self, user_id: UUID, name: str, person: CareerOsPerson, account: str
    ) -> dict[str, Any]:
        try:
            issued = await self.tokens.issue(
                user_id, name, provider=CAREER_OS, subject=person.subject
            )
        except RefreshError as error:
            raise SignInError("account_disabled") from error
        return {"status": "signed_in", "account": account, **tokens_out(issued)}

    # --- Tickets ------------------------------------------------------------------------------

    @staticmethod
    def _ticket_at(ticket: str) -> str:
        return f"auth:cos:ticket:{sha(ticket)[:32]}"

    @staticmethod
    def _ticket_key(ticket: str) -> AESGCM:
        return AESGCM(hashlib.sha256(b"copilot career-os ticket\0" + ticket.encode()).digest())

    async def _keep(self, payload: dict[str, Any]) -> str:
        ticket = secrets.token_urlsafe(32)
        nonce = os.urandom(12)
        sealed = self._ticket_key(ticket).encrypt(nonce, json.dumps(payload).encode(), None)
        await self.valkey.set(
            self._ticket_at(ticket), base64.b64encode(nonce + sealed).decode(), ex=TICKET_TTL_S
        )
        return ticket

    async def _open(self, ticket: str) -> dict[str, Any]:
        raw = await self.valkey.get(self._ticket_at(ticket))
        if raw is None:
            raise SignInError("ticket_expired")
        try:
            blob = base64.b64decode(text(raw))
            opened = self._ticket_key(ticket).decrypt(blob[:12], blob[12:], None)
        except (ValueError, InvalidTag) as e:
            raise SignInError("ticket_expired") from e
        payload: dict[str, Any] = json.loads(opened)
        return payload

    # --- The two calls ------------------------------------------------------------------------

    async def sign_in(
        self, code: str, code_verifier: str, redirect_uri: str, client: str,
        account: Principal | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """The app's authorization code for a sign-in, or for a choice the person has to make.
        `account`: the caller is signed in another way and adds Career-OS sign-in to that
        account, which is the same as linking it."""
        await hit(self.valkey, "cos:client", client, self.limit)
        person, link_code = await self._exchange(code, code_verifier, redirect_uri)
        if account is not None:
            await self._attach(account.user_id, person, link_code)
            log.info(
                "career-os sign-in: added to user %s (principal %s)",
                account.user_id, person.subject,
            )  # fmt: skip
            return {"status": "added"}

        known = await self._identity(person.subject)
        if known is not None:
            name = await self._attach(known, person, link_code)
            log.info("career-os sign-in: case 1, user %s (principal %s)", known, person.subject)
            return await self._signed_in(known, name, person, "existing")

        linked = await self._linked(person)
        if len(linked) == 1:
            name = await self._attach(linked[0], person, link_code)
            log.info(
                "career-os sign-in: case 2, user %s is the one linked to principal %s",
                linked[0], person.subject,
            )  # fmt: skip
            return await self._signed_in(linked[0], name, person, "existing")
        if len(linked) > 1:
            return await self._ask_which(person, link_code, linked)

        matches = await self._by_email(person.email) if person.email else []
        if matches:
            return await self._ask_existing_or_new(person, link_code, matches[0])

        user_id, name = await self._create(person, link_code)
        log.info("career-os sign-in: case 5, new user %s for principal %s", user_id, person.subject)
        return await self._signed_in(user_id, name, person, "new")

    async def _ask_which(
        self, person: CareerOsPerson, link_code: str, linked: list[UUID]
    ) -> dict[str, Any]:
        """Case 3: several accounts are linked to this Career-OS account."""
        candidates: dict[str, str] = {}
        shown: list[dict[str, Any]] = []
        for user_id in linked:
            summary = await self._summary(user_id)
            if summary is None:
                continue
            ref = secrets.token_urlsafe(6)
            candidates[ref] = str(user_id)
            shown.append({"id": ref, **summary})
        ticket = await self._keep({
            "case": 3, "person": asdict(person), "link_code": link_code,
            "candidates": candidates, "made_at": time.time(),
        })  # fmt: skip
        log.info(
            "career-os sign-in: case 3, principal %s is linked to %d accounts; asked which",
            person.subject, len(candidates),
        )  # fmt: skip
        return {
            "status": "choose_account", "ticket": ticket, "expires_in_s": TICKET_TTL_S,
            "accounts": shown,
        }  # fmt: skip

    async def _ask_existing_or_new(
        self, person: CareerOsPerson, link_code: str, match: UUID
    ) -> dict[str, Any]:
        """Case 4: no link, but Career-OS's email is an account's verified address."""
        verified = person.email_verified
        ticket = await self._keep({
            "case": 4, "person": asdict(person), "link_code": link_code,
            "match": str(match), "verified": verified, "made_at": time.time(),
        })  # fmt: skip
        log.info(
            "career-os sign-in: case 4, principal %s shares an email with user %s"
            " (verified by HubRuby: %s); asked",
            person.subject, match, verified,
        )  # fmt: skip
        return {
            "status": "existing_or_new", "ticket": ticket, "expires_in_s": TICKET_TTL_S,
            "verified": verified,
            # Only an address Career-OS verified says the account is this person's to see.
            "account": await self._summary(match) if verified else None,
        }  # fmt: skip

    async def complete(
        self, ticket: str, choice: str, account_id: str | None, client: str,
        account: Principal | None,
    ) -> dict[str, Any]:  # fmt: skip
        """The person's answer to case 3 or 4. `choice` "existing" needs `account`: the proof,
        a sign-in to that account with its own method. `choice` "new" (case 4 only) makes a
        separate account and signs in to it."""
        await hit(self.valkey, "cos:client", client, self.limit)
        kept = await self._open(ticket)
        person = CareerOsPerson(**kept["person"])
        link_code = str(kept["link_code"])
        case = int(kept["case"])

        if choice == "new":
            if case != 4:
                raise SignInError("not_a_choice")
            user_id, name = await self._create(person, link_code)
            await self.valkey.delete(self._ticket_at(ticket))
            log.info(
                "career-os sign-in: case 4, separate new user %s for principal %s",
                user_id, person.subject,
            )  # fmt: skip
            return await self._signed_in(user_id, name, person, "new")

        if account is None:
            raise SignInError("proof_required")
        if account.provider == CAREER_OS:
            # A Career-OS sign-in proves nothing about another account's own method.
            raise SignInError("proof_required")
        proven = account.user_id
        if case == 3:
            candidates: dict[str, str] = kept["candidates"]
            chosen = candidates.get(account_id or "")
            if chosen is None:
                raise SignInError("not_a_choice")
            if str(proven) != chosen:
                log.info(
                    "career-os sign-in: case 3, user %s was proven, not the one chosen", proven
                )
                raise SignInError("wrong_account")
        elif kept.get("verified") and str(proven) != kept["match"]:
            log.info("career-os sign-in: case 4, user %s was proven, not the match", proven)
            raise SignInError("wrong_account")
        await self._attach(proven, person, link_code)
        await self.valkey.delete(self._ticket_at(ticket))
        log.info(
            "career-os sign-in: case %d, user %s proven and recorded for principal %s",
            case, proven, person.subject,
        )  # fmt: skip
        return {"status": "added"}

    # --- Unlinking ----------------------------------------------------------------------------

    @staticmethod
    async def only_method(conn: Any, email_sign_in: bool) -> bool:
        """Whether Career-OS is the account's only way in (in the account's transaction):
        unlinking it then would lock the person out. An email sign-in counts only on a stage
        where email codes are on (`email_sign_in`): a Career-OS account gets one with its
        address, which is no way in where codes cannot be sent."""
        rows = await conn.fetch("select provider from app.identities")
        providers = {str(r["provider"]) for r in rows}
        usable = providers if email_sign_in else providers - {"email"}
        return CAREER_OS in providers and usable == {CAREER_OS}

    @staticmethod
    async def forget(conn: Any, user_id: UUID) -> None:
        """Unlinking removes the Career-OS sign-in with the link: the identity, and the
        sign-ins made with it."""
        await conn.execute(
            "delete from app.identities where provider = $1 and user_id = $2", CAREER_OS, user_id
        )
        await TokenService.revoke_provider(conn, user_id, CAREER_OS)
