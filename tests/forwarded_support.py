from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "mcp-proxy.example.com"
HOST = "localhost"
TYP = "mcp-proxy+identity"


@lru_cache(maxsize=1)
def _keys() -> tuple[Any, Any]:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def _pem(key: Any) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("utf-8")
    )


class Proxy:
    def __init__(self) -> None:
        self.key, self.stranger = _keys()

    @property
    def pem(self) -> str:
        return _pem(self.key)

    def token(self, sub: str = "1042", entitlements: tuple[str, ...] = ("vault:write",), **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": HOST,
            "sub": sub,
            "email": f"user{sub}@example.com",
            "name": f"User {sub}",
            "entitlements": list(entitlements),
            "iat": now,
            "exp": now + 60,
            "typ": TYP,
            **claims,
        }
        return jwt.encode(payload, self.key, algorithm="RS256")

    def foreign_token(self) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": HOST, "sub": "1", "entitlements": ["vault:write"], "exp": now + 60, "typ": TYP}
        return jwt.encode(payload, self.stranger, algorithm="RS256")
