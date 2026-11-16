"""content=identical: files with the same bytes are the same photo, whatever their names and dates, and files with other bytes are not."""
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
    result = runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical", "--format", "csv", "--fields", "left_name,right_name,matched_by"])
    assert rows_of(result) == [{"left_name": "IMAG0044.JPG", "right_name": "copy-of-something-else.jpg", "matched_by": "same bytes"}]
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "content=identical"]).exit_code == 0


def test_only_files_whose_size_occurs_on_the_other_side_are_read(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    folder = tmp_path / "backup"
    same_size = file_in(folder, "x.jpg", b"z" * len(BYTES))
    file_in(folder, "y.jpg", b"short")  # no photo of the library has this size
    read = []
    real = content_hash.file_digest
    monkeypatch.setattr(content_hash, "file_digest", lambda path: read.append(path.name) or real(path))
    runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical"])
    assert sorted(read) == ["UUID1.jpg", "x.jpg"]


def test_a_hash_is_cached_with_the_files_time_and_size_until_refresh(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "x.jpg", BYTES)
    read = []
    real = content_hash.file_digest
    monkeypatch.setattr(content_hash, "file_digest", lambda path: read.append(path.name) or real(path))
    options = ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical"]
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
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "content=identical"])
    assert "Imported 0 of 1" in result.output and "new.jpg" in result.stdout and "renamed" not in result.stdout
    plain = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 2" in plain.output


def test_identity_ignore_does_not_turn_content_off(mac, tmp_path):
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "renamed.jpg", BYTES, "2001-01-01 00:00:00")
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "identity=ignore,content=identical"]).exit_code == 0
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "filename=exact,extension=exact,date-range=exact,filesize=exact"]).exit_code == 1  # the names and dates differ
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "strict"]).exit_code == 0  # strict reads the bytes too


def test_the_loose_mode_matches_across_extensions_and_sizes_and_the_strict_mode_reads_the_bytes(mac, tmp_path):
    library_with(mac, [("2018-03-01 10:00:00", "IMG_0001.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "img_0001.heic", b"another format" * 10, "2018-03-01 20:00:00")  # another extension and size, ten hours later
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder)]).exit_code == 1
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "loose"]).exit_code == 0
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "loose,extension=exact"]).exit_code == 1
    renamed = tmp_path / "renamed"
    file_in(renamed, "anything-at-all.jpg", BYTES, "2001-01-01 00:00:00")
    assert runner.invoke(app, ["photos", "diff", "Source", str(renamed), "--diff-mode", "strict"]).exit_code == 0  # the same bytes
    assert runner.invoke(app, ["photos", "diff", "Source", str(renamed), "--diff-mode", "tolerant"]).exit_code == 1


def test_copies_within_one_folder_are_found_by_their_bytes_when_importing(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2001-01-01 00:00:00", 5, "unrelated.jpg")])
    folder = tmp_path / "backup"
    file_in(folder, "IMAG0044.JPG", BYTES, "2006-09-30 06:51:40")
    file_in(folder, "IMAG0044_ASLN.JPG", BYTES, "2005-01-01 00:00:00")  # the same bytes, another name and date
    file_in(folder, "other.jpg", b"different bytes" * 14, "2012-02-02 02:02:02")
    plain = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 3" in plain.output
    hashed = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "content=identical"])
    assert "Imported 0 of 2" in hashed.output and "1 left out as the same photo as another" in hashed.output
    assert "IMAG0044.JPG" in hashed.stdout and "ASLN" not in hashed.stdout  # the shorter name is kept


def test_without_only_missing_the_copies_within_the_folder_are_found_by_their_bytes_too(mac, tmp_path):
    folder = tmp_path / "backup"
    file_in(folder, "a.jpg", BYTES, "2006-09-30 06:51:40")
    file_in(folder, "a_copy_of_it.jpg", BYTES, "2005-01-01 00:00:00")
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--dry-run", "--diff-mode", "content=identical"])
    assert "Imported 0 of 1" in result.output and "1 left out as the same photo as another" in result.output


