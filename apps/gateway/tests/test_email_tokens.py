"""The gateway with email sign-in on: it takes Google's ID tokens and the API's own access
tokens, told apart by issuer, and `auth.refresh` keeps a session alive with either. A Google
sign-in resolves to the account `app.identities` records for it."""

import asyncio
import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from copilot_core.auth import (
    COPILOT_ISSUER,
    GOOGLE_JWKS_URL,
    ByIssuer,
    CopilotVerifier,
    GoogleVerifier,
    JwksCache,
    TokenSigner,
    google_user_id,
    new_signing_key,
)
from copilot_gateway.testing import GatewayServer, ProtocolClient

CLIENT = "123-abc.apps.googleusercontent.com"
SUB = "109876543210987654321"


class Recorded:
    """app.identities, in memory."""

    def __init__(self) -> None:
        self.known: dict[tuple[str, str], uuid.UUID] = {}

    async def user_of(self, provider: str, subject: str) -> uuid.UUID | None:
        return self.known.get((provider, subject))


def both(recorded: Recorded) -> tuple[Any, TokenSigner, ByIssuer]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "g1", "alg": "RS256"}

    async def fetch(url: str) -> dict[str, Any]:
        return {"keys": [jwk]}

    signing_key, public_key = new_signing_key()
    google = GoogleVerifier(
        [CLIENT], [], JwksCache(GOOGLE_JWKS_URL, fetch=fetch), identities=recorded
    )
    verifier = ByIssuer({COPILOT_ISSUER: CopilotVerifier([public_key], [])}, google)
    return key, TokenSigner(signing_key, ttl_s=600), verifier


def google_token(key: Any, **over: Any) -> str:
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT, "sub": SUB,
              "email": "alex@example.com", "email_verified": True, "name": "Alex",
              "iat": now, "exp": now + 600}  # fmt: skip
    return jwt.encode({**claims, **over}, key, "RS256", headers={"kid": "g1"})


async def test_the_gateway_takes_both_token_kinds(env) -> None:
    recorded = Recorded()
    key, signer, env.verifier = both(recorded)
    email_user = uuid.uuid4()
    async with GatewayServer(env.services()) as gw:
        by_email = ProtocolClient(gw.url, token=signer.sign(email_user, "sam@example.com", "Sam"))
        assert (await by_email.start())["type"] == "session.ready"
        by_google = ProtocolClient(gw.url, token=google_token(key))
        assert (await by_google.start())["type"] == "session.ready"
        sessions = {s.principal.user_id for s in gw.manager.sessions.values()}
        assert sessions == {email_user, google_user_id(SUB)}

        other_key, other_signer, _ = both(Recorded())
        for bad in (
            signer.sign(email_user, "sam@example.com", "Sam", now=time.time() - 3600),  # expired
            other_signer.sign(email_user, "sam@example.com", "Sam"),  # another deployment's key
            google_token(other_key),  # not Google's key
            google_token(key, iss=COPILOT_ISSUER),  # Google-signed, claiming to be ours
            "dev-token-0123456789",
        ):
            c = ProtocolClient(gw.url, token=bad)
            await c.connect()
            assert await c.closed() == 4401


async def test_auth_refresh_works_with_the_api_tokens(env) -> None:
    _, signer, env.verifier = both(Recorded())
    user = uuid.uuid4()
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url, token=signer.sign(user, "sam@example.com", "Sam"))
        await c.start()
        session = next(iter(gw.manager.sessions.values()))
        before = session.principal.expires_at
        assert before is not None
        # A fresh token for the same account, mid-call: the session carries on with it.
        signer.ttl_s = 1200
        later = signer.sign(user, "sam@example.com", "Sam")
        await c.send({"type": "auth.refresh", "access_token": later})
        await asyncio.sleep(0.3)
        assert c.close_code is None
        assert session.principal.expires_at is not None
        assert session.principal.expires_at > before
        # Another account's token ends it.
        other = signer.sign(uuid.uuid4(), "eve@example.com", "Eve")
        await c.send({"type": "auth.refresh", "access_token": other})
        assert await c.closed() == 4401


async def test_a_google_sign_in_goes_to_its_recorded_account(env) -> None:
    """An account an email sign-in made, later signed in to with Google for the same address:
    the API records the link, and the gateway's session is that account's."""
    recorded = Recorded()
    key, _, env.verifier = both(recorded)
    account = uuid.uuid4()
    recorded.known[("google", SUB)] = account
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url, token=google_token(key))
        assert (await c.start())["type"] == "session.ready"
        assert [s.principal.user_id for s in gw.manager.sessions.values()] == [account]
