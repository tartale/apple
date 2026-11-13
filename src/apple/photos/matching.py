"""When two photos are the same photo: how much their file name, date and size may differ.

A photo is its (UTC date, size, file name), as a library's database gives them. How closely two of those must agree to count as
one photo is a Mode, written as text by --diff-mode (or APPLE_PHOTOS_DIFF_MODE) on every command that compares photos:

    filename=full            the name without its extension, without regard to case (the default)
    filename=exact           the name without its extension, character for character
    filename=prefix=N        the first N characters of the name without its extension, as `full` reads it
    filename=regex=PATTERN   what the regular expression matches in the name without its extension (its first group, if it has
                             one), case ignored: regex=^(\\d{8}_\\d{6}) is the date and time a camera put in front of a name; a name
                             it does not match is read as `full` reads it
    filename=ignore          any name
    extension=full           the extension without regard to case, and .jpeg is .jpg (the default)
    extension=exact          the extension character for character
    extension=ignore         any extension: IMG_1234.HEIC and IMG_1234.JPG are one photo
    date=within=2h           within a time (a number and s, m, h or d; 2h is the default)
    date=exact | date=ignore
    filesize=within=1%       within a share of the size (1% is the default), or a number of bytes: filesize=within=512bytes
    filesize=exact | filesize=ignore

A mode is a comma-separated list read from left to right, starting from the default: `exact` (everything exact, a name as written)
and `tolerant` (the default) name whole modes, and a field after them changes just that field, as in
`exact,filename=full` or `filename=prefix=8,date=within=1d`. A size that is not known (0, or not a number, as for a placeholder)
does not tell photos apart.
"""
import os
import re
from functools import lru_cache
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
    filename: str = "full"  # exact, full, prefix, regex or ignore (of the name without its extension)
    extension: str = "full"  # exact, full or ignore
    prefix: int = 0  # for prefix: how many characters of the name must agree
    pattern: str = ""  # for regex: the regular expression
    date: str = "within"  # exact, within or ignore
    date_within: timedelta = timedelta(hours=2)  # a copy of a photo can differ by a time zone or daylight saving shift
    filesize: str = "within"  # exact, within or ignore
    size_within: float = 0.01  # and by a little in size, when the file's metadata was rewritten (a share of the size)
    size_bytes: Optional[int] = None  # or, when set, that many bytes whatever the size


TOLERANT = Mode()
EXACT = Mode(filename="exact", extension="exact", date="exact", filesize="exact")
PRESETS = {"tolerant": TOLERANT, "exact": EXACT}
HELP = (
    "How closely two photos must agree to be the same photo, as a comma-separated list of filename=full|exact|ignore|prefix=N|regex=PATTERN "
    "(of the name without its extension; full ignores case; regex compares what the pattern, or its first group, matches), "
    "extension=full|exact|ignore (full ignores case, and .jpeg is .jpg), date=exact|ignore|within=DURATION (such as 2h, 30m, 1d) and "
    "filesize=exact|ignore|within=PERCENT (such as 1%) or within=BYTES (such as 512bytes), or the whole modes exact and tolerant, read from left to right: "
    "`exact,filename=prefix=8`. The default is tolerant: filename=full,extension=full,date=within=2h,filesize=within=1%. Also read from "
    "APPLE_PHOTOS_DIFF_MODE."
)


PARTS = re.compile(r",(?=\s*(?:filename=|extension=|date=|filesize=|exact\b|tolerant\b))")  # a comma ends a part only before the next part: a regex may hold commas