def test_a_refresh_reads_the_files_again_but_keeps_the_hashes_of_the_files_it_did_not_read(mac, tmp_path, monkeypatch):
    library_with(mac, [("2006-09-30 06:51:40", "a.jpg", BYTES)])
    first, second = tmp_path / "first", tmp_path / "second"
    file_in(first, "x.jpg", BYTES)
    file_in(second, "y.jpg", BYTES)
    read = []
    real = content_hash.file_digest
    monkeypatch.setattr(content_hash, "file_digest", lambda path: read.append(str(path)) or real(path))

    def match(folder, *options):
        return runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical", *options])

    match(first)
    match(second, "--refresh")  # the library's file and y.jpg are read again; x.jpg is not
    assert sorted(path.rsplit("/", 1)[-1] for path in read) == ["UUID1.jpg", "UUID1.jpg", "x.jpg", "y.jpg"]
    read.clear()
    match(first)
    assert read == []  # the hash of x.jpg is still saved


def test_strict_import_leaves_out_what_photos_would_call_a_duplicate_and_lists_what_it_would_not(mac, tmp_path):
    """The library's photo and the files: same bytes under another name and date (a duplicate); another file that looks the same (it is not)."""
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "renamed.jpg", BYTES, "2001-01-01 00:00:00")  # the same bytes: a duplicate whatever it is called
    file_in(folder, "IMAG0044.JPG", b"z" * len(BYTES), "2006-09-30 06:51:40")  # the same name, date and size, other bytes: not a duplicate
    for mode, listed in (("strict", "IMAG0044.JPG"), ("strict,content=ignore", "renamed.jpg")):  # without the bytes it is the other way round
        result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", mode])
        assert "Imported 0 of 1" in result.output and listed in result.stdout, mode
    only = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "strict"])
    assert "IMAG0044.JPG" in only.stdout and "renamed" not in only.stdout


def test_explain_names_the_bytes_when_they_are_all_that_keeps_two_photos_apart(mac, tmp_path):
    library_with(mac, [("2006-09-30 06:51:40", "IMAG0044.JPG", BYTES)])
    folder = tmp_path / "backup"
    file_in(folder, "IMAG0044.JPG", b"z" * len(BYTES), "2006-09-30 06:51:40")
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--explain", "--dry-run", "--diff-mode", "tolerant,content=identical"])
    assert "differs: file bytes" in result.output
    assert "differs: file bytes" not in runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--explain", "--dry-run", "--diff-mode", "tolerant"]).output


def jpeg_with(*metadata, scan=b"\x01\x02\x03" * 10000):
    """A JPEG of this picture with these metadata segments in front of it."""
    import struct

    def segment(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    picture = segment(0xDB, b"\x00" + bytes(64)) + segment(0xC0, b"\x08" + struct.pack(">HH", 10, 10) + b"\x01\x01\x11\x00") + segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan + b"\xff\xd9"
    return b"\xff\xd8" + b"".join(segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + bytes(size)) for size in metadata) + picture


def test_metadata_ignore_takes_the_same_image_with_other_metadata_as_one_photo(mac, tmp_path):
    plain, profiled = jpeg_with(), jpeg_with(1370)
    library_with(mac, [("2014-08-03 17:01:46", "IMG_0897.JPG", plain)])
    folder = tmp_path / "backup"
    file_in(folder, "IMG_0897.JPG", profiled, "2014-08-03 17:01:46")
    other = file_in(folder, "IMG_0898.JPG", jpeg_with(scan=b"\x09\x08\x07" * 10000), "2014-08-03 17:01:46")  # another picture, the same size as the first
    assert len(other.read_bytes()) == len(plain)
    # the sizes differ by 1,370 bytes: exact sizes see two photos, content=identical too, and metadata=ignore sees one
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "strict,date-range=exact,filesize=exact", "--only", "left"]).exit_code == 1
    result = runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "strict,metadata=ignore", "--format", "csv", "--fields", "left_name,right_name,matched_by"])
    assert [(r["left_name"], r["right_name"], r["matched_by"]) for r in rows_of(result)] == [("IMG_0897.JPG", "IMG_0897.JPG", "same image data")]
    renamed = file_in(folder, "other-name.jpg", profiled, "2001-01-01 00:00:00")  # and whatever the name and date
    result = runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical,metadata=ignore", "--format", "csv", "--fields", "left_name,right_name,matched_by"])
    assert ("IMG_0897.JPG", "IMG_0897.JPG", "same image data") in [(r["left_name"], r["right_name"], r["matched_by"]) for r in rows_of(result)]
    assert renamed.exists()


