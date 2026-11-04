#!/bin/sh
# Installs mindin with uv, runs mindin setup and offers to add the vault to Claude Code, Codex and Cursor: curl -fsSL https://github.com/gronare/mindin/releases/latest/download/install.sh | sh
set -eu

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  bold=$(printf '\033[1m'); dim=$(printf '\033[2m'); green=$(printf '\033[32m'); yellow=$(printf '\033[33m'); red=$(printf '\033[31m'); off=$(printf '\033[0m')
else
  bold=""; dim=""; green=""; yellow=""; red=""; off=""
fi

say() { printf '%s\n' "$*"; }
ok() { printf '  %s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }
heading() { printf '\n  %s%s%s\n' "$bold" "$*" "$off"; }
fail() { printf '\n  %s✗%s %s\n\n' "$red" "$off" "$*" >&2; exit 1; }
short() { case "$1" in "$HOME"/*) printf '%s/%s' '~' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
snippet() { printf '%s\n' "$1" | sed 's/^/      /'; }

if (exec </dev/tty) 2>/dev/null; then tty=/dev/tty; else tty=""; fi

yes_to() {
  [ -n "$tty" ] || return 1
  printf '  %s [y/N] ' "$1"
  read -r answer <"$tty" || answer=n
  case "$answer" in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac
}

printf '\n  %smindin%s %s· install%s\n' "$bold" "$off" "$dim" "$off"

command -v curl >/dev/null 2>&1 || fail "mindin's installer needs curl."

if ! command -v uv >/dev/null 2>&1; then
  heading "uv"
  say "  mindin installs with uv, which is not on this machine: https://docs.astral.sh/uv/"
  if [ -n "$tty" ]; then
    printf '  Install uv? [Y/n] '
    read -r answer <"$tty" || answer=n
    case "$answer" in ""|y|Y|yes|Yes) ;; *) say ""; say "  Nothing installed."; say ""; exit 0 ;; esac
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || fail "uv's installer stopped."
  PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv is installed but not on PATH; open a new terminal and run this again."
  ok "uv $(uv --version | awk '{print $2}')"
fi

heading "mindin"
log=$(mktemp)
if uv tool install --upgrade mindin >"$log" 2>&1 </dev/null; then
  rm -f "$log"
else
  cat "$log" >&2; rm -f "$log"; fail "uv could not install mindin."
fi
bin=$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")
mindin="$bin/mindin"
[ -x "$mindin" ] || mindin=$(command -v mindin || true)
[ -n "$mindin" ] && [ -x "$mindin" ] || fail "uv installed mindin but the command is not in $(short "$bin")."
ok "$("$mindin" about | head -n 1) in $(short "$mindin")"
command -v mindin >/dev/null 2>&1 || warn "$(short "$bin") is not on PATH; uv tool update-shell adds it"

printf '\n  %s→ mindin setup%s\n\n' "$dim" "$off"
if [ -n "$tty" ]; then "$mindin" setup <"$tty"; else "$mindin" setup; fi || fail "mindin setup stopped before it was done. mindin is installed; run it again in this terminal: $(short "$mindin") setup"

heading "Clients"

if command -v claude >/dev/null 2>&1; then
  if yes_to "Add the vault plugin to Claude Code?"; then
    claude plugin marketplace add https://github.com/gronare/claude-plugins >/dev/null 2>&1 || true
    if claude plugin install vault@gronare >/dev/null 2>&1; then ok "Claude Code: the vault plugin"; else warn "Claude Code refused the plugin; run: claude plugin install vault@gronare"; fi
  else
    say "    Claude Code, when you want it:"
    snippet "claude plugin marketplace add https://github.com/gronare/claude-plugins
claude plugin install vault@gronare"
  fi
fi

codex_config="${CODEX_HOME:-$HOME/.codex}/config.toml"
if command -v codex >/dev/null 2>&1 || [ -d "${CODEX_HOME:-$HOME/.codex}" ]; then
  if [ -f "$codex_config" ] && grep -q '^\[mcp_servers\.vault\]' "$codex_config"; then
    ok "Codex: $(short "$codex_config") names a vault server already, left as it is"
  elif yes_to "Add the vault to Codex, in $(short "$codex_config")?"; then
    mkdir -p "$(dirname -- "$codex_config")"
    printf '\n[mcp_servers.vault]\ncommand = "%s"\n' "$mindin" >>"$codex_config"
    ok "Codex: the vault in $(short "$codex_config")"
  else
    say "    Codex, when you want it, in $(short "$codex_config"):"
    snippet "[mcp_servers.vault]
command = \"$mindin\""
  fi
fi

cursor_config="$HOME/.cursor/mcp.json"
cursor_entry="{\"mcpServers\": {\"vault\": {\"command\": \"$mindin\"}}}"
if command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
  if [ -f "$cursor_config" ] && grep -q '"vault"' "$cursor_config"; then
    ok "Cursor: $(short "$cursor_config") names a vault server already, left as it is"
  elif [ -f "$cursor_config" ] && ! command -v jq >/dev/null 2>&1; then
    say "    Cursor, in $(short "$cursor_config") (merging it needs jq):"
    snippet "$cursor_entry"
  elif yes_to "Add the vault to Cursor, in $(short "$cursor_config")?"; then
    mkdir -p "$(dirname -- "$cursor_config")"
    if [ -f "$cursor_config" ]; then
      merged=$(mktemp)
      jq --arg command "$mindin" '.mcpServers.vault = {command: $command}' "$cursor_config" >"$merged" || { rm -f "$merged"; fail "$(short "$cursor_config") is not JSON jq can read; nothing changed."; }
      mv "$merged" "$cursor_config"
    else
      printf '%s\n' "$cursor_entry" >"$cursor_config"
    fi
    ok "Cursor: the vault in $(short "$cursor_config")"
  else
    say "    Cursor, when you want it, in $(short "$cursor_config"):"
    snippet "$cursor_entry"
  fi
fi

say ""
say "  Done. $(short "$mindin") status shows where this machine's vault is."
say ""
