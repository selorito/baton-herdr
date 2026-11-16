#!/usr/bin/env bash
# Clean installs in containers: tools/e2e/run.sh [image ...] (default: four distributions).
# Builds the wheel and a static (musl) baton-detect, then runs clean-install.sh in each.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
DIST=$(mktemp -d)
chmod 755 "$DIST"  # read by the unprivileged user in the container
trap 'rm -rf "$DIST"' EXIT
images=("$@")
[ ${#images[@]} -gt 0 ] || images=(ubuntu:24.04 debian:12 fedora:42 archlinux:latest)
uv build --quiet --wheel --out-dir "$DIST" "$REPO"
tar -C "$REPO" -cf "$DIST/src.tar" $(git -C "$REPO" ls-files -co --exclude-standard | grep -v '^target/')
docker run --rm -v "$DIST:/dist" rust:1-bookworm bash -c '
    set -e; apt-get update -qq >/dev/null; apt-get install -y -qq musl-tools >/dev/null
    mkdir /b && tar -xf /dist/src.tar -C /b && cd /b && rustup target add x86_64-unknown-linux-musl >/dev/null 2>&1
    cargo build --quiet --release --locked -p baton-detect --target x86_64-unknown-linux-musl
    cp target/x86_64-unknown-linux-musl/release/baton-detect /dist/ && chmod 644 /dist/*'
for image in "${images[@]}"; do
    name=${image%%:*}
    docker run --rm -v "$REPO:/repo:ro" -v "$DIST:/dist:ro" "$image" bash /repo/tools/e2e/clean-install.sh "$name" \
        > "$REPO/target/e2e-$name.log" 2>&1 && echo "$image: ok" || echo "$image: FAILED (target/e2e-$name.log)"
done
