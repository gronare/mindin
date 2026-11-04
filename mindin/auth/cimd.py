from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl, ValidationError

from mindin.auth import SCOPES

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT = 5.0
DEFAULT_CACHE_SECONDS = 3600
MAX_CACHE_SECONDS = 24 * 3600
FETCHES_PER_MINUTE = 30
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
ALLOWED_GRANTS = frozenset({"authorization_code", "refresh_token"})
MAX_AGE_RE = re.compile(r"max-age=(\d+)")
SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*")
UNSAFE_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "about"})

Resolver = Callable[[str, int], Awaitable[list[str]]]


class ClientMetadataError(Exception):
    pass


def is_metadata_url(client_id: str) -> bool:
    return client_id.startswith("https://")


def is_loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def same_redirect(registered: str, requested: str) -> bool:
    if registered == requested:
        return True
    if not (is_loopback(registered) and is_loopback(requested)):
        return False
    left, right = urlsplit(registered), urlsplit(requested)
    return (left.hostname, left.path, left.query, left.fragment) == (
        right.hostname,
        right.path,
        right.query,
        right.fragment,
    )


def allowed_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return is_loopback(uri)
    return bool(SCHEME_RE.fullmatch(parts.scheme)) and parts.scheme not in UNSAFE_SCHEMES


class HostedClient(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            return super().validate_redirect_uri(None)
        requested = str(redirect_uri)
        if any(same_redirect(str(registered), requested) for registered in self.redirect_uris or []):
            return redirect_uri
        raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")


def check_metadata_url(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ClientMetadataError("a client id metadata URL must be https with a host")
    if parts.username or parts.password or parts.fragment:
        raise ClientMetadataError("a client id metadata URL may carry neither credentials nor a fragment")
    if parts.path in ("", "/") or any(segment in (".", "..") for segment in parts.path.split("/")):
        raise ClientMetadataError("a client id metadata URL needs a path without dot segments")
    return parts.hostname, parts.port or 443


def public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(answer[4][0]) for answer in answers))


def cache_seconds(header: str) -> int:
    lowered = header.lower()
    if "no-store" in lowered or "no-cache" in lowered:
        return 0
    found = MAX_AGE_RE.search(lowered)
    if found is None:
        return DEFAULT_CACHE_SECONDS
    return min(int(found.group(1)), MAX_CACHE_SECONDS)


def client_from_document(url: str, document: Any) -> HostedClient:
    if not isinstance(document, dict):
        raise ClientMetadataError("the client metadata document is not a JSON object")
    if document.get("client_id") != url:
        raise ClientMetadataError("the document's client_id is not the URL it was fetched from")
    redirects = document.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or not all(isinstance(uri, str) for uri in redirects):
        raise ClientMetadataError("the document lists no redirect_uris")
    if not all(allowed_redirect(uri) for uri in redirects):
        raise ClientMetadataError("a redirect URI is neither https, a loopback address nor an app scheme")
    method = document.get("token_endpoint_auth_method") or "none"
    if method != "none" or "client_secret" in document:
        raise ClientMetadataError("only public clients (token_endpoint_auth_method none) may use a metadata document")
    listed = document.get("grant_types") or ["authorization_code"]
    if not isinstance(listed, list) or "authorization_code" not in listed:
        raise ClientMetadataError("the document does not ask for the authorization_code grant")
    grants = [grant for grant in listed if grant in ALLOWED_GRANTS]
    name = document.get("client_name")
    try:
        return HostedClient(
            client_id=url,
            client_name=name if isinstance(name, str) and name.strip() else urlsplit(url).hostname,
            redirect_uris=redirects,
            token_endpoint_auth_method="none",
            grant_types=[*grants, "refresh_token"] if "refresh_token" not in grants else grants,
            response_types=["code"],
            scope=" ".join(SCOPES),
        )
    except ValidationError as exc:
        raise ClientMetadataError(f"the client metadata document is malformed: {exc.error_count()} errors") from exc


class ClientMetadataFetcher:
    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.resolver = resolver or system_resolver
        self.clock = clock
        self._cache: dict[str, tuple[HostedClient, float]] = {}
        self._fetches: list[float] = []

    async def client(self, url: str) -> HostedClient:
        cached = self._cache.get(url)
        if cached is not None and cached[1] > self.clock():
            return cached[0]
        host, port = check_metadata_url(url)
        self._throttle()
        document, lifetime = await self._fetch(url, host, port)
        client = client_from_document(url, document)
        if lifetime:
            self._cache[url] = (client, self.clock() + lifetime)
        else:
            self._cache.pop(url, None)
        return client

    def _throttle(self) -> None:
        now = self.clock()
        self._fetches = [moment for moment in self._fetches if now - moment < 60]
        if len(self._fetches) >= FETCHES_PER_MINUTE:
            raise ClientMetadataError("too many client metadata fetches; try again in a minute")
        self._fetches.append(now)

    async def _fetch(self, url: str, host: str, port: int) -> tuple[Any, int]:
        try:
            addresses = await asyncio.wait_for(self.resolver(host, port), FETCH_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            raise ClientMetadataError(f"cannot resolve {host}") from exc
        if not addresses or not all(public_address(address) for address in addresses):
            raise ClientMetadataError(f"{host} resolves to an address this server will not fetch from")
        pinned = pinned_url(url, addresses[0])
        headers = {"Host": host if port == 443 else f"{host}:{port}", "Accept": "application/json"}
        try:
            async with httpx2.AsyncClient(
                transport=self.transport, timeout=FETCH_TIMEOUT, follow_redirects=False, trust_env=False
            ) as http:
                request = http.build_request("GET", pinned, headers=headers, extensions={"sni_hostname": host})
                response = await http.send(request, stream=True)
                try:
                    body = await bounded_body(response)
                finally:
                    await response.aclose()
        except httpx2.HTTPError as exc:
            raise ClientMetadataError(f"fetching the client metadata document failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ClientMetadataError(f"the client metadata document answered {response.status_code}")
        try:
            document = json.loads(body)
        except ValueError as exc:
            raise ClientMetadataError("the client metadata document is not JSON") from exc
        return document, cache_seconds(response.headers.get("cache-control", ""))


def pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    ip = ipaddress.ip_address(address)
    netloc = f"[{ip}]" if ip.version == 6 else str(ip)
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


async def bounded_body(response: httpx2.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
        raise ClientMetadataError(f"the client metadata document is larger than {MAX_DOCUMENT_BYTES} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_DOCUMENT_BYTES:
            raise ClientMetadataError(f"the client metadata document is larger than {MAX_DOCUMENT_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
