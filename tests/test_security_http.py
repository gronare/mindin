from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from forwarded_support import HOST, ISSUER, TYP, Proxy

from notes_vault_mcp.auth import AuthConfig, ForwardedConfig
from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.auth.hosted_web import Browser, Limits
from notes_vault_mcp.auth.login import Login
from notes_vault_mcp.auth.login_forwarded import ForwardedSignIn, IdentityError, header_value


def verifier(proxy: Proxy, tmp_path: Path) -> ForwardedSignIn:
    config = AuthConfig(
        public_url="http://localhost",
        issuer="http://localhost",
        auth_dir=tmp_path / "auth",
        forwarded=ForwardedConfig(public_key=proxy.pem, issuer=ISSUER, typ=TYP),
    )
    store = HostedStore(tmp_path / "auth")
    limits = Limits()
    assert config.forwarded is not None
    browser = Browser(config, store, limits)
    return ForwardedSignIn(Login(config, store, browser, limits), config.forwarded)


# --- forwarded identity ---------------------------------------------------------------------------------------


def _claims(proxy: Proxy, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": HOST, "sub": "7", "entitlements": ["vault:write"], "exp": now + 60, "typ": TYP}
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def _refused(proxy: Proxy, tmp_path: Path, token: str) -> bool:
    with pytest.raises(IdentityError):
        verifier(proxy, tmp_path).verify(token)
    return True


def test_an_unsigned_token_is_refused(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy), key=None, algorithm="none")
    assert _refused(proxy, tmp_path, token)


def test_a_token_hmac_signed_with_the_public_key_is_refused(proxy: Proxy, tmp_path: Path):
    try:
        token = jwt.encode(_claims(proxy), key=proxy.pem.encode(), algorithm="HS256")
    except jwt.exceptions.InvalidKeyError:
        token = jwt.encode(_claims(proxy), key="x" * 64, algorithm="HS256")
    assert _refused(proxy, tmp_path, token)


@pytest.mark.parametrize("missing", ["sub", "aud", "exp", "iss"])
def test_a_token_missing_a_required_claim_is_refused(proxy: Proxy, tmp_path: Path, missing: str):
    token = jwt.encode(_claims(proxy, **{missing: None}), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token)


def test_a_token_for_another_host_is_refused(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, aud="other.example.com"), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token)


def test_an_expired_or_not_yet_valid_token_is_refused(proxy: Proxy, tmp_path: Path):
    now = int(time.time())
    expired = jwt.encode(_claims(proxy, exp=now - 1), proxy.key, algorithm="RS256")
    early = jwt.encode(_claims(proxy, nbf=now + 300), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, expired)
    assert _refused(proxy, tmp_path, early)


def test_a_token_of_another_type_is_refused_when_a_type_is_configured(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, typ="access"), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token)


def test_entitlements_may_arrive_as_one_string(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, entitlements="vault:read vault:write"), proxy.key, algorithm="RS256")
    assert verifier(proxy, tmp_path).verify(token).scopes == ["vault:read", "vault:write"]


def test_garbage_in_the_identity_header_is_refused(proxy: Proxy, tmp_path: Path):
    for token in ("", "not.a.jwt", "a.b.c", "\xff\xfe", "Bearer x"):
        with pytest.raises(IdentityError):
            verifier(proxy, tmp_path).verify(token)


def test_the_identity_header_name_is_matched_case_insensitively(proxy: Proxy):
    token = proxy.token()
    assert header_value({"headers": [(b"x-forwarded-identity", token.encode())]}, "X-Forwarded-Identity") == token


def test_a_verified_identity_names_its_subject_and_email(proxy: Proxy, tmp_path: Path):
    forwarded = verifier(proxy, tmp_path).verify(proxy.token("1042", entitlements=("vault:read",)))
    assert forwarded.proof.method == "forwarded" and forwarded.proof.subject == "1042"
    assert forwarded.proof.emails == ("user1042@example.com",)
    assert forwarded.scopes == ["vault:read"]
