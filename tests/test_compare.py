"""`compare`: what two files of one photo differ in, part by part, and which is the better one to keep."""
import os
import struct
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import file_compare
from apple.photos import recover_cache as cache_module
from test_powerphotos import add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()
SCAN = (bytes(range(255)) * 8)
TAKEN, CAMERA = (0x9003, 2, "2006:07:04 12:00:00"), [(0x010F, 2, "Canon"), (0x0110, 2, "PowerShot S3")]
PLACE = [(1, 2, "N"), (2, 5, [(47, 1), (30, 1), (0, 1)]), (3, 2, "W"), (4, 5, [(122, 1), (15, 1), (0, 1)])]


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))


def tiff(main=(), inner=(), gps=(), thumbnail=0):
    """A little-endian Exif block: entries are (tag, type, value), with type 2 text, 3 a short, 4 a long, 5 rationals, 7 bytes."""
    main = list(main)
    blocks = {"inner": list(inner), "gps": list(gps), "thumb": [(0x0202, 4, thumbnail)] if thumbnail else []}
    at = 8 + 2 + 12 * (len(main) + bool(inner) + bool(gps)) + 4
    where = {}
    for name, entries in blocks.items():
        where[name] = at if entries else 0
        at += 2 + 12 * len(entries) + 4 if entries else 0
    main += [(0x8769, 4, where["inner"])] * bool(inner) + [(0x8825, 4, where["gps"])] * bool(gps)
    extra = b""

    def ifd(entries, following=0):
        nonlocal extra
        out = struct.pack("<H", len(entries))
        for tag, kind, value in sorted(entries):
            raw = {2: lambda: value.encode() + b"\x00", 3: lambda: struct.pack("<H", value), 4: lambda: struct.pack("<I", value),
                   5: lambda: b"".join(struct.pack("<II", *pair) for pair in value), 7: lambda: value}[kind]()
            count = len(value) if kind == 5 else 1 if kind in (3, 4) else len(raw)
            if len(raw) > 4:
                out += struct.pack("<HHII", tag, kind, count, at + len(extra))
                extra += raw
            else:
                out += struct.pack("<HHI", tag, kind, count) + raw.ljust(4, b"\x00")
        return out + struct.pack("<I", following)

    body = ifd(main, where["thumb"]) + b"".join(ifd(entries) for entries in blocks.values() if entries)
    return b"II*\x00" + struct.pack("<I", 8) + body + extra


def segment(marker, body):
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body


