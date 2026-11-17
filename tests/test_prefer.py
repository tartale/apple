"""--prefer: a photo the destination has as another file (the same photo with other metadata) is written beside it and the old one is moved to the trash."""
import os
from collections import Counter
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import library_commands as lc
from apple.photos import powerphotos as pp
from apple.photos import prefer as prefer_module
from apple.photos import recover as recover_module
from apple.photos import recover_cache as cache_module
from apple.photos.prefer import Prefer
from test_compare import CAMERA, JFIF, PLACE, TAKEN, exif, jpeg
from test_powerphotos import add_photos, copying_updates_the_database, mac, make_library, no_waiting, scratch_root, trashing_updates_the_database  # noqa: F401

runner = CliRunner()
WHEN = "2014-08-03 17:01:46"
PLAIN = jpeg(JFIF, exif(CAMERA, [TAKEN]))
RICH = jpeg(JFIF, exif(CAMERA, [TAKEN], PLACE))  # the same picture, with the place it was taken
MODE = ["--diff-mode", "filesize=within=10%"]


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "cache" / "cache.sqlite"))
    monkeypatch.setattr(recover_module, "OPEN_WAIT_SECONDS", 0)
    for name in ("APPLE_PHOTOS_DIFF_MODE", "DRY_RUN"):
        monkeypatch.delenv(name, raising=False)


def unix(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def held(mac, library, content, name="IMG_0897.JPG"):
    add_photos(mac.libraries[library], [(WHEN, len(content), name)])
    (mac.libraries[library] / "originals" / "A" / "UUID1.jpg").write_bytes(content)


def folder_with(tmp_path, content, name="IMG_0897.JPG"):
    path = tmp_path / "backup" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (unix(WHEN), unix(WHEN)))
    return path


def opened(monkeypatch, library):
    monkeypatch.setenv("FAKE_LSOF", f"{library}/database/Photos.sqlite")


def do_import(tmp_path, *options):
    return runner.invoke(app, ["photos", "import", str(tmp_path / "backup"), "Source", "--only-missing", *MODE, *options])


def test_without_a_preference_the_same_photo_with_other_metadata_is_not_imported(mac, tmp_path):
    held(mac, "Source", PLAIN)
    folder_with(tmp_path, RICH)
    result = do_import(tmp_path, "--dry-run")
    assert "Imported 0 of 0 missing" in result.output and "replaces" not in result.output
    assert do_import(tmp_path, "--dry-run", "--prefer", "none").exit_code == 0


def test_recommended_writes_the_file_that_has_the_place_and_says_which_photo_it_replaces_on_a_dry_run(mac, tmp_path):
    held(mac, "Source", PLAIN)
    path = folder_with(tmp_path, RICH)
    result = do_import(tmp_path, "--dry-run", "--prefer", "recommended")
    assert result.exit_code == 0 and "Would write 1 preferred file beside the photos they are preferred to" in result.output
    assert f"{path.name}  replaces IMG_0897.JPG  {WHEN}" in result.output and "only this one has GPS" in result.output
    assert mac.calls("import-photos.applescript") == [] and mac.calls("trash-ids.applescript") == []


def test_recommended_leaves_the_photo_alone_when_the_destinations_file_is_the_richer_one(mac, tmp_path):
    held(mac, "Source", RICH)
    folder_with(tmp_path, PLAIN)
    result = do_import(tmp_path, "--dry-run", "--prefer", "recommended")
    assert "replaces" not in result.output and "Imported 0 of 0 missing" in result.output


def test_larger_takes_the_larger_file_whatever_compare_says(mac, tmp_path):
    held(mac, "Source", RICH)
    folder_with(tmp_path, jpeg(JFIF, exif(CAMERA, [TAKEN]), scan=bytes(range(255)) * 9))  # another picture, the larger file
    assert "replaces" not in do_import(tmp_path, "--dry-run", "--prefer", "recommended").output
    assert "replaces IMG_0897.JPG" in do_import(tmp_path, "--dry-run", "--prefer", "larger").output


