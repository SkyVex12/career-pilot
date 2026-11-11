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

from copilot_core.auth import (
    COPILOT_ISSUER,
    GOOGLE_JWKS_URL,
    AuthError,
    ByIssuer,
    CognitoVerifier,
    CopilotVerifier,
    GoogleVerifier,
    JwksCache,
    TokenSigner,
    email_public_keys,
    google_user_id,
    key_id,
    make_verifier,
    new_signing_key,
    public_key_of,
)
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


# --- Google ID tokens (Stage 0 on GCP) ------------------------------------------------------

GOOGLE_CLIENT = "123-abc.apps.googleusercontent.com"


def google_claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": GOOGLE_CLIENT,
        "azp": GOOGLE_CLIENT,
        "sub": "109876543210987654321",
        "email": "Alex@Example.com",
        "email_verified": True,
        "name": "Alex",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def google_verifier(key: KeyPair, allowed: list[str] | None = None) -> GoogleVerifier:
    async def fetch(url: str) -> Mapping[str, Any]:
        assert url == GOOGLE_JWKS_URL
        return {"keys": [key.jwk()]}

    return GoogleVerifier(
        [GOOGLE_CLIENT],
        ["alex@example.com"] if allowed is None else allowed,
        JwksCache(GOOGLE_JWKS_URL, fetch=fetch),
    )


async def test_google_id_token_for_an_allowed_email() -> None:
    key = KeyPair("g1")
    p = await google_verifier(key).verify(key.sign(google_claims()))
    assert p.email == "alex@example.com"
    assert p.display_name == "Alex"
    # A stable UUID from the Google subject, the same on every service and every sign-in.
    assert p.user_id == google_user_id("109876543210987654321")
    assert p.user_id.version == 5
    other = await google_verifier(key).verify(key.sign(google_claims(iss="accounts.google.com")))
    assert other.user_id == p.user_id


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"email": "mallory@example.com"}, "allow-list"),
        ({"email_verified": False}, "verified email"),
        ({"email": None}, "verified email"),
        ({"aud": "someone-elses-client"}, "another app client"),
        ({"iss": "https://evil.example.com"}, "not from Google"),
        ({"exp": int(time.time()) - 120}, "expired"),
    ],
)
async def test_google_rejects(over: dict[str, Any], message: str) -> None:
    key = KeyPair("g1")
    with pytest.raises(AuthError, match=message):
        await google_verifier(key).verify(key.sign(google_claims(**over)))


async def test_google_without_an_allow_list_takes_any_verified_email() -> None:
    key = KeyPair("g1")
    open_to_all = google_verifier(key, allowed=[])
    p = await open_to_all.verify(key.sign(google_claims(email="mallory@example.com")))
    assert p.email == "mallory@example.com"
    with pytest.raises(AuthError, match="verified email"):
        await open_to_all.verify(key.sign(google_claims(email_verified=False)))


async def test_google_rejects_a_key_google_did_not_publish() -> None:
    key, forged = KeyPair("g1"), KeyPair("g1")
    with pytest.raises(AuthError, match="invalid token"):
        await google_verifier(key).verify(forged.sign(google_claims()))


def test_make_verifier_for_google_takes_an_optional_allow_list() -> None:
    base = {"env": "staging", "auth": "google", "google_client_ids": GOOGLE_CLIENT}
    with pytest.raises(RuntimeError, match="CLIENT_IDS"):
        make_verifier(CoreSettings(_env_file=None, env="staging", auth="google"))  # pyright: ignore[reportCallIssue]
    open_to_all = make_verifier(CoreSettings(_env_file=None, **base))  # pyright: ignore[reportCallIssue]
    assert isinstance(open_to_all, GoogleVerifier)
    assert open_to_all.allowed == set()
    s = CoreSettings(_env_file=None, **base, google_allowed_emails="a@x.io, b@y.io")  # pyright: ignore[reportCallIssue]
    v = make_verifier(s)
    assert isinstance(v, GoogleVerifier)
    assert v.allowed == {"a@x.io", "b@y.io"}


# --- Email sign-in: the API's own access tokens ---------------------------------------------


def own_tokens(allowed: list[str] | None = None) -> tuple[TokenSigner, CopilotVerifier]:
    signing_key, public_key = new_signing_key()
    return TokenSigner(signing_key, ttl_s=3600), CopilotVerifier([public_key], allowed or [])


async def test_own_token_carries_the_user_id() -> None:
    signer, v = own_tokens()
    uid = uuid.uuid4()
    p = await v.verify(signer.sign(uid, "sam@example.com", "Sam"))
    assert (p.user_id, p.email, p.display_name) == (uid, "sam@example.com", "Sam")
    assert (p.provider, p.subject, p.new_identity) == ("email", "sam@example.com", False)
    assert p.expires_at is not None
    assert 3500 < p.expires_at - time.time() <= 3600


