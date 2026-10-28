#!/bin/bash
# Lint every shell script in the repo with shellcheck.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
exec shellcheck -x apple scripts/*.sh .claude/sandbox/*.sh
