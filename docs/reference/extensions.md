# Extensions

An extension is a Python package installed beside the server that adds to it: tables in the account store, pages, routes, readiness checks, CLI commands, or a limit on what an account may do. The server finds it through an entry point and calls its hooks while it starts.

Import everything an extension builds on from `notes_vault_mcp.extension_api`. The names there stay put between releases; the modules they come from may move.

## Register the package

Name a factory that returns an `Extension` in the `notes_vault_mcp.extensions` entry point group:

```toml
[project.entry-points."notes_vault_mcp.extensions"]
orchard = "orchard_extension:Orchard"
```

```python
from notes_vault_mcp.extension_api import Extension, Server


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
| `health_checks` | Functions `() -> bool` that `/ready` runs; any that returns `False` or raises makes it answer 503 and name the check. |

Escape everything a section prints with `esc`, and wrap a route that takes a form or a secret in `limited` so it shares the server's rate limits.

## The account store

`Database` is what the account store runs on: `connect()` gives a `Connection` that commits when the block ends cleanly, and SQL uses `?` parameters. Write SQL that both SQLite and the database you supply run: `INSERT … ON CONFLICT … DO …` rather than `INSERT OR …`, and `database.tables(connection)` and `database.columns(connection, table)` rather than SQLite's catalogue. `has_table(connection, name)` checks a table on SQLite alone. `STORE_VERSION` is the number of the store's own migrations, so a tool that copies the store can check it is copying the version this server writes. `rewrite_slugs(connection)` gives every vault whose slug breaks the one-word rule the slug the server would make today, for a database that brings its own schema.

## The change bus

A `ChangeBus` (`publish`, `listen`, `close`) carries `Change(vault_id, kind, origin)` messages. The server publishes one after each write, layout change or vault deletion, and calls `listen` once with the function that applies a change it receives: `write` marks the vault's index stale, `schema` reloads its layout, `gone` drops the vault and its local index, `all` marks every open vault stale. A change carrying the server's own `origin` is ignored, so a bus may hand the server its own changes back. Without a bus, an open vault notices writes made to its storage outside the server by re-reading its listing.

## Names

`notes_vault_mcp.extension_api` exports:

- Hooks and the server: `Extension`, `Server`, `Access`, `Section`, `VaultSection`, `HealthCheck`, `Subcommands`.
- The account store: `Database`, `Connection`, `HostedStore`, `Account`, `Vault`, `Identities`, `STORE_VERSION`, `rewrite_slugs`, `has_table`, `account_of`, `digest`, `scope_label`, `MACHINE_KEY_PURPOSE`, `INVITATION_TTL`.
- Pages and mail: `Browser`, `page`, `esc`, `stamp`, `limited`, `Mailer`, `SmtpFactory`, `EMAIL_ADDRESS`.
- Vaults and storage: `SubjectVaults`, `SealedVaults`, `SelfDeletion`, `VaultFacts`, `VaultBackend`, `make_subject_backend`, `MUST_NOT_EXIST`, `VersionConflict`, `honours_conditions`.
- Changes: `ChangeBus`, `Change`, `Kind`, `Listener`.
- Settings: `AuthConfig`, `ConfigError`, `env`, `local_root`, `auth_dir`, `semantic_requested`.
