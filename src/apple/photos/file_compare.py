"""What two files of one photo differ in, part by part, and which of the two is the better one to keep.

A photo file is its picture and what is written around it: the capture time, the place, the camera, a caption, keywords, a color
profile, a thumbnail. Two files of one photo that differ by a few hundred bytes mostly hold the same picture with something
added, dropped or rewritten around it, and that is what this tells apart.

A JPEG is read segment by segment: the picture is everything the decoder needs (the tables, the frame and the scans), and each
application segment is a part of its own (Exif, XMP, the color profile, IPTC, a comment, the JFIF header, what follows the end of
the picture). Any other file is compared only by where its bytes differ, which says whether the difference is at the start (a
header), at the end, or in the middle.
"""
import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from apple.photos import exif, image_size

BLOCK = 1 << 20
MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")  # in the data of a scan: a marker that is not a stuffed byte, a restart or a fill
PICTURE = "picture data"
# what a file can say about its photo that is worth keeping, in the order shown
WORTH = ("capture time", "GPS", "camera", "description", "title", "keywords", "rating", "faces", "color profile", "maker note", "orientation")
OTHER_FACTS = ("software", "thumbnail")
XMP_FACTS = {"keywords": (b"dc:subject",), "title": (b"dc:title",), "description": (b"dc:description",), "rating": (b"xmp:Rating",), "faces": (b"mwg-rs:Regions", b"MPRI:Regions")}
IPTC_FACTS = {"keywords": b"\x1c\x02\x19", "description": b"\x1c\x02\x78", "title": b"\x1c\x02\x05"}


class Part(NamedTuple):
    """A part of a file: what it is called, whether it is the `picture`, `metadata` or `other`, its size and its SHA-256."""
    name: str
    kind: str
    size: int
    digest: str


class Described(NamedTuple):
    """A file as its parts (empty when its format is not read), what it says about its photo, and its pixel size."""
    path: Path
    format: str
    size: int
    parts: List[Part]
    facts: Dict[str, str]
    dimensions: Optional[Tuple[int, int]]


def label(marker: int, payload: bytes) -> Tuple[str, str]:
    """(the name, the kind) of a JPEG application or comment segment."""
    if marker == 0xFE:
        return "comment", "metadata"
    for number, start, name, kind in (
        (0xE0, b"JFIF\x00", "JFIF header", "other"), (0xE0, b"JFXX\x00", "JFIF thumbnail", "other"),
        (0xE1, b"Exif\x00\x00", "Exif", "metadata"), (0xE1, b"http://ns.adobe.com/xap/1.0/\x00", "XMP", "metadata"),
        (0xE1, b"http://ns.adobe.com/xmp/extension/\x00", "XMP", "metadata"),
        (0xE2, b"ICC_PROFILE\x00", "color profile", "metadata"), (0xE2, b"MPF\x00", "MPF (index of further pictures)", "other"),
        (0xED, b"Photoshop 3.0\x00", "IPTC", "metadata"), (0xEE, b"Adobe", PICTURE, "picture"),  # the Adobe segment says how the colors are stored
    ):
        if marker == number and payload.startswith(start):
            return name, kind
    return f"APP{marker - 0xE0}", "other"


def exif_facts(tiff: bytes) -> Dict[str, str]:
    """What an Exif block (from its TIFF header on) says about the photo."""
    facts: Dict[str, str] = {}
    if tiff[:4] not in (b"II*\x00", b"MM\x00*"):
        return facts
    order = "<" if tiff[:2] == b"II" else ">"

    def number(entry, code: str = "I") -> Optional[int]:
        try:
            return struct.unpack_from(order + code, tiff, entry[2])[0] if entry else None
        except struct.error:
            return None

    def below(entry) -> dict:
        pointer = number(entry)
        return exif.read_ifd(tiff, 0, pointer, order) if pointer else {}

    first = number((0, 0, 4)) or 0
    main = exif.read_ifd(tiff, 0, first, order)
    inner, gps = below(main.get(exif.EXIF_IFD)), below(main.get(0x8825))
    taken = exif.text_of(tiff, inner.get(exif.DATE_TIME_ORIGINAL)) or exif.text_of(tiff, inner.get(exif.DATE_TIME_DIGITIZED))
    if taken and not taken.startswith("0000"):
        facts["capture time"] = taken
    place = []
    for value_tag, side_tag in ((2, 1), (4, 3)):
        entry = gps.get(value_tag)
        try:
            parts = struct.unpack_from(order + "6I", tiff, entry[2]) if entry and entry[1] == 3 else None
        except struct.error:
            parts = None
        if parts and all(parts[1::2]):
            degrees = parts[0] / parts[1] + parts[2] / parts[3] / 60 + parts[4] / parts[5] / 3600
            place.append(f"{-degrees if exif.text_of(tiff, gps.get(side_tag)) in ('S', 'W') else degrees:.5f}")
    if len(place) == 2:
        facts["GPS"] = ", ".join(place)
    camera = " ".join(text for text in (exif.text_of(tiff, main.get(0x010F)), exif.text_of(tiff, main.get(0x0110))) if text)
    for name, text in (("camera", camera), ("description", exif.text_of(tiff, main.get(0x010E))), ("software", exif.text_of(tiff, main.get(0x0131)))):
        if text:
            facts[name] = text
    if inner.get(0x927C):
        facts["maker note"] = f"{inner[0x927C][1]:,} bytes"
    turned = number(main.get(0x0112), "H")
    if turned and turned != 1:  # 1 is upright, which is what a file with no orientation is
        facts["orientation"] = str(turned)
    count = number((0, 0, first), "H")
    following = number((0, 0, first + 2 + 12 * count)) if count is not None else None
    length = number(exif.read_ifd(tiff, 0, following, order).get(0x0202)) if following else None
    if length:
        facts["thumbnail"] = f"{length:,} bytes"
    return facts


