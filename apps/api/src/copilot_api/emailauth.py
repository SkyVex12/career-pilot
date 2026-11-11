"""Email sign-in (ARCHITECTURE.md, "Email sign-in"): a one-time code by email, then tokens.

No password exists: Stage 0 is plain HTTP, so nothing a listener could use again may cross the
wire. A code works once and for ten minutes; a refresh token works once, and showing a used
one again revokes its whole chain.

- **Codes** live in Valkey for their ten minutes, one per address, as an HMAC of address and
  code under a key only the API has, so neither Valkey nor a dump of it gives a usable code.
  A new code replaces the old one. Five wrong tries kill it. Checking, counting the try and
  using the code up are one atomic step (`CHECK_CODE`).
- **Rate limits** are counters in Valkey, per address and per client address, on asking for a
  code and on trying one. They are keyed by a hash, so no address is kept there.
- **Refresh tokens** are rows in `app.refresh_tokens` (migration 0014), as SHA-256 hashes.
- **Access tokens** are JWTs the API signs (`copilot_core.auth.TokenSigner`), about an hour.

Asking for a code answers the same way and in the same time whatever the address is: the mail
is sent after the answer, and an address that may not sign in gets the same answer and no mail.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg

from copilot_api.config import ApiSettings
from copilot_api.mailer import Mailer, sign_in_message
from copilot_core import identities
from copilot_core.auth import TokenSigner, allow_list
from copilot_core.db import become, sign_in_tx, user_tx
from copilot_core.ids import uuid7
from copilot_core.valkey_client import Valkey

log = logging.getLogger(__name__)

CODE_DIGITS = 6

# KEYS[1]: the address's code. ARGV[1]: the hash of the code tried, ARGV[2]: the tries allowed.
# 0: no code (never sent, expired, or used). 1: right; the code is used up. 2: too many wrong
# tries; the code is dead, right or wrong. 3: wrong.
CHECK_CODE = """
local stored = redis.call('HGET', KEYS[1], 'hash')
if not stored then return 0 end
local tries = redis.call('HINCRBY', KEYS[1], 'tries', 1)
local allowed = tonumber(ARGV[2])
if tries > allowed then return 2 end
if stored == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
if tries >= allowed then return 2 end
return 3
"""


@dataclass(frozen=True)
class Limit:
    count: int
    window_s: int


@dataclass(frozen=True)
class Limits:
    """How often a code may be asked for and tried, per address and per client address."""

    start_address: tuple[Limit, ...] = (Limit(3, 15 * 60), Limit(10, 24 * 3600))
    start_client: tuple[Limit, ...] = (Limit(10, 15 * 60), Limit(50, 24 * 3600))
    verify_address: tuple[Limit, ...] = (Limit(10, 15 * 60),)
    verify_client: tuple[Limit, ...] = (Limit(30, 15 * 60),)


class RateLimited(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"too many requests; try again in {retry_after_s} s")
        self.retry_after_s = retry_after_s


class CodeError(Exception):
    """`reason`: "invalid" (wrong), "expired" (none, expired or used), "locked" (too many
    wrong tries)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RefreshError(Exception):
    """`reason`: "invalid" (unknown, expired or revoked), "reused" (a replaced token shown
    again: its chain is now revoked), "not_allowed" (off the allow-list)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    email: str


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class EmailSignIn:
    settings: ApiSettings
    pool: asyncpg.Pool
    valkey: Valkey
    mailer: Mailer
    limits: Limits = Limits()
    _sends: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def __post_init__(self) -> None:
        key = self.settings.email_signing_key
        if key is None:
            raise RuntimeError("COPILOT_EMAIL_SIGN_IN needs COPILOT_EMAIL_SIGNING_KEY on the API")
        seed = key.get_secret_value()
        self.signer = TokenSigner(seed, self.settings.email_access_ttl_s)
        # The codes' HMAC key: derived from the signing key, which only the API holds.
        self._code_key = hashlib.sha256(b"copilot sign-in code\0" + seed.encode()).digest()
        self.allowed = allow_list(self.settings.google_allowed_emails)

    # --- Limits -------------------------------------------------------------------------------

    async def _hit(self, scope: str, who: str, limits: tuple[Limit, ...]) -> None:
        """Count one request; RateLimited when any window is over its count."""
        for limit in limits:
            key = f"auth:limit:{scope}:{limit.window_s}:{_sha(who)[:32]}"
            await self.valkey.set(key, 0, ex=limit.window_s, nx=True)
            count = int(await self.valkey.incr(key))
            if count > limit.count:
                left = int(await self.valkey.ttl(key))
                if left < 0:  # the window ended between the two calls: start it again
                    await self.valkey.expire(key, limit.window_s)
                    left = limit.window_s
                raise RateLimited(max(left, 1))

    # --- Codes --------------------------------------------------------------------------------

    def _code_hash(self, email: str, code: str) -> str:
        return hmac.new(self._code_key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _code_at(email: str) -> str:
        return f"auth:code:{_sha(email)[:32]}"

    def may_sign_in(self, email: str) -> bool:
        return not self.allowed or email in self.allowed

    async def start(self, email: str, client: str) -> None:
        """Send a code to a normalized address. Returns the same way whether or not the
        address has an account or may sign in; raises only RateLimited."""
        await self._hit("start:client", client, self.limits.start_client)
        await self._hit("start:address", email, self.limits.start_address)
        if not self.may_sign_in(email):
            return
        code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
        key = self._code_at(email)
        async with self.valkey.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping={"hash": self._code_hash(email, code), "tries": 0})
            pipe.expire(key, self.settings.email_code_ttl_s)
            await pipe.execute()
        # After the answer, so its timing says nothing about the address.
        task = asyncio.create_task(self._send(email, code))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)

    async def _send(self, email: str, code: str) -> None:
        subject, text = sign_in_message(code, self.settings.email_code_ttl_s // 60)
        try:
            await self.mailer.send(email, subject, text)
        except Exception as e:
            # Neither the code nor the address: the log is not the place for either.
            log.error("could not send a sign-in code: %s: %s", type(e).__name__, e)

    async def drain(self) -> None:
        """Wait for the mails being sent (shutdown, and tests)."""
        if self._sends:
            await asyncio.gather(*list(self._sends), return_exceptions=True)

    async def verify(self, email: str, code: str, client: str) -> Tokens:
        """Trade the code for tokens; the account is made here if the address has none."""
        await self._hit("verify:client", client, self.limits.verify_client)
        await self._hit("verify:address", email, self.limits.verify_address)
        result = int(
            await self.valkey.eval(
                CHECK_CODE, 1, self._code_at(email), self._code_hash(email, code),
                self.settings.email_code_max_tries,
            )
        )  # fmt: skip
        if result != 1 or not self.may_sign_in(email):
            raise CodeError({0: "expired", 2: "locked"}.get(result, "invalid"))
        user_id, name = await identities.account_for_email(self.pool, email)
        refresh = await self._issue(user_id, email, uuid7())
        return self._tokens(user_id, email, name, refresh)

    # --- Tokens -------------------------------------------------------------------------------

    def _tokens(self, user_id: UUID, email: str, name: str, refresh: str) -> Tokens:
        access = self.signer.sign(user_id, email, name)
        return Tokens(access, refresh, self.signer.ttl_s, email)

    async def _insert(self, conn: Any, user_id: UUID, email: str, family: UUID) -> str:
        token = secrets.token_urlsafe(32)
        await conn.execute(
            "insert into app.refresh_tokens (token_hash, user_id, family_id, email, expires_at)"
            " values ($1, $2, $3, $4, now() + $5::interval)",
            _sha(token), user_id, family, email,
            timedelta(days=self.settings.email_refresh_ttl_days),
        )  # fmt: skip
        return token

    async def _issue(self, user_id: UUID, email: str, family: UUID) -> str:
        async with user_tx(self.pool, user_id) as conn:
            # Tokens past their time have nothing left to tell; replaced ones are kept until
            # then, to recognize one shown again.
            await conn.execute(
                "delete from app.refresh_tokens where user_id = $1 and expires_at < now()", user_id
            )
            return await self._insert(conn, user_id, email, family)

    async def refresh(self, token: str) -> Tokens:
        """Replace a refresh token: new access and refresh tokens, and the old one is spent.
        A spent token shown again revokes every token of its sign-in."""
        digest = _sha(token)
        reused = False
        async with sign_in_tx(self.pool, f"refresh:{digest}") as conn:
            found = await conn.fetchval(
                "select user_id from app.refresh_tokens where token_hash = $1", digest
            )
            if found is None:
                raise RefreshError("invalid")
            user_id: UUID = found
            await become(conn, user_id)
            row = await conn.fetchrow(
                "select family_id, email, replaced_at, revoked_at, expires_at <= now() as expired"
                "  from app.refresh_tokens where token_hash = $1 for update",
                digest,
            )
            if row is None or row["revoked_at"] is not None or row["expired"]:
                raise RefreshError("invalid")
            email, family = str(row["email"]), row["family_id"]
            if row["replaced_at"] is not None or not self.may_sign_in(email):
                reused = row["replaced_at"] is not None
                await self._revoke_family(conn, user_id, family)
            else:
                await conn.execute(
                    "update app.refresh_tokens set replaced_at = now() where token_hash = $1",
                    digest,
                )
                name = await conn.fetchval(
                    "select display_name from app.users where id = $1", user_id
                )
                fresh = await self._insert(conn, user_id, email, family)
                return self._tokens(user_id, email, str(name or email), fresh)
        # Outside the transaction, so the revocation above is committed.
        if reused:
            log.warning("refresh token reuse: revoked the sign-in of user %s", user_id)
        raise RefreshError("reused" if reused else "not_allowed")

    async def sign_out(self, token: str) -> bool:
        """Revoke the refresh token's whole sign-in; False if the token is not known."""
        digest = _sha(token)
        async with sign_in_tx(self.pool, f"refresh:{digest}") as conn:
            row = await conn.fetchrow(
                "select user_id, family_id from app.refresh_tokens where token_hash = $1", digest
            )
            if row is None:
                return False
            await become(conn, row["user_id"])
            await self._revoke_family(conn, row["user_id"], row["family_id"])
        return True

    @staticmethod
    async def _revoke_family(conn: Any, user_id: UUID, family: UUID) -> None:
        await conn.execute(
            "update app.refresh_tokens set revoked_at = now()"
            " where user_id = $1 and family_id = $2 and revoked_at is null",
            user_id, family,
        )  # fmt: skip
