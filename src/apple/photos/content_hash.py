"""The SHA-256 of the files of photos, as ids for matching (`content=hash` in --diff-mode): the same bytes are the same photo.

Two files can only have the same bytes if they have the same size, so only a photo whose size also occurs on the other side is read.
A photo is its file: a library's original (below `originals/`) or a folder's file. A hash is cached with the file's modification time
and size, so a file is read again only when it changed; `refresh` reads them all again.
"""
import hashlib
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from apple.photos import powerphotos as pp
from apple.photos.matching import Ids
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache, fingerprint

KIND = "sha256"
BLOCK = 1 << 20


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


def content_ids(sides: List[pp.Photos], selected: List[Dict], refresh: bool = False, say: Optional[Callable[[str], None]] = None) -> List[Ids]:
    """For each side, {photo: {"sha256": {hash}}} for the photos (of selected, a Counter of the photos in range) whose size occurs on
    the other side and whose file can be read."""
    sizes = [{key[1] for key in chosen if key[1].isdigit() and int(key[1])} for chosen in selected]
    wanted = [
        [(key, path) for key in chosen if key[1] in sizes[1 - index] for path in sides[index].files.get(key, [])[:1]]
        for index, chosen in enumerate(selected)
    ]
    total = sum(len(items) for items in wanted)
    if not total:
        return [{}, {}]
    cache, key = Cache(read=not refresh), fingerprint("sha256")
    previous = cache.get(key)
    known = previous[0] if previous else {}
    kept: Dict[str, list] = dict(known)
    progress, started, done = Progress(), time.monotonic(), 0
    ids: List[Ids] = [{}, {}]
    for index, items in enumerate(wanted):
        for photo, relative in items:
            path = Path(sides[index].library) / relative
            done += 1
            progress.update(f"Reading files to compare their bytes: {done:,}/{total:,}")
            try:
                info = path.stat()
            except OSError:
                continue
            stamp = [int(info.st_mtime), info.st_size]
            cached = known.get(str(path))
            digest = cached[2] if cached and cached[:2] == stamp else file_digest(path)
            if digest is None:
                continue
            kept[str(path)] = stamp + [digest]
            ids[index].setdefault(photo, {}).setdefault(KIND, set()).add(digest)
    if kept != known:
        cache.put(key, kept)
    (say or progress.line)(f"Read the bytes of {total:,} files to compare ({duration(time.monotonic() - started)})")
    return ids
