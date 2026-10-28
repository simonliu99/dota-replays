#!/bin/bash
# Print one release section of CHANGELOG.md, the text its release page
# publishes: every line after the `## [x.y.z]` heading up to the next `## `
# heading, without the leading and trailing blank lines. A `## ` line inside a
# fenced code block is content, not a heading.
#
# Usage: scripts/release-section.sh <x.y.z> [--root <dir>]
#
# --root names the work tree whose CHANGELOG.md is read; the default is the
# directory above this script. Stdout only: the script writes no file and
# calls no network service, and it prints nothing when it fails. It exits 1
# when the section is missing or empty and 2 on bad usage. Written by the
# agent-workflow scaffold for bash 3.2 and POSIX awk.

set -u -o pipefail

usage() {
    printf 'usage: release-section.sh <x.y.z> [--root <dir>]\n' >&2
    exit 2
}

VERSION=''
ROOT=''
while [ $# -gt 0 ]; do
    case "$1" in
        --root)
            [ $# -ge 2 ] || usage
            ROOT=$2
            shift 2
            ;;
        -h|--help)
            sed -n '2,13p' "$0"
            exit 0
            ;;
        -*)
            usage
            ;;
        *)
            [ -z "$VERSION" ] || usage
            VERSION=$1
            shift
            ;;
    esac
done
printf '%s\n' "$VERSION" | grep -q -E '^[0-9]+\.[0-9]+\.[0-9]+$' || usage

if [ -z "$ROOT" ]; then
    ROOT=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
fi
CHANGELOG="$ROOT/CHANGELOG.md"
[ -f "$CHANGELOG" ] || { printf 'no CHANGELOG.md in %s\n' "$ROOT" >&2; exit 1; }

# The awk program tracks fences across the whole file, so a heading-like line
# in a code block before the section cannot open or close it.
body=$(awk -v want="## [$VERSION]" '
    function fence_run(line,    run) {
        if (!match(line, /^[ \t]*(```+|~~~+)/)) return ""
        run = substr(line, RSTART, RLENGTH)
        gsub(/[ \t]/, "", run)
        return run
    }
    {
        line = $0
        if (fence != "") {
            run = fence_run(line)
            rest = line
            sub(/^[ \t]*(```+|~~~+)/, "", rest)
            if (run != "" && substr(run, 1, 1) == substr(fence, 1, 1) \
                && length(run) >= length(fence) && rest ~ /^[ \t]*$/) fence = ""
            if (inside) lines[++n] = line
            next
        }
        run = fence_run(line)
        if (run != "") {
            fence = run
            if (inside) lines[++n] = line
            next
        }
        if (substr(line, 1, 3) == "## ") {
            if (inside) exit
            if (index(line, want) == 1) { inside = 1; found = 1 }
            next
        }
        if (inside) lines[++n] = line
    }
    END {
        if (!found) exit 1
        first = 1
        while (first <= n && lines[first] ~ /^[ \t]*$/) first++
        last = n
        while (last >= first && lines[last] ~ /^[ \t]*$/) last--
        if (last < first) exit 1
        for (i = first; i <= last; i++) print lines[i]
    }
' "$CHANGELOG") || { printf 'CHANGELOG.md has no section for %s\n' "$VERSION" >&2; exit 1; }

printf '%s\n' "$body"