def jpeg(*segments, width=3072, height=2304, scan=SCAN, after=b""):
    """A JPEG with these segments before its picture: the tables, a frame of this size, and a scan with stuffed and restart bytes."""
    picture = segment(0xDB, b"\x00" + bytes(64)) + segment(0xC0, b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00") + segment(0xC4, b"\x00" + bytes(16))
    return b"\xff\xd8" + b"".join(segments) + picture + segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan + b"\xff\x00\xff\xd0" + scan + b"\xff\xd9" + after


def exif(*args, **kwargs):
    return segment(0xE1, b"Exif\x00\x00" + tiff(*args, **kwargs))


JFIF = segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
XMP = segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><dc:subject>beach</dc:subject><xmp:Rating>4</xmp:Rating></x:xmpmeta>")


def icc(name, version=2):
    """A color profile whose description is name: plain text as version 2 writes it, or in a language table as version 4 does."""
    if version == 2:
        described = b"desc" + bytes(4) + struct.pack(">I", len(name) + 1) + name.encode() + b"\x00"
    else:
        text = name.encode("utf-16-be")
        described = b"mluc" + bytes(4) + struct.pack(">II", 1, 12) + b"enUS" + struct.pack(">II", len(text), 28) + text
    profile = bytes(128) + struct.pack(">I", 2) + struct.pack(">4sII", b"cprt", 156, 0) + struct.pack(">4sII", b"desc", 156, len(described)) + described
    half = len(profile) // 2  # in two segments, the second one first
    return segment(0xE2, b"ICC_PROFILE\x00\x02\x02" + profile[half:]) + segment(0xE2, b"ICC_PROFILE\x00\x01\x02" + profile[:half])


PROFILE = icc("Display P3", version=4)


def compared(tmp_path, left, right):
    (tmp_path / "left.jpg").write_bytes(left)
    (tmp_path / "right.jpg").write_bytes(right)
    return file_compare.compare(tmp_path / "left.jpg", tmp_path / "right.jpg")


def test_a_jpeg_is_read_as_its_picture_and_the_parts_written_around_it(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(jpeg(JFIF, exif(CAMERA, [TAKEN, (0x927C, 7, bytes(40))], PLACE, thumbnail=5000), XMP, PROFILE, segment(0xFE, b"hello"), after=b"trailer"))
    described = file_compare.describe(path)
    assert described.format == "JPEG" and described.dimensions == (3072, 2304)
    assert [(part.name, part.kind) for part in described.parts] == [
        ("picture data", "picture"), ("JFIF header", "other"), ("Exif", "metadata"), ("XMP", "metadata"), ("color profile", "metadata"), ("comment", "metadata"), ("after the picture", "other")]
    assert sum(part.size for part in described.parts) == described.size  # every byte is in a part
    assert described.parts[-1].size == len(b"trailer")
    assert described.facts == {"capture time": "2006:07:04 12:00:00", "GPS": "47.50000, -122.25000", "camera": "Canon PowerShot S3", "maker note": "40 bytes",
                               "thumbnail": "5,000 bytes", "keywords": "yes", "rating": "yes", "color profile": "Display P3"}


def test_the_same_picture_with_more_said_about_it_is_the_better_one(tmp_path):
    result = compared(tmp_path, jpeg(JFIF, exif(CAMERA, [TAKEN])), jpeg(JFIF, exif(CAMERA, [TAKEN], PLACE), XMP))
    assert result.same_picture and result.differing == ["Exif", "XMP"]
    assert (result.only_first, result.only_second, result.changed) == ([], ["GPS", "keywords", "rating"], [])
    assert result.better == "second" and "only this one has GPS, keywords, rating" in result.why
    assert compared(tmp_path, jpeg(JFIF, exif(CAMERA, [TAKEN], PLACE), XMP), jpeg(JFIF, exif(CAMERA, [TAKEN]))).better == "first"


def test_files_that_differ_in_nothing_about_the_photo_are_as_good_as_each_other(tmp_path):
    result = compared(tmp_path, jpeg(JFIF, exif(CAMERA, [TAKEN], thumbnail=5000)), jpeg(exif(CAMERA + [(0x0131, 2, "iPhoto 9")], [TAKEN])))
    assert result.same_picture and result.differing == ["JFIF header", "Exif"] and result.better == "either"
    same = compared(tmp_path, jpeg(JFIF), jpeg(JFIF))
    assert same.better == "either" and same.why == "the same file, byte for byte" and same.differing == []


def test_files_that_disagree_or_each_say_something_the_other_does_not_get_no_recommendation(tmp_path):
    moved = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN])), jpeg(exif(CAMERA, [(0x9003, 2, "2006:07:04 13:00:00")], PLACE)))
    assert moved.better is None and moved.changed == ["capture time"] and "disagree on capture time" in moved.why
    both = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN])), jpeg(exif((), [TAKEN], PLACE)))
    assert both.better is None and (both.only_first, both.only_second) == (["camera"], ["GPS"])
    turned = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN])), jpeg(exif(CAMERA + [(0x0112, 3, 6)], [TAKEN])))  # not saying is upright, so they disagree
    assert turned.better is None and turned.changed == ["orientation"]
    assert compared(tmp_path, jpeg(exif(CAMERA + [(0x0112, 3, 1)], [TAKEN])), jpeg(exif(CAMERA, [TAKEN]))).better == "either"


def test_an_srgb_color_profile_is_what_a_file_without_a_profile_has(tmp_path):
    plain, srgb = jpeg(exif(CAMERA, [TAKEN])), jpeg(exif(CAMERA, [TAKEN]), icc("sRGB IEC61966-2.1"))
    result = compared(tmp_path, srgb, plain)
    assert result.first.facts["color profile"] == "sRGB IEC61966-2.1" and result.differing == ["color profile"]
    assert result.better == "either" and result.only_first == [] and "an sRGB color profile is what a picture without a profile is shown with" in result.why
    shown = runner.invoke(app, ["photos", "compare", str(tmp_path / "left.jpg"), str(tmp_path / "right.jpg")]).stdout
    assert ["color", "profile", "sRGB", "IEC61966-2.1", "-", "the", "same:", "no", "profile", "is", "sRGB"] in [line.split() for line in shown.splitlines()] and "Better to keep: either one" in shown
    wide = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN]), PROFILE), plain)  # another color space is something only that file says
    assert wide.better == "first" and wide.only_first == ["color profile"]
    assert compared(tmp_path, jpeg(PROFILE), srgb).changed == ["color profile"]
    unread = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN]), segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + bytes(100))), plain)  # a profile with no name may be anything
    assert unread.first.facts["color profile"] == "yes" and unread.better == "first"


