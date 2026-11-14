"""When two photos are the same photo: how much their file name, date and size may differ.

A photo is its (UTC date, size, file name), as a library's database gives them. How closely two of those must agree to count as
one photo is a Mode, written as text by --diff-mode (or APPLE_PHOTOS_DIFF_MODE) on every command that compares photos:

    filename=full            the name without its extension, without regard to case (the default)
    filename=exact           the name without its extension, character for character
    filename=ignore          any name
    extension=full           the extension without regard to case, and .jpeg is .jpg (the default)
    extension=exact          the extension character for character
    extension=ignore         any extension: IMG_1234.HEIC and IMG_1234.JPG are one photo
    date=exif                a file in a folder is dated by the capture time in its own metadata (EXIF), as Photos dates it on
                             import, with its modification time only where it has none
    date=modified            a file in a folder is dated by its modification time alone (the default)
    date=within=2h           within a time (a number and s, m, h or d; 2h is the default)
    date=exact | date=ignore
    filesize=within=1%       within a share of the size (1% is the default), or a number of bytes: filesize=within=512bytes
    filesize=exact | filesize=ignore

    identity=auto            photos that both carry an id from iCloud are the same photo when the ids are equal, whatever their
                             name, date and size, and are not when the ids differ (the default); the library's `stable` id (the
                             fingerprint of the original) and `cloud` id, and the 28 characters a backup file's name ends in when
                             its name was taken (`IMAG0044_<fingerprint>.JPG`). Where no id is known on both sides, the other
                             parts decide, all of which must agree.
    identity=ignore          the other parts alone decide
    dimensions=ignore        the pixel size is not looked at (the default)
    dimensions=exact         photos whose pixel sizes are both known must have the same one (a rotated copy has the same size); read
                             from the database for a library and from the file's header for a folder, and an unknown size passes
    content=ignore           the bytes of the files are not read (the default)
    content=hash             files with the same SHA-256 are the same photo, whatever their name, date and size; a different hash
                             does not make them different (the tolerance for rewritten metadata stays). Only files whose size also
                             occurs on the other side are read, and a hash is cached; reading is slow over a network share

A mode is a comma-separated list read from left to right, starting from the default: `exact` (everything exact, a name as written)
and `tolerant` (the default) name whole modes, and a field after them changes just that field, as in
`exact,filename=full` or `date=within=1d,filesize=ignore`. A size that is not known (0, or not a number, as for a placeholder)
does not tell photos apart.
"""
import os
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from apple.photos import powerphotos as pp

Photo = Tuple[str, str, str]  # (UTC date as YYYY-MM-DD HH:MM:SS, size, file name)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class Mode:
    filename: str = "full"  # exact, full or ignore (of the name without its extension)
    extension: str = "full"  # exact, full or ignore
    identity: str = "auto"  # auto or ignore: whether ids from iCloud decide when both photos have one
    content: str = "ignore"  # ignore or hash: whether files with the same bytes are the same photo
    dimensions: str = "ignore"  # ignore or exact: whether the pixel sizes must agree
    date_source: str = "modified"  # modified or exif: where a folder's files get their date (a library's photos have Photos' own)
    date: str = "within"  # exact, within or ignore
    date_within: timedelta = timedelta(hours=2)  # a copy of a photo can differ by a time zone or daylight saving shift
    filesize: str = "within"  # exact, within or ignore
    size_within: float = 0.01  # and by a little in size, when the file's metadata was rewritten (a share of the size)
    size_bytes: Optional[int] = None  # or, when set, that many bytes whatever the size


TOLERANT = Mode()
EXACT = Mode(filename="exact", extension="exact", date="exact", filesize="exact", identity="ignore")
PRESETS = {"tolerant": TOLERANT, "exact": EXACT}
HELP = (
    "How closely two photos must agree to be the same photo, as a comma-separated list of filename=full|exact|ignore "
    "(of the name without its extension; full ignores case), "
    "extension=full|exact|ignore (full ignores case, and .jpeg is .jpg), identity=auto|ignore (photos with the same iCloud id are the same photo, "
    "and with different ids are not, whatever the other parts say), content=ignore|hash (files with the same SHA-256 are the same photo; slow), dimensions=ignore|exact (the pixel sizes must agree), date=exif|modified (where a file in a folder gets its date: its EXIF capture time, as Photos does, or its modification time) or date=exact|ignore|within=DURATION (such as 2h, 30m, 1d) and "
    "filesize=exact|ignore|within=PERCENT (such as 1%) or within=BYTES (such as 512bytes), or the whole modes exact and tolerant, read from left to right: "
    "`exact,filename=ignore`. The default is tolerant: filename=full,extension=full,identity=auto,content=ignore,date=modified,date=within=2h,filesize=within=1%. Also read from "
    "APPLE_PHOTOS_DIFF_MODE."
)


