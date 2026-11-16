"""`--prefer`: what to do with a photo the destination already has, as another file: the same photo that differs in its metadata.

none         the destination's photo stays and the source's is not written (a photo it has is not missing).
recommended  the source's file is written when `compare` recommends it over the destination's (the same image with something worth
             keeping that the destination's lacks: GPS, a capture time, a camera, keywords ...).
larger       the source's file is written when it is larger than the destination's.

A preferred file is written beside the photo it is preferred to, as a duplicate that Photos is told to import without asking, and
the photo it is preferred to is moved to Recently Deleted, so nothing is removed for good. Only photos that match under --diff-mode
are compared, so a different photo is never a replacement.
"""
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import List, NamedTuple, Optional

from apple.photos import file_compare, matching
from apple.photos import powerphotos as pp


class Prefer(str, Enum):
    none = "none"
    recommended = "recommended"
    larger = "larger"


class Swap(NamedTuple):
    """The photo of the source to write, the photo of the destination it is preferred to, the Photos identifier of that one, and why."""
    source: tuple
    destination: tuple
    identifier: str
    why: str


def path_of(photos: pp.Photos, key: tuple) -> Optional[Path]:
    """The file of a photo that can be read, or None: a library's original (below the library), or a folder's file."""
    for stored in photos.files.get(key, []):
        path = Path(photos.library) / stored
        if path.is_file():
            return path
    return None


def decide(prefer: Prefer, source: Path, destination: Path) -> Optional[str]:
    """Why the source's file is preferred to the destination's, or None when it is not."""
    try:
        if prefer == Prefer.larger:
            return "larger" if source.stat().st_size > destination.stat().st_size else None
        result = file_compare.compare(source, destination)
    except OSError:
        return None
    return result.why if result.better == "first" else None


def swaps(prefer: Prefer, source: pp.Photos, held: pp.Photos, wanted: Counter, there: Counter, mode: matching.Mode,
          wanted_ids: Optional[dict] = None, held_ids: Optional[dict] = None) -> List[Swap]:
    """For each photo of wanted (in the source) that matches a photo of there (in the destination) under the mode, as a different
    file, the pair when the source's is preferred. The destination's photo needs a Photos identifier, to be moved to the trash."""
    if prefer == Prefer.none:
        return []
    matched, _, _ = matching.pairs(wanted, there, mode, wanted_ids, held_ids)
    found = []
    for first, second, _how in matched:
        own, theirs = path_of(source, first), path_of(held, second)
        uuids = held.uuids.get(second)
        if own is None or theirs is None or not uuids:
            continue
        try:
            if own.samefile(theirs):
                continue
        except OSError:
            continue
        why = decide(prefer, own, theirs)
        if why:
            found.append(Swap(first, second, uuids[0], why))
    return found
