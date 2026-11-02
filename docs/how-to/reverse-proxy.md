# Behind a reverse proxy

The Docker example ships Caddy. Any other reverse proxy works when it does these things:

1. Terminates TLS for the host in `VAULT_PUBLIC_URL`, and forwards every path to the server's port, `8765` by default.
2. Keeps the `Host` header. The MCP endpoint answers the hostname of `VAULT_PUBLIC_URL` and localhost, and answers any other `Host` with `421`. A proxy that rewrites `Host`, a Kubernetes Service name, or a LAN hostname you call the server by goes in `VAULT_ALLOWED_HOSTS`, space or comma separated.
3. Passes `/dav/*` straight through for [Obsidian](obsidian.md), with the `Authorization` header and the WebDAV verbs and headers intact: `PROPFIND`, `MKCOL`, `MOVE`, `COPY`, `Depth`, `Destination`, `Overwrite`.

When the server is not at the root of its host, put the path in `VAULT_PUBLIC_URL` (`https://example.com/vault`) and forward requests with the path unchanged: the server serves its routes under it.

A proxy that signs people in itself can pass their identity to the server; see [A trusted proxy](sign-in-methods.md#a-trusted-proxy).
