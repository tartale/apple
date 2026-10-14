# Shared setup for the bats tests: a fake iCloud (a directory tree), an empty backup root, and the fake rclone.
REPO=$(cd "$BATS_TEST_DIRNAME/.." && pwd)

common_setup() {
  export TZ=UTC
  export FAKE_CLOUD="$BATS_TEST_TMPDIR/cloud"
  export ICLOUD_ROOT="$BATS_TEST_TMPDIR/root"
  export ICLOUD_ACCOUNTS="tom=tom"
  export ICLOUD_RCLONE="$REPO/test/fake-rclone"
  export ICLOUD_LOCK_FILE="$BATS_TEST_TMPDIR/lock"
  unset DRY_RUN ICLOUD_RCLONE_CONF FAKE_FAIL
  mkdir -p "$FAKE_CLOUD/tom/PrimarySync" "$ICLOUD_ROOT"
}

# cloud_file <path under PrimarySync> <size> <mtime as epoch seconds>
cloud_file() {
  local p="$FAKE_CLOUD/tom/PrimarySync/$1"
  mkdir -p "$(dirname "$p")"
  truncate -s "$2" "$p"
  touch -d "@$3" "$p"
}

# Mid-month timestamps, so the by-date folder does not depend on the time zone
export JUN_2020=1592222400   # 2020-06-15 12:00 UTC
export JUL_2020=1594814400   # 2020-07-15 12:00 UTC

backup() { "$REPO/photos/backup.sh" "$@"; }
organize() { "$REPO/photos/organize.sh" "$@"; }

# The log file a script writes today: <ICLOUD_ROOT>/<script>-YYYY-MM-DD.log
logfile() { echo "$ICLOUD_ROOT/$1-$(date +%F).log"; }
