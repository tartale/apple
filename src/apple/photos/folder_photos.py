"""A folder of photo and video files read as if it were a library, so that `diff` can compare a library with a folder.

A file is its modification time (taken as its capture time, in UTC), its size and its name: the same triple a library's database
gives a photo, so the two compare with no further rule. That is right for what a backup or a copy keeps (camera files, an iCloud
backup, Time Machine) and wrong for a file that was re-saved, whose modification time is the day it was saved. Libraries
inside the folder are not read, and neither is what a disk keeps for itself (@eaDir, .Spotlight-V100, ...). Time Machine's
linked folders are followed, as `find` does, and a file hard-linked into many snapshots counts once. A folder is read with the
cache `find` uses (see recover_cache): a directory whose modification time is what it was is not listed again, which is quick
but does not notice a file written over in place; refresh reads everything again.
"""
import base64
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from apple.photos import exif as exif_module
from apple.photos import image_size as image_size_module
from apple.photos import powerphotos as pp
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache, fingerprint
from apple.photos.recover_sources import IGNORED_FOLDERS, PACKAGES, has_database, resolve, walk


FINGERPRINT = re.compile(r"_([A-Za-z0-9+/\uff0f]{28})$")  # what rclone adds to a name that was taken: iCloud's id for the file


def fingerprint_in(name: str) -> Optional[str]:
    """iCloud's fingerprint of a file when its name ends in it (`IMAG0044_ASLNFDmSOi1j8HVs5UVlqKvxeRlA.JPG`, where the `/` a name
    cannot hold is written as a full-width one), else None: only 28 characters that decode to 21 bytes starting with 1 count, so
    a name that merely ends in 28 letters does not."""
    found = FINGERPRINT.search(os.path.splitext(name)[0])
    if not found:
        return None
    token = found.group(1).replace("\uff0f", "/")
    try:
        raw = base64.b64decode(token, validate=True)
    except ValueError:
        return None
    return token if len(raw) == 21 and raw[0] == 1 else None


def is_folder(text: str) -> bool:
    """Whether text names a folder of files (as opposed to a library: a .photoslibrary, or a name PowerPhotos knows)."""
    path = Path(text).expanduser()
    return path.is_dir() and not text.rstrip("/").endswith(PACKAGES) and not has_database(path)


def folder_photos(folder: Path, say: Optional[Callable[[str], None]] = None, refresh: bool = False, exif: bool = False, dimensions: bool = False) -> pp.Photos:
    """The photos and videos below folder as a pp.Photos: all visible, none hidden, burst or shared, and with no original
    missing; `files` gives each one's paths (every copy, in order), which for a library are paths below it. say(text) is told
    the notes of the walk. With refresh the cache is not used, and the result is saved. With exif a photo is dated by the capture
    time in its own metadata, as Photos dates it on import, when it has one (a file's capture time is cached with its modification time
    and size, so a file is opened again only when it changed). With dimensions each photo's pixel size is read from its header and
    kept, cached the same way, under `dims` in ids."""
    path = folder.expanduser()
    progress, started, files, paths = Progress(), time.monotonic(), Counter(), {}
    real = resolve(path) or path
    cache, key = Cache(read=not refresh), fingerprint("tree", str(real))  # the key `find` uses for the same folder
    previous = cache.get(key)
    listings, remember, counts = previous[0] if previous else {}, {}, {}
    exif_key = fingerprint("exif", str(real))
    prior = cache.get(exif_key) if exif else None
    dates, new_dates = prior[0] if prior else {}, {}  # path -> [modification time, size, capture time, its offset], or just the first two when the file has none
    dims_key = fingerprint("dims", str(real))
    before = cache.get(dims_key) if dimensions else None
    sizes, new_sizes = before[0] if before else {}, {}  # path -> [modification time, size, `<larger>x<smaller>`, or "" when it is not an image we can read]
    dimensions_of: dict = {}

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
        if dimensions and pp.media_of(entry.logical.name) == "photos":
            stamp = [int(entry.status.st_mtime), entry.status.st_size]
            known = sizes.get(str(entry.real))
            if known and known[:2] == stamp:
                text = known[2]
            else:
                found = image_size_module.image_size(entry.real)
                text = f"{found[0]}x{found[1]}" if found else ""
            new_sizes[str(entry.real)] = stamp + [text]
            if text:
                dimensions_of[photo] = text
        files[photo] += 1
        paths.setdefault(photo, []).append(str(entry.real))
        count += 1
        progress.update(f"Reading {path}: {count:,} files so far")
    if remember:
        cache.put(key, remember)
    if exif and new_dates != dates:
        cache.put(exif_key, new_dates)
    if dimensions and new_sizes != sizes:
        cache.put(dims_key, new_sizes)
    reused = counts.get("reused", 0)
    progress.line(f"Read {count:,} photos and videos of {path} ({duration(time.monotonic() - started)})" + (f" [{reused:,} of {reused + counts.get('read', 0):,} folders as cached]" if reused else ""))
    ids: dict = {}
    for photo in files:
        token = fingerprint_in(photo[2])
        if token:
            ids.setdefault(photo, {}).setdefault("stable", set()).add(token)
    for photo, text in dimensions_of.items():
        ids.setdefault(photo, {}).setdefault("dims", set()).add(text)
    return pp.Photos(path, files, Counter(), Counter(), {photo: sorted(found) for photo, found in paths.items()}, set(), Counter(), {}, {}, Counter(), ids)
