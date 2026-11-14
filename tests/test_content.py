"""content=hash: files with the same bytes are the same photo, whatever their names, dates and sizes."""
import csv
import hashlib
import io
import os
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import content_hash
from apple.photos import powerphotos as pp
from apple.photos import recover_cache as cache_module
from test_powerphotos import add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()
BYTES = b"the picture" * 20


def unix(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))


def library_with(mac, photos):
    """Source holds photos given as (date, name, content); their sizes are those of the content."""
    add_photos(mac.libraries["Source"], [(when, len(content), name) for when, name, content in photos])
    for number, (_, _, content) in enumerate(photos, 1):
        (mac.libraries["Source"] / "originals" / "A" / f"UUID{number}.jpg").write_bytes(content)


def file_in(folder, name, content, when="2012-02-02 02:02:02"):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (unix(when), unix(when)))
    return path


def rows_of(result):
    return list(csv.DictReader(io.StringIO(result.stdout)))


def test_a_files_digest_is_its_sha256_and_a_file_that_cannot_be_read_has_none(tmp_path):
    path = file_in(tmp_path, "a.jpg", BYTES)
    assert content_hash.file_digest(path) == hashlib.sha256(BYTES).hexdigest()
    assert content_hash.file_digest(tmp_path / "gone.jpg") is None


def test_with_content_hash_a_renamed_and_re_dated_copy_is_the_same_photo(mac, tmp_path):
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "copy-of-something-else.jpg", BYTES, "2001-01-01 00:00:00")
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder)]).exit_code == 1  # nothing else agrees
    result = runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=hash", "--format", "csv", "--fields", "left_name,right_name,matched_by"])
    assert rows_of(result) == [{"left_name": "IMAG0044.JPG", "right_name": "copy-of-something-else.jpg", "matched_by": "same bytes"}]
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "content=hash"]).exit_code == 0


def test_different_bytes_do_not_make_two_photos_different(mac, tmp_path):
    other = b"another picture" * 20 + b"."[:0]
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "IMAG0044.JPG", other[: len(BYTES)].ljust(len(BYTES), b"z"), "2006-09-30 06:51:40")  # the same name, date and size, other bytes
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "content=hash"]).exit_code == 0  # the other parts still agree


def test_only_files_whose_size_occurs_on_the_other_side_are_read(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    folder = tmp_path / "backup"
    same_size = file_in(folder, "x.jpg", b"z" * len(BYTES))
    file_in(folder, "y.jpg", b"short")  # no photo of the library has this size
    read = []
    real = content_hash.file_digest
    monkeypatch.setattr(content_hash, "file_digest", lambda path: read.append(path.name) or real(path))
    runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=hash"])
    assert sorted(read) == ["UUID1.jpg", "x.jpg"]


def test_a_hash_is_cached_with_the_files_time_and_size_until_refresh(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "x.jpg", BYTES)
    read = []
    real = content_hash.file_digest
    monkeypatch.setattr(content_hash, "file_digest", lambda path: read.append(path.name) or real(path))
    options = ["photos", "match", "Source", str(folder), "--diff-mode", "content=hash"]
    runner.invoke(app, options)
    assert len(read) == 2
    runner.invoke(app, options)
    assert len(read) == 2  # both from the cache
    runner.invoke(app, [*options, "--refresh"])
    assert len(read) == 4


def test_the_default_reads_no_file_at_all(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "x.jpg", BYTES)
    monkeypatch.setattr(content_hash, "file_digest", lambda path: pytest.fail("a file was read"))
    runner.invoke(app, ["photos", "match", "Source", str(folder)])


def test_import_only_missing_leaves_out_a_file_with_the_bytes_of_a_photo_the_library_has(mac, tmp_path):
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "renamed.jpg", BYTES, "2001-01-01 00:00:00")
    file_in(folder, "new.jpg", b"brand new" * 10, "2013-03-03 03:03:03")
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "content=hash"])
    assert "Imported 0 of 1" in result.output and "new.jpg" in result.stdout and "renamed" not in result.stdout
    plain = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 2" in plain.output


def test_identity_ignore_does_not_turn_content_off(mac, tmp_path):
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "renamed.jpg", BYTES, "2001-01-01 00:00:00")
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "identity=ignore,content=hash"]).exit_code == 0
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "exact"]).exit_code == 1
