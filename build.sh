#!/usr/bin/env bash
set -Eeuo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly IMAGE_NAME="${WAKU_IMAGE_NAME:-waku-bot:local}"
readonly PNPM_VERSION="${PNPM_VERSION:-11.3.0}"
readonly MIN_FREE_GB="${MIN_FREE_GB:-5}"
readonly HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"

cd "$ROOT_DIR"

log() {
  printf '\n[build] %s\n' "$*"
}

die() {
  printf '\n[build] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

has_changed() {
  local pattern="$1"
  grep -Eq "$pattern" <<<"$CHANGED_FILES"
}

available_bytes() {
  local docker_root
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [[ -d "$docker_root" ]] || docker_root="/"
  df -PB1 "$docker_root" | awk 'NR == 2 {print $4}'
}

show_disk_usage() {
  local bytes
  bytes="$(available_bytes)"
  log "Docker disk usage (free: $((bytes / 1024 / 1024 / 1024)) GiB)"
  docker system df || true
}

ensure_build_space() {
  local required_bytes free_bytes
  required_bytes=$((MIN_FREE_GB * 1024 * 1024 * 1024))
  free_bytes="$(available_bytes)"
  if ((free_bytes >= required_bytes)); then
    return
  fi

  log "Less than ${MIN_FREE_GB} GiB is free. Pruning Docker cache older than 7 days..."
  docker builder prune -f --filter 'until=168h'
  docker image prune -f --filter 'until=168h'
  free_bytes="$(available_bytes)"
  ((free_bytes >= required_bytes)) || die \
    "Only $((free_bytes / 1024 / 1024 / 1024)) GiB is free after safe cleanup; free more disk space and retry."
}

select_pnpm() {
  if command -v pnpm >/dev/null 2>&1; then
    PNPM=(pnpm)
  elif command -v corepack >/dev/null 2>&1; then
    PNPM=(corepack "pnpm@${PNPM_VERSION}")
  elif command -v npx >/dev/null 2>&1; then
    PNPM=(npx --yes "pnpm@${PNPM_VERSION}")
  else
    die "WebApp build requires pnpm, corepack, or npx. Install Node.js and pnpm first."
  fi
}

build_webapp() {
  select_pnpm
  log "Building Mini App/WebApp frontend..."
  pushd "$ROOT_DIR/webapp" >/dev/null

  if [[ "$WEBAPP_DEPS_CHANGED" == true || ! -d node_modules ]]; then
    log "Installing WebApp dependencies with pnpm ${PNPM_VERSION}..."
    "${PNPM[@]}" install --frozen-lockfile --pm-on-fail=ignore
  fi

  "${PNPM[@]}" build
  [[ -f dist/index.html ]] || die "WebApp build completed without dist/index.html"
  popd >/dev/null
}

build_image() {
  local build_log
  build_log="$(mktemp)"

  ensure_build_space
  log "Building ${IMAGE_NAME} with Docker layer cache..."
  if docker compose build waku 2>&1 | tee "$build_log"; then
    rm -f "$build_log"
    return
  fi

  if grep -Eqi 'no space left on device|failed to extract layer' "$build_log"; then
    log "Docker ran out of snapshot/build space. Removing unused build cache and retrying once..."
    docker builder prune -af
    docker image prune -f
    rm -f "$build_log"
    ensure_build_space
    docker compose build waku
    return
  fi

  rm -f "$build_log"
  die "Docker image build failed. Existing running containers were left untouched."
}

image_commit() {
  docker image inspect "$IMAGE_NAME" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n 's/^WAKU_COMMIT=//p' \
    | head -n 1 || true
}

wait_for_waku() {
  local container_id deadline status
  container_id="$(docker compose ps -q waku)"
  [[ -n "$container_id" ]] || die "waku container was not created"
  deadline=$((SECONDS + HEALTH_TIMEOUT))

  while ((SECONDS < deadline)); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    case "$status" in
      healthy | running)
        log "waku status: ${status}"
        return
        ;;
      unhealthy | exited | dead)
        docker compose logs --tail=100 waku || true
        die "waku entered state: ${status}"
        ;;
    esac
    sleep 5
  done

  docker compose logs --tail=100 waku || true
  die "waku did not become healthy within ${HEALTH_TIMEOUT} seconds"
}

require_command git
require_command docker
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || die "Cannot connect to Docker daemon (check that Docker is running and your user has permission)"
[[ "$MIN_FREE_GB" =~ ^[0-9]+$ ]] || die "MIN_FREE_GB must be a whole number"
[[ "$HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || die "HEALTH_TIMEOUT must be a whole number"

BEFORE_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
log "Pulling latest code (fast-forward only)..."
git pull --ff-only
AFTER_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
[[ -n "$AFTER_COMMIT" ]] || die "Unable to determine the current git commit"

if [[ -n "$BEFORE_COMMIT" && "$BEFORE_COMMIT" != "$AFTER_COMMIT" ]]; then
  CHANGED_FILES="$(git diff --name-only "$BEFORE_COMMIT" "$AFTER_COMMIT")"
else
  CHANGED_FILES=""
fi

if [[ -n "$CHANGED_FILES" ]]; then
  log "Changed files:"
  sed 's/^/  - /' <<<"$CHANGED_FILES"
else
  log "No new files changed during git pull."
fi

export WAKU_VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo local)"
export WAKU_COMMIT="$AFTER_COMMIT"
export WAKU_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

WEBAPP_CHANGED=false
WEBAPP_DEPS_CHANGED=false
if [[ -n "$CHANGED_FILES" ]]; then
  if has_changed '^(webapp/|waku/webapp/|waku/plugins/panel\.py|waku/plugins/start\.py)$'; then
    WEBAPP_CHANGED=true
  fi
  if has_changed '^(webapp/package\.json|webapp/pnpm-lock\.yaml|webapp/pnpm-workspace\.yaml)$'; then
    WEBAPP_DEPS_CHANGED=true
  fi
fi
[[ -f webapp/dist/index.html ]] || WEBAPP_CHANGED=true

if [[ "$WEBAPP_CHANGED" == true ]]; then
  build_webapp
fi

CURRENT_IMAGE_COMMIT="$(image_commit)"
NEED_BUILD=false
if [[ -n "$CHANGED_FILES" || "$CURRENT_IMAGE_COMMIT" != "$AFTER_COMMIT" ]]; then
  NEED_BUILD=true
fi

if [[ "$NEED_BUILD" == true ]]; then
  if [[ -z "$CURRENT_IMAGE_COMMIT" ]]; then
    log "No valid local image found; a build is required."
  elif [[ "$CURRENT_IMAGE_COMMIT" != "$AFTER_COMMIT" ]]; then
    log "Image commit is stale (${CURRENT_IMAGE_COMMIT:0:12} != ${AFTER_COMMIT:0:12}); rebuilding."
  fi
  build_image
else
  log "Image already matches commit ${AFTER_COMMIT:0:12}; skipping rebuild."
fi

log "Starting/updating waku service..."
docker compose up -d --no-build waku
wait_for_waku

if [[ "${PRUNE_OLD_CACHE:-1}" == "1" ]]; then
  log "Pruning unused Docker cache older than 7 days..."
  docker builder prune -f --filter 'until=168h'
  docker image prune -f --filter 'until=168h'
fi

show_disk_usage
log "Deployment completed: ${WAKU_VERSION}"
