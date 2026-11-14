"""The pixel size of an image file, read from its header: JPEG, PNG, GIF and HEIC/HEIF (and AVIF). None for anything else.

A size is given as (the larger side, the smaller side), so a photo and its rotated copy have the same one: Photos keeps the size of
the original, and a file's header gives the size before its orientation is applied.
"""
import struct
from pathlib import Path
from typing import Optional, Tuple

READ_BYTES = 1 << 20
SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # the JPEG markers that start a frame: they carry the size


def ordered(width: int, height: int) -> Optional[Tuple[int, int]]:
    return (max(width, height), min(width, height)) if width > 0 and height > 0 else None


def jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1
            continue
        if marker in SOF and at + 9 <= len(data):
            height, width = struct.unpack(">HH", data[at + 5:at + 9])
            return ordered(width, height)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        (length,) = struct.unpack(">H", data[at + 2:at + 4])
        at += 2 + length
    return None


def heif_size(data: bytes) -> Optional[Tuple[int, int]]:
    """The largest `ispe` (image spatial extents) box: the primary image, not a thumbnail."""
    best, at = None, data.find(b"ispe")
    while at != -1:
        if at + 16 <= len(data):
            width, height = struct.unpack(">II", data[at + 8:at + 16])
            size = ordered(width, height)
            if size and (best is None or size[0] * size[1] > best[0] * best[1]):
                best = size
        at = data.find(b"ispe", at + 4)
    return best


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    """(the larger side, the smaller side) in pixels, or None when the file is not an image this can read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(READ_BYTES)
    except OSError:
        return None
    try:
        if data[:2] == b"\xff\xd8":
            return jpeg_size(data)
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return ordered(*struct.unpack(">II", data[16:24]))
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return ordered(*struct.unpack("<HH", data[6:10]))
        if data[4:8] == b"ftyp":
            return heif_size(data)
    except struct.error:
        return None
    return None
