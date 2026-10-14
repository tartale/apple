#!/usr/bin/env bats

load helpers
setup() { common_setup; }

acct() { echo "$ICLOUD_ROOT/tom"; }

# Puts a file straight into originals/ with the given size and mtime
original() {
  local p="$(acct)/originals/$1"
  mkdir -p "$(dirname "$p")"
  truncate -s "$2" "$p"
  touch -d "@$3" "$p"
}

@test "exits 2 and names the missing required variables" {
  unset ICLOUD_ROOT ICLOUD_ACCOUNTS
  run organize
  [ "$status" -eq 2 ]
  [[ $output == *"ICLOUD_ROOT ICLOUD_ACCOUNTS"* ]]
}

@test "by-date holds a hardlink to each original, by modification month" {
  original a.jpg 100 $JUN_2020
  original b.jpg 100 $JUL_2020
  run organize
  [ "$status" -eq 0 ]
  [ "$(acct)/by-date/2020/06/a.jpg" -ef "$(acct)/originals/a.jpg" ]
  [ "$(acct)/by-date/2020/07/b.jpg" -ef "$(acct)/originals/b.jpg" ]
}

@test "a different photo with the same name in the same month is linked with its size appended" {
  original a.jpg 100 $JUN_2020
  original sub/a.jpg 250 $JUN_2020
  run organize
  [ "$status" -eq 0 ]
  [ -f "$(acct)/by-date/2020/06/a.jpg" ]
  [ -f "$(acct)/by-date/2020/06/a-100.jpg" ] || [ -f "$(acct)/by-date/2020/06/a-250.jpg" ]
  [ "$(ls "$(acct)/by-date/2020/06" | wc -l)" -eq 2 ]
}

@test "Synology @eaDir folders are skipped" {
  original a.jpg 100 $JUN_2020
  original "@eaDir/a.jpg/SYNOPHOTO_THUMB.jpg" 10 $JUN_2020
  run organize
  [ "$status" -eq 0 ]
  [ "$(find "$(acct)/by-date" -type f | wc -l)" -eq 1 ]
}

@test "albums are hardlinks to the stored file named in albums.csv" {
  original a.jpg 100 $JUN_2020
  original "Beach/b.jpg" 200 $JUN_2020
  cat > "$(acct)/albums.csv" <<'CSV'
path,size,mtime,stored
Holiday/a.jpg,100,2020-06-15 12:00:00,a.jpg
Trip/b.jpg,200,2020-06-15 12:00:00,Beach/b.jpg
CSV
  run organize
  [ "$status" -eq 0 ]
  [ "$(acct)/albums/Holiday/a.jpg" -ef "$(acct)/originals/a.jpg" ]
  [ "$(acct)/albums/Trip/b.jpg" -ef "$(acct)/originals/Beach/b.jpg" ]
}

@test "quoted album entries with a comma are linked under their real name" {
  original "Trip/Day 1, morning.jpg" 500 $JUN_2020
  printf 'path,size,mtime,stored\n"Trip/Day 1, morning.jpg",500,2020-06-15 12:00:00,"Trip/Day 1, morning.jpg"\n' > "$(acct)/albums.csv"
  run organize
  [ "$status" -eq 0 ]
  [ "$(acct)/albums/Trip/Day 1, morning.jpg" -ef "$(acct)/originals/Trip/Day 1, morning.jpg" ]
}

@test "a second run creates nothing new and still succeeds" {
  original a.jpg 100 $JUN_2020
  printf 'path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n' > "$(acct)/albums.csv"
  run organize
  [ "$status" -eq 0 ]
  run organize
  [ "$status" -eq 0 ]
  grep -q "by-date done: new=0 existing=1" "$(logfile organize)"
  grep -q "albums done: new=0 existing=1" "$(logfile organize)"
}

@test "DRY_RUN=true creates no links" {
  original a.jpg 100 $JUN_2020
  printf 'path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n' > "$(acct)/albums.csv"
  DRY_RUN=true run organize
  [ "$status" -eq 0 ]
  [ ! -e "$(acct)/by-date" ]
  [ ! -e "$(acct)/albums" ]
}

@test "an account with no originals dir is an error" {
  run organize
  [ "$status" -eq 1 ]
}

@test "the day's runs append to one log named with the date" {
  mkdir -p "$(acct)/originals"
  run organize
  run organize
  [ "$status" -eq 0 ]
  [ "$(find "$ICLOUD_ROOT" -maxdepth 1 -name 'organize*.log' | wc -l)" -eq 1 ]
  [ "$(grep -c "link run started" "$(logfile organize)")" -eq 2 ]
}