def test_a_preferred_file_is_imported_without_the_duplicate_check_and_then_the_old_photo_is_moved_to_the_trash(mac, monkeypatch, tmp_path):
    held(mac, "Source", PLAIN)
    path = folder_with(tmp_path, RICH)
    opened(monkeypatch, mac.libraries["Source"])
    result = do_import(tmp_path, "--prefer", "recommended")
    assert result.exit_code == 0 and "Imported 1 of 1 missing files; 0 errors, 0 skipped" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(path)], "true"]]  # a duplicate, without asking
    assert mac.calls("trash-ids.applescript") == [["Source", ["UUID1"]]]


def test_nothing_is_moved_to_the_trash_when_photos_did_not_import_the_preferred_file(mac, monkeypatch, tmp_path):
    held(mac, "Source", PLAIN)
    folder_with(tmp_path, RICH)
    opened(monkeypatch, mac.libraries["Source"])
    monkeypatch.setenv("FAKE_IMPORT_COUNT", "0")
    result = do_import(tmp_path, "--prefer", "recommended")
    assert result.exit_code == 1 and "imported 0 of 1 preferred files, so nothing was moved" in result.output.replace("Photos imported", "imported")
    assert mac.calls("trash-ids.applescript") == []


def test_ordinary_missing_files_are_imported_with_the_check_and_the_preferred_one_without_it(mac, monkeypatch, tmp_path):
    held(mac, "Source", PLAIN)
    preferred = folder_with(tmp_path, RICH)
    new = folder_with(tmp_path, jpeg(JFIF, exif(CAMERA, [(0x9003, 2, "2015:01:01 00:00:00")])), "IMG_0900.JPG")
    os.utime(new, (unix("2015-01-01 00:00:00"), unix("2015-01-01 00:00:00")))
    opened(monkeypatch, mac.libraries["Source"])
    do_import(tmp_path, "--prefer", "recommended")
    assert mac.calls("import-photos.applescript") == [[[str(new)]], [[str(preferred)], "true"]]


def test_prefer_needs_only_missing(mac, tmp_path):
    held(mac, "Source", PLAIN)
    folder_with(tmp_path, RICH)
    result = runner.invoke(app, ["photos", "import", str(tmp_path / "backup"), "Source", "--prefer", "larger"])
    assert result.exit_code == 2 and "--prefer is for --only-missing" in result.output
    result = runner.invoke(app, ["photos", "copy", "Source", "photos-2018", "--prefer", "larger"])
    assert result.exit_code == 2 and "--prefer is for --only-missing" in result.output


def test_a_preference_is_only_for_the_values_it_knows(mac, tmp_path):
    result = runner.invoke(app, ["photos", "import", str(tmp_path), "Source", "--only-missing", "--prefer", "newest"])
    assert result.exit_code == 2


def libraries_with(mac, source, destination):
    held(mac, "photos-2018", destination)
    add_photos(mac.libraries["Source"], [(WHEN, len(source), "IMG_0897.JPG")])
    (mac.libraries["Source"] / "originals" / "A" / "UUID1.jpg").write_bytes(source)
    (mac.libraries["photos-2018"] / "originals" / "A" / "UUID1.jpg").write_bytes(destination)


