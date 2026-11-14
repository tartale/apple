"""Files with the capture time in their EXIF block, as a camera writes it."""
import os
import struct
from pathlib import Path


def tiff_with(taken: str, offset: str = "", tag: int = 0x9003) -> bytes:
    """A little-endian TIFF block whose Exif IFD has the capture time (and its UTC offset when given)."""
    text = taken.encode() + b"\x00"
    offset_text = offset.encode() + b"\x00" if offset else b""
    entries = 1 + (1 if offset else 0)
    ifd0_at, exif_at = 8, 8 + 2 + 12 + 4
    data_at = exif_at + 2 + 12 * entries + 4
    blob = struct.pack("<2sHI", b"II", 42, ifd0_at)
    blob += struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_at) + struct.pack("<I", 0)
    blob += struct.pack("<H", entries) + struct.pack("<HHII", tag, 2, len(text), data_at)
    if offset:
        blob += struct.pack("<HHII", {0x9003: 0x9011, 0x9004: 0x9012}[tag], 2, len(offset_text), data_at + len(text))
    blob += struct.pack("<I", 0) + text + offset_text
    return blob


def jpeg_taken(path: Path, taken: str, mtime: float, offset: str = "", size: int = 100) -> Path:
    """A JPEG whose EXIF says it was taken at `taken` (YYYY:MM:DD HH:MM:SS), padded to the size, with this modification time."""
    body = b"Exif\x00\x00" + tiff_with(taken, offset)
    data = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + b"\xff\xd9"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + b"\x00" * max(size - len(data), 0))
    os.utime(path, (mtime, mtime))
    return path
