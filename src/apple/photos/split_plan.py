"""Planning for `./apple photos split`: how a year divides into libraries under a size cap, and what each one lacks.

Everything here works on the photos read from the Photos databases (a Counter of (date, size, file name) with the date
in UTC as 'yyyy-mm-dd hh:mm:ss'); nothing talks to PowerPhotos.
"""
import re
from collections import Counter
from datetime import date, timedelta
from typing import List, NamedTuple, Optional, Sequence, Tuple

from apple.photos import powerphotos as pp

UNITS = {"": 1, "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}


class Part(NamedTuple):
    """One destination library: the photos from start up to (not including) end, and their size in bytes."""
    name: str
    start: date
    end: date
    size: int


def parse_size(text: str) -> int:
    """Bytes from a size such as 200GB, 1.5 TB or 500000000 (decimal units); ValueError when it is not a size."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?b)?\s*", text.lower())
    if not match or not float(match.group(1)):
        raise ValueError(f"{text!r} is not a size such as 200GB")
    return int(float(match.group(1)) * UNITS[match.group(2) or ""])


def format_size(size: int) -> str:
    for unit, factor in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"


def plan_year(source_photos: Optional[Counter], year: int, media: str, name: str, max_bytes: Optional[int]) -> List[Part]:
    """The libraries a year goes into: one called name, or when its photos are over max_bytes, name-1, name-2 ... each a
    run of whole days from January 1 on, filled up to the cap before the next one starts. A single day over the cap is
    a library of its own. Day boundaries are in UTC, so a photo near one may be filed by PowerPhotos in the next part."""
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    days: Counter = Counter()
    for (when, size, _), count in (pp.of_media(source_photos, media) if source_photos else Counter()).items():
        if start.isoformat() <= when[:10] < end.isoformat():
            days[when[:10]] += (int(size) if size.isdigit() else 0) * count
    total = sum(days.values())
    if max_bytes is None or total <= max_bytes:
        return [Part(name, start, end, total)]
    groups: List[List[str]] = [[]]
    used = 0
    for day in sorted(days):
        if groups[-1] and used + days[day] > max_bytes:
            groups.append([])
            used = 0
        groups[-1].append(day)
        used += days[day]
    firsts = [start] + [date.fromisoformat(group[0]) for group in groups[1:]] + [end]
    return [
        Part(f"{name}-{number}", firsts[number - 1], firsts[number], sum(days[day] for day in group))
        for number, group in enumerate(groups, 1)
    ]


def count_in_range(counted: Counter, start: date, end: date, media: str) -> int:
    """How many of the items (see plan_year) are dated from start up to end, in UTC, and of the media."""
    return sum(
        count for (when, _, _), count in pp.of_media(counted, media).items() if start.isoformat() <= when[:10] < end.isoformat()
    )


def absent(wanted: Counter, held: Counter) -> Counter:
    """The items of wanted that held has none of: a photo is missing when its date, size and file name are nowhere in the
    library, not when the library has fewer copies than the source (the same photo imported twice is still one picture)."""
    return Counter({key: number for key, number in wanted.items() if not held[key]})


def missing_in_range(
    source_photos: Counter, start: date, end: date, media: str, own: Optional[Counter], nearby: Sequence[Optional[Counter]]
) -> Optional[Tuple[Counter, Counter]]:
    """What a library lacks of the source's photos from start up to end, judged from the databases.

    Returns (missing, uncertain), or None when the library (own) is missing or cannot be read. The databases keep UTC
    dates and PowerPhotos files by local date, so a photo within a day of start or end may belong to the range next
    door: missing holds the photos inside the range proper that own lacks, and uncertain the ones near its ends that
    are in neither own nor any of the nearby libraries. A photo is lacking only if own has none with its date, size and
    file name (see absent): copies of one photo in the source do not each have to be in own.
    """
    if own is None:
        return None
    one_day = timedelta(days=1)
    core_from, core_to = (start + one_day).isoformat(), (end - one_day).isoformat()
    edge_from, edge_to = (start - one_day).isoformat(), (end + one_day).isoformat()
    core, edge = Counter(), Counter()
    for key, count in pp.of_media(source_photos, media).items():
        when = key[0]
        if core_from <= when < core_to:
            core[key] = count
        elif edge_from <= when < edge_to:
            edge[key] = count
    around = own
    for other in nearby:
        around = around + (other or Counter())
    return absent(core, own), absent(edge, around)


def complete(source_photos: Counter, start: date, end: date, media: str, own: Optional[Counter], nearby: Sequence[Optional[Counter]]) -> bool:
    """True when the library holds every photo of the range in the source (see missing_in_range)."""
    found = missing_in_range(source_photos, start, end, media, own, nearby)
    return found is not None and not found[0] and not found[1]


def outside_year(held: Counter, year: int, media: str) -> Tuple[Counter, int]:
    """(the items of the media dated outside the year, how many are within a day of its ends). Those within a day are not
    counted as outside: the databases keep UTC dates and PowerPhotos files by local date, so they may belong here."""
    start, end, one_day = date(year, 1, 1), date(year + 1, 1, 1), timedelta(days=1)
    outside, near_ends = Counter(), 0
    for key, number in pp.of_media(held, media).items():
        when = date.fromisoformat(key[0][:10])
        if start <= when < end:
            continue
        if start - one_day <= when < end + one_day:
            near_ends += number
        else:
            outside[key] = number
    return outside, near_ends
