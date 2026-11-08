#!/bin/sh
# Releases mindin and its former name: scripts/release.sh <X.Y.Z> "<what it brings>" moves CHANGES.md's Unreleased entries under X.Y.Z, bumps every version, tags, pushes and waits until PyPI, ghcr and the GitHub release serve it.
set -eu

version=${1:-}
summary=${2:-}
case "$version" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "usage: scripts/release.sh <X.Y.Z> \"<what it brings>\"" >&2; exit 2 ;;
esac
[ -n "$summary" ] || { echo "say what the release brings: scripts/release.sh $version \"<what it brings>\"" >&2; exit 2; }
tag="v$version"
image=ghcr.io/gronare/mindin
former=notes-vault-mcp
pinned="deploy/k8s/deployment.yaml deploy/docker/compose.yml docs/install/semantic-search.md"
plugin="plugin/.claude-plugin/plugin.json plugin/.mcp.json plugin/hooks/hooks.json plugin/commands/login.md"
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

fail() { echo "release: $*" >&2; exit 1; }

expect() {
  found=$(grep -c -F -- "$2" "$1" || true)
  [ "$found" = "$3" ] || fail "$1 names '$2' $found times, expected $3"
}

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || fail "check out main first"
[ -z "$(git status --porcelain)" ] || fail "commit or stash your changes first"
git fetch --quiet --tags origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || fail "main is not origin/main; pull or push first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && fail "$tag exists already"

head=$(git rev-parse HEAD)
ci=$(gh run list --commit "$head" --workflow ci.yml --json status,conclusion -q '.[0] | "\(.status) \(.conclusion)"' 2>/dev/null || true)
[ "$ci" = "completed success" ] || fail "CI on $(git rev-parse --short HEAD) is '${ci:-not found}'; release a commit whose CI is green"

sh scripts/release-notes.sh --move "$version" || fail "write what $version brings in CHANGES.md first"
for manifest in pyproject.toml shim/pyproject.toml; do perl -pi -e 's/^version = "[^"]*"/version = "'"$version"'"/ if $. <= 10' "$manifest"; done
perl -pi -e 's/"mindin(\[semantic\])?==[^"]*"/"mindin$1=='"$version"'"/g' shim/pyproject.toml
perl -pi -e 's/^VERSION = "[^"]*"/VERSION = "'"$version"'"/' mindin/server.py
perl -pi -e 's/"version": "[^"]*"/"version": "'"$version"'"/' server.json
perl -0pi -e 's/(\[\[package\]\]\nname = "mindin"\nversion = )"[^"]*"/$1"'"$version"'"/' uv.lock
perl -pi -e 's/^(# \{.*?"version": )"[^"]*"/$1"'"$version"'"/' docs/reference/agent-commands.md
# shellcheck disable=SC2086
perl -pi -e 's#(\Q'"$image"'\E):v[0-9][0-9.]*(-semantic)?$#$1:'"$tag"'$2#' $pinned
perl -pi -e 's/"version": "[^"]*"/"version": "'"$version"'"/' plugin/.claude-plugin/plugin.json
perl -pi -e 's/\b(mindin|notes-vault-mcp)==[0-9][0-9.]*/$1=='"$version"'/g' plugin/.mcp.json plugin/hooks/hooks.json plugin/commands/login.md

expect pyproject.toml "version = \"$version\"" 1
expect shim/pyproject.toml "version = \"$version\"" 1
expect shim/pyproject.toml "\"mindin==$version\"" 1
expect shim/pyproject.toml "\"mindin[semantic]==$version\"" 1
expect mindin/server.py "VERSION = \"$version\"" 1
expect server.json "\"version\": \"$version\"" 2
expect docs/reference/agent-commands.md "\"version\": \"$version\"" 3
expect docs/install/semantic-search.md "$image:$tag-semantic" 1
for file in deploy/k8s/deployment.yaml deploy/docker/compose.yml; do expect "$file" "$image:$tag" 1; done
expect plugin/.claude-plugin/plugin.json "\"version\": \"$version\"" 1
expect plugin/.mcp.json "==$version\"" 1
expect plugin/hooks/hooks.json "==$version hook" 2
expect plugin/commands/login.md "mindin==$version login" 2
stale=$(grep -rnE "$image:v[0-9]|\"version\": \"[0-9]" README.md docs deploy | grep -vF -e "$image:$tag" -e "\"version\": \"$version\"" || true)
[ -z "$stale" ] || fail "the bump did not reach: $stale"
stale=$(grep -rnoE "(mindin|notes-vault-mcp)==[0-9][0-9.]*" plugin | grep -vE "==$version\$" || true)
[ -z "$stale" ] || fail "the plugin still pins: $stale"
grep -A1 '^name = "mindin"$' uv.lock | grep -q "^version = \"$version\"$" || fail "uv.lock still names another mindin version"
uv lock --check --quiet || fail "uv.lock does not agree with pyproject.toml"

files="CHANGES.md pyproject.toml shim/pyproject.toml uv.lock mindin/server.py server.json docs/reference/agent-commands.md $pinned $plugin"
# shellcheck disable=SC2086
git add $files
[ -z "$(git status --porcelain --untracked-files=all | grep -v '^M  ' || true)" ] || fail "the bump changed files outside $files"
body=$(printf 'mindin %s brings %s. CHANGES.md lists each change under %s.\n' "$version" "$summary" "$version" | fold -s -w 72 | sed 's/ *$//')
git commit --quiet -m "Release mindin $version" -m "$body"
git tag -a "$tag" -m "mindin $version"
git push --atomic --quiet origin main "$tag"
echo "release: pushed $tag; waiting for the release workflow"

run=""
for _ in $(seq 1 30); do
  run=$(gh run list --workflow release.yml --branch "$tag" --json databaseId -q '.[0].databaseId' 2>/dev/null || true)
  [ -n "$run" ] && break
  sleep 5
done
[ -n "$run" ] || fail "the release workflow did not start for $tag"
gh run watch "$run" --exit-status >/dev/null || fail "the release workflow failed: $(gh run view "$run" --json url -q .url)"

for project in mindin "$former"; do
  pypi=""
  for _ in $(seq 1 12); do
    pypi=$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$project/$version/json" || true)
    [ "$pypi" = 200 ] && break
    sleep 10
  done
  [ "$pypi" = 200 ] || fail "PyPI answers $pypi for $project $version"
done

for repository in "${image#ghcr.io/}" "gronare/$former"; do
  token=$(curl -s "https://ghcr.io/token?scope=repository:$repository:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  [ -n "$token" ] || fail "ghcr gave no pull token for $repository"
  for reference in "$tag" "$tag-semantic"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -I -H "Authorization: Bearer $token" \
      -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json" \
      "https://ghcr.io/v2/$repository/manifests/$reference" || true)
    [ "$code" = 200 ] || fail "ghcr.io/$repository:$reference answers $code"
  done
done

slug=$(git remote get-url origin | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##')
draft=$(gh release view "$tag" --repo "$slug" --json isDraft -q .isDraft 2>/dev/null || true)
case "$draft" in
  false) ;;
  true) fail "the GitHub release $tag is still a draft: https://github.com/$slug/releases/tag/$tag" ;;
  *) fail "$slug has no GitHub release $tag" ;;
esac
assets=$(gh release view "$tag" --repo "$slug" --json assets -q '.assets[].name')
for asset in compose.yml Caddyfile example.env install.sh server.sh; do
  printf '%s\n' "$assets" | grep -qx "$asset" || fail "the GitHub release $tag lacks $asset, which the install docs fetch"
done
echo "release: mindin $version is published: https://github.com/$slug/releases/tag/$tag"
