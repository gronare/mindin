# Set up sign-in methods

A server has no passwords. Each method below is on when its settings are present, and the sign-in page at `/signin` lists only those. The server refuses to start without at least one method other than passkeys. [Configuration](../reference/configuration.md#sign-in) lists every setting with its default.

| Method | Settings | Register this callback |
| --- | --- | --- |
| Email link | `VAULT_SMTP_HOST`, `VAULT_SMTP_FROM`, `VAULT_SMTP_USERNAME`, `VAULT_SMTP_PASSWORD` | |
| GitHub | `VAULT_GITHUB_CLIENT_ID`, `VAULT_GITHUB_CLIENT_SECRET` | `VAULT_PUBLIC_URL/signin/github/callback` |
| Your identity provider (Pocket ID, Okta, Google Workspace, Keycloak) | `VAULT_OIDC_ISSUER`, `VAULT_OIDC_CLIENT_ID`, `VAULT_OIDC_CLIENT_SECRET` | `VAULT_PUBLIC_URL/oidc/callback` |
| A trusted proxy | `VAULT_IDENTITY_PUBLIC_KEY`, `VAULT_IDENTITY_ISSUER` | |
| Passkeys | on unless `VAULT_PASSKEYS=off` | |

## Email link

Set the SMTP server, the sender and the login. A link works once, for 15 minutes. `VAULT_SMTP_SECURITY` is `starttls` (port 587, the default), `ssl` (465) or `none` (25, only without a username and password); `VAULT_SMTP_PORT` overrides the port.

## GitHub

Create an OAuth app at github.com/settings/developers with the callback above, and set its client ID and secret. The account is tied to the GitHub user, so a renamed login keeps working; only verified emails count.

## Your identity provider

Register one confidential client with the callback above, and set the issuer, client ID and secret. `VAULT_OIDC_NAME` is the button's label; `VAULT_OIDC_SCOPES` defaults to `openid email profile`.

## A trusted proxy

When a proxy in front authenticates people and forwards a signed identity token (the pattern Cloudflare Access uses), the server verifies it with `VAULT_IDENTITY_PUBLIC_KEY` (PEM, RS256 or ES256, raw or base64 on one line) and signs that person in, MCP requests included.

- The token arrives in `X-Forwarded-Identity` (`VAULT_IDENTITY_HEADER`).
- It needs `iss`, `sub`, `exp` and an `aud` naming this server's host.
- It carries an entitlement in its `entitlements` list or its `scope`: `vault:write` or `vault:read` by default (`VAULT_IDENTITY_WRITE_CLAIMS`, `VAULT_IDENTITY_READ_CLAIMS`). A token with neither is refused.
- `VAULT_IDENTITY_TYP` pins its `typ`, and `VAULT_IDENTITY_NAME` labels the button.

The proxy's email counts as verified. The header is ignored unless these settings are present.

## Passkeys

Sign in once another way, then use "Add a passkey" on the account page. After that, "Sign in with a passkey" needs nothing else.

## Several ways into one account

An account can have several ways in. A sign-in whose verified email matches an account joins it, so a person can start with an email link and later use GitHub or a passkey. The account page lists every way in and removes any but the last.
