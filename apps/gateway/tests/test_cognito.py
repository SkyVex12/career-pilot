"""The gateway with the Cognito verifier: a locally signed access token opens a session; an
expired one or one for another app client closes with 4401."""

import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from copilot_core.auth import CognitoVerifier, JwksCache
from copilot_gateway.testing import GatewayServer, ProtocolClient

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Local"


async def test_cognito_access_tokens(env) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k1", "alg": "RS256"}

    async def fetch(url: str) -> dict[str, Any]:
        return {"keys": [jwk]}

    env.verifier = CognitoVerifier(ISSUER, ["desktop"], JwksCache(f"{ISSUER}/jwks", fetch=fetch))
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "iss": ISSUER, "client_id": "desktop",
              "token_use": "access", "iat": now, "exp": now + 600, "username": "sam"}  # fmt: skip

    def token(**over: Any) -> str:
        return jwt.encode({**claims, **over}, key, "RS256", headers={"kid": "k1"})

    async with GatewayServer(env.services()) as gw:
        ok = ProtocolClient(gw.url, token=token())
        ready = await ok.start()
        assert ready["type"] == "session.ready"
        for bad in (token(exp=now - 120), token(client_id="other-app"), token(token_use="id")):
            c = ProtocolClient(gw.url, token=bad)
            await c.connect()
            assert await c.closed() == 4401
