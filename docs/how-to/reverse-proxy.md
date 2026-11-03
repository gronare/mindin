# Behind a reverse proxy

The Docker example ships Caddy. Any other reverse proxy works when it does these things:

1. Terminates TLS for the host in `VAULT_PUBLIC_URL`, and forwards every path to the server's port, `8765` by default.
2. Keeps the `Host` header. The MCP endpoint answers the hostname of `VAULT_PUBLIC_URL` and localhost, and answers any other `Host` with `421`. A proxy that rewrites `Host`, a Kubernetes Service name, or a LAN hostname you call the server by goes in `VAULT_ALLOWED_HOSTS`, space or comma separated.
3. Passes `/dav/*` straight through for [Obsidian](obsidian.md), with the `Authorization` header and the WebDAV verbs and headers intact: `PROPFIND`, `MKCOL`, `MOVE`, `COPY`, `Depth`, `Destination`, `Overwrite`.
4. Tells the server who the client is, in `X-Forwarded-For`, and the server trusts that header only from the addresses in `VAULT_FORWARDED_ALLOW_IPS`. See [The client's address](#the-clients-address).

When the server is not at the root of its host, put the path in `VAULT_PUBLIC_URL` (`https://example.com/vault`) and forward requests with the path unchanged: the server serves its routes under it.

A proxy that signs people in itself can pass their identity to the server; see [A trusted proxy](sign-in-methods.md#a-trusted-proxy).

## The client's address

The server limits sign-in attempts and other requests per client address. Behind a proxy every request comes from the proxy's address, so unless the server trusts the proxy, all clients share one limit and one person's failed attempts lock everyone else out.

Set `VAULT_FORWARDED_ALLOW_IPS` to the addresses your proxies connect from, as IP addresses or CIDR ranges, comma separated: `VAULT_FORWARDED_ALLOW_IPS=10.0.0.0/24,192.0.2.10`. The server reads `X-Forwarded-For` from right to left and takes the first address it does not trust as the client.

- Trust only your own proxies. A client that reaches the server directly from a trusted address can claim any address.
- With several proxies in a row, list every one of them, and make each pass `X-Forwarded-For` on to the next, adding the address it received the request from. A proxy that drops or replaces the header from the hop before it hands the server that hop's address instead of the client's.
- The Docker example sets it to `*`, which trusts every peer: only Caddy can reach the server there, since its port is not published.

To check it, sign in from another network and read the server's log: each request line starts with the client's address, and leaves out the query string, where sign-in links carry their tokens. When it shows a proxy's address instead, a hop is missing from the list or does not pass the header on.
