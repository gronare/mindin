from __future__ import annotations

import os
from pathlib import Path

import pytest
from forwarded_support import Proxy

from mindin.config import PLUGIN_ENV_PREFIX, ConfigError
from mindin.hosted.config import READ_SCOPE, WRITE_SCOPE, AuthConfig, auth_config

PREFIXES = ("VAULT_SMTP_", "VAULT_GITHUB_", "VAULT_OIDC_", "VAULT_IDENTITY_", "VAULT_HOSTED_")
NAMES = (
    "VAULT_PUBLIC_URL",
    "VAULT_ALLOWED_HOSTS",
    "VAULT_REGISTRATION",
    "VAULT_PASSKEYS",
    "VAULT_AUTH_DIR",
    "VAULT_HTTP_STATELESS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        bare = name.removeprefix(PLUGIN_ENV_PREFIX)
        if bare in NAMES or bare.startswith(PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))


@pytest.fixture
def mailing(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com/")
    monkeypatch.setenv("VAULT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("VAULT_SMTP_FROM", "Vault <vault@example.com>")
    return monkeypatch


def test_a_server_refuses_to_start_without_a_public_url():
    with pytest.raises(ConfigError, match="VAULT_PUBLIC_URL"):
        auth_config()


def test_a_plain_http_public_url_is_refused_off_localhost(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_PUBLIC_URL", "http://vault.example.com")
    with pytest.raises(ConfigError, match="VAULT_PUBLIC_URL"):
        auth_config()
    mailing.setenv("VAULT_PUBLIC_URL", "http://localhost:8765")
    assert auth_config().public_url == "http://localhost:8765"


def test_a_server_refuses_to_start_without_a_way_to_sign_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    with pytest.raises(ConfigError, match="no way to sign in"):
        auth_config()


def test_email_links_read_their_smtp_settings(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_SMTP_USERNAME", "mailer")
    mailing.setenv("VAULT_SMTP_PASSWORD", "p4ss")
    config = auth_config()
    assert config.public_url == config.issuer == "https://vault.example.com"
    assert config.smtp is not None
    assert (config.smtp.host, config.smtp.port, config.smtp.security) == ("smtp.example.com", 587, "starttls")
    assert config.smtp.sender == "Vault <vault@example.com>"
    assert "p4ss" not in repr(config)
    assert (config.github, config.oidc, config.forwarded) == (None, None, None)
    assert config.passkeys is True and config.registration == "invite"


def test_implicit_tls_defaults_to_port_465(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_SMTP_SECURITY", "ssl")
    assert auth_config().smtp.port == 465
    mailing.setenv("VAULT_SMTP_PORT", "2465")
    assert auth_config().smtp.port == 2465


def test_smtp_without_tls_refuses_a_password(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_SMTP_SECURITY", "none")
    assert auth_config().smtp.port == 25
    mailing.setenv("VAULT_SMTP_PASSWORD", "p4ss")
    with pytest.raises(ConfigError, match="in the clear"):
        auth_config()


def test_smtp_needs_a_from_address(mailing: pytest.MonkeyPatch):
    mailing.delenv("VAULT_SMTP_FROM")
    with pytest.raises(ConfigError, match="VAULT_SMTP_FROM"):
        auth_config()


def test_github_needs_its_secret(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_GITHUB_CLIENT_ID", "Iv1.abc")
    with pytest.raises(ConfigError, match="VAULT_GITHUB_CLIENT_SECRET"):
        auth_config()
    mailing.setenv("VAULT_GITHUB_CLIENT_SECRET", "gh-s3cret")
    config = auth_config()
    assert config.github.client_id == "Iv1.abc"
    assert config.github_redirect_uri == "https://vault.example.com/signin/github/callback"
    assert "gh-s3cret" not in repr(config)


@pytest.mark.parametrize("missing", ["VAULT_OIDC_CLIENT_ID", "VAULT_OIDC_CLIENT_SECRET"])
def test_an_oidc_provider_needs_its_client(monkeypatch: pytest.MonkeyPatch, missing: str):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    for name in ("VAULT_OIDC_ISSUER", "VAULT_OIDC_CLIENT_ID", "VAULT_OIDC_CLIENT_SECRET"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError, match=missing):
        auth_config()


def test_an_oidc_provider_reads_its_issuer_scopes_and_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_OIDC_ISSUER", "https://id.example.com/")
    monkeypatch.setenv("VAULT_OIDC_CLIENT_ID", "vault")
    monkeypatch.setenv("VAULT_OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("VAULT_OIDC_NAME", "Pocket ID")
    config = auth_config()
    assert config.oidc.issuer == "https://id.example.com"
    assert config.oidc.scopes == ("openid", "email", "profile")
    assert config.oidc.name == "Pocket ID"
    assert config.oidc_redirect_uri == "https://vault.example.com/oidc/callback"
    assert "s3cret" not in repr(config)


def test_a_trusted_proxy_needs_its_key_and_issuer(monkeypatch: pytest.MonkeyPatch, proxy: Proxy):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_IDENTITY_PUBLIC_KEY", proxy.pem)
    with pytest.raises(ConfigError, match="VAULT_IDENTITY_ISSUER"):
        auth_config()
    monkeypatch.setenv("VAULT_IDENTITY_ISSUER", "mcp-proxy.example.com")
    monkeypatch.setenv("VAULT_IDENTITY_READ_CLAIMS", "vault vault:read")
    forwarded = auth_config().forwarded
    assert forwarded.header == "X-Forwarded-Identity"
    assert forwarded.read_claims == ("vault", "vault:read")
    assert forwarded.write_claims == (WRITE_SCOPE,)


def test_a_proxy_key_that_is_not_pem_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_IDENTITY_PUBLIC_KEY", "not a key")
    monkeypatch.setenv("VAULT_IDENTITY_ISSUER", "proxy")
    with pytest.raises(ConfigError, match="PEM"):
        auth_config()


def test_registration_and_passkeys_are_settings(mailing: pytest.MonkeyPatch):
    mailing.setenv("VAULT_REGISTRATION", "open")
    mailing.setenv("VAULT_PASSKEYS", "off")
    config = auth_config()
    assert config.registration == "open" and config.passkeys is False
    mailing.setenv("VAULT_REGISTRATION", "anyone")
    with pytest.raises(ConfigError, match="VAULT_REGISTRATION"):
        auth_config()


def test_stateless_http_is_off_unless_turned_on(mailing: pytest.MonkeyPatch):
    assert auth_config().stateless_http is False
    mailing.setenv("VAULT_HTTP_STATELESS", "on")
    assert auth_config().stateless_http is True
    mailing.setenv("VAULT_HTTP_STATELESS", "yes")
    with pytest.raises(ConfigError, match="VAULT_HTTP_STATELESS"):
        auth_config()


def test_the_allowed_hosts_are_read_as_a_space_or_comma_separated_list(mailing: pytest.MonkeyPatch):
    assert auth_config().allowed_hosts == ()
    mailing.setenv("VAULT_ALLOWED_HOSTS", "vault.svc.cluster.local, vault.lan")
    assert auth_config().allowed_hosts == ("vault.svc.cluster.local", "vault.lan")


def test_a_path_in_the_public_url_is_the_prefix_of_every_route():
    assert AuthConfig(public_url="https://vault.example.com").path_prefix == ""
    assert AuthConfig(public_url="https://host.example.com/vault/").path_prefix == "/vault"
    config = AuthConfig(public_url="https://host.example.com/vault")
    assert config.resource_url == "https://host.example.com/vault/mcp"
    assert config.public_origin == "https://host.example.com"
    assert config.public_host == "host.example.com"


def test_the_scopes_are_read_and_write():
    assert (READ_SCOPE, WRITE_SCOPE) == ("vault:read", "vault:write")
