#!/usr/bin/env bash
# Packs skills/baton into <out>/baton-skill-<version>.zip, with SKILL.md at the zip's root
# and a copy of docs/TROUBLESHOOTING.md beside it. Fails when SKILL.md names another
# version, or when the skill holds anything that looks like a secret.
#
#   tools/skill/pack.sh 1.4.0 dist
set -euo pipefail

version="${1:?usage: pack.sh <version> <out-dir>}"
out="${2:?usage: pack.sh <version> <out-dir>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
skill="$root/skills/baton"

grep -qx "  version: \"$version\"" "$skill/SKILL.md" \
    || { echo "skills/baton/SKILL.md does not name version $version" >&2; exit 1; }
# Telegram bot tokens, GitHub and Vercel-style tokens, private keys.
if grep -rEq '[0-9]{8,10}:[A-Za-z0-9_-]{35}|gh[pousr]_[A-Za-z0-9]{36}|BEGIN [A-Z ]*PRIVATE KEY' "$skill"; then
    echo "skills/baton looks like it holds a secret" >&2
    exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp "$skill/SKILL.md" "$stage/SKILL.md"
cp "$root/docs/TROUBLESHOOTING.md" "$stage/TROUBLESHOOTING.md"
# Stable bytes: fixed dates, no extra attributes, a fixed order.
touch -d "2000-01-01T00:00:00Z" "$stage"/*
mkdir -p "$out"
zip_path="$(cd "$out" && pwd)/baton-skill-$version.zip"
rm -f "$zip_path"
(cd "$stage" && zip -q -X "$zip_path" SKILL.md TROUBLESHOOTING.md)
echo "$zip_path"
