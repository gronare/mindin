# Extensions

An extension is a Python package installed beside the server that adds to it: tables in the account store, pages, routes, readiness checks, background work, CLI commands, a say in which new accounts are made, a check on the email-link form, or a limit on what an account may do or store. The server finds it through an entry point and calls its hooks while it starts.

Import everything an extension builds on from `mindin.extension_api`. The names there stay put between releases; the modules they come from may move.

## Register the package

Name a factory that returns an `Extension` in the `mindin.extensions` entry point group:

```toml
[project.entry-points."mindin.extensions"]
orchard = "orchard_extension:Orchard"
```

```python
from mindin.extension_api import Extension, Server


class Orchard(Extension):
    def attach(self, server: Server) -> None:
        server.account_sections.append(lambda account, csrf: "<h2>Orchard</h2>")
```

Every hook has a default that does nothing, so a class overrides only what it needs. With several extensions installed, they run in the order of their entry point names.

## Hooks

| Hook | When it runs | What it returns |
| --- | --- | --- |
| `migrate(connection)` | Every time the account store opens, after the server's own tables. | Nothing. Create or upgrade this extension's own tables with `CREATE … IF NOT EXISTS`. |
| `database(directory)` | Once, when the account store opens. | A `Database` to keep the account store in, or `None` for `hosted.sqlite` in `directory`. The first extension that returns one wins. |
| `commands(subparsers)` | When the CLI builds its parser. | Nothing. Add subcommands with `subparsers.add_parser(...)`. |
| `attach(server)` | Once, while the HTTP server is built. | Nothing. Add to the lists on `server`. |
| `bus(server)` | Once, right after `attach`. | A `ChangeBus`, or `None` for none. The first extension that returns one wins. |
| `access(account)` | On every request an account makes. | `"full"`, `"read"` or `"none"`. The strictest answer of all extensions applies. |
| `storage_limits(account, limits)` | On every write to a vault the account owns, through MCP tools, WebDAV, a zip import, `mindin accounts import` and `mindin move`, and on `GET /api/vault/limits`. | The `StorageLimits` for that account, given the ones earlier extensions chose, which start with none; `with_defaults(limits, file_bytes=…)` fills only those left unset. The server refuses a write past them with the limit named; reading, export and deleting still work. Without an extension there are no limits. |
| `refused_write(account, vault, key, reason)` | Each time the storage limits refuse a write to a vault the account owns, through MCP tools, WebDAV, a zip import or `mindin accounts import`, after the refusal is decided. A dry run refuses nothing, and a file `mindin move` holds back after reading the limits never reaches the server. | Nothing. `reason` is the `LimitReached`, with the message the writer sees and its `status` (413 for a file too large, 415 for a type not stored, 507 for a full account). `key` is the refused path, or `""` when an import is refused as a whole for filling the account. An exception here is logged, and the write is refused all the same. |
| `admits(registrant)` | Before sign-in makes a new account. | `""` to make it, or the reason it is refused, shown on the page. The first refusal wins. |
| `sign_in_check()` | Once, while the HTTP server is built with email links on. | A `SignInCheck(html, sources, passed)`, or `None` for none. `html` goes inside the email-link form, `sources` adds to the sign-in page's `Content-Security-Policy` (such as `{"script-src": ("https://check.example",)}`), and `await passed(fields, address)` gets the submitted form's fields and the client's address; a `False` from any check answers the form with an error and sends no link. |
| `registered(connection, account, registrant)` | Inside the transaction that makes any new account: from a sign-in, from `mindin accounts invite`, or added to an organisation by an admin. Once per account. | Nothing. Write rows for the new account on `connection`; they commit with it. |
| `may_erase(account)` | Before the server deletes an account on its own, as when an organisation removed it under export-and-delete. | `True` to delete it, `False` to keep it. Any `False` keeps it. |

A `Registrant` carries the sign-in's `method`, `subject` (for GitHub the numeric user id), its verified `emails`, its `label`, and `invited`, whether a shared vault or an organisation's domain invited one of the addresses. For an account made without a sign-in, `method` is `organisation` when an organisation's admin added it on the organisation page, or `operator` when the command line made it (`mindin accounts invite`, `mindin org create`, `mindin org member add`, `mindin org admin add`); an account made for an organisation carries its name as `label`; `subject` and `emails` hold the address, and `invited` is true. That account signs in later without a second call.

## The server an extension attaches to

`attach` and `bus` receive a `Server`:

