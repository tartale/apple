"""A folder of photo and video files read as if it were a library, so that `diff` can compare a library with a folder.

A file is its modification time (taken as its capture time, in UTC), its size and its name: the same triple a library's database
gives a photo, so the two compare with no further rule. That is right for what a backup or a copy keeps (camera files, an iCloud
backup, Time Machine) and wrong for a file that was re-saved, whose modification time is the day it was saved. Libraries
inside the folder are not read, and neither is what a disk keeps for itself (@eaDir, .Spotlight-V100, ...). Time Machine's
linked folders are followed, as `find` does, and a file hard-linked into many snapshots counts once. A folder is read with the
cache `find` uses (see recover_cache): a directory whose modification time is what it was is not listed again, which is quick
but does not notice a file written over in place; refresh reads everything again.
"""
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from apple.photos import exif as exif_module
from apple.photos import powerphotos as pp
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache, fingerprint
from apple.photos.recover_sources import IGNORED_FOLDERS, PACKAGES, has_database, resolve, walk


def is_folder(text: str) -> bool:
    """Whether text names a folder of files (as opposed to a library: a .photoslibrary, or a name PowerPhotos knows)."""
    path = Path(text).expanduser()
    return path.is_dir() and not text.rstrip("/").endswith(PACKAGES) and not has_database(path)


def folder_photos(folder: Path, say: Optional[Callable[[str], None]] = None, refresh: bool = False, exif: bool = False) -> pp.Photos:
    """The photos and videos below folder as a pp.Photos: all visible, none hidden, burst or shared, and with no original
    missing; `files` gives each one's paths (every copy, in order), which for a library are paths below it. say(text) is told
    the notes of the walk. With refresh the cache is not used, and the result is saved. With exif a photo is dated by the capture
    time in its own metadata, as Photos dates it on import, when it has one (a file's capture time is cached with its modification time
    and size, so a file is opened again only when it changed)."""
    path = folder.expanduser()
    progress, started, files, paths = Progress(), time.monotonic(), Counter(), {}
    real = resolve(path) or path
    cache, key = Cache(read=not refresh), fingerprint("tree", str(real))  # the key `find` uses for the same folder
    previous = cache.get(key)
    listings, remember, counts = previous[0] if previous else {}, {}, {}
    exif_key = fingerprint("exif", str(real))
    prior = cache.get(exif_key) if exif else None
    dates, new_dates = prior[0] if prior else {}, {}  # path -> [modification time, size, capture time, its offset], or just the first two when the file has none

    def skip(logical: Path, real: Path) -> bool:
        return logical.name in IGNORED_FOLDERS or logical.name.endswith(PACKAGES) or has_database(real)

    count = 0
    for entry in walk(real, skip, "all", say or (lambda text: progress.line(text)), logical_root=path, seen_directories=set(), seen_files=set(),
                      listings=listings, remember=remember, counts=counts):
        if entry.is_directory or entry.status is None or pp.media_of(entry.logical.name) is None:
            continue
        when = datetime.fromtimestamp(int(entry.status.st_mtime), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if exif and pp.media_of(entry.logical.name) == "photos":
            stamp = [int(entry.status.st_mtime), entry.status.st_size]
            known = dates.get(str(entry.real))
            taken = known[2:] if known and known[:2] == stamp else list(exif_module.capture_time(entry.real) or ())
            new_dates[str(entry.real)] = stamp + taken
            when = (exif_module.utc_of(*taken) if taken else None) or when
        photo = (when, str(entry.status.st_size), entry.logical.name)
        files[photo] += 1
        paths.setdefault(photo, []).append(str(entry.real))
        count += 1
        progress.update(f"Reading {path}: {count:,} files so far")
    if remember:
        cache.put(key, remember)
    if exif and new_dates != dates:
        cache.put(exif_key, new_dates)
    reused = counts.get("reused", 0)
    progress.line(f"Read {count:,} photos and videos of {path} ({duration(time.monotonic() - started)})" + (f" [{reused:,} of {reused + counts.get('read', 0):,} folders as cached]" if reused else ""))
    return pp.Photos(path, files, Counter(), Counter(), {photo: sorted(found) for photo, found in paths.items()}, set(), Counter(), {}, {})