def profile_name(profile: bytes) -> str:
    """The name an ICC color profile gives itself (its description, as `sRGB IEC61966-2.1`), or "" when it cannot be read."""
    try:
        (count,) = struct.unpack_from(">I", profile, 128)
        for index in range(min(count, 100)):
            tag, at, _ = struct.unpack_from(">4sII", profile, 132 + 12 * index)
            if tag != b"desc":
                continue
            if profile[at:at + 4] == b"desc":  # version 2: plain text
                (length,) = struct.unpack_from(">I", profile, at + 8)
                return profile[at + 12:at + 12 + length].split(b"\x00")[0].decode("ascii", "replace").strip()
            if profile[at:at + 4] == b"mluc" and struct.unpack_from(">I", profile, at + 8)[0]:  # version 4: the first language
                length, offset = struct.unpack_from(">II", profile, at + 20)
                return profile[at + offset:at + offset + length].decode("utf-16-be", "replace").strip("\x00 ")
    except struct.error:
        pass
    return ""


def is_default_profile(name: Optional[str]) -> bool:
    """True for no color profile and for an sRGB one: a picture without a profile is shown as sRGB, so the two say the same."""
    return name is None or "srgb" in name.lower()


def read_jpeg(data: bytes) -> Tuple[List[Part], Dict[str, str]]:
    """The parts of a JPEG in the order they first appear (segments of one name are one part) and what its metadata says."""
    pieces: Dict[str, Tuple[str, List[bytes]]] = {}
    facts: Dict[str, str] = {}
    profile: List[bytes] = []  # the chunks of the color profile, each starting with its number

    def add(name: str, kind: str, chunk: bytes) -> None:
        pieces.setdefault(name, (kind, []))[1].append(chunk)

    add(PICTURE, "picture", data[:2])
    at = 2
    while at + 2 <= len(data) and data[at] == 0xFF:
        marker = data[at + 1]
        if marker == 0xFF:  # a fill byte before a marker
            add(PICTURE, "picture", data[at:at + 1])
            at += 1
            continue
        if marker == 0xD9 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            add(PICTURE, "picture", data[at:at + 2])
            at += 2
            if marker == 0xD9:
                break
            continue
        if at + 4 > len(data):
            break
        end = at + 2 + struct.unpack_from(">H", data, at + 2)[0]
        payload = data[at + 4:end]
        if marker == 0xDA:  # a scan: its data runs to the next marker
            found = MARKER.search(data, end)
            end = found.start() if found else len(data)
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            name, kind = label(marker, payload)
            add(name, kind, data[at:end])
            if name == "Exif" and not any(key in facts for key in ("capture time", "camera")):
                facts.update(exif_facts(payload[6:]))
            elif name == "XMP":
                facts.update({fact: "yes" for fact, marks in XMP_FACTS.items() if fact not in facts and any(mark in payload for mark in marks)})
            elif name == "IPTC":
                facts.update({fact: "yes" for fact, mark in IPTC_FACTS.items() if fact not in facts and mark in payload})
            elif name == "color profile":
                profile.append(payload[12:])
        else:
            add(PICTURE, "picture", data[at:end])
        at = end
    if at < len(data):
        add("after the picture", "other", data[at:])
    if profile:
        facts["color profile"] = profile_name(b"".join(chunk[2:] for chunk in sorted(profile, key=lambda chunk: chunk[:1]))) or "yes"
    parts = []
    for name, (kind, chunks) in pieces.items():
        whole = b"".join(chunks)
        parts.append(Part(name, kind, len(whole), hashlib.sha256(whole).hexdigest()))
    return parts, facts


