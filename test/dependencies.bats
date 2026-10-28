#!/usr/bin/env bats

load helpers
setup() { common_setup; }

@test "backup.sh exits 2 with an install command when rclone is missing" {
  ICLOUD_RCLONE=/nonexistent/rclone run backup
  [ "$status" -eq 2 ]
  [[ $output == *"Missing required programs: /nonexistent/rclone"* ]]
  [[ $output == *"Install "* ]]
  [[ $output == *"  "*"rclone"* ]]
  [ ! -e "$(logfile backup)" ]
}

@test "the install command is a runnable line of its own" {
  ICLOUD_RCLONE=/nonexistent/rclone run backup
  echo "$output" | grep -Eq '^  (sudo )?(apt-get install -y|dnf install -y|apk add) .*rclone$'
}

@test "DRY_RUN accepts true, 1, yes and on in any case" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  for v in true TRUE 1 yes On; do
    DRY_RUN=$v run backup
    [ "$status" -eq 0 ]
    [ ! -e "$ICLOUD_ROOT/tom" ]
  done
}

@test "any other DRY_RUN value, including false, is a real run" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  DRY_RUN=false run backup
  [ "$status" -eq 0 ]
  [ -f "$ICLOUD_ROOT/tom/originals/a.jpg" ]
}

@test "on Homebrew a missing GNU find gets brew install and the PATH line for the g-prefixed tools" {
  mkdir -p "$BATS_TEST_TMPDIR/fake"
  printf '#!/bin/bash\nexit 1\n' > "$BATS_TEST_TMPDIR/fake/find"   # a find without -printf, like macOS's
  printf '#!/bin/bash\nexit 0\n' > "$BATS_TEST_TMPDIR/fake/brew"
  chmod +x "$BATS_TEST_TMPDIR"/fake/*
  PATH="$BATS_TEST_TMPDIR/fake:$PATH" run backup
  [ "$status" -eq 2 ]
  [[ $output == *"Missing required programs: GNU find"* ]]
  [[ $output == *"  brew install findutils"* ]]
  [[ $output == *"libexec/gnubin"* ]]
  [[ $output != *"sudo brew"* ]]
}
