# The install signal

mindin asks before it shares anonymous usage data, and sends nothing until you say yes: then, once a day, a random install id, mindin's version, the OS and CPU type and how mindin runs (plugin, CLI or server) go to signals.mindin.dev, never notes, names, paths or your vault's address. `mindin setup --signals yes|no` changes the answer.

## Asked once

`mindin setup` asks in a terminal, and the install script asks when setup did not:

```
Share anonymous usage data once a day? A random install id, mindin's version, the OS and CPU type and how mindin runs (plugin, CLI or server) go to signals.mindin.dev; never notes, names, paths or your vault's address. [y/N]
```

An empty answer is no. Either answer is saved in `~/.config/mindin/config.toml`, so you are asked once:

```toml
[signals]
install = true
```

`mindin setup --signals yes` or `--signals no` changes it. `VAULT_SIGNALS=on` or `off` in the environment, or `CLAUDE_PLUGIN_OPTION_VAULT_SIGNALS` for the plugin, wins over the saved answer. With neither, nothing is sent and no id is made: an install that never ran `setup`, such as the Claude Code plugin on its own or `uvx mindin`, sends nothing.

`mindin status` shows `"signals": "on"`, `"off"` or `"not asked"`, what decided it in `signals_source` (`env` or `config`), and the command that changes it.

## What it holds

| Field | Example | What it is |
| --- | --- | --- |
| `id` | `0123456789abcdef0123456789abcdef` | 32 random hexadecimal characters, made for this install alone after the yes. It is not derived from the machine, the user or anything else. |
| `version` | `0.14.3` | mindin's version. |
| `os` | `macos`, `linux`, `windows` | The operating system. |
| `arch` | `aarch64`, `x86_64` | The CPU type. |
| `runs` | `plugin`, `cli`, `server` | `plugin` when the Claude Code plugin started the stdio server, `cli` when another client did, `server` for `serve --transport http`. |

Nothing else goes with it: no note, vault, account, repo, path or host name, no key and no signature. Every field is limited to lowercase letters, digits and `.` `_` `+` `-`, so no text from your work can travel in one.

## When it goes

Only the long-running processes send it: the stdio server a client starts and the HTTP server. Hooks and commands never do. It runs in the background, at most once per UTC day per install, and checks once an hour whether the day has turned. The day is written down before the send, so a send that fails is dropped, not tried again that day, and it never holds up or stops the server. Nothing is sent while `CI` is set.

## How it is sent

1. `GET https://signals.mindin.dev/v1/reports/challenge` answers `{nonce, difficulty, expires, tag}`.
2. mindin finds a counter for which SHA-256 of `kebbie-report-pow-v1`, the nonce, the signal's digest and the counter as 8 bytes big-endian starts with `difficulty` zero bits. It refuses a difficulty above 26 and sends nothing.
3. `POST https://signals.mindin.dev/v1/signals` with `{"mindin": {id, version, os, arch, runs}, "challenge": <as received>, "counter": <n>}`.

The signal's digest is SHA-256 of `mindin-install-v1` followed by `id`, `version`, `os`, `arch` and `runs`, each as its length in 4 bytes big-endian and its UTF-8 bytes.

The address is fixed; no setting points it elsewhere.

## Where the id lives

`install-id` and the day's ledger, `install-signal.json`, are in mindin's state folder: `$XDG_STATE_HOME/mindin`, else the folder `state` beside `VAULT_CACHE_DIR` when that is set (`/data/state` in the image), else `~/.local/state/mindin`. Clearing the cache keeps them. A file that holds anything but an id is replaced with a new one.

## Saying no later

With the answer no, or `VAULT_SIGNALS=off`, nothing is sent and the next server to run deletes the id and the ledger, so a later yes makes a new install.
