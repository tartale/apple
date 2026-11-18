"""`--prefer`: what to do with a photo the destination already has, as another file: the same photo that differs in its metadata.

none         the destination's photo stays and the source's is not written (a photo it has is not missing).
recommended  the source's file is written when file_compare recommends it over the destination's (the same image with something worth
             keeping that the destination's lacks: GPS, a capture time, a camera, keywords ...).
larger       the source's file is written when it is larger than the destination's.

A preferred file is written beside the photo it is preferred to, as a duplicate that Photos is told to import without asking, and
the photo it is preferred to is moved to Recently Deleted, so nothing is removed for good. Only photos that match under --diff-mode
are compared, so a different photo is never a replacement.
"""
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import typer

from apple.photos import file_compare, matching
from apple.photos import powerphotos as pp


class Prefer(str, Enum):
    none = "none"
    recommended = "recommended"
    larger = "larger"


class Swap(NamedTuple):
    """The photo of the source to write, the photo of the destination it is preferred to, the Photos identifier of that one, why, and
    the identifiers of every photo of the destination it is preferred to (one photo can match several)."""
    source: tuple
    destination: tuple
    identifier: str
    why: str
    identifiers: list


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
          wanted_ids: Optional[dict] = None, held_ids: Optional[dict] = None) -> Tuple[List[Swap], List[tuple]]:
    """([for each photo of wanted (in the source) that matches photos of there (in the destination) under the mode, as different
    files, the pair when the source's is preferred to every one of them], [the destination's hidden photos that stop a swap]).

    Every matching photo of the destination is looked at, hidden ones too: when any of them is as good as the source's (the same
    bytes, or not beaten by it), the destination already has the better file and nothing is written. When one is hidden (mostly a
    member of a burst) nothing is written either: Photos does not take a duplicate of it, and PowerPhotos cannot move a hidden photo
    to the trash. A destination photo is replaced once, however many files of the source match it. It needs a Photos identifier,
    to be moved to the trash."""
    if prefer == Prefer.none:
        return [], []
    index = matching.Index(mode, list(there), held_ids)
    found, blocked, taken = [], [], set()
    for photo in sorted(wanted):
        matches = index.matches(photo, (wanted_ids or {}).get(photo))
        own = path_of(source, photo)
        if not matches or own is None:
            continue
        hidden = [other for other in matches if other in held.hidden]
        if hidden:
            blocked += hidden
            continue
        why = []
        for other in matches:
            theirs = path_of(held, other)
            reason = decide(prefer, own, theirs) if theirs is not None and not same_file(own, theirs) else None
            if reason is None or not held.uuids.get(other):
                break  # not preferred to this one, or it cannot be compared or trashed: leave the photo as it is
            why.append(reason)
        else:
            if taken.isdisjoint(matches):
                taken.update(matches)
                found.append(Swap(photo, matches[0], held.uuids[matches[0]][0], why[0], [identifier for other in matches for identifier in held.uuids[other]]))
    return found, blocked


def same_file(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return False


def report_hidden(blocked: List[tuple]) -> None:
    """Says on stderr which hidden photos of the destination left a photo alone."""
    blocked = sorted(set(blocked))
    if blocked:
        typer.echo(
            f"--prefer leaves alone the photos that {len(blocked)} hidden photo{'' if len(blocked) == 1 else 's'} of the destination also match (a burst member, "
            "mostly): Photos does not take a duplicate of one, and PowerPhotos cannot move one to the trash:\n" + "\n".join(f"  {key[2]}  {key[0]}" for key in blocked),
            err=True,
        )
