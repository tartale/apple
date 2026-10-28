"""`apple photos backup`: copy every photo and video from each iCloud account into <account>/originals/, plus albums.csv.

"All Photos" lands in originals/ as it is on iCloud; photos that exist only in albums land in originals/<Album>/, once each.
A photo is the same photo when its file name, size and modification time match. albums.csv maps each album entry to its
stored file. One lock for all accounts: they run one after the other, which is also gentler on Apple's rate limits.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from apple.photos.common import Log, exclusive_lock, is_dry_run, require_env, require_rclone

Key = Tuple[str, str, str]  # file name, size, modification time
Listing = Tuple[str, str, str]  # modification time, size, path
HEADER = "path,size,mtime,stored"


def csv_field(text: str) -> str:
    if any(c in text for c in '",\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def walk_files(root: Path) -> List[Path]:
    """Every regular file under root, skipping Synology's @eaDir index folders."""
    found = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = [d for d in subdirectories if d != "@eaDir"]
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def on_disk(originals: Path) -> List[Tuple[str, str, str]]:
    """What originals/ already holds as (modification time, size, path), sorted by path in byte order.

    Times are local, truncated to the second, like rclone's listing.
    """
    held = []
    for path in walk_files(originals):
        info = path.stat()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(info.st_mtime)))
        held.append((stamp, str(info.st_size), path.relative_to(originals).as_posix()))
    return sorted(held, key=lambda item: os.fsencode(item[2]))


def plan_albums(
    listing: List[Listing], held: List[Tuple[str, str, str]]
) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    """Decides where each album entry is stored. Returns the albums.csv rows and the one path to download per photo.

    For each entry (path, size, mtime, stored), stored is the All Photos copy if there is one, else a copy already in
    originals/, else the first album path (in byte order) holding that photo.
    """
    in_all_photos: Dict[Key, str] = {}
    entries = []
    for mtime, size, path in sorted(listing, key=lambda item: (os.fsencode(item[2]), os.fsencode("\t".join(item)))):
        if path.startswith("All Photos/"):
            relative = path[len("All Photos/"):]
            in_all_photos[(relative.rsplit("/", 1)[-1], size, mtime)] = relative  # where rclone copy puts it
        elif "/" in path:
            entries.append((mtime, size, path))
    in_originals: Dict[Key, str] = {}
    for mtime, size, path in held:
        in_originals.setdefault((path.rsplit("/", 1)[-1], size, mtime), path)

    rows, to_download, first = [], [], {}
    for mtime, size, path in entries:
        key = (path.rsplit("/", 1)[-1], size, mtime)
        if key in in_all_photos:
            stored = in_all_photos[key]
        elif key in in_originals:
            stored = in_originals[key]
        else:
            if key not in first:
                first[key] = path
                to_download.append(path)
            stored = first[key]
        rows.append((path, size, mtime, stored))
    return rows, to_download


def row_text(row: Tuple[str, str, str, str]) -> str:
    path, size, mtime, stored = row
    return f"{csv_field(path)},{size},{mtime},{csv_field(stored)}"


def run(dry_flag: bool = False) -> int:
    require_env()
    rclone = require_rclone()
    with exclusive_lock("Previous run still active, exiting"):
        dry = is_dry_run(dry_flag)
        log = Log("backup")
        config = ["--config", os.environ["ICLOUD_RCLONE_CONF"]] if os.environ.get("ICLOUD_RCLONE_CONF") else []
        # Interactive runs show rclone's live transfer progress; the log file gets INFO either way
        options = (["--dry-run"] if dry else []) + (["--progress"] if sys.stdout.isatty() else [])
        common = ["--transfers", "4", "--checkers", "8", "--retries", "5", "--low-level-retries", "20",
                  "--log-file", str(log.path), "--log-level", "INFO"]
        started = time.monotonic()

        def backup_account(remote: str, dest: Path) -> int:
            status = 0
            if not dry:
                (dest / "originals").mkdir(parents=True, exist_ok=True)
            log(f"[{remote}] copying All Photos (dry-run={str(dry).lower()})")
            copied = subprocess.run([rclone, "copy", f"{remote}:/PrimarySync/All Photos", str(dest / "originals"),
                                     *config, *options, *common])
            status = 1 if copied.returncode else status

            # Full listing (modification time, size, path), taken after the copy above. Entries outside "All Photos"
            # are album members.
            listed = subprocess.run([rclone, "lsf", f"{remote}:/PrimarySync", "-R", "--files-only", "--format", "tsp",
                                     "--separator", "\t", *config, "--log-file", str(log.path), "--log-level", "INFO"],
                                    stdout=subprocess.PIPE)
            if listed.returncode:
                log(f"[{remote}] ERROR: listing failed; album-only photos not copied")
                return 1
            listing = []
            for line in os.fsdecode(listed.stdout).splitlines():
                fields = line.split("\t", 2)
                if len(fields) == 3:
                    listing.append((fields[0], fields[1], fields[2]))

            originals = dest / "originals"
            rows, to_download = plan_albums(listing, on_disk(originals) if originals.is_dir() else [])

            log(f"[{remote}] copying {len(to_download)} album-only photos (one copy each)")
            with tempfile.NamedTemporaryFile("wb", suffix=".album-only") as wanted:
                wanted.write(b"".join(os.fsencode(p) + b"\n" for p in to_download))
                wanted.flush()
                copied = subprocess.run([rclone, "copy", f"{remote}:/PrimarySync", str(originals),
                                         "--files-from-raw", wanted.name, *config, *options, *common])
            status = 1 if copied.returncode else status

            # albums.csv lists only entries whose file is in originals/, so a download that failed leaves out its own
            # rows (the failure is reported by this run's exit status and tried again on the next run) and nothing else
            if dry:
                log(f"[{remote}] dry-run: albums.csv left as is ({len(rows)} entries in the new list)")
            else:
                present = {p.relative_to(originals).as_posix() for p in walk_files(originals)}
                kept = [row for row in rows if row[3] in present]
                if len(kept) < len(rows):
                    log(f"[{remote}] albums.csv written without {len(rows) - len(kept)} entries whose file was not downloaded")
                pending = dest / "albums.csv.new"
                pending.write_text(HEADER + "\n" + "".join(row_text(row) + "\n" for row in kept))
                os.replace(pending, dest / "albums.csv")
            log(f"[{remote}] finished (exit {status})")
            return status

        log("=== backup run started ===")
        overall = 0
        for account in os.environ["ICLOUD_ACCOUNTS"].split():
            name, _, remote = account.partition("=")
            remote = remote or name  # a bare name is both
            overall = backup_account(remote, Path(os.environ["ICLOUD_ROOT"]) / name) or overall
        log(f"=== backup run finished: exit {overall}, {int(time.monotonic() - started)}s total ===")
        return overall
