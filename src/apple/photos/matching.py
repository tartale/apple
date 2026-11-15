"""When two photos are the same photo.

A photo is its (UTC date, size, file name), as a library's database gives them, and a few more things that are known for some photos:
the ids iCloud gave it, its pixel size, and the bytes of its file. A Mode says which of these to look at and how closely they must
agree. It is written as text by --diff-mode (or APPLE_PHOTOS_DIFF_MODE) on every command that compares photos.

How a decision is made (the order of the parts in the text has no part in it):

  1. An id decides. If both photos have an iCloud id of the same kind (`identity=auto`), they are the same photo when the ids are equal,
     whatever their name, date and size, and different photos when the ids differ. The same SHA-256 of the files (`content=hash`) also
     makes them the same photo, but a different hash never makes them different, so a copy with rewritten metadata still matches.
  2. Without an id to go by, every part must agree: the name, the extension, the date, the size, and, with `dimensions=exact`, the pixel
     size. A part set to `ignore` is left out, and a size, or a pixel size, that is not known passes.
  3. When several photos match, a photo is paired with the one that shares its id, else the nearest in date, and each is paired once.

Three whole modes are the starting points; a part after one changes just that part, and the default is `tolerant`:

    strict     slow, most accurate      ids, the bytes of the files (`content=hash`), pixel size, the EXIF capture time, and
                                        everything else exactly: filename=exact,extension=exact,date=exif,date=exact,filesize=exact
    tolerant   medium speed and accuracy  ids, then the name (case ignored), the extension (`.jpeg` is `.jpg`), a date within 2 hours and a
                                        size within 1%; no file is opened (modification time as the date)
    loose      fast, least accurate     the name (case ignored) and a date within 1 day: any extension, any size, no ids

The parts:

    filename=full | exact | ignore         the name without its extension: case ignored (full), as written, or any name
    extension=full | exact | ignore        the extension: case ignored and `.jpeg` is `.jpg` (full), as written, or any extension
    date=within=2h | exact | ignore        how two dates compare (a number and s, m, h or d)
    date=exif | modified                   where a file in a folder gets its date: the capture time in its own metadata (EXIF), as Photos
                                           dates it on import, with the modification time only where it has none; or the modification time
    filesize=within=1% | within=512bytes | exact | ignore
    identity=auto | ignore                 whether iCloud ids decide: a library's `stable` id (the fingerprint of the original) and `cloud` id,
                                           and the 28 characters a backup file's name ends in when its name was taken (`IMAG0044_<fingerprint>.JPG`)
    content=ignore | hash                  whether files with the same SHA-256 are the same photo; only files whose size also occurs on the other
                                           side are read, a hash is cached, and reading is slow over a network share
    dimensions=ignore | exact              whether the pixel sizes must be the same (a rotated copy has the same size); read from the database
                                           for a library and from the file's header for a folder

Examples: `strict,filename=ignore` (the surest, whatever the names), `loose,filesize=within=5%`, `date=within=1d,filesize=ignore`.
"""
import base64
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
# The three whole modes, from the surest and slowest to the quickest and loosest. Each is a starting point that a part after it can change.
STRICT = Mode(
    filename="exact", extension="exact", identity="auto", content="hash", dimensions="exact", date_source="exif", date="exact", filesize="exact",
)
LOOSE = Mode(filename="full", extension="ignore", identity="ignore", content="ignore", dimensions="ignore", date="within", date_within=timedelta(days=1), filesize="ignore")
PRESETS = {"strict": STRICT, "tolerant": TOLERANT, "loose": LOOSE}
HELP = (
    "How closely two photos must agree to be the same photo: a whole mode, strict (slow, most accurate: ids, file bytes, pixel size, EXIF date, "
    "everything exact), tolerant (the default; medium: ids, then name, extension, date within 2h, size within 1%) or loose (fast, least "
    "accurate: name and a date within 1 day), optionally followed by parts that change it, comma-separated and read left to right: "
    "filename=full|exact|ignore, extension=full|exact|ignore, date=within=DURATION|exact|ignore (such as 2h, 30m, 1d), date=exif|modified "
    "(where a folder file's date comes from), filesize=within=PERCENT|within=BYTES|exact|ignore (such as 1% or 512bytes), "
    "identity=auto|ignore (iCloud ids), content=ignore|hash (the SHA-256 of the files; slow), dimensions=ignore|exact (pixel size). "
    "An id decides when both photos have one; otherwise every part must agree. Example: `strict,filename=ignore`. Also read from APPLE_PHOTOS_DIFF_MODE."
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
            raise ValueError(f"{item!r}: use filename=..., extension=..., dimensions=..., identity=..., content=..., date=..., filesize=..., strict, tolerant or loose")
    return mode


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

    def there(photo: Photo) -> bool:
        own = (wanted_ids or {}).get(photo)
        if held[photo]:  # a photo of the very same date, size and name, unless an id says it is another photo
            kept = {kind: values for kind, values in (own or {}).items() if kind in index.kept}
            if not conflicting(kept, index.ids.get(photo)):
                return True
        return bool(index.matches(photo, own))

    return Counter({photo: 1 for photo in wanted if not there(photo)})


def pairs(first: Counter, second: Counter, mode: Mode = TOLERANT, first_ids: Optional[Ids] = None, second_ids: Optional[Ids] = None) -> Tuple[List[Tuple[Photo, Photo, str]], Counter, Counter]:
    """([(a photo of first, the photo of second it is the same photo as, how that was decided)], photos only in first, photos
    only in second) when each photo of one is matched with at most one of the other under the mode, a photo with a count being
    that many photos: the extra copies are the ones left over. A photo is matched with one that shares its iCloud id, or else
    the nearest in date of those that are the same photo."""
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


KIND_NAMES = {"stable": "iCloud fingerprint", "cloud": "iCloud asset id", DIMENSIONS: "pixel size"}  # what a kind of id is called when it differs


def differences(mode: Mode, photo: Photo, other: Photo, photo_ids: Optional[Dict[str, set]] = None, other_ids: Optional[Dict[str, set]] = None) -> Tuple[Tuple[str, ...], float, int]:
    """(what differs between two photos under the mode, in words; their gap in seconds; their gap in bytes). photo_ids and other_ids
    are the ids and pixel sizes of the two: an id of the same kind that the mode looks at and that they do not share is a difference."""
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
    for kind in ("stable", "cloud", DIMENSIONS):
        looked_at = (kind == DIMENSIONS and mode.dimensions == "exact") or (kind != DIMENSIONS and mode.identity == "auto")
        values, others = (photo_ids or {}).get(kind), (other_ids or {}).get(kind)
        if looked_at and values and others and not (values & others):
            found.append(KIND_NAMES[kind])
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

    def __init__(self, mode: Mode, held, ids: Optional[Ids] = None) -> None:
        self.mode = mode
        self.ids: Ids = ids or {}
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

    def same_name(self, photo: Photo, limit: int = 3, own_ids: Optional[Dict[str, set]] = None) -> List[Closest]:
        """The photos with the same name as this one under the mode (the extension is not
        compared) however their dates and sizes differ, the nearest in date first."""
        found = []
        for other in self.by_stem.get(stem_key(self.mode, photo[2]), []):
            differs, gap, bytes_apart = differences(self.mode, photo, other, own_ids, self.ids.get(other))
            found.append((gap, bytes_apart, other, Closest(other, differs, gap, bytes_apart)))
        return [item[3] for item in sorted(found, key=lambda item: item[:3])[:limit]]

    def closest(self, photo: Photo, own_ids: Optional[Dict[str, set]] = None) -> Optional[Closest]:
        when, day = datetime.fromisoformat(photo[0]), timedelta(days=1)
        candidates = set(self.by_stem.get(stem_key(self.mode, photo[2]), []))
        if not candidates:  # nothing of that name: whatever is alike in size or date
            candidates.update(self.by_size.get(photo[1], []) if photo[1].isdigit() and int(photo[1]) else [])
            candidates.update(self.dated[bisect_left(self.dates, when - day):bisect_right(self.dates, when + day)])
        best = None
        for other in candidates:
            found, gap, bytes_apart = differences(self.mode, photo, other, own_ids, self.ids.get(other))
            score = (len(found), gap, bytes_apart, other)
            if best is None or score < best[0]:
                best = (score, Closest(other, found, gap, bytes_apart))
        return best[1] if best else None