| Field | What it is |
| --- | --- |
| `config` | The server's `AuthConfig`: `path_prefix`, `issuer` and the rest of its settings. |
| `store` | The `HostedStore` holding accounts and vaults; `store.database` is the `Database` it runs on. |
| `clock` | The server's clock, seconds since the epoch. |
| `browser` | The `Browser` that signs people in on pages; `browser.signed_in(request)` gives the signed-in account and its CSRF token, or `None`. |
| `vaults` | The `SubjectVaults` that open each vault on first use. |
| `sealed` | The `SealedVaults` that seal vault storage; `sealed.prefix` is the storage prefix of all vaults, `sealed.facts(vault_id)` counts a vault's notes and bytes. |
| `deletion` | The `SelfDeletion` that erases an account and its vaults. |
| `smtp_factory` | The SMTP client factory to hand `Mailer`, or `None` for `smtplib`. |
| `routes` | Starlette routes to add. Put them under `config.path_prefix`. |
| `account_sections` | Functions `(account, csrf) -> html` that add a section to the Account page (`/account/settings`). |
| `vault_sections` | Functions `(account, vault, csrf) -> html` that add a section to a vault's page. |
| `organisation_sections` | Functions `(organisation, viewer, csrf) -> html` that add a section to an organisation's page (`/account/organisation/{id}`), which only its admins open; `viewer` is the signed-in admin's `Account`. |
| `health_checks` | Functions `() -> bool` that `/ready` runs; any that returns `False` or raises makes it answer 503 and name the check. |
| `periodic` | `Periodic(name, seconds, run)` entries: the server calls `run()` in a thread of its own when it starts, then every `seconds` until it stops. A run that raises is logged and the next one still comes. Servers that share one account store take turns: each period, only the one that claims the job's name runs it. Make `run` safe to repeat. |

Escape everything a section prints with `esc`, and wrap a route that takes a form or a secret in `limited` so it shares the server's rate limits.

## The account store

`Database` is what the account store runs on: `connect()` gives a `Connection` that commits when the block ends cleanly, and SQL uses `?` parameters. Write SQL that both SQLite and the database you supply run: `INSERT … ON CONFLICT … DO …` rather than `INSERT OR …`, and `database.tables(connection)` and `database.columns(connection, table)` rather than SQLite's catalogue. `has_table(connection, name)` checks a table on SQLite alone. `STORE_VERSION` is the number of the store's own migrations, so a tool that copies the store can check it is copying the version this server writes. `rewrite_slugs(connection)` gives every vault whose slug breaks the one-word rule the slug the server would make today, for a database that brings its own schema.

## The change bus

A `ChangeBus` (`publish`, `listen`, `close`) carries `Change(vault_id, kind, origin)` messages. The server publishes one after each write, layout change or vault deletion, and calls `listen` once with the function that applies a change it receives: `write` marks the vault's index stale, `schema` reloads its layout, `gone` drops the vault and its local index, `all` marks every open vault stale. A change carrying the server's own `origin` is ignored, so a bus may hand the server its own changes back. Without a bus, an open vault notices writes made to its storage outside the server by re-reading its listing.

## Names

`mindin.extension_api` exports:

- Hooks and the server: `Extension`, `Server`, `Access`, `Section`, `VaultSection`, `OrganisationSection`, `HealthCheck`, `SignInCheck`, `Subcommands`.
- The account store: `Database`, `Connection`, `HostedStore`, `Account`, `Vault`, `Organisation`, `Organisations`, `OrganisationError`, `Identities`, `Proof`, `SupportAccess`, `SupportRequest`, `RequestRefused`, `ask_for_support`, `STORE_VERSION`, `rewrite_slugs`, `has_table`, `account_tables`, `account_of`, `digest`, `scope_label`, `delete_departed`, `SHARED`, `MACHINE_KEY_PURPOSE`, `INVITATION_TTL`.
- Pages and mail: `Browser`, `page`, `esc`, `stamp`, `limited`, `RateCounter`, `Mailer`, `SmtpFactory`, `EMAIL_ADDRESS`, and `branded(subject, heading, lead, button, link, note)`, a `Mail` in the sign-in mails' look whose `images()` go with it to `Mailer.send`.
- Accounts made and storage kept: `Registrant`, `Periodic`, `Leases`, `StorageLimits`, `with_defaults`, `LimitReached`.
- Vaults and storage: `SubjectVaults`, `SealedVaults`, `SelfDeletion`, `VaultFacts`, `VaultBackend`, `S3Backend`, `make_subject_backend`, `MUST_NOT_EXIST`, `VersionConflict`, `honours_conditions`, `Keyring`, `MasterKey`, `VaultKeys`, `History`, `Actor`.
- Changes: `ChangeBus`, `Change`, `Kind`, `Listener`.
- Support requests: `ask_for_support(store, config, account, vault, operator, reason)` records a request for one vault, or any with `vault=None`, and emails the account; it returns the `SupportRequest` and whether the email went out, and raises `RequestRefused` when the account already has one waiting. `SupportAccess(store)` lists them with `requests()`, reads an account's with `pending(account_id)` and takes one back with `withdraw(account_id)`.
- Settings: `AuthConfig`, `SmtpConfig`, `GithubConfig`, `OidcConfig`, `ConfigError`, `env`, `local_root`, `auth_dir`, `semantic_requested`.
- Running the server in a test: `build_hosted_app`, `cli_main`, `installed`, `passes`.