def parse(text: str) -> Mode:
    """The Mode a --diff-mode text says; ValueError, with what is wrong with it, when it is not one. Nothing is the default."""
    mode = TOLERANT
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if item in PRESETS:
            mode = PRESETS[item]
            continue
        field, _, value = item.partition("=")
        if field == "filename":
            if value in ("exact", "full", "ignore"):
                mode = replace(mode, filename=value)
            else:
                raise ValueError(f"filename={value!r}: use full, exact or ignore")
        elif field == "extension":
            if value in ("exact", "full", "ignore"):
                mode = replace(mode, extension=value)
            else:
                raise ValueError(f"extension={value!r}: use full, exact or ignore")
        elif field == "dimensions":
            if value in ("ignore", "exact"):
                mode = replace(mode, dimensions=value)
            else:
                raise ValueError(f"dimensions={value!r}: use ignore or exact")
        elif field == "content":
            if value in ("ignore", "hash"):
                mode = replace(mode, content=value)
            else:
                raise ValueError(f"content={value!r}: use ignore or hash")
        elif field == "identity":
            if value in ("auto", "ignore"):
                mode = replace(mode, identity=value)
            else:
                raise ValueError(f"identity={value!r}: use auto or ignore")
        elif field == "date":
            kind, _, amount = value.partition("=")
            if kind in ("exif", "modified") and not amount:
                mode = replace(mode, date_source=kind)
            elif kind in ("exact", "ignore") and not amount:
                mode = replace(mode, date=kind)
            elif kind == "within" and (found := re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", amount)):
                mode = replace(mode, date="within", date_within=timedelta(seconds=float(found.group(1)) * UNITS[found.group(2)]))
            else:
                raise ValueError(f"date={value!r}: use exif, modified, exact, ignore or within=DURATION (a number and s, m, h or d, such as 2h)")
        elif field == "filesize":
            kind, _, amount = value.partition("=")
            if kind in ("exact", "ignore") and not amount:
                mode = replace(mode, filesize=kind)
            elif kind == "within" and (found := re.fullmatch(r"(\d+(?:\.\d+)?)%", amount)):
                mode = replace(mode, filesize="within", size_within=float(found.group(1)) / 100, size_bytes=None)
            elif kind == "within" and (found := re.fullmatch(r"(\d+) ?(?:bytes?|b)", amount)):
                mode = replace(mode, filesize="within", size_bytes=int(found.group(1)))
            else:
                raise ValueError(f"filesize={value!r}: use exact, ignore or within=PERCENT (such as 1%) or within=N bytes (such as 512bytes)")
        else:
            raise ValueError(f"{item!r}: use filename=..., extension=..., dimensions=..., identity=..., content=..., date=..., filesize=..., exact or tolerant")
    return mode


def split_name(name: str) -> Tuple[str, str]:
    """(the name without its extension, the extension with its dot)."""
    stem, extension = os.path.splitext(name)
    return stem, extension


def name_key(mode: Mode, name: str) -> tuple:
    """What of a file name must agree: its name without the extension, and its extension, each as the mode reads it."""
    stem, extension = split_name(name)
    lowered = stem.lower()
    if mode.filename == "exact":
        stem_key = stem
    elif mode.filename == "ignore":
        stem_key = ""
    else:
        stem_key = lowered
    if mode.extension == "ignore":
        return stem_key, ""
    if mode.extension == "exact":
        return stem_key, extension
    return stem_key, ".jpg" if extension.lower() == ".jpeg" else extension.lower()


def same_size(mode: Mode, first: str, second: str) -> bool:
    if mode.filesize == "ignore":
        return True
    if mode.filesize == "exact":
        return first == second
    if not (first.isdigit() and second.isdigit() and int(first) and int(second)):
        return True
    allowed = mode.size_bytes if mode.size_bytes is not None else mode.size_within * max(int(first), int(second))
    return abs(int(first) - int(second)) <= allowed


def bucket(mode: Mode, photo: Photo) -> tuple:
    """What two photos must share to be compared at all: the name's key, and the date and size when those are exact."""
    return (name_key(mode, photo[2]), photo[0] if mode.date == "exact" else None, photo[1] if mode.filesize == "exact" else None)


Ids = Dict[Photo, Dict[str, set]]  # a photo -> {kind of id: the values it has}; a photo can have several items
CONTENT = "sha256"  # the kind of id that is the hash of a file's bytes: it shows photos to be the same, never different
DIMENSIONS = "dims"  # the kind of value that is a pixel size: it can show photos to be different, never the same


def conflicting(first: Optional[Dict[str, set]], second: Optional[Dict[str, set]]) -> bool:
    """Whether two photos have an id of the same kind and none of the values is shared: known, and different."""
    return bool(first and second) and any(
        kind != CONTENT and kind in second and first[kind] and second[kind] and not (first[kind] & second[kind]) for kind in first
    )


class Index:
    """Photos grouped for finding the ones that match a photo: by id, and by bucket and then date, so a date window is a slice."""

    def __init__(self, mode: Mode, photos, ids: Optional[Ids] = None) -> None:
        self.mode = mode
        self.kinds = ({"stable", "cloud"} if mode.identity == "auto" else set()) | ({CONTENT} if mode.content == "hash" else set())
        self.kept = self.kinds | ({DIMENSIONS} if mode.dimensions == "exact" else set())  # the kinds of value looked at
        self.ids: Ids = {photo: {kind: values for kind, values in kinds.items() if kind in self.kept} for photo, kinds in ids.items()} if ids and self.kept else {}
        self.by_id: Dict[Tuple[str, str], List[Photo]] = {}
        grouped: Dict[tuple, List[tuple]] = {}
        for photo in photos:
            grouped.setdefault(bucket(mode, photo), []).append((datetime.fromisoformat(photo[0]), photo))
            self.note(photo)
        self.groups: Dict[tuple, Tuple[List[tuple], List[datetime]]] = {}
        for where, items in grouped.items():
            items.sort(key=lambda item: item[0])
            self.groups[where] = (items, [at for at, _ in items])

    def note(self, photo: Photo) -> None:
        for kind, values in self.ids.get(photo, {}).items():
            for value in values:
                if kind in self.kinds:
                    self.by_id.setdefault((kind, value), []).append(photo)

    def add(self, photo: Photo) -> None:
        when = datetime.fromisoformat(photo[0])
        items, dates = self.groups.setdefault(bucket(self.mode, photo), ([], []))
        at = bisect_right(dates, when)
        dates.insert(at, when)
        items.insert(at, (when, photo))
        self.note(photo)

    def decide(self, photo: Photo, own_ids: Optional[Dict[str, set]] = None) -> List[Tuple[Photo, str]]:
        """The indexed photos that are the same photo as this one, with how it was decided (the kind of id they share, or
        `attributes`): those that share an id first, then, nearest in date first, those the name, extension, date and size
        agree with, unless an id of the same kind says they are different. own_ids are this photo's ids."""
        when = datetime.fromisoformat(photo[0])
        by_id: List[Tuple[Photo, str]] = []
        if own_ids:
            seen = set()
            own_ids = {kind: values for kind, values in own_ids.items() if kind in self.kept}
            for kind, values in own_ids.items():
                for value in values:
                    for other in self.by_id.get((kind, value), []):
                        if other not in seen:
                            seen.add(other)
                            by_id.append((other, kind))
        items, dates = self.groups.get(bucket(self.mode, photo), ([], []))
        if self.mode.date == "within":
            low, high = bisect_left(dates, when - self.mode.date_within), bisect_right(dates, when + self.mode.date_within)
            items = items[low:high]
        taken = {other for other, _ in by_id}
        found = [
            (abs(when - at), other) for at, other in items
            if other not in taken and same_size(self.mode, photo[1], other[1]) and not conflicting(own_ids, self.ids.get(other))
        ]
        by_id.sort(key=lambda item: abs(when - datetime.fromisoformat(item[0][0])))
        return by_id + [(other, "attributes") for _, other in sorted(found, key=lambda item: item[0])]

    def matches(self, photo: Photo, own_ids: Optional[Dict[str, set]] = None) -> List[Photo]:
        """The indexed photos that are the same photo as this one, those sharing an id and then the nearest in date first."""
        return [other for other, _ in self.decide(photo, own_ids)]


def absent(wanted: Counter, held: Counter, mode: Mode = TOLERANT, wanted_ids: Optional[Ids] = None, held_ids: Optional[Ids] = None) -> Counter:
    """The photos of wanted that held has none of, one for each. A photo is there when held has a photo that is the same
    photo under the mode: with the default, one with the same iCloud id, or else one with its file name (without regard to
    case, and .jpeg is .jpg), a date within two hours and a size within 1%, so a copy that was imported again, or whose
    metadata was rewritten, or that is filed under a shifted time, is the same picture. A photo is missing when nothing like it
    is there, not when held has fewer copies than wanted, and copies are not carried over. wanted_ids and held_ids are the iCloud
    ids of the photos of each side."""
    index = Index(mode, held, held_ids)
    return Counter({photo: 1 for photo in wanted if not held[photo] and not index.matches(photo, (wanted_ids or {}).get(photo))})


def pairs(first: Counter, second: Counter, mode: Mode = TOLERANT, first_ids: Optional[Ids] = None, second_ids: Optional[Ids] = None) -> Tuple[List[Tuple[Photo, Photo, str]], Counter, Counter]:
    """([(a photo of first, the photo of second it is the same photo as, how that was decided)], photos only in first, photos
    only in second) when each photo of one is matched with at most one of the other under the mode, a photo with a count being
    that many photos: the extra copies are the ones left over. A photo is matched with one that shares its iCloud id, or else
    the nearest in date of those that are the same photo."""
    if mode == EXACT:
        both = first & second
        return [(photo, photo, "attributes") for photo in sorted(both) for _ in range(both[photo])], first - second, second - first
    unused = Counter(second)
    index = Index(mode, list(second), second_ids)
    only_first, matched = Counter(), []
    for photo in sorted(first):
        for _ in range(first[photo]):
            partner = next(((other, how) for other, how in index.decide(photo, (first_ids or {}).get(photo)) if unused[other]), None)
            if partner is None:
                only_first[photo] += 1
            else:
                unused[partner[0]] -= 1
                matched.append((photo, partner[0], partner[1]))
    return matched, only_first, +unused


def pair(first: Counter, second: Counter, mode: Mode = TOLERANT, first_ids: Optional[Ids] = None, second_ids: Optional[Ids] = None) -> Tuple[Counter, Counter, int]:
    """(photos only in first, photos only in second, how many pairs): see pairs."""
    matched, only_first, only_second = pairs(first, second, mode, first_ids, second_ids)
    return only_first, only_second, len(matched)


def gaps(first: Photo, second: Photo) -> Tuple[str, ...]:
    """How two photos differ, however little and whatever the mode: the name and the extension as written, the date, the size.
    Nothing when they are the very same photo."""
    stem, extension = split_name(first[2])
    other_stem, other_extension = split_name(second[2])
    found = []
    if stem != other_stem:
        found.append("name")
    if extension != other_extension:
        found.append("extension")
    gap = abs((datetime.fromisoformat(first[0]) - datetime.fromisoformat(second[0])).total_seconds())
    if gap:
        found.append(f"date by {span(gap)}")
    if first[1] != second[1]:
        both = first[1].isdigit() and second[1].isdigit() and int(first[1]) and int(second[1])
        found.append(f"size by {abs(int(first[1]) - int(second[1])):,} bytes" if both else "size")
    return tuple(found)


def dedupe(photos, mode: Mode = TOLERANT, ids: Optional[Ids] = None) -> List[Photo]:
    """The photos with each photo that is the same photo as an earlier one under the mode left out: of photos that match, the
    first in the order given is kept."""
    index, kept = Index(mode, [], ids), []
    for photo in photos:
        if not index.matches(photo, (ids or {}).get(photo)):
            index.add(photo)
            kept.append(photo)
    return kept


@dataclass(frozen=True)
class Closest:
    """The photo of a library that is most like another, and in what the two differ under the mode."""
    photo: Photo
    differs: Tuple[str, ...]
    gap: float = 0  # seconds between the two photos' dates
    bytes_apart: int = 0  # bytes between their sizes


def span(seconds: float) -> str:
    """A length of time in words, its two largest units: 3d 4h, 2h 5m, 40s."""
    days, rest = divmod(int(round(seconds)), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    units = [f"{number}{unit}" for number, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")) if number]
    return " ".join(units[:2]) or "0s"


def differences(mode: Mode, photo: Photo, other: Photo) -> Tuple[Tuple[str, ...], float, int]:
    """(what differs between two photos under the mode, in words; their gap in seconds; their gap in bytes)."""
    stem, extension = name_key(mode, photo[2])
    other_stem, other_extension = name_key(mode, other[2])
    gap = abs((datetime.fromisoformat(photo[0]) - datetime.fromisoformat(other[0])).total_seconds())
    both = photo[1].isdigit() and other[1].isdigit() and int(photo[1]) and int(other[1])
    bytes_apart = abs(int(photo[1]) - int(other[1])) if both else 0
    found = []
    if stem != other_stem:
        found.append("name")
    if extension != other_extension:
        found.append("extension")
    if mode.date != "ignore" and (gap > 0 if mode.date == "exact" else gap > mode.date_within.total_seconds()):
        found.append(f"date by {span(gap)}")
    if not same_size(mode, photo[1], other[1]):
        percent = bytes_apart / max(int(photo[1]), int(other[1])) * 100 if both else 0
        found.append(f"size by {bytes_apart:,} bytes ({percent:.1f}%)" if both else "size")
    return tuple(found), gap, bytes_apart


def stem_key(mode: Mode, name: str) -> str:
    """What makes two names the same name for the finder: the name without its extension as the mode reads it, or, when the mode
    ignores names, as written without regard to case."""
    return os.path.splitext(name)[0].lower() if mode.filename == "ignore" else name_key(mode, name)[0]


class Finder:
    """Finds, for a photo that nothing in a library matches, the photo that is closest to it: of the photos with the same name
    under the mode, if there are any, else of all, the one that differs in the fewest of name, extension, date and size, and
    then in date and size the least."""

    def __init__(self, mode: Mode, held) -> None:
        self.mode = mode
        self.by_stem: Dict[str, List[Photo]] = {}
        self.by_size: Dict[str, List[Photo]] = {}
        dated = []
        for photo in held:
            self.by_stem.setdefault(stem_key(mode, photo[2]), []).append(photo)
            self.by_size.setdefault(photo[1], []).append(photo)
            dated.append((datetime.fromisoformat(photo[0]), photo))
        dated.sort(key=lambda item: item[0])
        self.dates = [at for at, _ in dated]
        self.dated = [photo for _, photo in dated]

    def same_name(self, photo: Photo, limit: int = 3) -> List[Closest]:
        """The photos with the same name as this one under the mode (the extension is not
        compared) however their dates and sizes differ, the nearest in date first."""
        found = []
        for other in self.by_stem.get(stem_key(self.mode, photo[2]), []):
            differs, gap, bytes_apart = differences(self.mode, photo, other)
            found.append((gap, bytes_apart, other, Closest(other, differs, gap, bytes_apart)))
        return [item[3] for item in sorted(found, key=lambda item: item[:3])[:limit]]

    def closest(self, photo: Photo) -> Optional[Closest]:
        when, day = datetime.fromisoformat(photo[0]), timedelta(days=1)
        candidates = set(self.by_stem.get(stem_key(self.mode, photo[2]), []))
        if not candidates:  # nothing of that name: whatever is alike in size or date
            candidates.update(self.by_size.get(photo[1], []) if photo[1].isdigit() and int(photo[1]) else [])
            candidates.update(self.dated[bisect_left(self.dates, when - day):bisect_right(self.dates, when + day)])
        best = None
        for other in candidates:
            found, gap, bytes_apart = differences(self.mode, photo, other)
            score = (len(found), gap, bytes_apart, other)
            if best is None or score < best[0]:
                best = (score, Closest(other, found, gap, bytes_apart))
        return best[1] if best else None
