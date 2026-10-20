#!/usr/bin/env bash
# Runs as root while this project's sandbox image is built (see build.sh).
# PLUGINS_DIR points at claude-sandbox's built-in plugins.
set -euo pipefail

# Built-in plugins:
# "${PLUGINS_DIR}/languages/cpp.sh"
# "${PLUGINS_DIR}/languages/go.sh"
# "${PLUGINS_DIR}/languages/java.sh"
# "${PLUGINS_DIR}/languages/python2.sh"
# "${PLUGINS_DIR}/languages/python3.sh"
# "${PLUGINS_DIR}/languages/react.sh"
# "${PLUGINS_DIR}/languages/ruby.sh"
# "${PLUGINS_DIR}/languages/rust.sh"
# "${PLUGINS_DIR}/languages/typescript.sh"
# "${PLUGINS_DIR}/tools/docker.sh"
# "${PLUGINS_DIR}/tools/playwright.sh"

# Project-specific packages:
# apt-get update && apt-get install -y --no-install-recommends <packages>