def test_copy_prefer_copies_the_richer_photo_with_the_check_off_and_moves_the_old_one_to_the_trash(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    libraries_with(mac, RICH, PLAIN)
    result = runner.invoke(app, ["photos", "copy", "Source", "photos-2018", "--only-missing", "--prefer", "recommended", *MODE])
    assert result.exit_code == 0, result.output
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID1"], "false"]]
    assert mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID1"]]]
    assert "replaced IMG_0897.JPG" in result.output


def test_copy_prefer_does_not_trash_the_old_photo_until_the_preferred_one_has_arrived(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    libraries_with(mac, RICH, PLAIN)  # the fake PowerPhotos copies nothing, so it never arrives
    result = runner.invoke(app, ["photos", "copy", "Source", "photos-2018", "--only-missing", "--prefer", "recommended", "--timeout", "1", *MODE])
    assert mac.calls("trash-ids.applescript") == [] and "not moved to Recently Deleted: IMG_0897.JPG" in result.output


def test_copy_prefer_dry_run_says_what_it_would_replace(mac):
    libraries_with(mac, RICH, PLAIN)
    result = runner.invoke(app, ["photos", "copy", "Source", "photos-2018", "--only-missing", "--prefer", "larger", "--dry-run", *MODE])
    assert result.exit_code == 0 and "would replace IMG_0897.JPG" in result.output
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == []


def test_prefer_never_pairs_different_photos(mac, tmp_path):
    held(mac, "Source", PLAIN)
    folder_with(tmp_path, RICH, "IMG_9999.JPG")  # another name: no match under the mode, so it is simply missing
    result = do_import(tmp_path, "--dry-run", "--prefer", "larger", "--diff-mode", "filesize=within=10%")
    assert "replaces" not in result.output and "Imported 0 of 1 missing" in result.output


def test_decide_gives_the_reason_or_none(tmp_path):
    big, small = tmp_path / "big.jpg", tmp_path / "small.jpg"
    big.write_bytes(RICH)
    small.write_bytes(PLAIN)
    assert prefer_module.decide(Prefer.larger, big, small) == "larger" and prefer_module.decide(Prefer.larger, small, big) is None
    assert "GPS" in prefer_module.decide(Prefer.recommended, big, small) and prefer_module.decide(Prefer.recommended, small, big) is None
    assert prefer_module.decide(Prefer.recommended, big, big) is None and prefer_module.decide(Prefer.recommended, big, tmp_path / "gone.jpg") is None


def two_in_library(mac, first, second, flags=()):
    """The library holds two photos of the same name and date: the first visible, the second with these flags."""
    add_photos(mac.libraries["Source"], [(WHEN, len(first), "IMG_0897.JPG"), (WHEN, len(second), "IMG_0897.JPG", *flags)])
    (mac.libraries["Source"] / "originals" / "A" / "UUID1.jpg").write_bytes(first)
    (mac.libraries["Source"] / "originals" / "A" / "UUID2.jpg").write_bytes(second)


def test_no_file_is_written_when_the_destination_holds_a_hidden_photo_with_the_better_bytes_already(mac, monkeypatch, tmp_path):
    two_in_library(mac, PLAIN, RICH, ("burst",))  # the visible one lacks the place, a hidden burst member has it
    folder_with(tmp_path, RICH)
    opened(monkeypatch, mac.libraries["Source"])
    result = do_import(tmp_path, "--prefer", "recommended")
    assert result.exit_code == 0 and "Imported 0 of 0 missing" in result.output
    assert "--prefer leaves alone the photos that 1 hidden photo of the destination also match" in result.output
    assert mac.calls("import-photos.applescript") == [] and mac.calls("trash-ids.applescript") == []


def test_no_file_is_written_when_another_photo_of_the_destination_is_as_good(mac, tmp_path):
    two_in_library(mac, PLAIN, RICH)  # both visible: the second has the bytes of the file already
    folder_with(tmp_path, RICH)
    result = do_import(tmp_path, "--dry-run", "--prefer", "recommended")
    assert "replaces" not in result.output and "Imported 0 of 0 missing" in result.output
    assert "replaces" not in do_import(tmp_path, "--dry-run", "--prefer", "larger").output  # the file is not larger than the same file


def test_a_file_better_than_every_copy_in_the_destination_replaces_them_all_once(mac, monkeypatch, tmp_path):
    two_in_library(mac, PLAIN, PLAIN)  # two copies of the poorer file
    folder_with(tmp_path, RICH, "IMG_0897.JPG")
    folder_with(tmp_path, RICH, "IMG_0897 copy.JPG")  # a second file for the same photo: still one replacement
    opened(monkeypatch, mac.libraries["Source"])
    result = do_import(tmp_path, "--prefer", "recommended", "--diff-mode", "filename=ignore,filesize=within=10%")
    assert result.exit_code == 0, result.output
    assert len(mac.calls("import-photos.applescript")) == 1 and mac.calls("import-photos.applescript")[0][1] == "true"
    assert len(mac.calls("import-photos.applescript")[0][0]) == 1
    assert mac.calls("trash-ids.applescript") == [["Source", ["UUID1", "UUID2"]]]  # both copies it is better than
