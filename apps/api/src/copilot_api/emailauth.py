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
- **Tokens** are the API's own (`copilot_api.tokens`): a signed access token, about an hour,
  and a refresh token that works once.

Asking for a code answers the same way and in the same time whatever the address is: the mail
is sent after the answer, and an address that may not sign in gets the same answer and no mail.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field

import asyncpg

from copilot_api.config import ApiSettings
from copilot_api.mailer import Mailer, sign_in_message
from copilot_api.tokens import RefreshError, Tokens, TokenService
from copilot_api.tokens import sha as _sha
from copilot_core import identities
from copilot_core.valkey_client import Valkey

__all__ = ["CodeError", "EmailSignIn", "Limit", "Limits", "RateLimited", "RefreshError", "Tokens"]

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


async def hit(valkey: Valkey, scope: str, who: str, limits: tuple[Limit, ...]) -> None:
    """Count one request; RateLimited when any window is over its count. Keyed by a hash, so
    neither an address nor a client address is kept in Valkey."""
    for limit in limits:
        key = f"auth:limit:{scope}:{limit.window_s}:{_sha(who)[:32]}"
        await valkey.set(key, 0, ex=limit.window_s, nx=True)
        count = int(await valkey.incr(key))
        if count > limit.count:
            left = int(await valkey.ttl(key))
            if left < 0:  # the window ended between the two calls: start it again
                await valkey.expire(key, limit.window_s)
                left = limit.window_s
            raise RateLimited(max(left, 1))


@dataclass
class EmailSignIn:
    settings: ApiSettings
    pool: asyncpg.Pool
    valkey: Valkey
    mailer: Mailer
    limits: Limits = Limits()
    # The API's tokens; shared with Career-OS sign-in when the stage has both.
    tokens: TokenService | None = None
    _sends: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def __post_init__(self) -> None:
        if self.tokens is None:
            self.tokens = TokenService(self.settings, self.pool)
        # The codes' HMAC key: derived from the signing key, which only the API holds.
        self._code_key = hashlib.sha256(
            b"copilot sign-in code\0" + self.tokens.seed.encode()
        ).digest()

    @property
    def _tokens(self) -> TokenService:
        assert self.tokens is not None
        return self.tokens

    @property
    def allowed(self) -> set[str]:
        """The allow-list (empty: any address). One list for codes, tokens and refreshes."""
        return self._tokens.allowed

    @allowed.setter
    def allowed(self, emails: set[str]) -> None:
        self._tokens.allowed = emails

    # --- Limits -------------------------------------------------------------------------------

    async def _hit(self, scope: str, who: str, limits: tuple[Limit, ...]) -> None:
        await hit(self.valkey, scope, who, limits)

    # --- Codes --------------------------------------------------------------------------------

    def _code_hash(self, email: str, code: str) -> str:
        return hmac.new(self._code_key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _code_at(email: str) -> str:
        return f"auth:code:{_sha(email)[:32]}"

    def may_sign_in(self, email: str) -> bool:
        return self._tokens.may_sign_in(email)

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
        try:
            return await self._tokens.issue(user_id, name, email=email)
        except RefreshError as error:
            raise CodeError("invalid") from error

    # --- Tokens -------------------------------------------------------------------------------

    async def refresh(self, token: str) -> Tokens:
        return await self._tokens.refresh(token)

    async def sign_out(self, token: str) -> bool:
        return await self._tokens.sign_out(token)
