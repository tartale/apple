#!/bin/bash
# Lint every shell script in the repo with shellcheck.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
exec shellcheck -x install src/apple/photos/scripts/*.sh scripts/*.sh test/fake-rclone test/helpers.bash .claude/sandbox/*.sh