def parse(text: str) -> Mode:
    """The Mode a --diff-mode text says; ValueError, with what is wrong with it, when it is not one. Nothing is the default."""
    mode = TOLERANT
    for item in (part.strip() for part in PARTS.split(text)):
        if not item:
            continue
        if item in PRESETS:
            mode = PRESETS[item]
            continue
        field, _, value = item.partition("=")
        if field == "filename":
            kind, _, count = value.partition("=")
            if kind in ("exact", "full", "ignore") and not count:
                mode = replace(mode, filename=kind)
            elif kind == "prefix" and count.isdigit() and int(count) > 0:
                mode = replace(mode, filename="prefix", prefix=int(count))
            elif kind == "regex" and count:
                try:
                    re.compile(count)
                except re.error as error:
                    raise ValueError(f"filename=regex={count}: not a regular expression ({error})")
                mode = replace(mode, filename="regex", pattern=count)
            else:
                raise ValueError(f"filename={value!r}: use full, exact, ignore, prefix=N (N characters, at least 1) or regex=PATTERN")
        elif field == "extension":
            if value in ("exact", "full", "ignore"):
                mode = replace(mode, extension=value)
            else:
                raise ValueError(f"extension={value!r}: use full, exact or ignore")
        elif field == "date":
            kind, _, amount = value.partition("=")
            if kind in ("exact", "ignore") and not amount:
                mode = replace(mode, date=kind)
            elif kind == "within" and (found := re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", amount)):
                mode = replace(mode, date="within", date_within=timedelta(seconds=float(found.group(1)) * UNITS[found.group(2)]))
            else:
                raise ValueError(f"date={value!r}: use exact, ignore or within=DURATION (a number and s, m, h or d, such as 2h)")
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
            raise ValueError(f"{item!r}: use filename=..., extension=..., date=..., filesize=..., exact or tolerant")
    return mode


@lru_cache(maxsize=None)
def compiled(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


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
    elif mode.filename == "regex":
        found = compiled(mode.pattern).search(stem)
        stem_key = lowered if not found else (found.group(1) if found.re.groups and found.group(1) is not None else found.group(0)).lower()
    else:
        stem_key = lowered[:mode.prefix] if mode.filename == "prefix" else lowered
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


class Index:
    """Photos grouped for finding the ones that match a photo: by bucket, then by date, so a date window is a slice."""

    def __init__(self, mode: Mode, photos) -> None:
        self.mode = mode
        grouped: Dict[tuple, List[tuple]] = {}
        for photo in photos:
            grouped.setdefault(bucket(mode, photo), []).append((datetime.fromisoformat(photo[0]), photo))
        self.groups: Dict[tuple, Tuple[List[tuple], List[datetime]]] = {}
        for where, items in grouped.items():
            items.sort(key=lambda item: item[0])
            self.groups[where] = (items, [at for at, _ in items])

    def add(self, photo: Photo) -> None:
        when = datetime.fromisoformat(photo[0])
        items, dates = self.groups.setdefault(bucket(self.mode, photo), ([], []))
        at = bisect_right(dates, when)
        dates.insert(at, when)
        items.insert(at, (when, photo))

    def matches(self, photo: Photo) -> List[Photo]:
        """The indexed photos that are the same photo as this one, nearest in date first."""
        items, dates = self.groups.get(bucket(self.mode, photo), ([], []))
        when = datetime.fromisoformat(photo[0])
        if self.mode.date == "within":
            low, high = bisect_left(dates, when - self.mode.date_within), bisect_right(dates, when + self.mode.date_within)
            items = items[low:high]
        found = [(abs(when - at), other) for at, other in items if same_size(self.mode, photo[1], other[1])]
        return [other for _, other in sorted(found, key=lambda item: item[0])]


def absent(wanted: Counter, held: Counter, mode: Mode = TOLERANT) -> Counter:
    """The photos of wanted that held has none of, one for each. A photo is there when held has a photo that is the same
    photo under the mode: with the default, one with its file name (without regard to case, and .jpeg is .jpg), a date within
    two hours and a size within 1%, so a copy that was imported again, or whose metadata was rewritten, or that is filed under
    a shifted time, is the same picture. A photo is missing when nothing like it is there, not when held has fewer copies than
    wanted, and copies are not carried over."""
    index = Index(mode, held)
    return Counter({photo: 1 for photo in wanted if not held[photo] and not index.matches(photo)})


def pair(first: Counter, second: Counter, mode: Mode = TOLERANT) -> Tuple[Counter, Counter, int]:
    """(photos only in first, photos only in second, how many pairs) when each photo of one is matched with at most one of
    the other under the mode, a photo with a count being that many photos: the extra copies are the ones left over."""
    if mode == EXACT:
        return first - second, second - first, sum((first & second).values())
    unused = Counter(second)
    index = Index(mode, list(second))
    only_first, pairs = Counter(), 0
    for photo in sorted(first):
        for _ in range(first[photo]):
            partner = next((other for other in index.matches(photo) if unused[other]), None)
            if partner is None:
                only_first[photo] += 1
            else:
                unused[partner] -= 1
                pairs += 1
    return only_first, +unused, pairs


def dedupe(photos, mode: Mode = TOLERANT) -> List[Photo]:
    """The photos with each photo that is the same photo as an earlier one under the mode left out: of photos that match, the
    first in the order given is kept."""
    index, kept = Index(mode, []), []
    for photo in photos:
        if not index.matches(photo):
            index.add(photo)
            kept.append(photo)
    return kept


@dataclass(frozen=True)
class Closest:
    """The photo of a library that is most like another, and in what the two differ under the mode."""
    photo: Photo
    differs: Tuple[str, ...]


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


class Finder:
    """Finds, for a photo that nothing in a library matches, the photo that is closest to it: the one that differs in the fewest
    of name, extension, date and size, and then in date and size the least."""

    def __init__(self, mode: Mode, held) -> None:
        self.mode = mode
        self.by_stem: Dict[str, List[Photo]] = {}
        self.by_size: Dict[str, List[Photo]] = {}
        dated = []
        for photo in held:
            self.by_stem.setdefault(os.path.splitext(photo[2])[0].lower(), []).append(photo)
            self.by_size.setdefault(photo[1], []).append(photo)
            dated.append((datetime.fromisoformat(photo[0]), photo))
        dated.sort(key=lambda item: item[0])
        self.dates = [at for at, _ in dated]
        self.dated = [photo for _, photo in dated]

    def closest(self, photo: Photo) -> Optional[Closest]:
        when, day = datetime.fromisoformat(photo[0]), timedelta(days=1)
        candidates = set(self.by_stem.get(os.path.splitext(photo[2])[0].lower(), []))
        candidates.update(self.by_size.get(photo[1], []) if photo[1].isdigit() and int(photo[1]) else [])
        candidates.update(self.dated[bisect_left(self.dates, when - day):bisect_right(self.dates, when + day)])
        best = None
        for other in candidates:
            found, gap, bytes_apart = differences(self.mode, photo, other)
            score = (len(found), gap, bytes_apart, other)
            if best is None or score < best[0]:
                best = (score, Closest(other, found))
        return best[1] if best else None
