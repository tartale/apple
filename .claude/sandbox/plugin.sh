#!/usr/bin/env bash
# Runs as root while this project's sandbox image is built (see build.sh).
# PLUGINS_DIR points at claude-sandbox's built-in plugins.
set -euo pipefail

# Built-in plugins:
# "${PLUGINS_DIR}/languages/go.sh"
# "${PLUGINS_DIR}/tools/docker.sh"

# Project-specific packages:

# Test runner for photos/test.sh (bats-core 1.x)
apt-get update
apt-get install -y --no-install-recommends bats

