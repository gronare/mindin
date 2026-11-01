from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2

PUBLIC_ADDRESS = "93.184.216.34"
PRIVATE_HOSTS = {"intranet.example.test": "10.0.0.5", "metadata.example.test": "169.254.169.254"}


@dataclass
class MetadataHost:
    documents: dict[str, tuple[int, bytes, dict[str, str]]] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    def serve(self, url: str, document: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.documents[url] = (status, body, {"content-type": "application/json", **(headers or {})})

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == PUBLIC_ADDRESS, "the fetch must go to the address that was checked"
        assert request.extensions.get("sni_hostname") == request.headers["host"]
        url = f"https://{request.headers['host']}{request.url.raw_path.decode()}"
        self.fetched.append(url)
        status, body, headers = self.documents.get(url, (404, b"{}", {}))
        return httpx2.Response(status, content=body, headers=headers)

    async def resolve(self, host: str, port: int) -> list[str]:
        return [PRIVATE_HOSTS.get(host, PUBLIC_ADDRESS)]
