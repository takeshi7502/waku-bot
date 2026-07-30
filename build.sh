#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

log() {
  printf '
[build] %s
' "$*"
}

has_changed() {
  local pattern="$1"
  printf '%s
' "$CHANGED_FILES" | grep -Eq "$pattern"
}

BEFORE_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"

log "Pulling latest code..."
git pull

AFTER_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"

if [[ -n "$BEFORE_COMMIT" && -n "$AFTER_COMMIT" && "$BEFORE_COMMIT" != "$AFTER_COMMIT" ]]; then
  CHANGED_FILES="$(git diff --name-only "$BEFORE_COMMIT" "$AFTER_COMMIT")"
else
  CHANGED_FILES=""
fi

if [[ -z "$CHANGED_FILES" ]]; then
  log "No changed files from git pull. Ensuring waku service is running..."
else
  log "Changed files:"
  printf '%s
' "$CHANGED_FILES" | sed 's/^/  - /'
fi

export WAKU_VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo local)"
export WAKU_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
export WAKU_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

WEBAPP_CHANGED=false
WEBAPP_DEPS_CHANGED=false
DOCKER_CHANGED=false

if [[ -n "$CHANGED_FILES" ]]; then
  if has_changed '^(webapp/|waku/webapp/|waku/plugins/panel\.py|waku/plugins/start\.py)$'; then
    WEBAPP_CHANGED=true
  fi

  if has_changed '^(webapp/package\.json|webapp/pnpm-lock\.yaml)$'; then
    WEBAPP_DEPS_CHANGED=true
  fi

  if has_changed '^(Dockerfile|docker-compose(\..*)?\.ya?ml|\.dockerignore)$'; then
    DOCKER_CHANGED=true
  fi
fi

if [[ "$WEBAPP_CHANGED" == true ]]; then
  log "Mini App/WebApp files changed. Building frontend bundle..."
  cd "$ROOT_DIR/webapp"

  if [[ "$WEBAPP_DEPS_CHANGED" == true || ! -d node_modules ]]; then
    log "WebApp dependencies changed or node_modules missing. Installing with pnpm..."
    corepack pnpm@11.3.0 install --frozen-lockfile --pm-on-fail=ignore
  fi

  corepack pnpm@11.3.0 build
  cd "$ROOT_DIR"
fi

if [[ "$DOCKER_CHANGED" == true ]]; then
  log "Docker-related files changed. Rebuilding waku image with --no-cache..."
  docker compose build --no-cache waku
  docker compose up -d waku
elif [[ -n "$CHANGED_FILES" ]]; then
  log "Code changed. Rebuilding/updating waku service with Docker cache..."
  docker compose up -d --build waku
else
  log "No code changes. Starting/updating waku service without rebuild..."
  docker compose up -d waku
fi

log "Pruning dangling Docker images..."
docker image prune -f

log "Done."
