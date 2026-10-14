#!/usr/bin/env bats

load helpers
setup() { common_setup; }

@test "backup.sh exits 2 with an install command when rclone is missing" {
  ICLOUD_RCLONE=/nonexistent/rclone run backup
  [ "$status" -eq 2 ]
  [[ $output == *"Missing required programs: /nonexistent/rclone"* ]]
  [[ $output == *"Install "* ]]
  [[ $output == *"  "*"rclone"* ]]
  [ ! -e "$ICLOUD_ROOT/backup.log" ]
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
