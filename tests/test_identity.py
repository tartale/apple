"""Photos with the same iCloud id are the same photo, and with different ids are not, whatever their names, dates and sizes."""
import base64
import csv
import io
import os
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import folder_photos
from apple.photos import powerphotos as pp
from apple.photos import recover_cache as cache_module
from test_powerphotos import add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()


def fingerprint(seed: int) -> str:
    """A fingerprint as iCloud writes it: 28 characters, the base64 of 1 and 20 bytes."""
    return base64.b64encode(b"\x01" + bytes((seed + n) % 256 for n in range(20))).decode()


def unix(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def file_at(path, when, size=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (unix(when), unix(when)))
    return path


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))


def rows_of(result):
    return list(csv.DictReader(io.StringIO(result.stdout)))


def test_a_name_that_ends_in_a_fingerprint_gives_it_and_one_that_only_looks_like_one_does_not():
    token = fingerprint(1)
    assert folder_photos.fingerprint_in(f"IMAG0044_{token}.JPG") == token
    assert folder_photos.fingerprint_in("IMAG0044.JPG") is None
    assert folder_photos.fingerprint_in("holiday_abcdefghijklmnopqrstuvwxyzAB.jpg") is None  # 28 letters that do not decode to 1 and 20 bytes
    slashed = base64.b64encode(b"\x01" + b"\xff" * 20).decode()
    assert "/" in slashed and folder_photos.fingerprint_in(f"a_{slashed.replace('/', chr(0xff0f))}.jpg") == slashed  # the full-width slash a name uses


def test_a_library_reads_the_ids_of_its_photos_and_of_those_in_recently_deleted(mac):
    token = fingerprint(2)
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg", f"stable:{token}", "cloud:GUID-1"), ("2018-03-02 10:00:00", 100, "b.jpg", "trashed", f"stable:{fingerprint(3)}")])
    photos = pp.library_photos(mac.libraries["Source"])
    assert photos.ids == {("2018-03-01 10:00:00", "100", "a.jpg"): {"stable": {token}, "cloud": {"GUID-1"}}}
    assert photos.trashed_ids == {("2018-03-02 10:00:00", "100", "b.jpg"): {"stable": {fingerprint(3)}}}


def test_diff_and_match_treat_a_file_and_a_photo_with_the_same_fingerprint_as_one_photo(mac, tmp_path):
    token = fingerprint(4)
    add_photos(mac.libraries["Source"], [("2006-09-30 06:51:40", 1492992, "IMAG0044.JPG", f"stable:{token}")])
    folder = tmp_path / "backup"
    file_at(folder / f"IMAG0044_{token}.JPG", "2005-09-30 06:51:40", 777)  # another name, a year off, another size
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder)]).exit_code == 0
    matched = runner.invoke(app, ["photos", "match", "Source", str(folder), "--format", "csv", "--fields", "left_name,right_name,differs,matched_by"])
    assert rows_of(matched) == [{"left_name": "IMAG0044.JPG", "right_name": f"IMAG0044_{token}.JPG", "differs": rows_of(matched)[0]["differs"], "matched_by": "iCloud fingerprint"}]
    assert "name" in rows_of(matched)[0]["differs"] and "date by 365d" in rows_of(matched)[0]["differs"]
    ignored = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "identity=ignore"])
    assert ignored.exit_code == 1  # the ids alone made them one photo


def test_different_fingerprints_make_two_photos_though_the_name_date_and_size_agree(mac, tmp_path):
    name = f"IMAG0044_{fingerprint(6)}.JPG"
    add_photos(mac.libraries["Source"], [("2006-09-30 06:51:40", 100, name, f"stable:{fingerprint(5)}")])  # the library says another fingerprint than the name does
    folder = tmp_path / "backup"
    file_at(folder / name, "2006-09-30 06:51:40", 100)
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder)]).exit_code == 1
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "identity=ignore"]).exit_code == 0


def test_two_libraries_match_on_the_cloud_id(mac):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg", "cloud:GUID-1")])
    add_photos(mac.libraries["photos-2018"], [("2019-07-07 07:00:00", 5, "z.jpg", "cloud:GUID-1")])
    result = runner.invoke(app, ["photos", "match", "Source", "photos-2018", "--format", "csv", "--fields", "left_name,right_name,matched_by"])
    assert rows_of(result) == [{"left_name": "a.jpg", "right_name": "z.jpg", "matched_by": "iCloud asset id"}]


def test_import_only_missing_leaves_out_a_file_the_library_has_under_its_fingerprint_even_in_the_trash(mac, tmp_path):
    token, trashed = fingerprint(7), fingerprint(8)
    add_photos(mac.libraries["Source"], [
        ("2006-09-30 06:51:40", 100, "IMAG0044.JPG", f"stable:{token}"),
        ("2007-01-01 00:00:00", 100, "gone.jpg", "trashed", f"stable:{trashed}"),
    ])
    folder = tmp_path / "backup"
    file_at(folder / f"IMAG0044_{token}.JPG", "2005-09-30 06:51:40", 777)
    file_at(folder / f"other_{trashed}.jpg", "2001-01-01 00:00:00", 55)
    file_at(folder / "new.jpg", "2012-02-02 02:02:02", 33)
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 1" in result.output and "new.jpg" in result.stdout and "IMAG0044" not in result.stdout and "other_" not in result.stdout
    listed = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--ignore-trash"])
    assert "Imported 0 of 2" in listed.output and "other_" in listed.stdout


def test_a_folder_leaves_out_a_file_with_the_fingerprint_of_another_when_importing(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])
    token = fingerprint(9)
    folder = tmp_path / "backup"
    file_at(folder / f"x_{token}.jpg", "2012-02-02 02:02:02", 33)
    file_at(folder / f"much-longer-name_{token}.jpg", "2013-03-03 03:03:03", 44)  # the same fingerprint: the same file
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 1" in result.output and "1 left out as the same photo as another" in result.output