def test_another_picture_is_ranked_only_by_its_pixel_size(tmp_path):
    saved_again = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN])), jpeg(exif(CAMERA, [TAKEN], PLACE), scan=SCAN[::-1]))
    assert saved_again.same_picture is False and saved_again.better is None and "picture data" in saved_again.differing
    smaller = compared(tmp_path, jpeg(exif(CAMERA, [TAKEN], PLACE), width=1536, height=1152), jpeg(JFIF))
    assert smaller.same_picture is False and smaller.better == "second" and "more pixels" in smaller.why


def test_a_file_of_another_type_is_compared_by_where_its_bytes_differ(tmp_path):
    body = os.urandom(3 << 20)  # more than one block, so the ends are found across blocks
    (tmp_path / "a.mov").write_bytes(b"HEADER-ONE" + body + b"end")
    (tmp_path / "b.mov").write_bytes(b"HEADER-ONE-LONGER" + body + b"end")
    result = file_compare.compare(tmp_path / "a.mov", tmp_path / "b.mov")
    assert result.ends == (10, len(body) + 3) and result.same_picture is None and result.better is None
    (tmp_path / "c.mov").write_bytes(b"HEADER-ONE" + body + b"END")
    assert file_compare.compare(tmp_path / "a.mov", tmp_path / "c.mov").ends == (10 + len(body), 0)
    assert file_compare.compare(tmp_path / "a.mov", tmp_path / "a.mov").better == "either"


def test_compare_shows_the_parts_what_each_file_says_and_the_better_one(tmp_path):
    (tmp_path / "left.jpg").write_bytes(jpeg(JFIF, exif(CAMERA, [TAKEN], PLACE), XMP))
    (tmp_path / "right.jpg").write_bytes(jpeg(JFIF, exif(CAMERA, [TAKEN])))
    result = runner.invoke(app, ["photos", "compare", str(tmp_path / "left.jpg"), str(tmp_path / "right.jpg")])
    lines = [line.split() for line in result.stdout.splitlines()]
    larger = len(XMP) + len(exif(CAMERA, [TAKEN], PLACE)) - len(exif(CAMERA, [TAKEN]))
    assert result.exit_code == 0 and ["size:", "left", "by", f"+{larger}", "bytes", "(+5.0%)"] in lines
    picture = f"{len(jpeg()):,}"
    assert ["picture", "data", picture, picture, "same"] in lines and ["XMP", str(len(XMP)), "-", "only", "in", "left"] in lines
    assert any(line[:1] == ["Exif"] and line[-4:] == ["differs", "by", "+114", "bytes"] for line in lines)
    assert ["GPS", "47.50000,", "-122.25000", "-", "differs"] in lines and ["capture", "time", "2006:07:04", "12:00:00", "2006:07:04", "12:00:00"] in lines
    assert "The same picture; what differs: Exif, XMP." in result.stdout
    assert "Better to keep: left (the same picture, and only this one has GPS, keywords, rating)." in result.stdout


def test_compare_looks_a_file_name_up_in_a_folder_and_in_a_library(mac, tmp_path):
    when = datetime(2018, 3, 1, 10, tzinfo=timezone.utc).timestamp()
    rich, plain = jpeg(JFIF, exif(CAMERA, [TAKEN], PLACE)), jpeg(JFIF, exif(CAMERA, [TAKEN]))
    folder = tmp_path / "folder" / "2018"
    folder.mkdir(parents=True)
    (folder / "IMG_1.JPG").write_bytes(rich)
    os.utime(folder / "IMG_1.JPG", (when, when))
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", len(plain), "img_1.jpeg")])
    (mac.libraries["Source"] / "originals" / "A" / "UUID1.jpg").write_bytes(plain)
    result = runner.invoke(app, ["photos", "compare", str(tmp_path / "folder"), "Source", "IMG_1.jpg"])
    assert result.exit_code == 0 and "originals/A/UUID1.jpg" in result.stdout
    assert "Better to keep: left (the same picture, and only this one has GPS)." in result.stdout
    missing = runner.invoke(app, ["photos", "compare", str(tmp_path / "folder"), "Source", "other.jpg"])
    assert missing.exit_code != 0 and "No file named other.jpg" in missing.output
    unnamed = runner.invoke(app, ["photos", "compare", str(tmp_path / "folder"), "Source"])
    assert unnamed.exit_code == 2 and "give NAME" in unnamed.output
