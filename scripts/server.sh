#!/bin/sh
# Sets up a mindin server with Docker Compose and invites its first account: curl -fsSL https://github.com/gronare/mindin/releases/latest/download/server.sh | sh
set -eu

release=${MINDIN_RELEASE:-latest}
case "$release" in
  latest) base=https://github.com/gronare/mindin/releases/latest/download ;;
  *) base=https://github.com/gronare/mindin/releases/download/$release ;;
esac

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

if (exec </dev/tty) 2>/dev/null; then tty=/dev/tty; else tty=""; fi

docker_failed() {
  cause=$(grep -v '^[[:space:]]*$' "$errors" | tail -n 5 | sed 's/^/    /')
  case "$cause" in
    *"no matching manifest"*) cause="$cause
    The image is not built for this machine's platform, $(uname -m). Running this script with DOCKER_DEFAULT_PLATFORM=linux/amd64 runs the amd64 image emulated, where Docker can." ;;
  esac
  fail "$1${cause:+ Docker said:
$cause}"
}

ask() {
  [ -n "$tty" ] || { answer=""; return 0; }
  printf '  %s ' "$1"
  read -r answer <"$tty" || answer=""
}

ask_secret() {
  [ -n "$tty" ] || { answer=""; return 0; }
  printf '  %s ' "$1"
  stty -echo <"$tty" 2>/dev/null || true
  read -r answer <"$tty" || answer=""
  stty echo <"$tty" 2>/dev/null || true
  printf '\n'
}

setting() {
  KEY=$1 VALUE=$2 awk '
    BEGIN { key = ENVIRON["KEY"]; line = key "=" ENVIRON["VALUE"] }
    !done && ($0 ~ "^" key "=" || $0 ~ "^# *" key "=") { print line; done = 1; next }
    { print }
    END { if (!done) print line }
  ' "$env" >"$env.next"
  chmod 600 "$env.next"
  mv "$env.next" "$env"
}

commented() {
  KEY=$1 awk '$0 ~ "^" ENVIRON["KEY"] "=" { print "# " $0; next } { print }' "$env" >"$env.next"
  chmod 600 "$env.next"
  mv "$env.next" "$env"
}

value_of() { sed -n "s/^$1=//p" "$env" | tail -n 1; }

unquotable() {
  case "$1" in *\'*) fail "$2 holds a single quote, which this script cannot write to .env; give another, or set up .env by hand from example.env." ;; esac
}

printf '\n  %smindin%s %s· server%s\n' "$bold" "$off" "$dim" "$off"

command -v curl >/dev/null 2>&1 || fail "This script needs curl."
command -v docker >/dev/null 2>&1 || fail "The server runs with Docker, which is not on this machine: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || fail "The server runs with Docker Compose v2, and docker compose is not there: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 || fail "Docker is installed but does not answer; start it, or run this as a user that may use it."
errors=$(mktemp)
trap 'rm -f "$errors"' EXIT

if [ -f compose.yml ]; then
  dir=$(pwd)
else
  mkdir -p mindin
  dir=$(CDPATH='' cd -- mindin && pwd)
fi
cd "$dir"
env="$dir/.env"

heading "Files"
curl -fsSL -o compose.yml "$base/compose.yml" || fail "Could not download compose.yml from $base."
curl -fsSL -o example.env "$base/example.env" || fail "Could not download example.env from $base."
[ -f Caddyfile ] || curl -fsSL -o Caddyfile "$base/Caddyfile" || fail "Could not download Caddyfile from $base."
ok "compose.yml, Caddyfile and example.env in $dir"

if [ -f "$env" ]; then
  ok ".env is there already, left as it is"
  fresh=""
