from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt

from mindin.hosted.config import OidcConfig

ALGORITHMS = ["RS256", "ES256", "EdDSA"]
DISCOVERY_SECONDS = 3600.0
TIMEOUT = 10.0


class UpstreamError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    name: str


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class Upstream:
    def __init__(
        self,
        config: OidcConfig,
        redirect_uri: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.redirect_uri = redirect_uri
        self.transport = transport
        self.clock = clock
        self._discovery: tuple[dict[str, Any], float] | None = None
        self._jwks: jwt.PyJWKSet | None = None

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self.transport, timeout=TIMEOUT, follow_redirects=False)

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is not None and self._discovery[1] > self.clock():
            return self._discovery[0]
        url = f"{self.config.issuer}/.well-known/openid-configuration"
        document = await self._get_json(url)
        if document.get("issuer", "").rstrip("/") != self.config.issuer:
            raise UpstreamError("the identity provider's discovery names another issuer")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(document.get(key), str):
                raise UpstreamError(f"the identity provider's discovery has no {key}")
        self._discovery = (document, self.clock() + DISCOVERY_SECONDS)
        return document

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        document = await self.discovery()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        endpoint = document["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def identity(self, code: str, verifier: str, nonce: str) -> Identity:
        document = await self.discovery()
        async with self.client() as http:
            try:
                response = await http.post(
                    document["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                        "code_verifier": verifier,
                    },
                    auth=(self.config.client_id, self.config.client_secret),
                    headers={"Accept": "application/json"},
                )
            except httpx2.HTTPError as exc:
                raise UpstreamError(f"the identity provider's token endpoint failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise UpstreamError(f"the identity provider refused the code ({response.status_code})")
        tokens = response.json()
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise UpstreamError("the identity provider returned no id_token")
        claims = await self.verify_id_token(id_token, nonce)
        if "email" not in claims and isinstance(tokens.get("access_token"), str):
            claims = {**(await self.userinfo(tokens["access_token"], claims["sub"])), **claims}
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise UpstreamError("the identity provider sent no email address; add the email scope")
        return Identity(
            sub=str(claims["sub"]),
            email=email.strip().lower(),
            email_verified=claims.get("email_verified") is True,
            name=str(claims.get("name") or ""),
        )

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            key = await self.signing_key(str(header.get("kid") or ""))
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=ALGORITHMS,
                audience=self.config.client_id,
                issuer=[self.config.issuer, self.config.issuer + "/"],
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise UpstreamError(f"the id_token is not valid: {type(exc).__name__}") from exc
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise UpstreamError("the id_token's nonce does not match this sign-in")
        return claims

    async def signing_key(self, kid: str) -> Any:
        for attempt in range(2):
            if self._jwks is None or attempt:
                document = await self.discovery()
                self._jwks = jwt.PyJWKSet.from_dict(await self._get_json(document["jwks_uri"]))
            for key in self._jwks.keys:
                if not kid or key.key_id == kid:
                    return key.key
        raise UpstreamError("the id_token is signed with a key the identity provider does not publish")

    async def userinfo(self, access_token: str, sub: str) -> dict[str, Any]:
        endpoint = (await self.discovery()).get("userinfo_endpoint")
        if not isinstance(endpoint, str):
            return {}
        async with self.client() as http:
            response = await http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
        info = response.json() if response.status_code == 200 else {}
        return info if isinstance(info, dict) and info.get("sub") == sub else {}

    async def _get_json(self, url: str) -> dict[str, Any]:
        async with self.client() as http:
            try:
                response = await http.get(url, headers={"Accept": "application/json"})
            except httpx2.HTTPError as exc:
                raise UpstreamError(f"the identity provider is unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise UpstreamError(f"the identity provider answered {response.status_code} for {url}")
        document = response.json()
        if not isinstance(document, dict):
            raise UpstreamError(f"the identity provider sent no JSON object for {url}")
        return document