async def test_own_token_rejects() -> None:
    signer, v = own_tokens()
    uid = uuid.uuid4()
    with pytest.raises(AuthError, match="token expired"):
        await v.verify(signer.sign(uid, "sam@example.com", "Sam", now=time.time() - 7200))
    # Signed by another deployment's key: same kid check first, then the signature.
    other, _ = own_tokens()
    with pytest.raises(AuthError, match="unknown signing key"):
        await v.verify(other.sign(uid, "sam@example.com", "Sam"))
    forged = jwt.encode(
        {"iss": COPILOT_ISSUER, "aud": "copilot", "sub": str(uid), "email": "sam@example.com",
         "token_use": "access", "iat": int(time.time()), "exp": int(time.time()) + 60},
        other.key, algorithm="EdDSA", headers={"kid": signer.kid},
    )  # fmt: skip
    with pytest.raises(AuthError, match="invalid token"):
        await v.verify(forged)
    hs = jwt.encode({"iss": COPILOT_ISSUER, "sub": str(uid)}, "x" * 32, algorithm="HS256")
    with pytest.raises(AuthError, match="algorithm"):
        await v.verify(hs)
    with pytest.raises(AuthError, match="malformed"):
        await v.verify("not-a-jwt")
    for claim, value, message in (
        ("token_use", "refresh", "not an access token"),
        ("sub", "not-a-uuid", "sub is not a user id"),
        ("aud", "another-app", "invalid token"),
        ("iss", "someone-else", "invalid token"),
        ("email", "", "no email"),
    ):
        base = {"iss": COPILOT_ISSUER, "aud": "copilot", "sub": str(uid),
                "email": "sam@example.com", "token_use": "access",
                "iat": int(time.time()), "exp": int(time.time()) + 60}  # fmt: skip
        bad = jwt.encode({**base, claim: value}, signer.key, "EdDSA", headers={"kid": signer.kid})
        with pytest.raises(AuthError, match=message):
            await v.verify(bad)


async def test_own_token_checks_the_allow_list_on_every_use() -> None:
    signer, v = own_tokens(allowed=["sam@example.com"])
    uid = uuid.uuid4()
    assert (await v.verify(signer.sign(uid, "sam@example.com", "Sam"))).user_id == uid
    with pytest.raises(AuthError, match="allow-list"):
        await v.verify(signer.sign(uid, "mallory@example.com", "M"))


def test_key_helpers() -> None:
    signing_key, public_key = new_signing_key()
    assert public_key_of(signing_key) == public_key
    assert len(key_id(public_key)) == 16
    assert new_signing_key()[0] != signing_key
    with pytest.raises(RuntimeError, match="32 bytes"):
        public_key_of("c2hvcnQ")
    with pytest.raises(RuntimeError, match="PUBLIC_KEYS"):
        CopilotVerifier([], [])


async def test_tokens_are_told_apart_by_issuer() -> None:
    key = KeyPair("g1")
    signer, own = own_tokens()
    both = ByIssuer({COPILOT_ISSUER: own}, google_verifier(key))
    uid = uuid.uuid4()
    assert (await both.verify(signer.sign(uid, "sam@example.com", "Sam"))).user_id == uid
    g = await both.verify(key.sign(google_claims()))
    assert g.user_id == google_user_id("109876543210987654321")
    assert g.provider == "google"
    # A Google-signed token that claims to be ours goes to our verifier, which refuses it.
    with pytest.raises(AuthError):
        await both.verify(key.sign(google_claims(iss=COPILOT_ISSUER)))
    with pytest.raises(AuthError):
        await both.verify("garbage")


class FakeIdentities:
    def __init__(self, known: dict[tuple[str, str], uuid.UUID]) -> None:
        self.known = known

    async def user_of(self, provider: str, subject: str) -> uuid.UUID | None:
        return self.known.get((provider, subject))


async def test_google_sign_in_resolves_through_identities() -> None:
    key = KeyPair("g1")
    sub = "109876543210987654321"

    async def fetch(url: str) -> Mapping[str, Any]:
        return {"keys": [key.jwk()]}

    def verifier_with(known: dict[tuple[str, str], uuid.UUID]) -> GoogleVerifier:
        return GoogleVerifier(
            [GOOGLE_CLIENT], [], JwksCache(GOOGLE_JWKS_URL, fetch=fetch),
            identities=FakeIdentities(known),
        )  # fmt: skip

    # Nothing recorded: the id Google sign-ins always had, and the API is told to record it.
    p = await verifier_with({}).verify(key.sign(google_claims()))
    assert (p.user_id, p.new_identity, p.subject) == (google_user_id(sub), True, sub)
    # Recorded for the account an email sign-in made: that account.
    email_account = uuid.uuid4()
    p = await verifier_with({("google", sub): email_account}).verify(key.sign(google_claims()))
    assert (p.user_id, p.new_identity) == (email_account, False)


def test_make_verifier_adds_the_api_tokens_when_email_sign_in_is_on() -> None:
    signing_key, public_key = new_signing_key()
    base = {"env": "staging", "auth": "google", "google_client_ids": GOOGLE_CLIENT,
            "email_sign_in": True}  # fmt: skip
    with pytest.raises(RuntimeError, match="PUBLIC_KEYS"):
        make_verifier(CoreSettings(_env_file=None, **base))  # pyright: ignore[reportCallIssue]
    # The gateway: the public key only.
    v = make_verifier(CoreSettings(_env_file=None, **base, email_public_keys=public_key))  # pyright: ignore[reportCallIssue]
    assert isinstance(v, ByIssuer)
    assert isinstance(v.default, GoogleVerifier)
    assert set(v.issuers) == {COPILOT_ISSUER}
    # The API (and the local stack): the signing key's own public key.
    s = CoreSettings(_env_file=None, **base, email_signing_key=signing_key)  # pyright: ignore[reportCallIssue]
    assert email_public_keys(s) == [public_key]
    # Off: exactly what it was.
    off = make_verifier(CoreSettings(_env_file=None, **{**base, "email_sign_in": False}))  # pyright: ignore[reportCallIssue]
    assert isinstance(off, GoogleVerifier)
