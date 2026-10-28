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


# Test runner for scripts/test.sh (bats-core 1.x)
apt-get update
apt-get install -y --no-install-recommends bats


# Reads photo and video metadata (EXIF dates, camera, dimensions) for comparing libraries
apt-get install -y --no-install-recommends libimage-exiftool-perl

# The apple CLI and its tests (typer and rich for the CLI, pytest for tests/), pinned by major
apt-get install -y --no-install-recommends python3-pip
python3 -m pip install --break-system-packages 'typer>=0.12,<1' 'rich>=13,<15' 'pytest>=8,<9'
