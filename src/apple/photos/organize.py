"""`apple photos organize`: build by-date/YYYY/MM/ and albums/<Album>/ hardlink trees beside originals/.

Safe to rerun: existing links are skipped. Nothing in originals/ is modified.
"""
import csv
import filecmp
import os
import time
from pathlib import Path
from typing import List, NamedTuple, Tuple

from apple.photos.backup import walk_files
from apple.photos.common import Log, exclusive_lock, is_dry_run, require_env

CREATED, EXISTED, FAILED = 0, 1, 2
MAX_NAMES = 1000  # names tried for one photo before giving up


class Outcome(NamedTuple):
    result: int  # CREATED, EXISTED or FAILED
    detail: str = ""  # for FAILED, why


def link_one(source: Path, target: Path, dry: bool) -> Outcome:
    if target.exists():
        return Outcome(EXISTED)
    if dry:
        return Outcome(CREATED)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
    except OSError as error:
        return Outcome(FAILED, f"{error.strerror or error} ({target})")
    return Outcome(CREATED)


def same_photo(source: Path, other: Path) -> bool:
    """Whether other is the same file as source: the same inode, or a copy with the very same bytes (a link left from a time when
    originals/ held another copy, or a copy rclone downloaded again)."""
    try:
        return os.path.samefile(source, other) or (source.stat().st_size == other.stat().st_size and filecmp.cmp(source, other, shallow=False))
    except OSError:
        return False


def names_for(target: Path, size: int):
    """The names a photo can have in a folder, in order: its own, then with its size (<name>-<size>.<ext>), then with a number
    after that (<name>-<size>-2.<ext>, -3 ...)."""
    yield target
    stem, dot, extension = target.name.rpartition(".") if "." in target.name else (target.name, "", "")
    for number in range(1, MAX_NAMES):
        suffix = f"-{size}" if number == 1 else f"-{size}-{number}"
        yield target.with_name(f"{stem}{suffix}{dot}{extension}")


def link_unique(source: Path, target: Path, dry: bool) -> Outcome:
    """Like link_one, but when another photo already has that name, links it under the next name that is free: a photo that is
    already there under one of those names (the same inode, or a copy with the same bytes) counts as existing, and different
    photos of the same name and size are told apart by a number."""
    size = source.stat().st_size
    for candidate in names_for(target, size):
        if not candidate.exists():
            return link_one(source, candidate, dry)
        if same_photo(source, candidate):
            return Outcome(EXISTED)
    return Outcome(FAILED, f"{MAX_NAMES} photos of the name {target.name} and this size ({target.parent})")


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
                outcome = link_unique(path, target, dry)
                counts[outcome.result] += 1
                if outcome.result == FAILED and counts[FAILED] <= 20:
                    log(f"[{base}] FAILED to link: {path}: {outcome.detail}")
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
                        outcome = link_one(source / stored, base / "albums" / album / name, dry)
                        counts[outcome.result] += 1
                        if outcome.result == FAILED and counts[FAILED] <= 20:
                            log(f"[{base}] FAILED to link: {line}: {outcome.detail}")
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
