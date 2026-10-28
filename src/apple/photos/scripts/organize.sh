#!/bin/bash
# Build by-date/ and albums/ hardlink trees next to originals/, where every photo is stored once.
# Safe to rerun: existing links are skipped. Nothing in originals/ is modified.
# The whole script lives in main so bash has parsed all of it before running any of it;
# editing the file mid-run then cannot corrupt that run.
main() {
  # shellcheck source=src/apple/photos/scripts/lib.sh
  source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
  # Configuration comes from the environment only (see README)
  require_env
  require_tools flock find stat awk
  take_lock "Backup still running, exiting"
  parse_dry_run   # DRY_RUN=true ./organize.sh -> count only, create nothing

  LOG=$ICLOUD_ROOT/organize-$(date +%F).log   # one file per day; the day's runs append to it
  PROGRESS_EVERY=${PROGRESS_EVERY:-1000} # log a progress line every N files

  # returns 0 = created, 1 = already existed, 2 = failed
  link_one() {
    [ -e "$2" ] && return 1
    [ "$DRY" = true ] && return 0
    mkdir -p "${2%/*}" && ln "$1" "$2" || return 2
  }

  # Like link_one, but when a different photo already has that name, links it as <name>-<size>.<ext>
  link_unique() {
    local alt
    [ -e "$2" ] || { link_one "$1" "$2"; return; }
    [ "$1" -ef "$2" ] && return 1
    if [[ ${2##*/} == *.* ]]; then alt="${2%.*}-$(stat -c %s "$1").${2##*.}"; else alt="$2-$(stat -c %s "$1")"; fi
    if [ -e "$alt" ] && ! [ "$1" -ef "$alt" ]; then return 2; fi
    link_one "$1" "$alt"
  }

  # Reads albums.csv (path,size,mtime,stored) and prints "path<TAB>stored" for each entry
  albums_tsv() {
    awk '
      function parse(line, f,   n, i, c, q, cur) {
        n = 1; cur = ""; q = 0
        for (i = 1; i <= length(line); i++) {
          c = substr(line, i, 1)
          if (q) {
            if (c != "\"") cur = cur c
            else if (substr(line, i + 1, 1) == "\"") { cur = cur c; i++ }
            else q = 0
          } else if (c == "\"") q = 1
          else if (c == ",") { f[n++] = cur; cur = "" }
          else cur = cur c
        }
        f[n] = cur
        return n
      }
      NR > 1 { n = parse($0, f); print f[1] "\t" f[n] }' "$1"
  }

  link_account() {
    local base="$1" src="$1/originals" t0=$SECONDS
    local d_new=0 d_have=0 d_fail=0 a_new=0 a_have=0 a_fail=0 a_miss=0
    local ym f line stored album name total i

    [ -d "$src" ] || { log "[$base] ERROR: $src not found"; return 1; }
    log "[$base] starting (dry=$DRY)"

    # --- by-date/YYYY/MM (uses file modification time; Synology @eaDir index folders are skipped) ---
    log "[$base] by-date: counting files..."
    total=$(find "$src" -name @eaDir -prune -o -type f -print | wc -l)
    log "[$base] by-date: $total files to check"
    i=0
    while IFS= read -r -d '' ym && IFS= read -r -d '' f; do
      link_unique "$f" "$base/by-date/$ym/${f##*/}"
      case $? in
        0) ((d_new++));;
        1) ((d_have++));;
        *) ((d_fail++)); if ((d_fail <= 20)); then log "[$base] FAILED to link: $f"; fi;;
      esac
      ((i++))
      if ((i % PROGRESS_EVERY == 0)); then
        log "[$base] by-date: $i/$total (new=$d_new existing=$d_have failed=$d_fail) $((SECONDS - t0))s elapsed"
      fi
    done < <(find "$src" -name @eaDir -prune -o -type f -printf '%TY/%Tm\0%p\0')
    log "[$base] by-date done: new=$d_new existing=$d_have failed=$d_fail"

    # --- albums/<Album>/ (from albums.csv, which the backup script writes) ---
    if [ -s "$base/albums.csv" ]; then
      total=$(albums_tsv "$base/albums.csv" | wc -l)
      log "[$base] albums: $total entries to check"
      i=0
      while IFS=$'\t' read -r line stored; do
        album="${line%/*}"; name="${line##*/}"
        ((i++))
        if [ ! -f "$src/$stored" ]; then
          ((a_miss++))
          if ((a_miss <= 20)); then log "[$base] not in originals: $line -> $stored"; fi
        else
          link_one "$src/$stored" "$base/albums/$album/$name"
          case $? in
            0) ((a_new++));;
            1) ((a_have++));;
            *) ((a_fail++)); if ((a_fail <= 20)); then log "[$base] FAILED to link: $line"; fi;;
          esac
        fi
        if ((i % PROGRESS_EVERY == 0)); then
          log "[$base] albums: $i/$total (new=$a_new existing=$a_have failed=$a_fail not-in-originals=$a_miss) $((SECONDS - t0))s elapsed"
        fi
      done < <(albums_tsv "$base/albums.csv")
      log "[$base] albums done: new=$a_new existing=$a_have failed=$a_fail not-in-originals=$a_miss"
    else
      log "[$base] albums.csv missing or empty; skipped albums"
    fi

    log "[$base] finished in $((SECONDS - t0))s"
    [ "$d_fail" -eq 0 ] && [ "$a_fail" -eq 0 ] && [ "$a_miss" -eq 0 ]
  }

  log "=== link run started ==="
  overall=0
  for account in $ICLOUD_ACCOUNTS; do
    link_account "$ICLOUD_ROOT/${account%%=*}" || overall=1
  done
  log "=== link run finished: exit $overall, ${SECONDS}s total ==="
  return "$overall"
}

main "$@"; exit
