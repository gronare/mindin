from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from notes_vault_mcp.config import ConfigError, cache_dir, env, private_dir

Registration = Literal["invite", "open"]
SmtpSecurity = Literal["starttls", "ssl", "none"]
READ_SCOPE = "vault:read"
WRITE_SCOPE = "vault:write"
SCOPES = (READ_SCOPE, WRITE_SCOPE)
MACHINE_KEY_PURPOSE = "device"
DEFAULT_IDENTITY_HEADER = "X-Forwarded-Identity"
DEFAULT_SUBJECT_PREFIX = "users"
DEFAULT_OIDC_SCOPES = ("openid", "email", "profile")
SMTP_PORTS = {"starttls": 587, "ssl": 465, "none": 25}

NO_PUBLIC_URL = (
    "notes-vault-mcp: an HTTP server needs VAULT_PUBLIC_URL, the https address clients reach it on "
    "(for example https://vault.example.com); there is no default and never a fallback."
)
NO_SIGN_IN = (
    "notes-vault-mcp: no way to sign in is configured. Set at least one of VAULT_SMTP_HOST (email links), "
    "VAULT_GITHUB_CLIENT_ID (GitHub), VAULT_OIDC_ISSUER (an OpenID provider) or VAULT_IDENTITY_PUBLIC_KEY "
    "(a trusted proxy); passkeys are added on the account page after a first sign-in."
)
BAD_IDENTITY_KEY = "notes-vault-mcp: VAULT_IDENTITY_PUBLIC_KEY is not a PEM public key: {reason}"


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    sender: str
    port: int = 587
    security: SmtpSecurity = "starttls"
    username: str = ""
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class GithubConfig:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    scopes: tuple[str, ...] = DEFAULT_OIDC_SCOPES
    name: str = "your organisation"


@dataclass(frozen=True)
class ForwardedConfig:
    public_key: str
    issuer: str
    header: str = DEFAULT_IDENTITY_HEADER
    typ: str = ""
    write_claims: tuple[str, ...] = (WRITE_SCOPE,)
    read_claims: tuple[str, ...] = (READ_SCOPE,)
    name: str = "your company's sign-in"


