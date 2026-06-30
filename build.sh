#!/usr/bin/env bash
set -euo pipefail

git pull

export WAKU_VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo local)"
export WAKU_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
export WAKU_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

docker compose up -d --build waku

docker image prune -f