def test_metadata_ignore_keeps_two_images_apart_and_says_so_in_explain(mac, tmp_path):
    library_with(mac, [("2014-08-03 17:01:46", "IMG_0897.JPG", jpeg_with())])
    folder = tmp_path / "backup"
    file_in(folder, "IMG_0897.JPG", jpeg_with(scan=b"\x09\x08\x07" * 10000), "2014-08-03 17:01:46")  # the same size, name and date: another picture
    assert runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "tolerant"]).exit_code == 0
    result = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--diff-mode", "content=identical,metadata=ignore", "--explain"])
    assert result.exit_code == 1 and "differs: image data" in result.output


def test_metadata_ignore_reads_only_the_files_near_in_size_to_one_on_the_other_side(mac, tmp_path, monkeypatch):
    library_with(mac, [("2014-08-03 17:01:46", "a.jpg", jpeg_with())])
    folder = tmp_path / "backup"
    file_in(folder, "near.jpg", jpeg_with(1370), "2014-08-03 17:01:46")
    file_in(folder, "far.jpg", jpeg_with(*[60000] * 10), "2014-08-03 17:01:46")  # twenty times the size
    read = []
    real = content_hash.file_compare.image_digest
    monkeypatch.setattr(content_hash.file_compare, "image_digest", lambda path: read.append(path.name) or real(path))
    runner.invoke(app, ["photos", "match", "Source", str(folder), "--diff-mode", "content=identical,metadata=ignore"])


def test_name_narrows_diff_match_and_import_to_the_photos_with_that_name(mac, tmp_path):
    other = b"another picture" * 12
    library_with(mac, [("2014-08-03 17:01:46", "IMG_0897.JPG", BYTES), ("2014-08-03 17:02:46", "IMG_0898.JPG", other)])
    folder = tmp_path / "backup"
    file_in(folder, "IMG_0897.JPG", BYTES, "2014-08-03 17:01:46")
    file_in(folder, "IMG_0898.JPG", b"z" * len(other), "2014-08-03 17:02:46")  # the same name, date and size, other bytes
    file_in(folder, "IMG_0899.JPG", BYTES, "2014-08-03 17:03:46")  # the bytes of 0897 under another name
    base = ["photos", "diff", "Source", str(folder), "--format", "csv", "--fields", "name", "--diff-mode", "content=identical"]
    assert sorted((r["side"], r["name"]) for r in rows_of(runner.invoke(app, base))) == [("left", "IMG_0898.JPG"), ("right", "IMG_0898.JPG"), ("right", "IMG_0899.JPG")]  # 0899 is a second copy of 0897
    assert [r["name"] for r in rows_of(runner.invoke(app, base + ["--name", "img_089[89]*"]))] == []  # a bracket is only that character
    result = runner.invoke(app, base + ["--name", "IMG_089*"])
    assert sorted((r["side"], r["name"]) for r in rows_of(result)) == [("left", "IMG_0898.JPG"), ("right", "IMG_0898.JPG"), ("right", "IMG_0899.JPG")]  # 0897 is not named, so 0899 has no partner
    result = runner.invoke(app, base + ["--name", "IMG_0899.JPG", "--name", "img_0897.jpeg"])
    assert sorted((r["side"], r["name"]) for r in rows_of(result)) == [("right", "IMG_0899.JPG")]
    matches = runner.invoke(app, ["photos", "match", "Source", str(folder), "--name", "IMG_0897", "--format", "csv", "--fields", "left_name"])
    assert [r["left_name"] for r in rows_of(matches)] == ["IMG_0897.JPG"] and "In both: 1. Only in Source: 0. Only in" in matches.output
    imported = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "content=identical", "--name", "IMG_089?.JPG"])
    assert "Imported 0 of 1" in imported.output and "IMG_0898.JPG" in imported.stdout and "IMG_0899" not in imported.stdout  # 0899 has the bytes of 0897, which the library holds
    # the names narrow what is imported, not what the library is searched for: 0897 is not named, and still is what 0899 is held as
    kept = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run", "--diff-mode", "content=identical", "--name", "IMG_0899.JPG"])
    assert "IMG_0899" not in kept.stdout and kept.exit_code == 0
    everything = runner.invoke(app, ["photos", "import", str(folder), "Source", "--dry-run", "--name", "IMG_0899.JPG"])
    assert "IMG_0899.JPG" in everything.stdout and "IMG_0897" not in everything.stdout


def test_name_with_copy_is_for_only_missing(mac):
    result = runner.invoke(app, ["photos", "copy", "Source", "photos-2018", "--name", "a.jpg"])
    assert result.exit_code == 2 and "--name is for --only-missing" in result.output