else
  fresh=1
  heading "Settings"
  host=${VAULT_HOST:-}
  if [ -z "$host" ]; then
    say "  The domain the server answers on. It must point at this machine, with ports 80 and 443 open."
    say "  A name under localhost, such as vault.localhost, tries it on this machine alone."
    ask "Domain (vault.example.com):"
    host=$answer
  fi
  host=${host#https://}; host=${host#http://}; host=${host%%/*}
  case "$host" in
    "") fail "The server needs a domain; set VAULT_HOST=vault.example.com when running without a terminal." ;;
    *[!A-Za-z0-9.-]*|.*|*.|*..*) fail "$host is not a domain name." ;;
    *.localhost) ;;
    *.*) ;;
    *) fail "$host is not a domain name. To try the server on this machine alone, use a name under localhost, such as vault.localhost." ;;
  esac

  smtp_host=${VAULT_SMTP_HOST:-}
  smtp_port=${VAULT_SMTP_PORT:-}
  smtp_security=${VAULT_SMTP_SECURITY:-}
  smtp_from=${VAULT_SMTP_FROM:-}
  smtp_user=${VAULT_SMTP_USERNAME:-}
  smtp_password=${VAULT_SMTP_PASSWORD:-}
  if [ -z "$smtp_host" ] && [ -n "$tty" ]; then
    say ""
    say "  Sign-in links go out by email through an SMTP server. Leave the host empty to skip:"
    say "  people then sign in with GitHub, and add a passkey after their first sign-in."
    ask "SMTP host (smtp.example.com):"
    smtp_host=$answer
    if [ -n "$smtp_host" ]; then
      ask "SMTP port [587]:"; smtp_port=$answer
      case "$smtp_port" in
        ""|465|587) ;;
        *) [ -n "$smtp_security" ] || { ask "Security, starttls, ssl or none [starttls]:"; smtp_security=${answer:-starttls}; } ;;
      esac
      ask "Sender (Vault <vault@$host>):"; smtp_from=${answer:-Vault <vault@$host>}
      ask "SMTP username:"; smtp_user=$answer
      ask_secret "SMTP password:"; smtp_password=$answer
    fi
  fi

  github_id=${VAULT_GITHUB_CLIENT_ID:-}
  github_secret=${VAULT_GITHUB_CLIENT_SECRET:-}
  if [ -z "$smtp_host" ] && [ -z "$github_id" ] && [ -n "$tty" ]; then
    say ""
    say "  Without email, sign-in is by GitHub: an OAuth app at github.com/settings/developers"
    say "  with the callback https://$host/signin/github/callback. Leave it empty to set up another way in .env."
    ask "GitHub client ID:"; github_id=$answer
    [ -z "$github_id" ] || { ask_secret "GitHub client secret:"; github_secret=$answer; }
  fi

  [ -n "$smtp_security" ] || [ "$smtp_port" != 465 ] || smtp_security=ssl
  case "$smtp_security" in
    ""|starttls|ssl) ;;
    none) [ -z "$smtp_user$smtp_password" ] || fail "SMTP security none sends the login unencrypted, so the server refuses it with a username or password; use starttls or ssl, or leave the login empty." ;;
    *) fail "SMTP security is starttls, ssl or none, not $smtp_security." ;;
  esac
  unquotable "$smtp_user" "The SMTP username"
  unquotable "$smtp_password" "The SMTP password"
  unquotable "$github_secret" "The GitHub client secret"
  umask 077
  cp example.env "$env"
  chmod 600 "$env"
  setting VAULT_HOST "$host"
  if [ -n "$smtp_host" ]; then
    [ -n "$smtp_from" ] || smtp_from="Vault <vault@$host>"
    setting VAULT_SMTP_HOST "$smtp_host"
    setting VAULT_SMTP_FROM "$smtp_from"
    if [ -n "$smtp_user" ]; then setting VAULT_SMTP_USERNAME "'$smtp_user'"; else commented VAULT_SMTP_USERNAME; fi
    if [ -n "$smtp_password" ]; then setting VAULT_SMTP_PASSWORD "'$smtp_password'"; else commented VAULT_SMTP_PASSWORD; fi
    [ -z "$smtp_security" ] || setting VAULT_SMTP_SECURITY "$smtp_security"
    [ -z "$smtp_port" ] || setting VAULT_SMTP_PORT "$smtp_port"
  else
    for key in VAULT_SMTP_HOST VAULT_SMTP_FROM VAULT_SMTP_USERNAME VAULT_SMTP_PASSWORD; do commented "$key"; done
  fi
  if [ -n "$github_id" ]; then
    setting VAULT_GITHUB_CLIENT_ID "$github_id"
    setting VAULT_GITHUB_CLIENT_SECRET "'$github_secret'"
  fi
  ok ".env written, readable only by $(id -un)"
fi
chmod 600 "$env"

if [ -z "$(value_of VAULT_HOSTED_MASTER_KEY)" ]; then
  heading "Master key"
  key=$(docker compose run --rm --no-deps -T vault master-key generate </dev/null 2>"$errors" | tail -n 1) || key=""
  case "$key" in
    ""|*[!A-Za-z0-9+/=]*) docker_failed "The image did not make a master key." ;;
  esac
  setting VAULT_HOSTED_MASTER_KEY "$key"
  key=""
  ok "A master key is in $env, as VAULT_HOSTED_MASTER_KEY."
  warn "Keep a copy of it outside this machine: without it no vault on this server can be read."
fi

methods=""
for key in VAULT_SMTP_HOST VAULT_GITHUB_CLIENT_ID VAULT_OIDC_ISSUER VAULT_IDENTITY_PUBLIC_KEY; do
  [ -z "$(value_of "$key")" ] || methods=1
done
[ -n "$methods" ] || fail "The server needs a way to sign in. Fill in the SMTP or GitHub lines in $env, or another method (https://github.com/gronare/mindin/blob/main/docs/how-to/sign-in-methods.md), then run this again."

heading "Start"
docker compose up -d --quiet-pull >/dev/null 2>"$errors" || docker_failed "docker compose up stopped."
tries=0
until docker compose exec -T vault python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/ready', timeout=3)" >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    docker compose logs --tail 20 vault >&2 || true
    fail "The server did not get ready within two minutes; the log above says why. Fix $env and run this again."
  fi
  sleep 2
done
ok "The server is up"

host=$(value_of VAULT_HOST)
email=${FIRST_EMAIL:-}
if [ -z "$email" ] && [ -n "$tty" ]; then
  heading "First account"
  if [ -n "$fresh" ]; then ask "Your email address:"; else ask "An email address to invite (empty to skip):"; fi
  email=$answer
fi
if [ -n "$email" ]; then
  docker compose exec -T vault mindin accounts invite "$email" </dev/null >/dev/null || fail "Inviting $email stopped; docker compose exec vault mindin accounts invite $email shows why."
  ok "$email may sign in"
elif [ -n "$fresh" ]; then
  warn "No account yet: docker compose exec vault mindin accounts invite you@example.com adds one."
fi

say ""
say "  Open https://$host/account and sign in. Caddy fetches the certificate on the first visit."
say ""
