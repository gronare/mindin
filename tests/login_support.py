from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import struct
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, urlsplit

import cbor2
import httpx2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

GITHUB_CLIENT_ID = "gh-client"
GITHUB_CLIENT_SECRET = "gh-secret-value"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


@dataclass
class FakeGithub:
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def approve(
        self, authorize_url: str, user_id: int, login: str, emails: list[tuple[str, bool, bool]]
    ) -> tuple[str, str]:
        params = query(authorize_url)
        assert authorize_url.startswith("https://github.com/login/oauth/authorize?")
        assert params["client_id"] == GITHUB_CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert "user:email" in params["scope"].split()
        code = secrets.token_urlsafe(12)
        self.codes[code] = {"params": params, "id": user_id, "login": login, "emails": emails}
        return params["state"], code

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == "https://github.com/login/oauth/access_token":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            grant = self.codes.pop(form.get("code", ""), None)
            if (
                grant is None
                or form.get("client_id") != GITHUB_CLIENT_ID
                or form.get("client_secret") != GITHUB_CLIENT_SECRET
                or b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                != grant["params"]["code_challenge"]
                or form.get("redirect_uri") != grant["params"]["redirect_uri"]
            ):
                return httpx2.Response(200, json={"error": "bad_verification_code"})
            token = "gho_" + secrets.token_urlsafe(12)
            self.tokens[token] = grant
            return httpx2.Response(200, json={"access_token": token, "token_type": "bearer", "scope": "user:email"})
        grant = self.tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if grant is None:
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if url == "https://api.github.com/user":
            return httpx2.Response(200, json={"id": grant["id"], "login": grant["login"], "name": grant["login"]})
        if url == "https://api.github.com/user/emails":
            rows = [
                {"email": email, "verified": verified, "primary": primary, "visibility": None}
                for email, verified, primary in grant["emails"]
            ]
            return httpx2.Response(200, json=rows)
        return httpx2.Response(404)


class FakeSmtpSession:
    def __init__(self, server: FakeSmtp, host: str, port: int) -> None:
        self.server = server
        self.host = host
        self.port = port
        self.tls = False

    def __enter__(self) -> FakeSmtpSession:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.tls = True

    def login(self, username: str, password: str) -> None:
        self.server.logins.append((username, password, self.tls))

    def send_message(self, message: EmailMessage) -> None:
        self.server.messages.append(message)


@dataclass
class FakeSmtp:
    messages: list[EmailMessage] = field(default_factory=list)
    logins: list[tuple[str, str, bool]] = field(default_factory=list)
    connections: list[tuple[str, int]] = field(default_factory=list)

    def factory(self, host: str, port: int, timeout: float | None = None, context: Any = None) -> FakeSmtpSession:
        self.connections.append((host, port))
        return FakeSmtpSession(self, host, port)

    def last_link(self, to: str) -> str:
        for message in reversed(self.messages):
            if message["To"] == to.lower():
                found = re.search(r"https?://\S+/signin/email/confirm\?token=\S+", message.get_content())
                assert found, message.get_content()
                return found.group(0)
        raise AssertionError(f"no email to {to}")


class Authenticator:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle = b""
        self.count = 0

    def client_data(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        payload = {"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}
        return json.dumps(payload).encode()

    def cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    def create(self, options: dict[str, Any], origin: str | None = None) -> dict[str, Any]:
        assert options["rp"]["id"] == self.rp_id
        assert options["authenticatorSelection"]["residentKey"] == "required"
        self.user_handle = unb64url(options["user"]["id"])
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_key()
        auth_data = rp_hash + bytes([0x45]) + struct.pack(">I", self.count) + attested
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(self.client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict[str, Any], origin: str | None = None) -> dict[str, Any]:
        assert options["rpId"] == self.rp_id
        self.count += 1
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        client_data = self.client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(self.user_handle),
            },
        }
