#!/usr/bin/env bash
set -euo pipefail

git pull

docker compose up -d --build waku

docker image prune -f
