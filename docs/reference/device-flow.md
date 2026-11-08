# Device flow

A tool that speaks the OAuth device flow can sign itself in to a vault server the way [`login`](../how-to/sign-in-a-machine.md) does.

1. `POST /device_authorization` with `name=<machine>`, optionally `scope=vault:read`, and optionally `vault=<slug>` to ask for that vault. It answers `device_code`, `user_code`, `verification_uri`, `verification_uri_complete`, `interval` and `expires_in`.
2. The person opens `VAULT_PUBLIC_URL/device`, enters the code and approves. Without `vault=<slug>` they pick a vault when approving.
3. Poll `POST /token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code` and the `device_code` every `interval` seconds.

`/token` answers:

| Answer | Meaning |
| --- | --- |
| `authorization_pending` | The person has not approved yet. |
| `slow_down` | Polled too often. |
| `access_denied` | The person refused. |
| `expired_token` | The code ran out. |
| `{"access_token": "nvm_...", "token_type": "Bearer", "scope": "...", "key_name": "...", "resource": ".../mcp", "vault": "<slug>", "vault_name": "..."}` | Approved. Send the token as `Authorization: Bearer`. |

## Limits of the key's vault

`GET /api/vault/limits` with the key as `Authorization: Bearer` answers what the server takes into that vault, so a tool can check files before it sends them:

```json
{"vault": "notes", "kind": "personal", "write": true, "file_bytes": 5000000, "account_bytes": 1000000000,
 "content_types": ["text/*", "image/*", "application/pdf", "application/json"],
 "used_bytes": 52000000, "vault_bytes": 52000000, "upload_bytes": 33554432}
```

`file_bytes`, `account_bytes` and `content_types` are the [storage limits](extensions.md#hooks) an extension sets for the vault's owner, `null` and `[]` when there are none. `used_bytes` counts every vault the owner has, `vault_bytes` this one, and `upload_bytes` is the largest body one WebDAV `PUT` takes. A file's type is judged by its name.

A missing or unknown key, a key whose account is not active, or a key to a vault whose owner is gone answers `401` with `{"error": "invalid_token"}`. A key an extension leaves no access answers `403` with `{"error": "insufficient_scope"}`.
