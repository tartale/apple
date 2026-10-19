#!/usr/bin/env bash
# Starts this project's sandbox in the background; claude waits on a detached tty.
# Usage: .claude/sandbox/start.sh [claude args]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export CS_IMAGE_TAG="${CS_IMAGE_TAG:-icloud-tools}"
NAME="${CS_CONTAINER_NAME:-cs-icloud-tools}"
export CS_CONTAINER_NAME="$NAME"

# Each start creates a fresh container (--rm removes it on stop), so changed
# variables and flags always apply.
if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = true ]; then
    echo "$NAME is already running"
else
    docker rm "$NAME" >/dev/null 2>&1 || true   # a stopped one created without --rm
    CS_DOCKER_FLAGS="-d -t --rm ${CS_DOCKER_FLAGS:-}" \
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/tartale/claude-sandbox/refs/heads/main/claude-sandbox.sh)" claude-sandbox --name "icloud-tools" "$@" >/dev/null
    echo "started $NAME"
fi
printf '\nAttach (detach with ctrl-p ctrl-q):\n\ndocker attach %s\n\nStop:\n\ndocker stop %s\n' "$NAME" "$NAME"
