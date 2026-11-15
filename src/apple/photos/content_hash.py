"""The SHA-256 of the files of photos, as ids for matching (`content=hash` in --diff-mode): the same bytes are the same photo. With
`content=picture` it is the SHA-256 of the picture data alone (a JPEG without its metadata), so the same picture with other metadata
around it is the same photo.

Two files can only have the same bytes if they have the same size, so only a photo whose size also occurs on the other side, or twice
on its own (for finding copies within a side), is read. The same picture can be in files of different sizes, so for the picture a photo is
read when the other side has a photo whose size is within 10% of it.
A photo is its file: a library's original (below `originals/`) or a folder's file. A hash is cached with the file's modification time
and size, so a file is read again only when it changed; `refresh` reads them all again.
"""
import hashlib
import time
from bisect import bisect_left
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional

from apple.photos import file_compare
from apple.photos import powerphotos as pp
from apple.photos.matching import PICTURE, Ids
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache, fingerprint

KIND = "sha256"
BLOCK = 1 << 20
PICTURE_SIZES = 0.10  # how far apart in size two files of one picture are looked for


def file_digest(path: Path) -> Optional[str]:
    """The SHA-256 of a file, as hex, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(BLOCK), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def content_ids(sides: List[pp.Photos], selected: List[Dict], refresh: bool = False, say: Optional[Callable[[str], None]] = None, kind: str = KIND) -> List[Ids]:
    """For each side, {photo: {kind: {hash}}} (kind `sha256` of the file, or `picture` of its picture data) for the photos (of selected,
    a Counter of the photos in range) whose size occurs on the other side (for the picture, is within 10% of one there), or on the same
    side for another photo, and whose file can be read and has that size. One side alone finds the copies within it."""
    counts = [Counter(key[1] for key in chosen if key[1].isdigit() and int(key[1])) for chosen in selected]
    elsewhere = [set().union(*[set(other) for other_index, other in enumerate(counts) if other_index != index]) for index in range(len(counts))]
    sizes = [sorted(int(size) for size in count) for count in counts]

    def near(index: int, size: str) -> bool:
        """Whether a side other than index holds a file of about this size."""
        if not (size.isdigit() and int(size)):
            return False
        low, high = int(int(size) * (1 - PICTURE_SIZES)), int(int(size) * (1 + PICTURE_SIZES)) + 1
        return any(bisect_left(other, high) > bisect_left(other, low) for other_index, other in enumerate(sizes) if other_index != index)

    def wants(index: int, key: tuple) -> bool:
        return (near(index, key[1]) if kind == PICTURE else key[1] in elsewhere[index]) or counts[index][key[1]] > 1

    wanted = [
        [(key, path) for key in chosen if wants(index, key) for path in sides[index].files.get(key, [])[:1]]
        for index, chosen in enumerate(selected)
    ]
    total = sum(len(items) for items in wanted)
    if not total:
        return [{} for _ in sides]
    cache, key = Cache(), fingerprint(kind)
    previous = cache.get(key)
    stored = previous[0] if previous else {}  # every file ever hashed: one entry, so a refresh must add to it and not replace it
    known = {} if refresh else stored
    kept: Dict[str, list] = dict(stored)
    progress, started, done = Progress(), time.monotonic(), 0
    ids: List[Ids] = [{} for _ in sides]
    for index, items in enumerate(wanted):
        for photo, relative in items:
            path = Path(sides[index].library) / relative
            done += 1
            progress.update(f"Reading files to compare their bytes: {done:,}/{total:,}")
            try:
                info = path.stat()
            except OSError:
                continue
            if str(info.st_size) != photo[1]:  # not the original the database describes (a placeholder, or a file that was changed)
                continue
            stamp = [int(info.st_mtime), info.st_size]
            cached = known.get(str(path))
            digest = cached[2] if cached and cached[:2] == stamp else (file_compare.picture_digest(path) if kind == PICTURE else file_digest(path))
            if digest is None:
                continue
            kept[str(path)] = stamp + [digest]
            ids[index].setdefault(photo, {}).setdefault(kind, set()).add(digest)
    if kept != stored:
        cache.put(key, kept)
    (say or progress.line)(f"Read the bytes of {total:,} files to compare ({duration(time.monotonic() - started)})")
    return ids
