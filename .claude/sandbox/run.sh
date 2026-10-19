#!/usr/bin/env bash
# Starts this project's sandbox interactively. Usage: .claude/sandbox/run.sh [claude args]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export CS_IMAGE_TAG="${CS_IMAGE_TAG:-icloud-tools}"

# Name the container cs-<repo>, or cs-<repo>-2, -3... when another is already
# running, and remove it on exit so the name is free again.
if [ -z "${CS_CONTAINER_NAME:-}" ]; then
    CS_CONTAINER_NAME="cs-icloud-tools"
    n=1
    while docker inspect "$CS_CONTAINER_NAME" >/dev/null 2>&1; do
        n=$((n + 1)); CS_CONTAINER_NAME="cs-icloud-tools-$n"
    done
    export CS_CONTAINER_NAME
fi
export CS_DOCKER_FLAGS="--rm ${CS_DOCKER_FLAGS:-}"

exec /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/tartale/claude-sandbox/refs/heads/main/claude-sandbox.sh)" claude-sandbox --name "icloud-tools" "$@"
