#!/usr/bin/env bats

load helpers
setup() { common_setup; }

acct() { echo "$ICLOUD_ROOT/tom"; }

@test "exits 2 and names the missing required variables" {
  unset ICLOUD_ROOT ICLOUD_ACCOUNTS
  run backup
  [ "$status" -eq 2 ]
  [[ $output == *"ICLOUD_ROOT ICLOUD_ACCOUNTS"* ]]
}

@test "copies All Photos into originals and points albums.csv at them" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  cloud_file "Holiday/a.jpg" 100 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/a.jpg" ]
  [ ! -e "$(acct)/originals/Holiday" ]
  grep -qx "Holiday/a.jpg,100,2020-06-15 12:00:00,a.jpg" "$(acct)/albums.csv"
}

@test "an album-only photo is downloaded once, to the first album path, however many albums hold it" {
  cloud_file "Beach/b.jpg" 200 $JUN_2020
  cloud_file "Trip/b.jpg" 200 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/Beach/b.jpg" ]
  [ ! -e "$(acct)/originals/Trip" ]
  grep -qx "Trip/b.jpg,200,2020-06-15 12:00:00,Beach/b.jpg" "$(acct)/albums.csv"
}

@test "photos sharing a name and size but not mtime are different photos" {
  cloud_file "All Photos/c.jpg" 300 $JUN_2020
  cloud_file "Odd/c.jpg" 300 $JUL_2020
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/c.jpg" ]
  [ -f "$(acct)/originals/Odd/c.jpg" ]
}

@test "a photo nested under All Photos is stored once, where rclone puts it" {
  cloud_file "All Photos/sub/n.jpg" 400 $JUN_2020
  cloud_file "Nest/n.jpg" 400 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/sub/n.jpg" ]
  [ ! -e "$(acct)/originals/Nest" ]
  grep -qx "Nest/n.jpg,400,2020-06-15 12:00:00,sub/n.jpg" "$(acct)/albums.csv"
}

@test "album paths containing a comma are quoted in albums.csv" {
  cloud_file "Trip/Day 1, morning.jpg" 500 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  grep -qxF '"Trip/Day 1, morning.jpg",500,2020-06-15 12:00:00,"Trip/Day 1, morning.jpg"' "$(acct)/albums.csv"
}

@test "DRY_RUN=true changes nothing under the account dir" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  DRY_RUN=true run backup
  [ "$status" -eq 0 ]
  [ ! -e "$(acct)" ]
}

@test "DRY_RUN=true leaves an existing albums.csv alone" {
  mkdir -p "$(acct)"
  echo "path,size,mtime,stored" > "$(acct)/albums.csv"
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  DRY_RUN=true run backup
  [ "$status" -eq 0 ]
  [ "$(cat "$(acct)/albums.csv")" = "path,size,mtime,stored" ]
  [ "$(ls "$(acct)")" = albums.csv ]
}

@test "a failed listing is an error and downloads no album-only photos" {
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  FAKE_FAIL=lsf run backup
  [ "$status" -eq 1 ]
  [ ! -e "$(acct)/originals/Holiday" ]
}

@test "exits 0 without doing anything while another run holds the lock" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  hold_lock
  run backup
  kill "$LOCK_HOLDER" 2>/dev/null || true
  [ "$status" -eq 0 ]
  [[ $output == *"still active"* ]]
  [ ! -e "$(acct)" ]
}

@test "the day's runs append to one log named with the date" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  run backup
  run backup
  [ "$status" -eq 0 ]
  [ "$(find "$ICLOUD_ROOT" -maxdepth 1 -name 'backup*.log' | wc -l)" -eq 1 ]
  [ -f "$ICLOUD_ROOT/backup-2026-01-02.log" ]
  [ "$(grep -c "backup run started" "$(logfile backup)")" -eq 2 ]
}

@test "when the album-only download fails albums.csv leaves out those rows and the run fails" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  cloud_file "Holiday/a.jpg" 100 $JUN_2020
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  FAKE_FAIL=copy-album run backup
  [ "$status" -eq 1 ]
  grep -qx "Holiday/a.jpg,100,2020-06-15 12:00:00,a.jpg" "$(acct)/albums.csv"
  ! grep -q "h.jpg" "$(acct)/albums.csv"
  grep -q "albums.csv written without 1 entries" "$(logfile backup)"
}

@test "the next run adds the rows left out after a failed download" {
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  FAKE_FAIL=copy-album run backup
  [ "$status" -eq 1 ]
  run backup
  [ "$status" -eq 0 ]
  grep -qx "Holiday/h.jpg,150,2020-06-15 12:00:00,Holiday/h.jpg" "$(acct)/albums.csv"
}

@test "albums.csv is written when every download succeeds" {
  cloud_file "Holiday/h.jpg" 150 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  grep -qx "Holiday/h.jpg,150,2020-06-15 12:00:00,Holiday/h.jpg" "$(acct)/albums.csv"
}

@test "a photo that left All Photos but is in an album is not downloaded again" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  cloud_file "Recently Deleted/a.jpg" 100 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  rm "$FAKE_CLOUD/tom/PrimarySync/All Photos/a.jpg"
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/a.jpg" ]
  [ ! -e "$(acct)/originals/Recently Deleted" ]
  grep -qx "Recently Deleted/a.jpg,100,2020-06-15 12:00:00,a.jpg" "$(acct)/albums.csv"
}

@test "an album-only photo downloaded earlier is reused by another album" {
  cloud_file "Beach/b.jpg" 200 $JUN_2020
  run backup
  [ "$status" -eq 0 ]
  cloud_file "Trip/b.jpg" 200 $JUN_2020
  rm "$FAKE_CLOUD/tom/PrimarySync/Beach/b.jpg"
  run backup
  [ "$status" -eq 0 ]
  [ ! -e "$(acct)/originals/Trip" ]
  grep -qx "Trip/b.jpg,200,2020-06-15 12:00:00,Beach/b.jpg" "$(acct)/albums.csv"
}

@test "a different photo with the same name and size but another mtime is still downloaded" {
  cloud_file "All Photos/a.jpg" 100 $JUN_2020
  run backup
  cloud_file "Odd/a.jpg" 100 $JUL_2020
  run backup
  [ "$status" -eq 0 ]
  [ -f "$(acct)/originals/Odd/a.jpg" ]
}
