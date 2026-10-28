#!/usr/bin/env bats

REPO=$(cd "$BATS_TEST_DIRNAME/.." && pwd)

setup() {
  export APPLE_HOME="$BATS_TEST_TMPDIR/home"
  export APPLE_BIN_DIR="$BATS_TEST_TMPDIR/binlink"
  mkdir -p "$BATS_TEST_TMPDIR/fake"
  # PATH starts with our fakes, so they win over anything installed
  export PATH="$BATS_TEST_TMPDIR/fake:$PATH"
  export CALLS="$BATS_TEST_TMPDIR/calls"
  fake apple 'echo 0.1.0'
}

# fake <name> <script body>: a stand-in program that logs its call
fake() {
  printf '#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' "$1" "$2" > "$BATS_TEST_TMPDIR/fake/$1"
  chmod +x "$BATS_TEST_TMPDIR/fake/$1"
}

# A python that passes the version check, and makes a virtual environment holding a stub pip and apple
fake_python() {
  fake python3.13 '
    case $1 in
      -c) exit 0;;
      -m) mkdir -p "$3/bin"; printf "#!/bin/bash\necho pip \$* >> \"$CALLS\"\n" > "$3/bin/pip"; printf "#!/bin/bash\necho 0.1.0\n" > "$3/bin/apple"; chmod +x "$3/bin/pip" "$3/bin/apple";;
    esac'
}

@test "DRY_RUN=true shows the plan and installs nothing" {
  fake_python
  DRY_RUN=true run "$REPO/install"
  [ "$status" -eq 0 ]
  [[ $output == *"-m venv $APPLE_HOME"* ]]
  [ ! -e "$APPLE_HOME" ] && [ ! -e "$APPLE_BIN_DIR" ]
}

@test "without Python 3.9 or later it exits 2 with an install command" {
  for p in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do fake $p 'exit 1'; done
  run "$REPO/install"
  [ "$status" -eq 2 ]
  [[ $output == *"Python 3.9 or later is required"* ]]
  echo "$output" | grep -Eq '^  (brew install python|sudo apt-get install -y python3 python3-venv|sudo dnf install -y python3|sudo apk add python3|\(install Python)'
}

@test "uv is used first" {
  fake_python; fake uv 'exit 0'; fake pipx 'exit 0'
  run "$REPO/install"
  [ "$status" -eq 0 ]
  grep -q "uv tool install --force $REPO" "$CALLS"
  ! grep -q '^pipx' "$CALLS"
}

@test "pipx is used when there is no uv" {
  fake_python; fake pipx 'exit 0'
  # hide any real uv
  fake uv 'exit 0'; rm "$BATS_TEST_TMPDIR/fake/uv"
  PATH="$BATS_TEST_TMPDIR/fake:$(echo "$PATH" | tr ':' '\n' | while read -r d; do [ -x "$d/uv" ] || printf '%s:' "$d"; done)" run "$REPO/install"
  [ "$status" -eq 0 ]
  grep -q "pipx install --force $REPO" "$CALLS"
}

@test "without uv or pipx it makes a virtual environment and links apple into the bin directory" {
  fake_python
  PATH="$BATS_TEST_TMPDIR/fake:$(echo "$PATH" | tr ':' '\n' | while read -r d; do [ -x "$d/uv" ] || [ -x "$d/pipx" ] || printf '%s:' "$d"; done)" run "$REPO/install"
  [ "$status" -eq 0 ]
  grep -q "pip install --upgrade $REPO" "$CALLS"
  [ -L "$APPLE_BIN_DIR/apple" ]
  [[ $output == *"Installed apple (via venv): 0.1.0"* ]]
  [[ $output == *"Add $APPLE_BIN_DIR to your PATH"* ]]
}

@test "a failed virtual environment suggests installing the venv module" {
  fake python3.13 'case $1 in -c) exit 0;; *) exit 1;; esac'
  PATH="$BATS_TEST_TMPDIR/fake:$(echo "$PATH" | tr ':' '\n' | while read -r d; do [ -x "$d/uv" ] || [ -x "$d/pipx" ] || printf '%s:' "$d"; done)" run "$REPO/install"
  [ "$status" -eq 2 ]
  [[ $output == *"sudo apt-get install -y python3-venv"* ]]
}