def describe(path: Path) -> Described:
    """A file as its parts. Raises OSError when it cannot be read."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        start = handle.read(2)
        if start == b"\xff\xd8":
            parts, facts = read_jpeg(start + handle.read())
            return Described(path, "JPEG", size, parts, facts, image_size.image_size(path))
    return Described(path, "", size, [], {}, image_size.image_size(path))


def shared_ends(first: Path, second: Path) -> Tuple[int, int]:
    """How many bytes two files have in common at the start, and how many of the rest at the end."""
    sizes = first.stat().st_size, second.stat().st_size
    limit = min(sizes)
    with open(first, "rb") as one, open(second, "rb") as other:
        start = 0
        while start < limit:
            a, b = one.read(BLOCK), other.read(BLOCK)
            if a != b or not a:
                start += len(os.path.commonprefix([a, b]))
                break
            start += len(a)
        start = min(start, limit)
        end = 0
        while end < limit - start:
            step = min(BLOCK, limit - start - end)
            chunks = []
            for handle, size in ((one, sizes[0]), (other, sizes[1])):
                handle.seek(size - end - step)
                chunks.append(handle.read(step)[::-1])
            end += len(os.path.commonprefix(chunks))
            if chunks[0] != chunks[1]:
                break
    return start, end


class Compared(NamedTuple):
    """Two files compared: each one described, the bytes they share at the start and at the end, the names of the parts that
    differ, whether the picture is the same (None when the format is not read), the facts only the first has, only the second has,
    and that both have with another value (an sRGB color profile and no profile count as the same), which one is the better to keep (`first`, `second`, `either` or None), and why."""
    first: Described
    second: Described
    ends: Tuple[int, int]
    differing: List[str]
    same_picture: Optional[bool]
    only_first: List[str]
    only_second: List[str]
    changed: List[str]
    better: Optional[str]
    why: str


def compare(one: Path, other: Path) -> Compared:
    """Compares two files of one photo. Raises OSError when either cannot be read."""
    first, second = describe(one), describe(other)
    if "orientation" in first.facts or "orientation" in second.facts:  # a file that does not say how it is turned is upright
        first, second = (described._replace(facts={"orientation": "upright", **described.facts}) for described in (first, second))
    ends = shared_ends(one, other)
    identical = first.size == second.size and ends[0] == first.size
    sides = [{part.name: part for part in described.parts} for described in (first, second)]
    differing = [name for name in dict.fromkeys(list(sides[0]) + list(sides[1])) if sides[0].get(name) != sides[1].get(name)]
    readable = bool(first.parts and second.parts)
    same_picture = True if identical else (PICTURE not in differing if readable else None)
    profiles = [described.facts.get("color profile") for described in (first, second)]
    default_profile = profiles[0] != profiles[1] and all(is_default_profile(name) for name in profiles)
    said = [{fact: value for fact, value in described.facts.items() if not (fact == "color profile" and default_profile)} for described in (first, second)]
    only_first = [fact for fact in WORTH if fact in said[0] and fact not in said[1]]
    only_second = [fact for fact in WORTH if fact in said[1] and fact not in said[0]]
    changed = [fact for fact in WORTH if fact in said[0] and fact in said[1] and said[0][fact] != said[1][fact]]
    if identical:
        better, why = "either", "the same file, byte for byte"
    elif same_picture is None:
        better, why = None, "not a format whose parts are read, so only where the bytes differ is known"
    elif not same_picture:
        if first.dimensions and second.dimensions and first.dimensions != second.dimensions:
            larger = first.dimensions[0] * first.dimensions[1] > second.dimensions[0] * second.dimensions[1]
            better, why = ("first" if larger else "second"), "the pictures are of different pixel sizes, and this is the one with more pixels"
        else:
            better, why = None, "the picture itself differs (saved again, edited or turned), which the files alone do not rank"
    elif changed:
        better, why = None, f"the same picture, but they disagree on {', '.join(changed)}: which is right is for you to say"
    elif only_first and only_second:
        better, why = None, "the same picture, and each says something the other does not"
    elif only_first or only_second:
        better, why = ("first" if only_first else "second"), f"the same picture, and only this one has {', '.join(only_first or only_second)}"
    else:
        better, why = "either", "the same picture, and they say the same about it: what differs holds nothing about the photo"
        if default_profile:
            why += ", and an sRGB color profile is what a picture without a profile is shown with"
    return Compared(first, second, ends, differing, same_picture, only_first, only_second, changed, better, why)
