#!/usr/bin/env bats

load helpers
setup() {
  common_setup
  export LAUNCH_AGENTS_DIR="$BATS_TEST_TMPDIR/agents"
  export SHORTCUT_NAME="Split Photo Library"
  # Stand-ins for the macOS commands the script calls
  printf '#!/bin/bash\necho Darwin\n' > "$BATS_TEST_TMPDIR/bin/uname"
  printf '#!/bin/bash\necho "launchctl $*" >> "%s/calls"\n' "$BATS_TEST_TMPDIR" > "$BATS_TEST_TMPDIR/bin/launchctl"
  printf '#!/bin/bash\n' > "$BATS_TEST_TMPDIR/bin/shortcuts"
  chmod +x "$BATS_TEST_TMPDIR"/bin/*
  export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

schedule() { "$REPO/macos/schedule.sh" "$@"; }

@test "install writes a plist that runs the shortcut at the chosen time and loads it" {
  SCHEDULE_HOUR=04 SCHEDULE_MINUTE=05 run schedule install
  [ "$status" -eq 0 ]
  plist=$LAUNCH_AGENTS_DIR/local.apple.shortcut.Split-Photo-Library.plist
  grep -q '<string>Split Photo Library</string>' "$plist"
  grep -q '<key>Hour</key><integer>4</integer>' "$plist"
  grep -q '<key>Minute</key><integer>5</integer>' "$plist"
  grep -q 'launchctl bootstrap .* '"$plist" "$BATS_TEST_TMPDIR/calls"
  [ ! -e "$plist.new" ]
}

@test "uninstall removes the plist and unloads it" {
  schedule install
  run schedule uninstall
  [ "$status" -eq 0 ]
  [ ! -e "$LAUNCH_AGENTS_DIR/local.apple.shortcut.Split-Photo-Library.plist" ]
  grep -q 'launchctl bootout' "$BATS_TEST_TMPDIR/calls"
}

@test "the default time is 03:00" {
  schedule install
  grep -q '<key>Hour</key><integer>3</integer>' "$LAUNCH_AGENTS_DIR"/*.plist
  grep -q '<key>Minute</key><integer>0</integer>' "$LAUNCH_AGENTS_DIR"/*.plist
}

@test "bad input exits 2 and writes nothing" {
  SHORTCUT_NAME='a<b' run schedule install; [ "$status" -eq 2 ]
  SCHEDULE_HOUR=24 run schedule install; [ "$status" -eq 2 ]
  SCHEDULE_MINUTE=60 run schedule install; [ "$status" -eq 2 ]
  SCHEDULE_HOUR=x run schedule install; [ "$status" -eq 2 ]
  env -u SHORTCUT_NAME "$REPO/macos/schedule.sh" install && false || [ "$?" -eq 2 ]
  run schedule frobnicate; [ "$status" -eq 2 ]
  [ ! -e "$LAUNCH_AGENTS_DIR" ]
}

@test "it refuses to run off macOS" {
  printf '#!/bin/bash\necho Linux\n' > "$BATS_TEST_TMPDIR/bin/uname"
  run schedule install
  [ "$status" -eq 2 ]
  [[ $output == *"macOS only"* ]]
}
