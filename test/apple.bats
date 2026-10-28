#!/usr/bin/env bats

REPO=$(cd "$BATS_TEST_DIRNAME/.." && pwd)

setup() {
  # A copy of the launcher next to a stand-in pyproject.toml, so the real one is never touched
  export PROJECT="$BATS_TEST_TMPDIR/project"
  mkdir -p "$PROJECT/scripts" "$BATS_TEST_TMPDIR/fake"
  cp "$REPO/apple" "$PROJECT/apple"
  cp "$REPO/scripts/venvpath.py" "$PROJECT/scripts/venvpath.py"
  export REAL_PYTHON=$(command -v python3)
  : > "$PROJECT/pyproject.toml"
  export APPLE_VENV="$BATS_TEST_TMPDIR/venv"
  export CALLS="$BATS_TEST_TMPDIR/calls"
  export PATH="$BATS_TEST_TMPDIR/fake:$PATH"   # our fakes win over anything installed
}

# fake_python [pip exit status]: a python that passes the version check and makes a venv holding a stub pip and python
fake_python() {
  cat > "$BATS_TEST_TMPDIR/fake/python3.13" <<SCRIPT
#!/bin/bash
echo "python3.13 \$*" >> "\$CALLS"
case \$1 in
  -c) exit 0;;
  *venvpath.py) exec "\$REAL_PYTHON" "\$@";;
  -m) mkdir -p "\$3/bin"
      printf '#!/bin/bash\necho "pip \$*" >> "\$CALLS"\nexit ${1:-0}\n' > "\$3/bin/pip"
      printf '#!/bin/bash\necho "venv-python \$* APPLE_PROG=\$APPLE_PROG" >> "\$CALLS"\n' > "\$3/bin/python"
      : > "\$3/pyvenv.cfg"
      chmod +x "\$3/bin/pip" "\$3/bin/python";;
esac
SCRIPT
  chmod +x "$BATS_TEST_TMPDIR/fake/python3.13"
}

@test "the first run sets up the virtual environment, installs the project and starts the CLI with the arguments" {
  fake_python
  run "$PROJECT/apple" photos --help
  [ "$status" -eq 0 ]
  grep -q "python3.13 -m venv $APPLE_VENV" "$CALLS"
  grep -q "pip install --quiet --upgrade -e $PROJECT" "$CALLS"
  grep -q "venv-python -m apple photos --help" "$CALLS"
  [ -f "$APPLE_VENV/.installed" ]
}

@test "a second run goes straight to the CLI" {
  fake_python
  "$PROJECT/apple" doctor
  : > "$CALLS"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 0 ]
  ! grep -q -- '-m venv' "$CALLS"
  ! grep -q '^pip' "$CALLS"
  grep -q "venv-python -m apple doctor" "$CALLS"
}

@test "a pyproject.toml newer than the last setup installs again" {
  fake_python
  "$PROJECT/apple" doctor
  touch -d '+1 minute' "$PROJECT/pyproject.toml"
  : > "$CALLS"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 0 ]
  grep -q "^pip install" "$CALLS"
}

@test "the program name shown in help is the command as typed" {
  fake_python
  cd "$PROJECT"
  run ./apple doctor
  grep -q "APPLE_PROG=./apple" "$CALLS"
}

@test "without Python 3.9 or later it exits 2 with an install command" {
  for p in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    printf '#!/bin/bash\nexit 1\n' > "$BATS_TEST_TMPDIR/fake/$p"; chmod +x "$BATS_TEST_TMPDIR/fake/$p"
  done
  run "$PROJECT/apple" doctor
  [ "$status" -eq 2 ]
  [[ $output == *"Python 3.9 or later is required"* ]]
  echo "$output" | grep -Eq '^  (brew install python|sudo apt-get install -y python3 python3-venv|sudo dnf install -y python3|sudo apk add python3|\(install Python)'
}

@test "a failed virtual environment suggests installing the venv module" {
  printf '#!/bin/bash\ncase $1 in -c) exit 0;; *) exit 1;; esac\n' > "$BATS_TEST_TMPDIR/fake/python3.13"
  chmod +x "$BATS_TEST_TMPDIR/fake/python3.13"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 2 ]
  [[ $output == *"sudo apt-get install -y python3-venv"* ]]
}

@test "a failed install exits 1, leaves no stamp, and the next run tries again" {
  fake_python 1
  run "$PROJECT/apple" doctor
  [ "$status" -eq 1 ]
  [[ $output == *"the next run will try again"* ]]
  [ ! -f "$APPLE_VENV/.installed" ]
  printf '#!/bin/bash\necho "pip $*" >> "$CALLS"\n' > "$APPLE_VENV/bin/pip"   # the network is back
  : > "$CALLS"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 0 ]
  grep -q "^pip install" "$CALLS"
}

@test "by default the venv is named for the OS, the architecture and the checkout's path, inside the checkout" {
  fake_python
  unset APPLE_VENV
  run "$PROJECT/apple" doctor
  [ "$status" -eq 0 ]
  expected=$("$REAL_PYTHON" "$PROJECT/scripts/venvpath.py" "$PROJECT")
  [[ $expected == "$PROJECT"/.venv-* ]]
  [ -f "$expected/.installed" ]
}

@test "copies of a checkout at two different paths get two venvs" {
  fake_python
  unset APPLE_VENV
  second="$BATS_TEST_TMPDIR/other"
  cp -R "$PROJECT" "$second"
  "$PROJECT/apple" doctor
  "$second/apple" doctor
  [ "$(ls -d "$PROJECT"/.venv-* | wc -l)" -eq 1 ]
  [ "$(ls -d "$second"/.venv-* | wc -l)" -eq 1 ]
  [ "$(basename "$(ls -d "$PROJECT"/.venv-*)")" != "$(basename "$(ls -d "$second"/.venv-*)")" ]
}

@test "a venv built elsewhere whose python is unusable here is replaced, not built over" {
  fake_python
  mkdir -p "$APPLE_VENV"
  : > "$APPLE_VENV/pyvenv.cfg"
  ln -s /nonexistent/python "$APPLE_VENV/python-from-elsewhere"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 0 ]
  [[ $output == *"Replacing the unusable venv"* ]]
  [ ! -L "$APPLE_VENV/python-from-elsewhere" ]
  grep -q "venv-python -m apple doctor" "$CALLS"
}

@test "a directory in the venv's place that is not a venv is left alone" {
  fake_python
  mkdir -p "$APPLE_VENV"
  echo precious > "$APPLE_VENV/data"
  run "$PROJECT/apple" doctor
  [ "$status" -eq 1 ]
  [[ $output == *"not a venv"* ]]
  [ -f "$APPLE_VENV/data" ]
}

@test "an old shared .venv is pointed out as unused" {
  fake_python
  mkdir -p "$PROJECT/.venv"
  : > "$PROJECT/.venv/pyvenv.cfg"
  run "$PROJECT/apple" doctor
  [[ $output == *"$PROJECT/.venv is unused"* ]]
  [ -e "$PROJECT/.venv/pyvenv.cfg" ]
}
