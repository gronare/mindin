#!/bin/sh
# Release notes from CHANGES.md: scripts/release-notes.sh <X.Y.Z> prints that version's entries as markdown; --move <X.Y.Z> first moves the Unreleased entries under a new ## X.Y.Z heading. Both refuse a version with no entries.
set -eu

move=no
if [ "${1:-}" = --move ]; then move=yes; shift; fi
version=${1:-}
case "$version" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "usage: scripts/release-notes.sh [--move] <X.Y.Z> [CHANGES.md]" >&2; exit 2 ;;
esac
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
file=${2:-$root/CHANGES.md}
[ -f "$file" ] || { echo "release notes: $file is missing" >&2; exit 1; }

# The entries of one section: a group heading shows only above its first entry, and an indented line continues the entry above it.
notes() {
  awk -v want="## $1" '
    /^## / { on = ($0 == want); next }
    !on { next }
    /^### / { held = $0; next }
    /^[-*] / { if (held != "") { if (printed) print ""; print held; held = "" } print; printed = 1; next }
    /^[ \t]+[^ \t]/ { if (printed) print; next }
    END { exit printed ? 0 : 1 }
  ' "$file"
}

if [ "$move" = yes ]; then
  if notes "$version" > /dev/null; then
    notes Unreleased > /dev/null && { echo "release notes: $file has entries under both ## $version and ## Unreleased; move them by hand" >&2; exit 1; }
    exit 0
  fi
  notes Unreleased > /dev/null || { echo "release notes: nothing under ## Unreleased in $file says what $version brings; write one line per change under ### What's new or ### Fixed first" >&2; exit 1; }
  moved="$file.$$"
  awk -v version="$version" '
    $0 == "## Unreleased" && !done { print; print ""; print "### What'"'"'s new"; print ""; print "### Fixed"; print ""; print "## " version; done = 1; next }
    { print }
  ' "$file" > "$moved" && mv "$moved" "$file"
  exit 0
fi

notes "$version" || { echo "release notes: $file has no entries under ## $version" >&2; exit 1; }
