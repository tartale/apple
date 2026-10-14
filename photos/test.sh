#!/bin/bash
# Runs the bats tests in test/
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
if ! command -v bats >/dev/null; then
  echo "bats is not installed. Install it with:" >&2
  echo "  sudo apt-get install -y bats" >&2
  exit 1
fi
exec bats test/
