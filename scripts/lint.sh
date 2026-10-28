#!/bin/bash
# Lint every shell script in the repo with shellcheck.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
exec shellcheck -x src/apple/photos/scripts/*.sh scripts/*.sh macos/*.sh test/fake-rclone test/helpers.bash .claude/sandbox/*.sh
