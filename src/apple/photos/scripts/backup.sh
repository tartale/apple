#!/bin/bash
# Copy every photo and video from each iCloud account into <account>/originals/, plus the album listing.
# "All Photos" lands in originals/ as it is on iCloud (flat today); photos that exist only in albums land in originals/<Album>/,
# once each (a photo is the same when file name, size and modification time match). albums.csv maps each album entry to its stored file.
# One lock for all accounts: they run one after the other, which is also gentler on Apple's rate limits
# The whole script lives in main so bash has parsed all of it before running any of it;
# editing the file mid-run then cannot corrupt that run.
main() {
  # shellcheck source=src/apple/photos/scripts/lib.sh
  source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
  # Configuration comes from the environment only (see README)
  require_env
  RCLONE=${ICLOUD_RCLONE:-rclone}
  require_tools "$RCLONE" flock awk sort find
  take_lock "Previous run still active, exiting"
  parse_dry_run

  TAB=$'\t'
  CONF_OPTS=()
  if [ -n "${ICLOUD_RCLONE_CONF:-}" ]; then CONF_OPTS=(--config "$ICLOUD_RCLONE_CONF"); fi
  LOG=$ICLOUD_ROOT/backup-$(date +%F).log   # one file per day; the day's runs append to it

  # Interactive runs show rclone's live transfer progress; the log file gets INFO either way
  RCLONE_OPTS=()
  if [ "$DRY" = true ]; then RCLONE_OPTS+=(--dry-run); fi
  if [ -t 1 ]; then RCLONE_OPTS+=(--progress); fi

  # Removes the working files in the dir $1 (and the dir itself on a dry run, where it is a temp dir)
  cleanup_work() {
    rm -f "$1/listing.tmp" "$1/on-disk.tmp" "$1/album-only.tmp" "$1/albums.csv.new" "$1/stored.tmp" "$1/albums.csv.ok"
    if [ "$DRY" = true ]; then rmdir "$1"; fi
  }

  backup_account() {
    local remote="$1" dest="$2"
    local rc=0 work="$2" listing on_disk album_only albums_new stored_now albums_ok kept

    # A dry run keeps its working files outside the account dir and changes nothing in it
    if [ "$DRY" = true ]; then work=$(mktemp -d) || return 1; else mkdir -p "$dest/originals"; fi
    listing="$work/listing.tmp" on_disk="$work/on-disk.tmp" album_only="$work/album-only.tmp" albums_new="$work/albums.csv.new" stored_now="$work/stored.tmp" albums_ok="$work/albums.csv.ok"
    log "[$remote] copying All Photos (dry-run=$DRY_RUN)"

    $RCLONE copy "${remote}:/PrimarySync/All Photos" "$dest/originals" \
      "${CONF_OPTS[@]}" "${RCLONE_OPTS[@]}" \
      --transfers 4 --checkers 8 \
      --retries 5 --low-level-retries 20 \
      --log-file "$LOG" --log-level INFO || rc=1

    # Full listing (modification time, size, path; tab-separated), taken after the copy above and sorted by path so that
    # the same photo always resolves to the same stored copy. Entries outside "All Photos" are album members.
    if ! $RCLONE lsf "${remote}:/PrimarySync" -R --files-only --format tsp --separator "$TAB" \
         "${CONF_OPTS[@]}" --log-file "$LOG" --log-level INFO > "$listing"; then
      log "[$remote] ERROR: listing failed; album-only photos not copied"
      cleanup_work "$work"
      return 1
    fi

    # What originals/ already holds (time, size, path), so a photo that has left All Photos (deleted from iCloud, but still in
    # Recently Deleted or an album) is not downloaded again. Times are local, truncated to the second, like rclone's listing.
    if [ -d "$dest/originals" ]; then
      find "$dest/originals" -name @eaDir -prune -o -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS\t%s\t%P\n' |
        LC_ALL=C sort -t "$TAB" -k3 > "$on_disk"
    else
      : > "$on_disk"
    fi

    # A photo is identified by file name + size + modification time. For each album entry, "stored" is where its file lives
    # in originals/: the All Photos copy if there is one, else a copy already in originals/, else the first album path
    # holding that photo.
    # albums.csv records that; album_only lists the one path to download per photo that is only in albums.
    if ! LC_ALL=C sort -t "$TAB" -k3 "$listing" | awk -F "$TAB" -v albums="$albums_new" -v only="$album_only" -v disk="$on_disk" '
      function csv(s) {
        if (s ~ /[",\n]/) { gsub(/"/, "\"\"", s); s = "\"" s "\"" }
        return s
      }
      FILENAME == disk {
        t = $1; sub(/\.[0-9]*$/, "", t); name = $3; sub(/.*\//, "", name)
        key = name SUBSEP $2 SUBSEP t
        if (!(key in onDisk)) onDisk[key] = $3
        next
      }
      {
        path = $3
        if (index(path, "All Photos/") == 1) {
          rel = substr(path, 12); name = rel; sub(/.*\//, "", name)
          have[name SUBSEP $2 SUBSEP $1] = rel   # where rclone copy puts it in originals/
          next
        }
        if (index(path, "/") > 0) { n++; mtime[n] = $1; size[n] = $2; entry[n] = path }
      }
      END {
        print "path,size,mtime,stored\t" > albums   # each row: the csv line, a tab, then the stored path
        for (i = 1; i <= n; i++) {
          name = entry[i]; sub(/.*\//, "", name)
          key = name SUBSEP size[i] SUBSEP mtime[i]
          if (key in have) stored = have[key]
          else if (key in onDisk) stored = onDisk[key]
          else {
            if (!(key in first)) { first[key] = entry[i]; print entry[i] > only }
            stored = first[key]
          }
          print csv(entry[i]) "," size[i] "," mtime[i] "," csv(stored) "\t" stored > albums
        }
        close(albums); close(only)
      }' "$on_disk" -; then
      log "[$remote] ERROR: could not build the album list"
      cleanup_work "$work"
      return 1
    fi
    [ -e "$album_only" ] || : > "$album_only"

    log "[$remote] copying $(wc -l < "$album_only") album-only photos (one copy each)"
    $RCLONE copy "${remote}:/PrimarySync" "$dest/originals" \
      --files-from-raw "$album_only" \
      "${CONF_OPTS[@]}" "${RCLONE_OPTS[@]}" \
      --transfers 4 --checkers 8 \
      --retries 5 --low-level-retries 20 \
      --log-file "$LOG" --log-level INFO || rc=1

    # albums.csv lists only entries whose file is in originals/, so a download that failed leaves out its own rows
    # (the failure is reported by this run's exit status and tried again on the next run) and nothing else
    if [ "$DRY" = true ]; then
      log "[$remote] dry-run: albums.csv left as is ($(($(wc -l < "$albums_new") - 1)) entries in the new list)"
    else
      find "$dest/originals" -name @eaDir -prune -o -type f -printf '%P\n' > "$stored_now"
      awk -F "$TAB" -v have="$stored_now" '
        FILENAME == have { present[$0] = 1; next }
        FNR == 1 || ($2 in present) { print $1 }' "$stored_now" "$albums_new" > "$albums_ok"
      kept=$(($(wc -l < "$albums_ok") - 1))
      if [ "$kept" -lt $(($(wc -l < "$albums_new") - 1)) ]; then
        log "[$remote] albums.csv written without $(($(wc -l < "$albums_new") - 1 - kept)) entries whose file was not downloaded"
      fi
      mv "$albums_ok" "$dest/albums.csv"
    fi

    cleanup_work "$work"
    log "[$remote] finished (exit $rc)"
    return $rc
  }

  log "=== backup run started ==="
  overall=0
  for account in $ICLOUD_ACCOUNTS; do
    backup_account "${account#*=}" "$ICLOUD_ROOT/${account%%=*}" || overall=1
  done
  log "=== backup run finished: exit $overall, ${SECONDS}s total ==="
  return "$overall"
}

main "$@"; exit
