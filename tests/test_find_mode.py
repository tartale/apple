"""find matches by --diff-mode, and by the iCloud fingerprint of a photo (from `missing`) and of a file's name or a library's database."""
import base64
import json

from typer.testing import CliRunner

from apple.cli import app
from test_powerphotos import add_photos, mac, make_library, scratch_root  # noqa: F401
from test_recover import LATER, WHEN, make_file, missing, other_library, private_cache, recover, rows_of  # noqa: F401

runner = CliRunner()


def fingerprint(seed: int) -> str:
    return base64.b64encode(b"\x01" + bytes((seed + n) % 256 for n in range(20))).decode()


def lost(mac, *photos):
    """Source holds shared placeholders (size 0, no original) given as (date, name, ids...)."""
    add_photos(mac.libraries["Source"], [(when, 0, name, "shared", *ids) for when, name, *ids in photos])


def test_missing_gives_the_ids_of_the_photos_it_lists(mac):
    lost(mac, (WHEN, "one.heic", f"stable:{fingerprint(1)}", "cloud:GUID-1"), (LATER, "two.heic"))
    one, two = rows_of(missing("--format", "csv"))
    assert (one["stable"], one["cloud"], two["stable"], two["cloud"]) == (fingerprint(1), "GUID-1", "", "")
    assert [r["stable"] for r in json.loads(missing("--format", "json").stdout)] == [fingerprint(1), ""]
    assert "stable" not in missing().stdout.splitlines()[0]  # the text table leaves out what is for find


def test_find_uses_the_mode_for_the_name_the_extension_and_the_time(mac, tmp_path):
    lost(mac, (WHEN, "one.heic"))
    folder = tmp_path / "backup"
    make_file(folder / "ONE.HEIC", "2018-03-06 04:00:00")  # the next day, in capitals
    sources = ("--folders", str(folder), "--format", "csv")
    assert rows_of(recover(*sources))[0]["source"] == "none"  # more than two hours on
    assert rows_of(recover(*sources, "--diff-mode", "loose"))[0]["source"] == "folder"
    jpg = tmp_path / "jpg"
    make_file(jpg / "one.jpg", WHEN)
    assert rows_of(recover("--folders", str(jpg), "--format", "csv"))[0]["source"] == "none"  # another extension
    assert rows_of(recover("--folders", str(jpg), "--format", "csv", "--diff-mode", "extension=ignore"))[0]["source"] == "folder"
    assert rows_of(recover("--folders", str(jpg), "--format", "csv", "--diff-mode", "loose,extension=exact"))[0]["source"] == "none"


def test_a_file_whose_name_ends_in_the_fingerprint_of_a_missing_photo_is_found_whatever_its_name_and_date(mac, tmp_path):
    token = fingerprint(2)
    lost(mac, (WHEN, "one.heic", f"stable:{token}"), (LATER, "two.heic"))
    folder = tmp_path / "backup"
    make_file(folder / f"renamed_{token}.heic", "2001-01-01 00:00:00")
    first, second = rows_of(recover("--folders", str(folder), "--format", "csv"))
    assert first["source"] == "folder" and first["files"] == str(folder / f"renamed_{token}.heic") and second["source"] == "none"
    ignored = rows_of(recover("--folders", str(folder), "--format", "csv", "--diff-mode", "identity=ignore"))
    assert ignored[0]["source"] == "none"


def test_a_file_with_another_fingerprint_is_not_the_photo_though_its_name_and_time_agree(mac, tmp_path):
    lost(mac, (WHEN, f"one_{fingerprint(3)}.heic", f"stable:{fingerprint(3)}"))
    folder = tmp_path / "backup"
    make_file(folder / f"one_{fingerprint(3)}.heic", WHEN)
    assert rows_of(recover("--folders", str(folder), "--format", "csv"))[0]["source"] == "folder"
    other = tmp_path / "other"
    lost(mac, (WHEN, f"one_{fingerprint(4)}.heic", f"stable:{fingerprint(3)}"))
    make_file(other / f"one_{fingerprint(4)}.heic", WHEN)  # the name says 4, the photo says 3
    assert rows_of(recover("--folders", str(other), "--format", "csv"))[0]["source"] == "none"


def test_a_photo_of_a_library_under_the_folder_is_found_by_its_fingerprint(mac, tmp_path):
    token = fingerprint(5)
    lost(mac, (WHEN, "one.heic", f"stable:{token}"))
    other_library(tmp_path, [("2001-01-01 00:00:00", 500, "something-else.jpg", f"stable:{token}")])
    row = rows_of(recover("--folders", str(tmp_path / "libraries"), "--format", "csv"))[0]
    assert row["source"] == "library" and row["files"].endswith("UUID1.jpg")
    assert rows_of(recover("--folders", str(tmp_path / "libraries"), "--format", "csv", "--diff-mode", "identity=ignore"))[0]["source"] == "none"
