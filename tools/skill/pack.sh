#!/usr/bin/env bash
# Packs the skill into <skill dir>/dist/baton-skill-<version>.zip: its SKILL.md at the zip's
# root, with this repository's docs/TROUBLESHOOTING.md beside it.
#
#   tools/skill/pack.sh 1.4.0          (or: just skill-zip)
#
# The skill's source is private (selorito/baton-skill), checked out next to this
# repository, or wherever BATON_SKILL_DIR names. The zip is sold, not released: nothing in
# this repository or its release workflow publishes it.
#
# Fails when SKILL.md names another version, when its frontmatter lacks name or
# description, or when the skill holds anything that looks like a secret.
set -euo pipefail

version="${1:?usage: pack.sh <version>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
skill="${BATON_SKILL_DIR:-$root/../baton-skill}"
[ -f "$skill/SKILL.md" ] || { echo "no SKILL.md in $skill (clone selorito/baton-skill there, or set BATON_SKILL_DIR)" >&2; exit 1; }
skill="$(cd "$skill" && pwd)"

head -1 "$skill/SKILL.md" | grep -qx -- '---' || { echo "SKILL.md does not start with YAML frontmatter" >&2; exit 1; }
for key in name description; do
    sed -n '2,/^---$/p' "$skill/SKILL.md" | grep -q "^$key:" \
        || { echo "SKILL.md's frontmatter has no $key" >&2; exit 1; }
done
grep -qx "  version: \"$version\"" "$skill/SKILL.md" \
    || { echo "SKILL.md does not name version $version" >&2; exit 1; }
# Telegram bot tokens, GitHub and OpenAI/Anthropic-style keys, private keys.
secrets='[0-9]{8,10}:[A-Za-z0-9_-]{35}|gh[pousr]_[A-Za-z0-9]{36}|sk-[A-Za-z0-9_-]{20,}|BEGIN [A-Z ]*PRIVATE KEY'
if grep -Eq "$secrets" "$skill/SKILL.md" "$root/docs/TROUBLESHOOTING.md"; then
    echo "the skill looks like it holds a secret" >&2
    exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp "$skill/SKILL.md" "$stage/SKILL.md"
cp "$root/docs/TROUBLESHOOTING.md" "$stage/TROUBLESHOOTING.md"
# Stable bytes: fixed dates, no extra attributes, a fixed order.
touch -d "2000-01-01T00:00:00Z" "$stage"/*
mkdir -p "$skill/dist"
zip_path="$skill/dist/baton-skill-$version.zip"
rm -f "$zip_path"
(cd "$stage" && zip -q -X "$zip_path" SKILL.md TROUBLESHOOTING.md)
echo "$zip_path"
