"""Cognito access token verification with locally generated keys (no user pool needed)."""

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from copilot_core.auth import AuthError, CognitoVerifier, JwksCache, make_verifier
from copilot_core.config import CoreSettings

POOL = "us-east-1_TestPool1"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL}"
CLIENT = "app-client-1"


class KeyPair:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict[str, Any]:
        public = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        return {**public, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def sign(self, claims: Mapping[str, Any], **header: Any) -> str:
        return jwt.encode(
            dict(claims), self.private, algorithm="RS256", headers={"kid": self.kid, **header}
        )


def claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "scope": "openid email",
        "username": "alex",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class Jwks:
    def __init__(self, *pairs: KeyPair) -> None:
        self.pairs = list(pairs)
        self.calls = 0

    async def __call__(self, url: str) -> Mapping[str, Any]:
        assert url == f"{ISSUER}/.well-known/jwks.json"
        self.calls += 1
        return {"keys": [p.jwk() for p in self.pairs]}


def verifier(jwks: Jwks, clock: Any = time.monotonic) -> CognitoVerifier:
    cache = JwksCache(f"{ISSUER}/.well-known/jwks.json", fetch=jwks, clock=clock)
    return CognitoVerifier(ISSUER, [CLIENT], cache)


async def test_valid_access_token() -> None:
    key = KeyPair("k1")
    v = verifier(Jwks(key))
    c = claims(email="alex@example.com")
    p = await v.verify(key.sign(c))
    assert str(p.user_id) == c["sub"]
    assert p.email == "alex@example.com"
    assert p.display_name == "alex"
    assert p.expires_at == c["exp"]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"}, "invalid"),
        ({"token_use": "id"}, "not an access token"),
        ({"client_id": "someone-else"}, "another app client"),
        ({"token_use": None}, "invalid"),
        ({"sub": "not-a-uuid"}, "not a user id"),
    ],
)
async def test_rejects_bad_claims(over: dict[str, Any], message: str) -> None:
    key = KeyPair("k1")
    with pytest.raises(AuthError, match=message):
        await verifier(Jwks(key)).verify(key.sign(claims(**over)))


async def test_rejects_wrong_signature_and_unknown_key() -> None:
    good, other = KeyPair("k1"), KeyPair("k1")  # same kid, different key
    v = verifier(Jwks(good))
    with pytest.raises(AuthError, match="invalid"):
        await v.verify(other.sign(claims()))
    with pytest.raises(AuthError, match="unknown signing key"):
        await v.verify(KeyPair("k9").sign(claims()))
    with pytest.raises(AuthError, match="malformed"):
        await v.verify("not.a.jwt")


async def test_rejects_alg_none_and_hs256() -> None:
    key = KeyPair("k1")
    v = verifier(Jwks(key))
    unsigned = jwt.encode(claims(), key="", algorithm="none", headers={"kid": "k1"})
    with pytest.raises(AuthError, match="algorithm"):
        await v.verify(unsigned)
    hs = jwt.encode(claims(), "secret-secret-secret-secret-1234", "HS256", headers={"kid": "k1"})
    with pytest.raises(AuthError, match="algorithm"):
        await v.verify(hs)


async def test_jwks_is_cached_and_refreshed_on_rotation() -> None:
    now = [1000.0]
    old = KeyPair("old")
    jwks = Jwks(old)
    v = verifier(jwks, clock=lambda: now[0])
    for _ in range(5):
        await v.verify(old.sign(claims()))
    assert jwks.calls == 1  # one fetch serves every token

    new = KeyPair("new")
    jwks.pairs.append(new)
    # An unknown kid right after a fetch does not refetch (no hammering on made-up kids).
    with pytest.raises(AuthError, match="unknown signing key"):
        await v.verify(new.sign(claims()))
    assert jwks.calls == 1
    now[0] += 61
    await v.verify(new.sign(claims()))  # rotated key found after the minimum interval
    assert jwks.calls == 2
    now[0] += 3601
    await v.verify(old.sign(claims()))  # TTL expiry refetches
    assert jwks.calls == 3


def test_make_verifier_needs_pool_and_clients() -> None:
    with pytest.raises(RuntimeError, match="USER_POOL_ID"):
        make_verifier(CoreSettings(_env_file=None, auth="cognito"))  # pyright: ignore[reportCallIssue]
    s = CoreSettings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth="cognito",
        cognito_user_pool_id=POOL,
        cognito_client_ids="a,b",  # pyright: ignore[reportArgumentType]
    )
    v = make_verifier(s)
    assert isinstance(v, CognitoVerifier)
    assert v.issuer == ISSUER
    assert v.client_ids == {"a", "b"}
