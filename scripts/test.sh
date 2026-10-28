#!/bin/bash
# Runs the bats tests in test/ (the shell scripts) and the pytest tests in tests/ (the CLI)
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
if ! command -v bats >/dev/null; then
  echo "bats is not installed. Install it with:" >&2
  echo "  sudo apt-get install -y bats" >&2
  exit 1
fi
if ! python3 -c "import pytest, typer, rich" 2>/dev/null; then
  echo "The CLI tests need pytest, typer and rich. Install them with:" >&2
  echo "  python3 -m pip install 'typer>=0.12,<1' 'rich>=13,<15' 'pytest>=8,<9'" >&2
  exit 1
fi
bats test/ && python3 -m pytest -q tests/
