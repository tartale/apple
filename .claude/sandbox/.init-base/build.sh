#!/usr/bin/env bash
# Builds this project's sandbox image from plugin.sh and pushes it.
# Usage: .claude/sandbox/build.sh [--no-push]
set -euo pipefail

THIS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The claude-sandbox version: CS_REF (a commit, tag or branch of tartale/claude-sandbox), else
# the commit init.sh pinned in the file "ref" next to this script, else main.
# CS_REF=latest follows main.
export CS_REF="${CS_REF:-$(cat "${THIS_SCRIPT_DIR}/ref" 2>/dev/null || echo main)}"
[ "${CS_REF}" != latest ] || CS_REF=main

CS_PROJECT_DIR="$(cd "${THIS_SCRIPT_DIR}/../.." && pwd)"
export CS_PROJECT_DIR CS_PROJECT_NAME="apple"
exec /bin/bash -c "$(curl -fsSL "https://raw.githubusercontent.com/tartale/claude-sandbox/${CS_REF}/cs")" cs build "$@"
