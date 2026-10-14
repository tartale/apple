#!/usr/bin/env bash
set -euo pipefail


# Test runner for photos/test.sh (bats-core 1.x)
apt-get update
apt-get install -y --no-install-recommends bats