@dataclass(frozen=True)
class AuthConfig:
    public_url: str = ""
    issuer: str = ""
    auth_dir: Path | None = None
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX
    allowed_hosts: tuple[str, ...] = ()
    registration: Registration = "invite"
    passkeys: bool = True
    stateless_http: bool = False
    index_snapshots: bool = False
    snapshot_minutes: float = 10.0
    smtp: SmtpConfig | None = None
    github: GithubConfig | None = None
    oidc: OidcConfig | None = None
    forwarded: ForwardedConfig | None = None

    @property
    def path_prefix(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/") if self.public_url else ""

    @property
    def public_host(self) -> str:
        url = self.public_url if "://" in self.public_url else f"//{self.public_url}"
        return urlsplit(url).hostname or ""

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def resource_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"

    @property
    def dav_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/dav/"

    @property
    def oidc_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/oidc/callback"

    @property
    def github_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/signin/github/callback"


def auth_dir() -> Path:
    raw = env("VAULT_AUTH_DIR")
    return private_dir(Path(raw).expanduser() if raw else cache_dir() / "auth")


def public_key_pem(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("-----BEGIN"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ConfigError(BAD_IDENTITY_KEY.format(reason="neither PEM nor base64 PEM")) from exc
    from cryptography.hazmat.primitives import serialization

    try:
        serialization.load_pem_public_key(text.encode("utf-8"))
    except ValueError as exc:
        raise ConfigError(BAD_IDENTITY_KEY.format(reason=exc)) from exc
    return text


def _text(name: str) -> str:
    return (env(name) or "").strip()


def _words(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_text(name).replace(",", " ").split()) or default


def _needs(name: str, because: str) -> str:
    value = _text(name)
    if not value:
        raise ConfigError(f"notes-vault-mcp: {because} needs {name} as well")
    return value


def _minutes(name: str, default: float) -> float:
    raw = _text(name)
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = -1.0
    if value < 0:
        raise ConfigError(f"notes-vault-mcp: {name} must be a number of minutes, 0 or more")
    return value


def _choice(name: str, choices: tuple[str, ...], default: str) -> str:
    value = _text(name).lower() or default
    if value not in choices:
        raise ConfigError(f"notes-vault-mcp: {name} must be one of {', '.join(choices)}")
    return value


def auth_config() -> AuthConfig:
    public_url = _text("VAULT_PUBLIC_URL").rstrip("/")
    if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
        raise ConfigError(NO_PUBLIC_URL)
    config = AuthConfig(
        public_url=public_url,
        issuer=public_url,
        auth_dir=auth_dir(),
        subject_prefix=(_text("VAULT_SUBJECT_PREFIX") or DEFAULT_SUBJECT_PREFIX).strip("/"),
        allowed_hosts=_words("VAULT_ALLOWED_HOSTS", ()),
        registration="open" if _choice("VAULT_REGISTRATION", ("invite", "open"), "invite") == "open" else "invite",
        passkeys=_choice("VAULT_PASSKEYS", ("on", "off"), "on") == "on",
        stateless_http=_choice("VAULT_HTTP_STATELESS", ("on", "off"), "off") == "on",
        index_snapshots=_choice("VAULT_INDEX_SNAPSHOTS", ("on", "off"), "off") == "on",
        snapshot_minutes=_minutes("VAULT_INDEX_SNAPSHOT_MINUTES", 10.0),
        smtp=smtp_config(),
        github=github_config(),
        oidc=oidc_config(),
        forwarded=forwarded_config(),
    )
    if not (config.smtp or config.github or config.oidc or config.forwarded):
        raise ConfigError(NO_SIGN_IN)
    return config


def smtp_config() -> SmtpConfig | None:
    host = _text("VAULT_SMTP_HOST")
    if not host:
        return None
    security = _choice("VAULT_SMTP_SECURITY", ("starttls", "ssl", "none"), "starttls")
    port = _text("VAULT_SMTP_PORT") or str(SMTP_PORTS[security])
    if not port.isdigit():
        raise ConfigError("notes-vault-mcp: VAULT_SMTP_PORT must be a port number")
    username, password = _text("VAULT_SMTP_USERNAME"), env("VAULT_SMTP_PASSWORD") or ""
    if security == "none" and (username or password):
        raise ConfigError(
            "notes-vault-mcp: VAULT_SMTP_SECURITY=none would send the SMTP password in the clear; use starttls or ssl"
        )
    return SmtpConfig(
        host=host,
        sender=_needs("VAULT_SMTP_FROM", "VAULT_SMTP_HOST"),
        port=int(port),
        security="ssl" if security == "ssl" else "none" if security == "none" else "starttls",
        username=username,
        password=password,
    )


def github_config() -> GithubConfig | None:
    client_id = _text("VAULT_GITHUB_CLIENT_ID")
    if not client_id:
        return None
    return GithubConfig(client_id=client_id, client_secret=_needs("VAULT_GITHUB_CLIENT_SECRET", "GitHub sign-in"))


def oidc_config() -> OidcConfig | None:
    issuer = _text("VAULT_OIDC_ISSUER").rstrip("/")
    if not issuer:
        return None
    return OidcConfig(
        issuer=issuer,
        client_id=_needs("VAULT_OIDC_CLIENT_ID", "VAULT_OIDC_ISSUER"),
        client_secret=_needs("VAULT_OIDC_CLIENT_SECRET", "VAULT_OIDC_ISSUER"),
        scopes=_words("VAULT_OIDC_SCOPES", DEFAULT_OIDC_SCOPES),
        name=_text("VAULT_OIDC_NAME") or OidcConfig.name,
    )


def forwarded_config() -> ForwardedConfig | None:
    raw_key = _text("VAULT_IDENTITY_PUBLIC_KEY")
    if not raw_key:
        return None
    return ForwardedConfig(
        public_key=public_key_pem(raw_key),
        issuer=_needs("VAULT_IDENTITY_ISSUER", "VAULT_IDENTITY_PUBLIC_KEY"),
        header=_text("VAULT_IDENTITY_HEADER") or DEFAULT_IDENTITY_HEADER,
        typ=_text("VAULT_IDENTITY_TYP"),
        write_claims=_words("VAULT_IDENTITY_WRITE_CLAIMS", (WRITE_SCOPE,)),
        read_claims=_words("VAULT_IDENTITY_READ_CLAIMS", (READ_SCOPE,)),
        name=_text("VAULT_IDENTITY_NAME") or ForwardedConfig.name,
    )
