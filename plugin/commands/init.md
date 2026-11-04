---
description: Set the vault up: write the schema and the Obsidian Bases views into its root, keeping what already exists
argument-hint: [--force]
---

Call the vault server's `init` tool. Pass `force: true` only when the user wrote `--force` in "$ARGUMENTS"; force overwrites the schema and the bases with the built-in defaults, so never add it on your own.

Report in a few lines what was written and what was kept. When everything was kept, say the vault was already set up. Then say that the vault is ready and that the next session start will print its context.
