#!/usr/bin/env bash
# Builds this project's sandbox image from plugin.sh. Usage: .claude/sandbox/build.sh
# Then start it with run.sh or start.sh in this directory.
set -euo pipefail

THIS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PLUGINS="${THIS_SCRIPT_DIR}/plugin.sh"
export CS_IMAGE_TAG="${CS_IMAGE_TAG:-apple}"

/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/tartale/claude-sandbox/refs/heads/main/build-image.sh)"
