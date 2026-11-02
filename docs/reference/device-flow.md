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
