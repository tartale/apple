#!/usr/bin/env bash
# Rebuilds the image, stops this project's container and starts it again in the
# background. Usage: .claude/sandbox/restart.sh [claude args]
# Without arguments it reuses the claude arguments of the container it replaces.
set -euo pipefail

THIS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="${CS_CONTAINER_NAME:-cs-icloud-tools}"

if [ "$#" -eq 0 ]; then
    while IFS= read -r arg; do set -- "$@" "$arg"; done \
        < <(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' "$NAME" 2>/dev/null || true)
fi

"${THIS_SCRIPT_DIR}/build.sh"
docker stop "$NAME" >/dev/null 2>&1 || true
exec "${THIS_SCRIPT_DIR}/start.sh" "$@"
