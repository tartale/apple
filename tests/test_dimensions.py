"""dimensions=exact: photos whose pixel sizes are both known must have the same one."""
import csv
import io
import os
import struct
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import image_size
from apple.photos import powerphotos as pp
from apple.photos import recover_cache as cache_module
from test_powerphotos import add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()


def jpeg(width, height):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def heic(*sizes):
    boxes = b"".join(struct.pack(">I", 20) + b"ispe" + struct.pack(">IIII", 0, w, h, 0)[:12] for w, h in sizes)
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"meta" + boxes


def unix(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))


def file_in(folder, name, content, when="2018-03-01 10:00:00"):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (unix(when), unix(when)))
    return path


def test_the_size_is_read_from_a_jpeg_png_gif_and_heic_header_larger_side_first(tmp_path):
    for name, content, expected in (("a.jpg", jpeg(2304, 1728), (2304, 1728)), ("b.jpg", jpeg(1728, 2304), (2304, 1728)), ("c.png", png(640, 480), (640, 480)),
                                    ("d.gif", gif(10, 20), (20, 10)), ("e.heic", heic((160, 120), (4032, 3024)), (4032, 3024))):
        assert image_size.image_size(file_in(tmp_path, name, content)) == expected, name
    for name, content in (("x.txt", b"not an image"), ("empty.jpg", b""), ("short.jpg", b"\xff\xd8\xff"), ("t.png", b"\x89PNG\r\n\x1a\n")):
        assert image_size.image_size(file_in(tmp_path, name, content)) is None, name
    assert image_size.image_size(tmp_path / "missing.jpg") is None


def test_a_library_reads_the_size_of_the_original_and_not_of_an_edit(mac):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg", "dims:4000x3000", "edited:2000x1500"), ("2018-03-02 10:00:00", 100, "b.jpg", "edited:640x480")])
    ids = pp.library_photos(mac.libraries["Source"]).ids
    assert ids[("2018-03-01 10:00:00", "100", "a.jpg")] == {"dims": {"4000x3000"}}
    assert ids[("2018-03-02 10:00:00", "100", "b.jpg")] == {"dims": {"640x480"}}  # no original size recorded: the library's own


def test_with_dimensions_exact_photos_of_another_size_are_different_photos(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg", "dims:2304x1728")])
    folder_b = tmp_path / "other"
    path = file_in(folder_b, "a.jpg", jpeg(1000, 800).ljust(100, b"\x00"))
    assert path.stat().st_size == 100
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder_b)]).exit_code == 0  # the name, date and size agree
    different = runner.invoke(app, ["photos", "diff", "Source", str(folder_b), "--diff-mode", "dimensions=exact"])
    assert different.exit_code == 1
    same = file_in(tmp_path / "same", "a.jpg", jpeg(1728, 2304).ljust(100, b"\x00"))  # rotated: the same size
    assert runner.invoke(app, ["photos", "diff", "Source", str(same.parent), "--diff-mode", "dimensions=exact"]).exit_code == 0


def test_an_unknown_size_passes_and_a_shared_id_is_not_vetoed_by_the_size(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])  # the library has no size
    folder = tmp_path / "backup"
    file_in(folder, "a.jpg", jpeg(1000, 800).ljust(100, b"\x00"))
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "dimensions=exact"]).exit_code == 0


def test_the_size_of_a_folder_file_is_cached_with_its_time_and_size(mac, tmp_path, monkeypatch):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg", "dims:1000x800")])
    folder = tmp_path / "backup"
    file_in(folder, "a.jpg", jpeg(1000, 800).ljust(100, b"\x00"))
    read = []
    real = image_size.image_size
    monkeypatch.setattr(image_size, "image_size", lambda path: read.append(path.name) or real(path))
    options = ["photos", "diff", "Source", str(folder), "--diff-mode", "dimensions=exact"]
    runner.invoke(app, options)
    runner.invoke(app, options)
    assert read == ["a.jpg"]
    runner.invoke(app, [*options, "--refresh"])
    assert read == ["a.jpg", "a.jpg"]
    read.clear()
    runner.invoke(app, ["photos", "diff", "Source", str(folder)])
    assert read == []  # not asked for: no header is read
