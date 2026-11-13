"""apple.photos.exif and a folder dated by it."""
import io
import csv
import os
import time
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import exif
from apple.photos import recover_cache as cache_module
from exif_files import jpeg_taken, tiff_with
from test_powerphotos import A, add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()


def unix(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def denver(monkeypatch, tmp_path):
    """A Mac in Denver, where the photos of September 2005 were taken in daylight saving time (UTC-6)."""
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_capture_time_is_read_from_a_jpeg(tmp_path):
    path = jpeg_taken(tmp_path / "a.jpg", "2005:09:30 00:51:40", unix("2006-09-30 06:51:40"))
    assert exif.capture_time(path) == ("2005:09:30 00:51:40", "")


def test_the_offset_is_read_with_it_and_a_time_without_one_is_read_in_this_macs_zone():
    assert exif.utc_of("2005:09:30 00:51:40") == "2005-09-30 06:51:40"  # Denver in September: UTC-6
    assert exif.utc_of("2005:12:30 00:51:40") == "2005-12-30 07:51:40"  # and UTC-7 in winter
    assert exif.utc_of("2005:09:30 00:51:40", "+02:00") == "2005-09-29 22:51:40"
    assert exif.utc_of("2005:09:30 00:51:40", "-05:30") == "2005-09-30 06:21:40"
    assert exif.utc_of("0000:00:00 00:00:00") is None and exif.utc_of("not a time") is None


def test_the_offset_tag_is_read_when_the_file_has_one(tmp_path):
    path = jpeg_taken(tmp_path / "a.jpg", "2005:09:30 00:51:40", 0, offset="+02:00")
    assert exif.capture_time(path) == ("2005:09:30 00:51:40", "+02:00")


def test_a_tiff_based_file_and_a_heic_style_block_are_read_too(tmp_path):
    raw = tmp_path / "a.dng"
    raw.write_bytes(tiff_with("2010:01:02 03:04:05"))
    assert exif.capture_time(raw) == ("2010:01:02 03:04:05", "")
    heic = tmp_path / "a.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40 + b"\x00\x00\x00\x06Exif\x00\x00" + tiff_with("2012:05:06 07:08:09"))
    assert exif.capture_time(heic) == ("2012:05:06 07:08:09", "")


def test_a_file_with_no_capture_time_has_none(tmp_path):
    for name, content in (("plain.jpg", b"\xff\xd8\xff\xd9"), ("empty.jpg", b""), ("junk.jpg", b"Exif\x00\x00II*\x00\xff\xff\xff\xff")):
        (tmp_path / name).write_bytes(content)
        assert exif.capture_time(tmp_path / name) is None, name
    assert exif.capture_time(tmp_path / "missing.jpg") is None


def rows_of(result):
    return list(csv.DictReader(io.StringIO(result.stdout)))


def test_a_folder_file_is_dated_by_its_exif_time_not_its_modification_time(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2005-09-30 06:51:40", 100, "IMAG0044.JPG")])  # what Photos filed it under
    folder = tmp_path / "backup"
    jpeg_taken(folder / "IMAG0044.JPG", "2005:09:30 00:51:40", unix("2006-09-30 06:51:40"))  # the backup says a year later
    same = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--fields", "date,size,name"])
    assert same.exit_code == 0 and "In both: 1." in same.output
    by_mtime = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--fields", "date,size,name", "--diff-mode", "date=modified"])
    assert by_mtime.exit_code == 1 and "Only in Source: 1. Only in " in by_mtime.output


def test_import_only_missing_does_not_import_a_file_the_library_has_under_its_exif_date(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2005-09-30 06:51:40", 100, "IMAG0044.JPG")])
    folder = tmp_path / "backup"
    jpeg_taken(folder / "IMAG0044.JPG", "2005:09:30 00:51:40", unix("2006-09-30 06:51:40"))
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 0" in result.output
    again = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "date=modified"])
    assert "Imported 0 of 1" in again.output  # by the modification time alone it looks missing


def test_the_capture_time_is_cached_until_the_file_changes(mac, tmp_path, monkeypatch):
    add_photos(mac.libraries["Source"], [A])
    folder = tmp_path / "backup"
    path = jpeg_taken(folder / "a.jpg", "2005:09:30 00:51:40", unix("2006-09-30 06:51:40"))
    reads = []
    real = exif.capture_time
    monkeypatch.setattr(exif, "capture_time", lambda file: reads.append(str(file)) or real(file))
    options = ["photos", "diff", "Source", str(folder), "--fields", "date,size,name"]
    runner.invoke(app, options)
    runner.invoke(app, options)
    assert reads == [str(path)]  # the second run used the cache
    other = jpeg_taken(folder / "b.jpg", "2004:01:01 00:00:00", unix("2006-09-30 06:51:40") + 5)  # a new file: the folder is listed again
    runner.invoke(app, options)
    assert reads == [str(path), str(other)]  # only the new one is opened
    runner.invoke(app, [*options, "--refresh"])
    assert len(reads) == 4  # a refresh opens them all again


def test_a_video_and_a_file_with_no_exif_keep_their_modification_time(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "clip.mov"), ("2018-03-02 11:00:00", 100, "plain.jpg")])
    folder = tmp_path / "backup"
    for name, when in (("clip.mov", "2018-03-01 10:00:00"), ("plain.jpg", "2018-03-02 11:00:00")):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x" * 100)
        os.utime(folder / name, (unix(when), unix(when)))
    result = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--fields", "date,size,name"])
    assert result.exit_code == 0
