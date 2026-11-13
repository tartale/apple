"""The time a photo says it was taken, read from the file's own metadata (EXIF), which is the time Photos files it under.

A file's modification time is only what a copy or a backup kept, and for a photo whose camera clock was wrong, or whose date was
corrected later, it is not the time Photos gives the photo on import. EXIF holds the capture time as the camera's wall clock, with
no time zone (newer files also have an offset); Photos reads it in the time zone the Mac is in, so that is how it is read here.

Read: JPEG, HEIC and other files that carry an Exif block, and TIFF-based files (DNG and most raw formats) whose own header is
the TIFF header. A file with no readable capture time (a video, a PNG, a screenshot) has none, and its modification time stands in.
"""
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

READ_BYTES = 1 << 20  # the Exif block is at the start of the file, or just after its header in HEIC
EXIF_IFD, DATE_TIME, DATE_TIME_ORIGINAL, DATE_TIME_DIGITIZED = 0x8769, 0x0132, 0x9003, 0x9004
OFFSET_ORIGINAL, OFFSET_DIGITIZED = 0x9011, 0x9012
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def tiff_start(data: bytes) -> Optional[int]:
    """Where the TIFF header of the Exif block is: at the start for a TIFF-based file, after `Exif\\0\\0` in a JPEG or HEIC."""
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return 0
    at = data.find(b"Exif\x00\x00")
    while at != -1:
        if data[at + 6:at + 10] in (b"II*\x00", b"MM\x00*"):
            return at + 6
        at = data.find(b"Exif\x00\x00", at + 1)
    return None


def read_ifd(data: bytes, start: int, offset: int, order: str) -> Dict[int, Tuple[int, int, int]]:
    """tag -> (type, count, where its value is in data) for the entries of the IFD at offset (from start)."""
    entries: Dict[int, Tuple[int, int, int]] = {}
    at = start + offset
    try:
        (number,) = struct.unpack_from(order + "H", data, at)
        for index in range(min(number, 512)):
            tag, kind, count = struct.unpack_from(order + "HHI", data, at + 2 + 12 * index)
            value_at = at + 2 + 12 * index + 8
            if SIZES.get(kind, 1) * count > 4:
                (value_at,) = struct.unpack_from(order + "I", data, value_at)
                value_at += start
            entries[tag] = (kind, count, value_at)
    except struct.error:
        pass
    return entries


def text_of(data: bytes, entry: Optional[Tuple[int, int, int]]) -> str:
    if entry is None or entry[0] != 2:
        return ""
    return data[entry[2]:entry[2] + entry[1]].split(b"\x00")[0].decode("ascii", "replace").strip()


def capture_time(path: Path) -> Optional[Tuple[str, str]]:
    """(the capture time as the file writes it, `YYYY:MM:DD HH:MM:SS`, and its UTC offset as `+HH:MM` or "" when it has none),
    or None when the file has no capture time we can read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(READ_BYTES)
    except OSError:
        return None
    start = tiff_start(data)
    if start is None:
        return None
    order = "<" if data[start:start + 2] == b"II" else ">"
    try:
        (first,) = struct.unpack_from(order + "I", data, start + 4)
    except struct.error:
        return None
    main = read_ifd(data, start, first, order)
    inner = {}
    if EXIF_IFD in main:
        try:
            (pointer,) = struct.unpack_from(order + "I", data, main[EXIF_IFD][2])
            inner = read_ifd(data, start, pointer, order)
        except struct.error:
            pass
    for tag, offset_tag, where in ((DATE_TIME_ORIGINAL, OFFSET_ORIGINAL, inner), (DATE_TIME_DIGITIZED, OFFSET_DIGITIZED, inner), (DATE_TIME, None, main)):
        taken = text_of(data, where.get(tag))
        if taken and not taken.startswith("0000"):
            return taken, text_of(data, where.get(offset_tag)) if offset_tag else ""
    return None


def utc_of(taken: str, offset: str = "") -> Optional[str]:
    """The capture time as the databases keep it, `YYYY-MM-DD HH:MM:SS` in UTC: the offset says what zone the camera was in,
    and without one the time is read in this Mac's time zone (with its daylight saving on that date), as Photos reads it."""
    try:
        naive = datetime.strptime(taken, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    try:
        if offset:
            sign = -1 if offset.startswith("-") else 1
            hours, _, minutes = offset.lstrip("+-").partition(":")
            aware = naive.replace(tzinfo=timezone(sign * timedelta(hours=int(hours), minutes=int(minutes or 0))))
        else:
            aware = naive.astimezone()
    except (ValueError, OverflowError, OSError):
        return None
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
