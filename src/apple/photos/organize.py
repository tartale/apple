"""`apple photos organize`: build by-date/YYYY/MM/ and albums/<Album>/ hardlink trees beside originals/.

Safe to rerun: existing links are skipped. Nothing in originals/ is modified.
"""
import csv
import os
import time
from pathlib import Path
from typing import List, Tuple

from apple.photos.backup import walk_files
from apple.photos.common import Log, exclusive_lock, is_dry_run, require_env

CREATED, EXISTED, FAILED = 0, 1, 2


def link_one(source: Path, target: Path, dry: bool) -> int:
    if target.exists():
        return EXISTED
    if dry:
        return CREATED
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
    except OSError:
        return FAILED
    return CREATED


def link_unique(source: Path, target: Path, dry: bool) -> int:
    """Like link_one, but when a different photo already has that name, links it as <name>-<size>.<ext>."""
    if not target.exists():
        return link_one(source, target, dry)
    if os.path.samefile(source, target):
        return EXISTED
    size = source.stat().st_size
    name = target.name
    alternative = target.with_name(f"{name.rsplit('.', 1)[0]}-{size}.{name.rsplit('.', 1)[1]}" if "." in name else f"{name}-{size}")
    if alternative.exists() and not os.path.samefile(source, alternative):
        return FAILED
    return link_one(source, alternative, dry)


def album_rows(csv_file: Path) -> List[Tuple[str, str]]:
    """The (path, stored) pairs of albums.csv (path,size,mtime,stored), without its header."""
    with open(csv_file, newline="") as handle:
        rows = list(csv.reader(handle))[1:]
    return [(row[0], row[-1]) for row in rows if row]


def run(dry_flag: bool = False) -> int:
    require_env()
    with exclusive_lock("Backup still running, exiting"):
        dry = is_dry_run(dry_flag)
        log = Log("organize")
        every = int(os.environ.get("PROGRESS_EVERY") or 1000)  # log a progress line every N files
        started = time.monotonic()

        def link_account(base: Path) -> bool:
            source = base / "originals"
            began = time.monotonic()
            if not source.is_dir():
                log(f"[{base}] ERROR: {source} not found")
                return False
            log(f"[{base}] starting (dry={str(dry).lower()})")

            # by-date/YYYY/MM (uses file modification time; Synology @eaDir index folders are skipped)
            log(f"[{base}] by-date: counting files...")
            files = sorted(walk_files(source), key=os.fsencode)
            log(f"[{base}] by-date: {len(files)} files to check")
            counts = [0, 0, 0]
            for index, path in enumerate(files, 1):
                moment = time.localtime(int(path.stat().st_mtime))
                target = base / "by-date" / time.strftime("%Y", moment) / time.strftime("%m", moment) / path.name
                result = link_unique(path, target, dry)
                counts[result] += 1
                if result == FAILED and counts[FAILED] <= 20:
                    log(f"[{base}] FAILED to link: {path}")
                if index % every == 0:
                    log(f"[{base}] by-date: {index}/{len(files)} (new={counts[CREATED]} existing={counts[EXISTED]} "
                        f"failed={counts[FAILED]}) {int(time.monotonic() - began)}s elapsed")
            log(f"[{base}] by-date done: new={counts[CREATED]} existing={counts[EXISTED]} failed={counts[FAILED]}")
            date_failed = counts[FAILED]

            # albums/<Album>/ (from albums.csv, which the backup writes)
            albums_csv = base / "albums.csv"
            album_failed = missing = 0
            if albums_csv.is_file() and albums_csv.stat().st_size > 0:
                rows = album_rows(albums_csv)
                log(f"[{base}] albums: {len(rows)} entries to check")
                counts = [0, 0, 0]
                for index, (line, stored) in enumerate(rows, 1):
                    album, _, name = line.rpartition("/")
                    if not (source / stored).is_file():
                        missing += 1
                        if missing <= 20:
                            log(f"[{base}] not in originals: {line} -> {stored}")
                    else:
                        result = link_one(source / stored, base / "albums" / album / name, dry)
                        counts[result] += 1
                        if result == FAILED and counts[FAILED] <= 20:
                            log(f"[{base}] FAILED to link: {line}")
                    if index % every == 0:
                        log(f"[{base}] albums: {index}/{len(rows)} (new={counts[CREATED]} existing={counts[EXISTED]} "
                            f"failed={counts[FAILED]} not-in-originals={missing}) {int(time.monotonic() - began)}s elapsed")
                log(f"[{base}] albums done: new={counts[CREATED]} existing={counts[EXISTED]} failed={counts[FAILED]} "
                    f"not-in-originals={missing}")
                album_failed = counts[FAILED]
            else:
                log(f"[{base}] albums.csv missing or empty; skipped albums")

            log(f"[{base}] finished in {int(time.monotonic() - began)}s")
            return date_failed == 0 and album_failed == 0 and missing == 0

        log("=== link run started ===")
        overall = 0
        for account in os.environ["ICLOUD_ACCOUNTS"].split():
            if not link_account(Path(os.environ["ICLOUD_ROOT"]) / account.partition("=")[0]):
                overall = 1
        log(f"=== link run finished: exit {overall}, {int(time.monotonic() - started)}s total ===")
        return overall
