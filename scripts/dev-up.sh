#!/usr/bin/env bash
# dev-up.sh — start the dev stack with runtime git-derived APP_VERSION.
#
# Usage: scripts/dev-up.sh [compose-up-args...]
# APP_VERSION stays "development" so app.core.config derives the visible
# version from mounted .git after every uvicorn hot reload.
set -Eeuo pipefail

cd "$(dirname "$0")/.."

export APP_VERSION=development
echo "[dev-up] APP_VERSION=development (runtime git describe)"
exec docker compose up "$@"
