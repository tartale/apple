"""apple photos find and import: finding the originals a library does not store, and importing them."""
import csv
import io
import json
import os
import re
import shutil
import tempfile
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple.cli import app
from apple.photos import powerphotos as pp
from apple.photos import progress as progress_module
from apple.photos import recover_cache as cache_module
from apple.photos import recover as recover_module
from apple.photos import recover_sources as rs
# fixtures and helpers shared with the other PowerPhotos tests
from test_powerphotos import FAKES, add_photos, mac, make_library, scratch_root  # noqa: F401

runner = CliRunner()
WHEN = "2018-03-05 10:00:00"  # the capture time (UTC) of the first placeholder
LATER = "2018-03-06 10:00:00"


def unix(when: str) -> float:
    return datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def make_file(path: Path, when: str = WHEN, size: int = 100) -> Path:
    """A file whose modification time is the capture time, as an original in a backup has."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (unix(when), unix(when)))
    return path


def placeholders(mac):
    """Source holds a saved photo and two shared placeholders (their Messages identifiers are at_0_2 and at_0_3)."""
    add_photos(mac.libraries["Source"], [
        ("2018-03-01 10:00:00", 100, "a.jpg"), (WHEN, 0, "one.heic", "shared"), (LATER, 0, "two.heic", "shared"),
    ])


def attachments(tmp_path, monkeypatch, **folders):
    """A fake Messages attachments folder: identifier -> file names, at <root>/aa/bb/<identifier>/."""
    root = tmp_path / "Messages" / "Attachments"
    for identifier, names in folders.items():
        folder = root / "aa" / "bb" / identifier
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).write_bytes(b"x" * 100)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rs, "messages_attachments", lambda: root)
    return root


def missing(*options, default="Source"):
    return runner.invoke(app, ["photos", "missing", *(["--library", default] if default else []), *options])


def recover(*options, default="Source"):
    """What `missing` lists for the default library, searched with `find` and these options; with --list FILE, find reads FILE."""
    options = list(options)
    if "--list" in options:
        at = options.index("--list")
        listing = options[at + 1]
        del options[at:at + 2]
    else:
        listed = missing("--format", "csv", default=default)
        if listed.exit_code:
            return listed
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        handle.write(listed.stdout)
        handle.close()
        listing = handle.name
    return runner.invoke(app, ["photos", "find", listing, *options])


def import_found(listing, *options, to="Source"):
    return runner.invoke(app, ["photos", "import", str(listing), *([to] if to else []), *options])


def found_list(tmp_path, *options):
    """What `find --format csv` finds with these options, saved as the list that import reads."""
    listing = tmp_path / "found.csv"
    listing.write_text(recover(*options, "--format", "csv").stdout)
    return listing


def rows_of(result):
    return list(csv.DictReader(io.StringIO(result.stdout)))


def opened(monkeypatch, *libraries):
    monkeypatch.setenv("FAKE_LSOF", "\n".join(f"{library}/database/Photos.sqlite" for library in libraries))


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    """The cache of indexed places goes to a folder of the test's own, never to the real one."""
    monkeypatch.setenv(cache_module.FILE_VARIABLE, str(tmp_path / "recover-cache" / "cache.sqlite"))


@pytest.fixture(autouse=True)
def photos_does_not_wait(monkeypatch):
    monkeypatch.setattr(recover_module, "OPEN_WAIT_SECONDS", 0)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("APPLE_PHOTOS_RECOVER_SEARCH", "APPLE_PHOTOS_RECOVER_MAPPINGS", "APPLE_PHOTOS_DEFAULT_LIBRARY", "DRY_RUN"):
        monkeypatch.delenv(name, raising=False)


# What to search ---------------------------------------------------------------------------------------------------------

def test_parse_search_reads_words_like_a_shell_and_keeps_the_order():
    assert rs.parse_search("messages folders=/a:/b") == [("messages", [rs.DEFAULT]), ("folders", ["/a", "/b"])]
    assert rs.parse_search("messages=default:/m folders='/a b':/c") == [("messages", [rs.DEFAULT, "/m"]), ("folders", ["/a b", "/c"])]
    assert rs.parse_search("folders=/a folders=/b") == [("folders", ["/a"]), ("folders", ["/b"])]
    assert rs.parse_search("") == []


def test_parse_search_rejects_what_it_cannot_read():
    for text, message in (("libraries=/a", "unknown place"), ("folders", "needs a path"), ("folders=", "names no path"),
                          ("folders=default", "needs a path"), ("folders=/a b", "unknown place"), ("folders='/a", "quotation")):
        with pytest.raises(ValueError) as error:
            rs.parse_search(text)
        assert message in str(error.value)


def test_build_sources_joins_the_same_kind_and_keeps_the_first_seen_order(tmp_path):
    sources = rs.build_sources(rs.parse_search("folders=/a messages folders=/b"), tmp_path / "d.photoslibrary", "ends")
    assert [source.name for source in sources] == ["folders", "messages"]
    assert [str(folder) for folder in sources[0].patterns] == ["/a", "/b"] and sources[0].snapshots == "ends"


def test_missing_lists_what_the_default_library_lacks_and_find_needs_a_place_to_search(mac, tmp_path):
    placeholders(mac)
    result = missing("--format", "csv")
    assert result.exit_code == 0 and "2 missing files" in result.output
    assert [(r["name"], r["shared"], r["identifier"]) for r in rows_of(result)] == [("one.heic", "true", "at_0_2"), ("two.heic", "true", "at_0_3")]
    assert missing(default=None).exit_code == 2
    nowhere = recover()
    assert nowhere.exit_code == 2 and "nothing to search" in nowhere.output


def test_the_search_variable_is_used_unless_an_option_names_a_place(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_SEARCH", f"folders={folder}")
    assert [r["source"] for r in rows_of(recover("--format", "csv"))] == ["folder", "none"]
    other = tmp_path / "other"
    other.mkdir()
    assert [r["source"] for r in rows_of(recover("--folders", str(other), "--format", "csv"))] == ["none", "none"]  # the option wins
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_SEARCH", "libraries=/x")
    bad = recover("--format", "csv")
    assert bad.exit_code == 2 and "APPLE_PHOTOS_RECOVER_SEARCH" in bad.output and "unknown place" in bad.output


def test_a_folder_that_does_not_exist_is_an_error_but_a_wildcard_matching_nothing_is_a_note(mac, tmp_path):
    placeholders(mac)
    gone = recover("--folders", str(tmp_path / "nope"))
    assert gone.exit_code == 1 and "Cannot search folders" in gone.output and "does not exist" in gone.output
    pattern = recover("--folders", str(tmp_path / "no*"), "--format", "csv")
    assert pattern.exit_code == 0 and "1 pattern matched no folder:" in pattern.output and "no*" in pattern.output


def test_find_needs_no_library_and_never_looks_one_up(mac, monkeypatch, tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    listing = tmp_path / "missing.csv"
    listing.write_text("date,name\n" + f"{WHEN},one.heic\n")
    monkeypatch.setattr("apple.photos.recover.find_library", lambda name: pytest.fail("the library must not be looked up"))
    monkeypatch.setenv("APPLE_PHOTOS_DEFAULT_LIBRARY", "Default Library")  # set on a NAS too, and must not be looked up
    assert runner.invoke(app, ["photos", "find", str(listing), "--folders", str(folder)]).exit_code == 0
    monkeypatch.delenv("APPLE_PHOTOS_DEFAULT_LIBRARY")
    needs = import_found(listing, to=None)
    assert needs.exit_code == 2 and "Missing argument" in needs.output


# Messages ----------------------------------------------------------------------------------------------------------------

def test_messages_finds_the_attachment_by_the_id_in_the_library(mac, monkeypatch, tmp_path):
    placeholders(mac)
    root = attachments(tmp_path, monkeypatch, at_0_2=["one.heic"])  # nothing for the other placeholder
    result = recover("--messages", "--format", "json")
    first, second = json.loads(result.stdout)
    assert first["source"] == "messages" and first["size"] == 100 and first["root"] == str(root)
    assert first["files"] == [str(root / "aa" / "bb" / "at_0_2" / "one.heic")] and first["library"] == ""
    assert second["source"] == "none" and second["files"] == [] and second["size"] is None


def test_messages_pairs_a_live_photo_and_ignores_helper_files(mac, monkeypatch, tmp_path):
    placeholders(mac)
    attachments(tmp_path, monkeypatch, at_0_2=["one.heic", "one.mov", "at_0_2.pvt"], at_0_3=["renamed.heic", "at_0_3.pvt"])
    first, second = json.loads(recover("--messages", "--format", "json").stdout)
    assert sorted(Path(f).name for f in first["files"]) == ["one.heic", "one.mov"]
    assert [Path(f).name for f in second["files"]] == ["renamed.heic"]  # the name changed, and the helper file sorts first


def test_messages_can_use_another_folder_and_a_missing_one_is_an_error(mac, monkeypatch, tmp_path):
    placeholders(mac)
    other = tmp_path / "other-attachments"
    (other / "aa" / "bb" / "at_0_2").mkdir(parents=True)
    (other / "aa" / "bb" / "at_0_2" / "one.heic").write_bytes(b"x")
    assert [r["source"] for r in rows_of(recover("--messages-folder", str(other), "--format", "csv"))] == ["messages", "none"]
    gone = recover("--messages-folder", str(tmp_path / "nope"))
    assert gone.exit_code == 1 and "Cannot search messages" in gone.output
    monkeypatch.setattr(rs, "messages_attachments", lambda: tmp_path / "no-messages")
    assert recover("--messages").exit_code == 1  # the default folder is missing, as on a NAS


# Folders: raw files ------------------------------------------------------------------------------------------------------

def test_folders_match_by_file_name_and_capture_time(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic", WHEN)            # right name, right time
    make_file(folder / "two.heic", "2018-03-06 11:30:00")  # right name, an hour and a half off: daylight saving
    make_file(folder / "sub" / "TWO.HEIC", "2019-01-01 00:00:00")  # the same name, another day
    first, second = json.loads(recover("--folders", str(folder), "--format", "json").stdout)
    assert (first["source"], first["root"], first["library"]) == ("folder", str(folder), "")
    assert second["source"] == "folder" and second["files"] == [str(folder / "two.heic")]
    (folder / "two.heic").unlink()  # replaced by a file more than two hours off (a new file, so the folder changes)
    make_file(folder / "two.heic", "2018-03-06 13:00:01")
    assert [r["source"] for r in rows_of(recover("--folders", str(folder), "--format", "csv"))] == ["folder", "none"]


def test_a_live_photo_s_video_is_listed_with_its_still(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    make_file(folder / "one.mov")
    make_file(folder / "other.heic")
    first = json.loads(recover("--folders", str(folder), "--format", "json").stdout)[0]
    assert sorted(Path(f).name for f in first["files"]) == ["one.heic", "one.mov"]


def test_folders_skip_what_a_disk_keeps_for_itself(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    for hidden in ("@eaDir/x", ".Spotlight-V100", "__MACOSX"):
        make_file(folder / hidden / "one.heic")
    make_file(folder / "real" / "._one.heic")  # an archive's resource fork
    make_file(folder / "#recycle" / "one.heic")  # a recycle bin is searched: a deleted original may be wanted
    first = json.loads(recover("--folders", str(folder), "--format", "json").stdout)[0]
    assert first["files"] == [str(folder / "#recycle" / "one.heic")]


def test_a_file_hard_linked_into_many_places_is_indexed_once(tmp_path):
    folder = tmp_path / "backup"
    make_file(folder / "snapshot1" / "one.heic")
    (folder / "snapshot2").mkdir()
    os.link(folder / "snapshot1" / "one.heic", folder / "snapshot2" / "one.heic")
    make_file(folder / "snapshot3" / "one.heic")  # a copy is another file
    source = rs.Folders([folder, folder], tmp_path / "d.photoslibrary")  # the same folder twice reads it once
    assert len(source.index()["one.heic"]) == 2


def test_several_folders_of_one_search_share_what_they_have_in_common(tmp_path):
    folder = tmp_path / "backup"
    make_file(folder / "a" / "one.heic")
    os.link(folder / "a" / "one.heic", folder / "b" / "one.heic") if (folder / "b").mkdir() is None else None
    source = rs.Folders([folder / "a", folder / "b"], tmp_path / "d.photoslibrary")
    assert len(source.index()["one.heic"]) == 1


# Folders: Photos libraries ----------------------------------------------------------------------------------------------

def other_library(tmp_path, items, name="other", folder="libraries"):
    library = make_library(tmp_path / folder, name, 19607)
    return add_photos(library, items)


def test_a_photos_library_under_a_folder_is_read_through_its_database(mac, tmp_path):
    placeholders(mac)
    library = other_library(tmp_path, [(WHEN, 500, "one.heic")])  # stored as UUID1.jpg: only the database knows the name
    first, second = json.loads(recover("--folders", str(tmp_path / "libraries"), "--format", "json").stdout)
    assert first["source"] == "library" and first["library"] == str(library) and first["root"] == str(tmp_path / "libraries")
    assert first["files"] == [str(library / "originals" / "A" / "UUID1.jpg")] and first["size"] == 1
    assert second["source"] == "none"


def test_a_library_with_no_original_stored_for_a_photo_does_not_count(mac, tmp_path):
    placeholders(mac)
    other_library(tmp_path, [(WHEN, 0, "one.heic")])  # a placeholder there too
    assert [r["source"] for r in rows_of(recover("--folders", str(tmp_path / "libraries"), "--format", "csv"))] == ["none", "none"]


def test_the_library_of_the_list_is_never_searched_and_a_shared_database_is_read_once(mac, tmp_path):
    first = other_library(tmp_path, [(WHEN, 500, "one.heic")], "first", "libraries")
    second = make_library(tmp_path / "copies", "second", 19607)
    shutil.rmtree(second / "database")
    os.makedirs(second / "database")
    os.link(first / "database" / "Photos.sqlite", second / "database" / "Photos.sqlite")  # the same file, as in two backups
    shutil.copytree(first / "originals", second / "originals")
    found = rs.Folders([tmp_path / "libraries", tmp_path / "copies"], first)  # first is the default library
    assert found.index() == {}
    found = rs.Folders([tmp_path / "libraries", tmp_path / "copies"], tmp_path / "somewhere-else.photoslibrary")
    assert len(found.index()["one.heic"]) == 1


def hollow(folder: Path, name: str, database=None, masters=False) -> Path:
    """A library folder: with database content (None for no database at all) and optionally a Masters folder holding one.heic."""
    library = folder / f"{name}.photoslibrary"
    library.mkdir(parents=True)
    if database is not None:
        (library / "database").mkdir()
        (library / "database" / "Photos.sqlite").write_bytes(database)
    if masters:
        make_file(library / "Masters" / "2018" / "one.heic")
    return library


def test_libraries_that_cannot_be_read_are_listed_one_to_a_line_grouped_by_reason(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "libraries"
    broken = hollow(folder, "broken", b"not a database")
    empty = hollow(folder, "empty", b"")
    also_empty = hollow(folder, "also-empty", b"")
    none = hollow(folder, "none")
    (none / "originals").mkdir()
    lines = recover("--folders", str(folder), "--format", "csv").output.splitlines()
    start = lines.index("4 libraries could not be read:")
    assert lines[start + 1:start + 9] == [
        "  [database read error: file is not a database]", f"  {broken}",
        "  [empty database]", f"  {also_empty}", f"  {empty}",
        "  [no database]", f"  {none}",
        lines[start + 8],  # the last line follows
    ]


def test_a_library_whose_database_cannot_be_read_is_searched_by_the_names_in_its_masters(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "libraries"
    library = hollow(folder, "old", b"not a database", masters=True)
    also = hollow(folder, "also-old", b"")  # an empty database too, and a Masters of its own
    make_file(also / "Masters" / "other.heic")
    result = recover("--folders", str(folder), "--format", "json")
    first = json.loads(result.stdout)[0]
    assert first["source"] == "folder" and first["library"] == str(library) and first["root"] == str(folder)
    assert first["files"] == [str(library / "Masters" / "2018" / "one.heic")]
    assert "could not be read" not in result.output  # there was something to examine
    assert "2 libraries read" not in result.output and "libraries read" not in result.output  # and none was read through its database


def test_the_libraries_read_count_is_for_libraries_read_through_their_database(mac, tmp_path):
    placeholders(mac)
    other_library(tmp_path, [(WHEN, 500, "one.heic")])
    hollow(tmp_path / "libraries", "broken", b"not a database")
    assert "  1 files, 1 library (" in recover("--folders", str(tmp_path / "libraries"), "--format", "csv").output


def test_an_old_library_that_keeps_names_is_searched_like_any_folder(mac, tmp_path):
    placeholders(mac)
    old = tmp_path / "iphoto" / "iPhoto Library"
    make_file(old / "Masters" / "2018" / "03" / "05" / "one.heic")
    first = json.loads(recover("--folders", str(tmp_path / "iphoto"), "--format", "json").stdout)[0]
    assert first["source"] == "folder" and first["files"][0].endswith("Masters/2018/03/05/one.heic")


def test_a_current_library_s_id_named_originals_are_not_walked_as_raw_files(mac, tmp_path):
    placeholders(mac)
    library = other_library(tmp_path, [(WHEN, 500, "unrelated.jpg")])
    source = rs.Folders([tmp_path / "libraries"], tmp_path / "somewhere-else.photoslibrary")
    assert list(source.index()) == ["unrelated.jpg"]  # by the database's name, not UUID1.jpg
    assert rs.is_current_library_originals(library / "originals") and not rs.is_current_library_originals(tmp_path / "originals")


# Wildcards ---------------------------------------------------------------------------------------------------------------

def tree(tmp_path):
    base = tmp_path / "ds"
    for folder in ("a/b/c/Pictures", "a/Pictures", "a/x/y/Pictures/z", "a/@eaDir/Pictures", "a/Lib.photoslibrary/Pictures", "a/b/Pictures"):
        (base / folder).mkdir(parents=True)
    return base


def relative(base, matches):
    return sorted(str(match.relative_to(base)) for match in matches)


def test_wildcards_name_several_folders(tmp_path):
    base = tree(tmp_path)
    assert relative(base, rs.expand(base / "a/*/Pictures")) == ["a/@eaDir/Pictures", "a/Lib.photoslibrary/Pictures", "a/b/Pictures"]
    assert relative(base, rs.expand(base / "a/?/Pictures")) == ["a/b/Pictures"]
    assert relative(base, rs.expand(base / "a/[bx]/Pictures")) == ["a/b/Pictures"]
    assert rs.expand(base / "a/nope*") == []


def test_double_star_is_any_number_of_folders_none_included(tmp_path):
    base = tree(tmp_path)
    assert relative(base, rs.expand(base / "a/**/Pictures")) == ["a/Pictures", "a/b/Pictures", "a/b/c/Pictures", "a/x/y/Pictures"]
    assert relative(base, rs.expand(base / "a/**/c")) == ["a/b/c"]
    assert relative(base, rs.expand(base / "**/y/Pictures")) == ["a/x/y/Pictures"]
    assert relative(base, rs.expand(base / "a/**/Pictures/z")) == ["a/x/y/Pictures/z"]
    assert relative(base, rs.expand(base / "a/**")) == ["a"]  # at the end it adds nothing: a search of a covers what is below


def test_double_star_does_not_go_into_library_packages_or_a_disk_s_own_folders(tmp_path):
    base = tree(tmp_path)
    assert not any("photoslibrary" in str(match) or "@eaDir" in str(match) for match in rs.expand(base / "a/**/Pictures"))


def test_a_path_that_exists_as_written_is_not_a_pattern(tmp_path):
    odd = tmp_path / "br[1]"
    odd.mkdir()
    assert rs.expand(odd) == [odd]
    assert rs.expand(tmp_path / "plain") == [tmp_path / "plain"]


def test_expanding_a_double_star_reports_how_far_it_is(tmp_path):
    base = tmp_path / "many"
    for number in range(450):
        (base / f"d{number}").mkdir(parents=True)
    said = []
    rs.expand(base / "**" / "Nope", said.append)
    assert said and "folders looked at so far" in said[0]


def test_a_wildcard_search_reads_every_folder_it_names(mac, tmp_path):
    placeholders(mac)
    for snapshot in ("2018-01-01-000000", "2018-02-01-000000"):
        make_file(tmp_path / "backup" / snapshot / "Pictures" / ("one.heic" if snapshot.startswith("2018-01") else "two.heic"), WHEN if snapshot.startswith("2018-01") else LATER)
    result = recover("--folders", str(tmp_path / "backup" / "*" / "Pictures"), "--format", "csv")
    assert [r["source"] for r in rows_of(result)] == ["folder", "folder"]
    assert "Expanding" in result.output and "2 folders" in result.output


# Time Machine -------------------------------------------------------------------------------------------------------------

Status = namedtuple("Status", "st_mode st_size st_nlink")
REGULAR = 0o100644


def test_a_directory_link_on_linux_is_an_empty_file_with_a_huge_link_count(tmp_path):
    volume = tmp_path / "disk"
    (volume / rs.HIDDEN_DIRECTORIES / "dir_123456").mkdir(parents=True)
    assert rs.directory_link_target(Status(REGULAR, 0, 123456), volume) == volume / rs.HIDDEN_DIRECTORIES / "dir_123456"
    assert rs.directory_link_target(Status(REGULAR, 0, 123457), volume) is None  # no such directory
    assert rs.directory_link_target(Status(REGULAR, 5, 123456), volume) is None  # a real file
    assert rs.directory_link_target(Status(REGULAR, 0, 1), volume) is None  # an ordinary empty file
    assert rs.volume_of(volume / "Backups.backupdb" / "x") == volume and rs.volume_of(tmp_path / "other") is None


def backupdb(tmp_path, snapshots=("2018-01-01-000000", "2018-06-01-000000", "2018-12-01-000000", "2019-03-01-000000")):
    """A Backups.backupdb with one machine and a different photo in each snapshot; returns the machine folder."""
    machine = tmp_path / "Backups.backupdb" / "Mac"
    for number, snapshot in enumerate(snapshots):
        make_file(machine / snapshot / "Macintosh HD" / "Users" / "t" / "Pictures" / f"p{number}.jpg", WHEN)
    (machine / "2019-03-01-000000.inProgress").mkdir()
    return machine


def indexed(source) -> list:
    return sorted(source.index())


def test_snapshots_all_reads_every_one_and_the_others_choose_which(tmp_path):
    machine = backupdb(tmp_path)
    default = tmp_path / "d.photoslibrary"
    assert indexed(rs.Folders([machine], default, "all")) == ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"]
    assert indexed(rs.Folders([machine], default, "ends")) == ["p0.jpg", "p3.jpg"]
    assert indexed(rs.Folders([machine], default, "newest")) == ["p3.jpg"]
    assert indexed(rs.Folders([machine], default, "oldest")) == ["p0.jpg"]
    assert indexed(rs.Folders([tmp_path / "Backups.backupdb"], default, "ends")) == ["p0.jpg", "p3.jpg"]  # a folder of machines


def test_an_unfinished_snapshot_is_never_chosen_and_the_skipped_ones_are_named(tmp_path):
    machine = backupdb(tmp_path)
    make_file(machine / "2019-03-01-000000.inProgress" / "Pictures" / "partial.jpg")
    said = []
    list(rs.walk(machine, snapshots="ends", notes=said.append))
    assert said and "2018-01-01-000000, 2019-03-01-000000" in said[0] and "2 others are skipped" in said[0]
    assert indexed(rs.Folders([machine], tmp_path / "d.photoslibrary", "all")).count("partial.jpg") == 1  # only "all" reads it


def test_snapshots_option_is_checked_and_set_from_the_command_line(mac, tmp_path):
    placeholders(mac)
    machine = backupdb(tmp_path)
    make_file(machine / "2018-06-01-000000" / "only-in-between.jpg", WHEN)
    wrong = recover("--folders", str(machine), "--snapshots", "sometimes")
    assert wrong.exit_code == 2 and "--snapshots" in wrong.output
    assert "reading the snapshots" not in recover("--folders", str(machine), "--format", "csv").output  # all is the default
    assert "reading the snapshots" in recover("--folders", str(machine), "--snapshots", "ends", "--format", "csv").output


def test_walk_reports_a_path_the_way_the_backup_shows_it(tmp_path):
    folder = tmp_path / "x"
    make_file(folder / "a" / "one.jpg")
    seen = {entry.logical for entry in rs.walk(folder) if not entry.is_directory}
    assert seen == {folder / "a" / "one.jpg"}


def test_walk_reads_each_directory_once_however_many_walks_share_the_memory(tmp_path):
    folder = tmp_path / "x"
    make_file(folder / "one.jpg")
    directories, files = set(), set()
    first = [e for e in rs.walk(folder, seen_directories=directories, seen_files=files) if not e.is_directory]
    second = [e for e in rs.walk(folder, seen_directories=directories, seen_files=files) if not e.is_directory]
    assert len(first) == 1 and second == []  # and the second walk returns instead of waiting for work that never comes


# Translating paths ---------------------------------------------------------------------------------------------------------

def test_the_first_translation_that_matches_in_the_order_given_wins():
    mapping = recover_module.parse_map(["/volume1/pictures=/Volumes/home/Pictures", "/volume1=/Volumes"])
    assert recover_module.translate("/volume1/pictures/a.jpg", mapping) == "/Volumes/home/Pictures/a.jpg"
    assert recover_module.translate("/volume1/icloud/b.jpg", mapping) == "/Volumes/icloud/b.jpg"
    reversed_order = recover_module.parse_map(["/volume1=/Volumes", "/volume1/pictures=/Volumes/home/Pictures"])
    assert recover_module.translate("/volume1/pictures/a.jpg", reversed_order) == "/Volumes/pictures/a.jpg"


def test_a_translation_matches_whole_folders_only_and_leaves_other_paths_alone():
    mapping = recover_module.parse_map(["/volume1=/Volumes"])
    for path in ("/volume10/c.jpg", "/volume2/d.jpg", "/other"):
        assert recover_module.translate(path, mapping) == path
    assert recover_module.translate("/volume1", mapping) == "/Volumes"


def test_a_path_that_exists_as_written_is_left_alone_so_one_set_of_translations_fits_every_machine(tmp_path):
    real = make_file(tmp_path / "here" / "a.jpg")
    mapping = recover_module.parse_map([f"{tmp_path}/here=/nowhere"])
    assert recover_module.translate(str(real), mapping) == str(real)  # the machine that made the list
    assert recover_module.translate(f"{tmp_path}/here/missing.jpg", mapping) == "/nowhere/missing.jpg"  # not there: translated


def test_translations_come_from_the_option_or_the_variable_and_must_be_old_equals_new(monkeypatch):
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_MAPPINGS", "/a=/b '/c d=/e f'")
    assert recover_module.parse_map(None) == [("/a", "/b"), ("/c d", "/e f")]
    assert recover_module.parse_map(["/x=/y"]) == [("/x", "/y")]  # an option replaces the variable
    import typer
    for bad in (["noequals"], ["=/y"], ["/x="]):
        with pytest.raises(typer.BadParameter):
            recover_module.parse_map(bad)
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_MAPPINGS", "bad")
    with pytest.raises(typer.BadParameter) as error:
        recover_module.parse_map(None)
    assert "APPLE_PHOTOS_RECOVER_MAPPINGS" in str(error.value)


# A list ---------------------------------------------------------------------------------------------------------------------

def write_list(path: Path, rows, header="date,name,hidden,shared,source,root,library,files"):
    path.write_text(header + "\n" + "\n".join(rows) + "\n")
    return path


def test_a_list_needs_a_name_and_a_date_and_is_csv_or_json(tmp_path):
    for text in ("name\none.heic\n", "not,a,list\n1,2,3\n", "[1, 2]"):
        listing = tmp_path / "bad"
        listing.write_text(text)
        result = recover("--list", str(listing), "--folders", str(tmp_path), default=None)
        assert result.exit_code == 2, text
    listing = tmp_path / "list.json"
    listing.write_text(json.dumps([{"date": WHEN, "name": "one.heic", "hidden": True, "shared": "true"}]))
    row = rows_of(recover("--list", str(listing), "--folders", str(tmp_path), "--format", "csv", default=None))[0]
    assert (row["name"], row["hidden"], row["shared"], row["source"]) == ("one.heic", "true", "true", "none")


def test_a_list_is_searched_for_without_the_library_and_can_come_from_stdin(tmp_path):
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    result = runner.invoke(app, ["photos", "find", "-", "--folders", str(folder), "--format", "csv"], input=f"date,name\n{WHEN},one.heic\n{LATER},two.heic\n")
    assert result.exit_code == 0 and [r["source"] for r in rows_of(result)] == ["folder", "none"]


def test_messages_are_found_by_the_identifier_a_list_carries(mac, monkeypatch, tmp_path):
    root = attachments(tmp_path, monkeypatch, at_0_2=["one.heic"])
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,at_0_2", f"{LATER},two.heic,false,true,"], header="date,name,hidden,shared,identifier")
    rows = rows_of(recover("--list", str(listing), "--messages", "--format", "csv", default=None))
    assert [(r["source"], r["files"]) for r in rows] == [("messages", str(root / "aa" / "bb" / "at_0_2" / "one.heic")), ("none", "")]


def test_a_list_row_that_says_where_its_files_are_is_used_as_it_is(tmp_path):
    original = make_file(tmp_path / "here" / "one.heic", size=321)
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,library,{tmp_path}/here,{tmp_path}/lib,{original}"])
    row = rows_of(import_found(listing, "--format", "csv", "--dry-run"))[0]
    assert (row["source"], row["root"], row["library"], row["size"], row["files"]) == ("library", f"{tmp_path}/here", f"{tmp_path}/lib", "321", str(original))


def test_paths_in_a_list_are_translated_and_those_that_cannot_be_reached_are_said_once(tmp_path):
    original = make_file(tmp_path / "mac" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [
        f"{WHEN},one.heic,false,false,folder,/nas/pics,,/nas/pics/one.heic",
        f"{LATER},two.heic,false,false,folder,/nas/other,,/nas/other/two.heic",
    ])
    result = import_found(listing, "--map", f"/nas/pics={tmp_path}/mac", "--format", "csv", "--dry-run")
    first, second = rows_of(result)
    assert (first["source"], first["files"]) == ("folder", str(original)) and second["source"] == "none"
    assert "cannot be reached from here: /nas/other (1 photo)" in result.output and "--map /nas/other=<path here>" in result.output


def test_a_list_is_limited_by_dates_and_media(tmp_path):
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,,,,,,", f"{LATER},two.mov,,,,,,", "2019-01-01 00:00:00,three.jpg,,,,,,"])
    def names(*options):
        return [r["name"] for r in rows_of(recover("--list", str(listing), *options, "--folders", str(tmp_path), "--format", "csv", default=None))]
    assert names() == ["one.heic", "two.mov", "three.jpg"]
    assert names("--since", "2018-03-06") == ["two.mov", "three.jpg"]
    assert names("--until", "2018-03-05") == ["one.heic"]
    assert names("--media", "videos") == ["two.mov"]
    assert recover("--list", str(listing), "--since", "2019-01-01", "--until", "2018-01-01", "--folders", str(tmp_path), default=None).exit_code == 2


# The report ---------------------------------------------------------------------------------------------------------------

def test_the_table_has_the_kind_of_place_and_csv_and_json_have_the_rest(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic", size=77)
    table = recover("--folders", str(folder))
    header, first, second = table.stdout.splitlines()
    assert header.split() == ["date", "size", "name", "hidden", "shared", "source"] and "folder" in first and "none" in second
    assert str(folder) not in table.stdout
    assert recover("--folders", str(folder), "--format", "csv").stdout.splitlines()[0] == "date,size,name,hidden,shared,source,root,library,files"
    assert set(json.loads(recover("--folders", str(folder), "--format", "json").stdout)[0]) == {"date", "size", "name", "hidden", "shared", "source", "root", "library", "files"}


def test_the_last_line_says_what_was_done(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])
    assert "0 missing files" in missing().output
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    assert "Found 1 of 2 missing files" in recover("--folders", str(folder)).output and "[list only]" not in recover("--folders", str(folder)).output
    listing = found_list(tmp_path, "--folders", str(folder))
    assert "Imported 0 of 2 missing files; 0 errors, 2 skipped [dry run]" in import_found(listing, "--dry-run").output
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "[dry run]" not in import_found(found_list(tmp_path, "--folders", str(empty))).output


def test_range_and_media_limit_what_is_looked_at(mac, tmp_path):
    placeholders(mac)
    assert "Found 0 of 1 missing files" in recover("--since", "2018-03-06", "--folders", str(tmp_path)).output
    assert "Found 0 of 0 missing files" in recover("--media", "videos", "--folders", str(tmp_path)).output
    assert recover("--since", "2018-03-07", "--until", "2018-03-01", "--folders", str(tmp_path)).exit_code == 2


# Importing -----------------------------------------------------------------------------------------------------------------

def found_folder(tmp_path):
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    make_file(folder / "two.heic", LATER)
    return folder


def test_dry_run_never_imports(mac, tmp_path):
    placeholders(mac)
    listing = found_list(tmp_path, "--folders", str(found_folder(tmp_path)))
    for options, env in ((["--dry-run"], {}), ([], {"DRY_RUN": "true"})):
        result = runner.invoke(app, ["photos", "import", str(listing), "Source", *options], env=env)
        assert result.exit_code == 0 and "[dry run]" in result.output
    assert mac.calls("import-photos.applescript") == []


def test_import_goes_into_the_library_photos_has_open(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    opened(monkeypatch, mac.libraries["Source"], "/Users/x/Library/Photos/Libraries/Syndication.photoslibrary")
    result = import_found(found_list(tmp_path, "--folders", str(folder)))
    assert result.exit_code == 0 and "Imported 2 of 2 missing files; 0 errors, 0 skipped" in result.output
    assert "[dry run]" not in result.output and "Importing 2 photos into Source" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(folder / "one.heic"), str(folder / "two.heic")]]]


def test_import_refuses_unless_photos_has_the_default_library_open(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    monkeypatch.setenv("FAKE_LSOF", "")
    stopped = import_found(found_list(tmp_path, "--folders", str(folder)))
    assert stopped.exit_code == 1 and "Photos did not open" in stopped.output and "Photos is not running" in stopped.output
    opened(monkeypatch, mac.libraries["photos-2018"], "/Users/x/Library/Photos/Libraries/Syndication.photoslibrary")
    other = import_found(found_list(tmp_path, "--folders", str(folder)))
    assert other.exit_code == 1 and "Photos did not open" in other.output and "photos-2018.photoslibrary" in other.output
    assert mac.calls("import-photos.applescript") == []


def test_a_failed_import_is_an_error_and_exits_1(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    opened(monkeypatch, mac.libraries["Source"])
    monkeypatch.setenv("FAKE_IMPORT_ERROR", "Photos got an error")
    result = import_found(found_list(tmp_path, "--folders", str(folder)))
    assert result.exit_code == 1 and "Photos failed to import" in result.output
    assert "Imported 0 of 2 missing files; 1 error, 1 skipped" in result.output


def test_photos_photos_already_had_are_not_missing(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    opened(monkeypatch, mac.libraries["Source"])
    monkeypatch.setenv("FAKE_IMPORT_COUNT", "1")  # Photos took one of the two: it already had the other
    assert "Imported 1 of 1 missing files; 0 errors, 0 skipped" in import_found(found_list(tmp_path, "--folders", str(folder))).output


def test_a_list_can_be_imported_from_the_paths_it_names(mac, monkeypatch, tmp_path):
    original = make_file(tmp_path / "mac" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,folder,/nas,,/nas/one.heic"])
    opened(monkeypatch, mac.libraries["Source"])
    result = import_found(listing, "--map", f"/nas={tmp_path}/mac")
    assert result.exit_code == 0 and "Imported 1 of 1 missing files" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(original)]]]


def test_the_photos_app_s_libraries_are_read_from_lsof_and_syndication_is_left_out(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LSOF", "/a/Photos Library.photoslibrary/database/Photos.sqlite\n/a/Photos Library.photoslibrary/database/Photos.sqlite-shm\n/b/Syndication.photoslibrary/database/Photos.sqlite\n/c/other.photoslibrary/database/Photos.sqlite")
    assert pp.libraries_open_in_photos() == [Path("/a/Photos Library.photoslibrary"), Path("/c/other.photoslibrary")]
    monkeypatch.setenv("FAKE_LSOF", "")
    assert pp.libraries_open_in_photos() == []


def test_the_import_script_avoids_applescript_s_own_words():
    script = (pp.APPLESCRIPTS / "import-photos.applescript").read_text()
    assert "skip check duplicates false" in script and "set files" not in script and "set paths" not in script


# Progress ----------------------------------------------------------------------------------------------------------------

class Stream(io.StringIO):
    def __init__(self, terminal):
        super().__init__()
        self.terminal = terminal

    def isatty(self):
        return self.terminal


def test_progress_rewrites_one_line_on_a_terminal_and_is_throttled_elsewhere(monkeypatch):
    ticks = iter([100.0, 100.1, 101.0, 101.1, 130.0, 130.1])  # a real clock is large, so the first status is never too soon
    monkeypatch.setattr(progress_module.time, "monotonic", lambda: next(ticks))
    terminal = Stream(True)
    shown = progress_module.Progress(terminal)
    shown.update("first")   # written
    shown.update("second")  # a tenth of a second later: too soon
    shown.line("kept")      # wipes the status and keeps a line
    assert terminal.getvalue().startswith("\rfirst") and terminal.getvalue().endswith("kept\n") and "second" not in terminal.getvalue()
    log = Stream(False)
    quiet = progress_module.Progress(log, interval=15.0)
    quiet.update("a")  # first
    quiet.update("b")  # more than 15 s later
    quiet.update("c")  # a tenth of a second later: too soon
    assert log.getvalue() == "a\nb\n"


def test_durations_are_short():
    assert [progress_module.duration(s) for s in (0, 12, 59.6, 185, 3725)] == ["0s", "12s", "1m 00s", "3m 05s", "1h 02m"]


# The cache ---------------------------------------------------------------------------------------------------------------

def test_the_cache_keeps_values_and_says_when_and_ignores_what_it_cannot_use(tmp_path):
    cache = cache_module.Cache(tmp_path / "c" / "index.sqlite")
    assert cache.get("k") is None
    cache.put("k", {"a": [1, "two"]})
    value, saved = cache.get("k")
    assert value == {"a": [1, "two"]} and abs(saved - time.time()) < 60
    assert cache_module.Cache(tmp_path / "c" / "index.sqlite", read=False).get("k") is None  # a refresh does not use it
    cache.put("k", "newer")
    assert cache.get("k")[0] == "newer"
    assert cache_module.fingerprint("a", 1) != cache_module.fingerprint("a", 2)


def test_a_cache_that_cannot_be_used_is_only_an_empty_cache(tmp_path):
    (tmp_path / "file").write_text("x")
    broken = cache_module.Cache(tmp_path / "file" / "index.sqlite")  # a folder cannot be made below a file
    broken.put("k", 1)
    assert broken.get("k") is None
    damaged = tmp_path / "damaged.sqlite"
    damaged.write_bytes(b"not a database")
    cache = cache_module.Cache(damaged)
    cache.put("k", 1)
    assert cache.get("k") is None


def count_database_reads(monkeypatch):
    reads = []
    real = rs.sqlite3.connect
    monkeypatch.setattr(rs.sqlite3, "connect", lambda *args, **kwargs: (reads.append(args[0]) if str(args[0]).startswith("file:") else None) or real(*args, **kwargs))  # the cache's own file is not counted
    return reads


def test_a_library_database_is_read_again_only_when_it_changes(tmp_path, monkeypatch):
    library = other_library(tmp_path, [(WHEN, 500, "one.heic")])
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    reads = count_database_reads(monkeypatch)
    first = list(rs.database_originals(library, library, cache))
    assert len(first) == 1 and len(reads) == 1
    assert list(rs.database_originals(library, library, cache)) == first and len(reads) == 1  # from the cache
    database = library / "database" / "Photos.sqlite"
    later = database.stat().st_mtime + 10
    os.utime(database, (later, later))  # the database was written to
    list(rs.database_originals(library, library, cache))
    assert len(reads) == 2


def test_a_cached_library_is_found_under_the_path_it_is_read_from_now(tmp_path):
    library = other_library(tmp_path, [(WHEN, 500, "one.heic")])
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    list(rs.database_originals(library, library, cache))
    elsewhere = Path("/mounted/somewhere/other.photoslibrary")
    (name, taken, real, logical), = rs.database_originals(library, elsewhere, cache)
    assert real == library / "originals" / "A" / "UUID1.jpg" and logical == elsewhere / "originals" / "A" / "UUID1.jpg"


def run_folders(roots, tmp_path, cache, monkeypatch=None, snapshots="all"):
    source = rs.Folders(roots, tmp_path / "d.photoslibrary", snapshots, cache)
    source.index()
    return source


def count_walks(monkeypatch):
    walks = []
    real = rs.walk
    monkeypatch.setattr(rs, "walk", lambda root, *args, **kwargs: walks.append(root) or real(root, *args, **kwargs))
    return walks


def test_backup_folders_are_read_once_and_reused(tmp_path, monkeypatch):
    machine = backupdb(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    walks = count_walks(monkeypatch)
    first = run_folders([machine], tmp_path, cache)
    assert walks and sorted(first.index()) == ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"]
    walks.clear()
    said = io.StringIO()
    second = rs.Folders([machine], tmp_path / "d.photoslibrary", "all", cache)
    second.progress = progress_module.Progress(said)
    second.index()
    assert walks == []
    shown = said.getvalue()
    heading, result = shown.splitlines()  # a search like any other: what was searched, then what was in it
    assert heading == f"Searching {machine}" and result.startswith("  4 files (") and re.search(r"\) \[cached \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\]$", result)
    assert "Reusing" not in shown
    assert sorted(second.index()) == ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"]
    found = second.index()["p0.jpg"][0]
    assert found[1].name == "p0.jpg" and isinstance(found[1], Path) and found[2].parts[-1] == "p0.jpg"  # real and logical paths are Paths again


def test_a_new_snapshot_a_removed_one_or_another_setting_means_a_new_index(tmp_path, monkeypatch):
    machine = backupdb(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([machine], tmp_path, cache)
    walks = count_walks(monkeypatch)
    make_file(machine / "2019-06-01-000000" / "Pictures" / "new.jpg")
    assert "new.jpg" in run_folders([machine], tmp_path, cache).index() and walks  # a new snapshot
    walks.clear()
    run_folders([machine], tmp_path, cache)
    assert walks == []  # and now that one is saved
    shutil.rmtree(machine / "2018-06-01-000000")
    assert "p1.jpg" not in run_folders([machine], tmp_path, cache).index() and walks  # a removed snapshot
    walks.clear()
    run_folders([machine], tmp_path, cache, snapshots="ends")
    assert walks  # another snapshots setting reads what it needs


def test_a_snapshot_still_being_written_is_never_cached(tmp_path, monkeypatch):
    machine = backupdb(tmp_path)
    unfinished = machine / "2019-04-01-000000.inProgress" / "Pictures"
    make_file(unfinished / "partial.jpg")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    walks = count_walks(monkeypatch)
    run_folders([unfinished], tmp_path, cache)
    run_folders([unfinished], tmp_path, cache)
    assert len(walks) == 2  # read both times


def test_ordinary_folders_are_read_every_time(tmp_path, monkeypatch):
    folder = tmp_path / "plain"
    make_file(folder / "one.jpg")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    walks = count_walks(monkeypatch)
    run_folders([folder], tmp_path, cache)
    make_file(folder / "two.jpg")
    assert sorted(run_folders([folder], tmp_path, cache).index()) == ["one.jpg", "two.jpg"] and len(walks) == 2


def test_backups_and_other_folders_in_one_search_are_each_handled_their_own_way(tmp_path, monkeypatch):
    machine = backupdb(tmp_path)
    plain = tmp_path / "plain"
    make_file(plain / "one.jpg")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([machine, plain], tmp_path, cache)
    walks = count_walks(monkeypatch)
    make_file(plain / "two.jpg")
    again = run_folders([machine, plain], tmp_path, cache)
    assert sorted(again.index()) == ["one.jpg", "p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg", "two.jpg"]
    assert walks == [plain]  # only the plain folder was read


def test_libraries_that_could_not_be_read_are_remembered_with_the_backups(tmp_path):
    machine = tmp_path / "Backups.backupdb" / "Mac"
    snapshot = machine / "2018-01-01-000000" / "Pictures"
    hollow(snapshot, "empty", b"")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    first = run_folders([machine], tmp_path, cache)
    again = rs.Folders([machine], tmp_path / "d.photoslibrary", "all", cache)
    again.index()
    assert first.unreadable == again.unreadable != [] and again.unreadable[0][1] == "empty database"


def test_refresh_reads_again_and_saves(mac, tmp_path, monkeypatch):
    placeholders(mac)
    machine = backupdb(tmp_path)
    make_file(machine / "2018-01-01-000000" / "Pictures" / "one.heic")
    walks = count_walks(monkeypatch)
    recover("--folders", str(machine), "--format", "csv")
    first = len(walks)
    recover("--folders", str(machine), "--format", "csv")
    assert len(walks) == first  # reused
    again = recover("--folders", str(machine), "--refresh", "--format", "csv")
    assert len(walks) > first and "Reusing" not in again.output
    walks_after = len(walks)
    assert "[cached 20" in recover("--folders", str(machine), "--format", "csv").output and len(walks) == walks_after


def test_a_cached_backup_and_a_folder_with_a_photos_library_in_one_search(mac, tmp_path):
    placeholders(mac)
    machine = backupdb(tmp_path)
    library = other_library(tmp_path, [(WHEN, 500, "one.heic")])
    options = ("--folders", str(machine), "--folders", str(tmp_path / "libraries"), "--format", "json")
    first = recover(*options)
    second = recover(*options)  # the backup now comes from the cache; the library is read after it
    assert second.exit_code == 0 and "Traceback" not in second.output
    assert json.loads(second.stdout) == json.loads(first.stdout)
    assert json.loads(second.stdout)[0]["library"] == str(library) and "[cached 20" in second.output


def test_each_path_given_is_one_search_with_one_result_line_cached_or_not(tmp_path):
    machine = tmp_path / "Backups.backupdb" / "Mac"
    other_library(machine / "2018-01-01-000000", [(WHEN, 500, "one.heic")], "inside", "Pictures")
    make_file(machine / "2018-06-01-000000" / "Pictures" / "two.jpg")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    pattern = machine / "*" / "Pictures"
    plain = tmp_path / "plain"
    make_file(plain / "three.jpg")

    def lines(roots):
        said = io.StringIO()
        source = rs.Folders(roots, tmp_path / "d.photoslibrary", "all", cache)
        source.progress = progress_module.Progress(said)
        source.index()
        return said.getvalue().splitlines()

    for run in ("read", "cached"):
        shown = lines([pattern, plain])
        assert [line for line in shown if line.startswith("Searching")] == [f"Searching {pattern}", f"Searching {plain}"], run  # not one for each folder the pattern names
        result = shown[shown.index(f"Searching {pattern}") + 1]
        assert result.startswith("  2 files, 1 library (") and ("[cached " in result) == (run == "cached"), run  # and no "read" after libraries
        assert "read" not in result.replace("[cached", "")
    assert shown[-1].startswith("Indexed 3 files in ")  # the total over all the searches


def test_saved_times_are_iso_8601_without_a_zone():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", rs.iso(1_700_000_000.4))


# The cache of ordinary folders ----------------------------------------------------------------------------------------

def count_listings(monkeypatch):
    listed = []
    real = rs.os.scandir
    monkeypatch.setattr(rs.os, "scandir", lambda path: listed.append(Path(path)) or real(path))
    return listed


def folder_tree(tmp_path):
    base = tmp_path / "pics"
    make_file(base / "one.jpg")
    make_file(base / "a" / "two.jpg")
    make_file(base / "a" / "b" / "three.jpg")
    make_file(base / "c" / "four.jpg")
    return base


def index_names(source) -> list:
    return sorted(source.index())


def test_an_unchanged_folder_is_not_listed_again(tmp_path, monkeypatch):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    listed = count_listings(monkeypatch)
    assert index_names(run_folders([base], tmp_path, cache)) == ["four.jpg", "one.jpg", "three.jpg", "two.jpg"]
    assert len(listed) == 4  # base, a, a/b, c
    listed.clear()
    again = run_folders([base], tmp_path, cache)
    assert index_names(again) == ["four.jpg", "one.jpg", "three.jpg", "two.jpg"] and listed == []
    found = again.index()["three.jpg"][0]
    assert found[1] == base / "a" / "b" / "three.jpg" and found[0] == unix(WHEN)  # the same file and capture time as before


def test_only_the_directories_that_changed_are_listed_again(tmp_path, monkeypatch):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([base], tmp_path, cache)
    listed = count_listings(monkeypatch)
    make_file(base / "a" / "b" / "five.jpg")  # a new file changes its directory, a/b, and nothing above it
    (base / "c" / "four.jpg").unlink()        # a removed one changes c
    (base / "new").mkdir()                    # a new directory changes the base
    make_file(base / "new" / "six.jpg")
    again = run_folders([base], tmp_path, cache)
    assert index_names(again) == ["five.jpg", "one.jpg", "six.jpg", "three.jpg", "two.jpg"]
    assert sorted(path.relative_to(base).as_posix() for path in listed) == [".", "a/b", "c", "new"]  # a was not listed


def test_what_is_said_about_a_folder_that_came_from_the_cache(tmp_path, monkeypatch):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([base], tmp_path, cache)
    make_file(base / "a" / "b" / "five.jpg")
    said = io.StringIO()
    again = rs.Folders([base], tmp_path / "d.photoslibrary", "all", cache)
    again.progress = progress_module.Progress(said)
    again.index()
    assert f"Searching {base}" in said.getvalue()
    assert "5 files (" in said.getvalue() and "[cached 20" in said.getvalue() and "; 1 of 4 folders read again]" in said.getvalue()
    quiet = io.StringIO()
    third = rs.Folders([base], tmp_path / "d.photoslibrary", "all", cache)
    third.progress = progress_module.Progress(quiet)
    third.index()
    assert "folders read again" not in quiet.getvalue() and "[cached 20" in quiet.getvalue()


def test_a_file_changed_in_place_is_not_noticed_until_a_refresh(tmp_path):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([base], tmp_path, cache)
    make_file(base / "one.jpg", "2019-01-01 00:00:00")  # the same file, written over: its folder does not change
    assert run_folders([base], tmp_path, cache).index()["one.jpg"][0][0] == unix(WHEN)
    refreshed = run_folders([base], tmp_path, cache_module.Cache(tmp_path / "c.sqlite", read=False))
    assert refreshed.index()["one.jpg"][0][0] == unix("2019-01-01 00:00:00")
    assert run_folders([base], tmp_path, cache).index()["one.jpg"][0][0] == unix("2019-01-01 00:00:00")  # and it was saved


def test_ordinary_folders_are_cached_separately_and_backups_are_not_listed_by_directory(tmp_path, monkeypatch):
    first, second = folder_tree(tmp_path), tmp_path / "other"
    make_file(second / "x.jpg")
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([first, second], tmp_path, cache)
    listed = count_listings(monkeypatch)
    run_folders([first, second], tmp_path, cache)
    assert listed == []
    machine = backupdb(tmp_path / "bk")
    run_folders([machine], tmp_path, cache)
    keys = [row[0] for row in cache.db().execute("select key from entries")]
    assert len(keys) == 3  # two ordinary folders and one group of backups


def test_a_folder_moved_aside_and_back_is_read_again_as_a_different_path(tmp_path, monkeypatch):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([base], tmp_path, cache)
    moved = tmp_path / "moved"
    base.rename(moved)
    listed = count_listings(monkeypatch)
    assert index_names(run_folders([moved], tmp_path, cache)) == ["four.jpg", "one.jpg", "three.jpg", "two.jpg"] and len(listed) == 4


def test_the_same_files_reached_by_two_cached_folders_are_still_indexed_once(tmp_path):
    base = folder_tree(tmp_path)
    cache = cache_module.Cache(tmp_path / "c.sqlite")
    run_folders([base, base / "a"], tmp_path, cache)
    again = run_folders([base, base / "a"], tmp_path, cache)
    assert len(again.index()["two.jpg"]) == 1


def test_the_cache_does_not_change_what_a_search_finds(mac, tmp_path):
    placeholders(mac)
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    make_file(folder / "deeper" / "two.heic", LATER)
    reports = [recover("--folders", str(folder), "--format", "json").stdout for _ in range(3)]
    assert reports[0] == reports[1] == reports[2] and [r["source"] for r in json.loads(reports[0])] == ["folder", "folder"]
    refreshed = recover("--folders", str(folder), "--refresh", "--format", "json")
    assert refreshed.stdout == reports[0] and "[cached" not in refreshed.output


def test_importing_from_a_list_with_nothing_to_search_is_a_plain_import(mac, monkeypatch, tmp_path):
    original = make_file(tmp_path / "mac" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,folder,/nas,,/nas/one.heic", f"{LATER},two.heic,false,true,none,,,"])
    opened(monkeypatch, mac.libraries["Source"])
    imported = import_found(listing, "--map", f"/nas={tmp_path}/mac")
    assert imported.exit_code == 0 and "Imported 1 of 2 missing files; 0 errors, 1 skipped" in imported.output
    assert "[list only]" not in imported.output and mac.calls("import-photos.applescript") == [[[str(original)]]]
    report = import_found(listing, "--map", f"/nas={tmp_path}/mac", "--dry-run")
    assert "[dry run]" in report.output and "[list only]" not in report.output  # something is known, and --dry-run keeps it from being imported
    nothing = write_list(tmp_path / "n.csv", [f"{WHEN},one.heic,false,true,none,,,"])
    assert "[dry run]" not in import_found(nothing).output  # nothing known, nothing to import


def test_the_places_to_search_are_translated_like_the_paths_in_a_list(mac, tmp_path, monkeypatch):
    found = make_file(tmp_path / "mac" / "backups" / "2020" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,false,none,,,"])
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_SEARCH", "folders=/nas/backups folders=/nas/*/2020")
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_MAPPINGS", f"/nas={tmp_path}/mac")
    result = recover("--list", str(listing), "--format", "csv", default=None)
    assert result.exit_code == 0 and rows_of(result)[0]["files"] == str(found)


def test_a_place_to_search_that_does_not_exist_here_is_an_error(mac, tmp_path):
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,false,,,,"])
    result = recover("--list", str(listing), "--folders", str(tmp_path / "gone"), default=None)
    assert result.exit_code != 0 and "does not exist" in result.output


def test_importing_a_list_searches_nothing(mac, tmp_path, monkeypatch):
    make_file(tmp_path / "mac" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,false,folder,/nas,,/nas/one.heic"])
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_SEARCH", f"folders={tmp_path}/mac")
    monkeypatch.setattr(recover_module, "build_sources", lambda *a, **k: pytest.fail("nothing is searched"))
    result = import_found(listing, "--map", f"/nas={tmp_path}/mac", "--dry-run")
    assert result.exit_code == 0 and "Imported 0 of 1 missing files" in result.output


# Permission errors --------------------------------------------------------------------------------------------------------

needs_a_user = pytest.mark.skipif(os.geteuid() == 0, reason="root can read what is locked")


@pytest.fixture
def locked(tmp_path):
    """Makes paths unreadable (and restores them for the cleanup)."""
    paths = []

    def lock(path: Path) -> Path:
        path.chmod(0)
        paths.append(path)
        return path

    yield lock
    for path in paths:
        path.chmod(0o755)


@needs_a_user
def test_a_folder_that_cannot_be_listed_is_said_and_the_rest_is_searched(tmp_path, locked):
    make_file(tmp_path / "search" / "ok" / "one.heic")
    (tmp_path / "search" / "private").mkdir()
    locked(tmp_path / "search" / "private")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,"], header="date,name,hidden,shared,identifier")
    for _ in range(2):  # the second time the rest of the folder is cached, and the locked one is said again
        result = runner.invoke(app, ["photos", "find", str(listing), "--folders", str(tmp_path / "search"), "--format", "csv"])
        assert rows_of(result)[0]["source"] == "folder"
        assert "1 folder could not be searched (permission denied):" in result.output and str(tmp_path / "search" / "private") in result.output


@needs_a_user
def test_messages_attachments_that_cannot_be_read_are_an_error_naming_full_disk_access(tmp_path, locked, monkeypatch):
    folder = tmp_path / "Attachments"
    folder.mkdir()
    monkeypatch.setattr(rs, "messages_attachments", lambda: folder)  # Messages' own folder, which a Terminal without Full Disk Access cannot read
    locked(folder)
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,at_1"], header="date,name,hidden,shared,identifier")
    result = runner.invoke(app, ["photos", "find", str(listing), "--messages"])
    words = " ".join(result.output.replace("│", " ").split())  # the error is wrapped in a box
    assert result.exit_code != 0 and "permission denied" in words and "Full Disk Access" in words


@needs_a_user
def test_a_list_that_cannot_be_read_is_an_error_not_a_traceback(tmp_path, locked):
    listing = locked(write_list(tmp_path / "l.csv", [f"{WHEN},one.heic"], header="date,name"))
    for command in (["find", str(listing), "--folders", str(tmp_path)], ["import", str(listing), "Source", "--dry-run"]):
        result = runner.invoke(app, ["photos", *command])
        assert result.exit_code == 1 and "Cannot read" in result.output and "Traceback" not in result.output


@needs_a_user
def test_a_file_that_cannot_be_read_is_an_error_and_the_others_are_still_looked_at(tmp_path, locked):
    ok = make_file(tmp_path / "ok" / "one.heic")
    secret = make_file(tmp_path / "private" / "two.heic", LATER)
    locked(tmp_path / "private")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,folder,{tmp_path}/ok,,{ok}", f"{LATER},two.heic,false,true,folder,{tmp_path}/private,,{secret}"])
    result = import_found(listing, "--format", "csv", "--dry-run")
    assert result.exit_code == 1 and "1 file cannot be read (permission denied)" in result.output and str(secret) in result.output
    assert [r["source"] for r in rows_of(result)] == ["folder", "none"] and "1 error" in result.output


def test_a_place_with_wildcards_that_exists_as_written_is_not_translated(tmp_path, monkeypatch):
    """The settings are shared: on the machine the places were written for, the translations to another machine's paths must not apply."""
    make_file(tmp_path / "usb" / "Backups.backupdb" / "mac" / "2020" / "Pictures" / "one.heic")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},one.heic,false,true,"], header="date,name,hidden,shared,identifier")
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_SEARCH", f"folders='{tmp_path}/usb/Backups.backupdb/*/*/Pictures'")
    monkeypatch.setenv("APPLE_PHOTOS_RECOVER_MAPPINGS", f"{tmp_path}/usb=/Volumes/elsewhere")
    result = runner.invoke(app, ["photos", "find", str(listing), "--format", "csv"])
    assert result.exit_code == 0 and "matched no folder" not in result.output and rows_of(result)[0]["source"] == "folder"


def test_a_3g2_video_is_searched_for_by_name_and_time(tmp_path):
    make_file(tmp_path / "backup" / "clip.3g2")
    listing = write_list(tmp_path / "l.csv", [f"{WHEN},clip.3g2,false,false,"], header="date,name,hidden,shared,identifier")
    result = runner.invoke(app, ["photos", "find", str(listing), "--folders", str(tmp_path / "backup"), "--format", "csv"])
    assert rows_of(result)[0]["source"] == "folder"


def test_a_cached_directory_listing_keeps_each_files_size(tmp_path):
    make_file(tmp_path / "pics" / "a.jpg", size=321)
    make_file(tmp_path / "pics" / "sub" / "b.mov", LATER, size=77)
    remember = {}
    first = {entry.real.name: entry.status.st_size for entry in rs.walk(tmp_path / "pics", remember=remember) if not entry.is_directory}
    assert first == {"a.jpg": 321, "b.mov": 77}
    counts = {}
    cached = {entry.real.name: (entry.status.st_size, entry.status.st_mtime) for entry in rs.walk(tmp_path / "pics", listings=remember, remember={}, counts=counts) if not entry.is_directory}
    assert counts == {"reused": 2} and cached == {"a.jpg": (321, unix(WHEN)), "b.mov": (77, unix(LATER))}  # from the cache: the sizes are still there
    assert cache_module.VERSION >= 2  # an entry saved before sizes were kept is never used


def test_import_can_go_into_a_library_that_is_not_the_default_when_photos_has_it_open(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    listing = found_list(tmp_path, "--folders", str(folder))
    opened(monkeypatch, mac.libraries["photos-2018"])
    result = import_found(listing, to="photos-2018")  # the default, Source, is not the one
    assert result.exit_code == 0 and "Importing 2 photos into photos-2018" in result.output and "Imported 2 of 2 missing files" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(folder / "one.heic"), str(folder / "two.heic")]]]


def test_import_into_another_library_refuses_while_photos_has_a_different_one_open(mac, monkeypatch, tmp_path):
    placeholders(mac)
    listing = found_list(tmp_path, "--folders", str(found_folder(tmp_path)))
    opened(monkeypatch, mac.libraries["Source"])  # the default is open, but the one asked for is not
    result = import_found(listing, to="photos-2018")
    words = " ".join(result.output.replace("│", " ").split())
    assert result.exit_code == 1 and "Photos did not open" in words and "photos-2018.photoslibrary" in words and "hold Option while starting Photos" in words
    assert mac.calls("import-photos.applescript") == []


def test_import_into_a_library_given_by_path_is_not_looked_up(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    listing = found_list(tmp_path, "--folders", str(folder))
    opened(monkeypatch, mac.libraries["photos-2019"])
    result = import_found(listing, to=str(mac.libraries["photos-2019"]))
    assert result.exit_code == 0 and "Importing 2 photos into" in result.output and len(mac.calls("import-photos.applescript")) == 1


def test_import_needs_a_destination(mac, tmp_path):
    placeholders(mac)
    listing = found_list(tmp_path, "--folders", str(found_folder(tmp_path)))
    result = import_found(listing, to=None)
    assert result.exit_code == 2 and "Missing argument" in result.output



def test_import_of_a_list_that_says_nothing_about_originals_is_told_to_run_find_first(mac, tmp_path):
    listing = tmp_path / "diff.csv"
    listing.write_text("side,date,size,name\n<,2020-01-01 18:42:17,100,one.heic\n<,2020-01-02 18:42:17,100,two.heic\n")  # as `apple photos diff --format csv` writes it
    result = import_found(listing, to="photos-2018")
    words = " ".join(result.output.replace("│", " ").split())
    assert result.exit_code == 2 and "lists photos but not where their originals are" in words and "apple photos find" in words and f"found.csv" in words
    assert mac.calls("import-photos.applescript") == []
    missing = tmp_path / "missing.csv"
    missing.write_text("date,name,hidden,shared,identifier\n2020-01-01 18:42:17,one.heic,false,false,\n")  # as `apple photos missing` writes it
    assert import_found(missing).exit_code == 2


def test_import_of_a_list_whose_originals_were_all_searched_for_and_not_found_says_so(mac, tmp_path):
    listing = write_list(tmp_path / "found.csv", [f"{WHEN},one.heic,false,false,none,,,", f"{LATER},two.heic,false,false,none,,,"])  # find ran and found nothing
    result = import_found(listing)
    assert result.exit_code == 0 and "None of the 2 photos in" in result.output and "nothing to import" in result.output
    assert "Imported 0 of" not in result.output


def test_what_a_library_lacks_of_a_folder_is_imported_from_the_diff_of_the_two(mac, monkeypatch, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    make_file(folder / "sub" / "two.heic", LATER)
    make_file(folder / "a.jpg", "2018-03-01 10:00:00")  # the library has this one
    listing = tmp_path / "missing.csv"
    diffed = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--only", "right", "--format", "csv"])
    listing.write_text(diffed.stdout)
    opened(monkeypatch, mac.libraries["photos-2018"])
    result = import_found(listing, to="photos-2018")
    assert result.exit_code == 0 and "Imported 2 of 2 missing files" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(folder / "one.heic"), str(folder / "sub" / "two.heic")]]]


def folder_with_a_library_photo(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])
    folder = tmp_path / "backup"
    make_file(folder / "one.heic")
    make_file(folder / "sub" / "two.heic", LATER)
    make_file(folder / "a.jpg", "2018-03-01 10:00:00")  # the library has this one
    return folder


def test_import_from_a_folder_with_only_missing_imports_the_files_the_library_lacks(mac, monkeypatch, tmp_path):
    folder = folder_with_a_library_photo(mac, tmp_path)
    opened(monkeypatch, mac.libraries["Source"])
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing"])
    assert result.exit_code == 0 and "Imported 2 of 2 missing files" in result.output
    assert mac.calls("import-photos.applescript") == [[[str(folder / "one.heic"), str(folder / "sub" / "two.heic")]]]


def test_import_from_a_folder_without_only_missing_imports_every_file(mac, monkeypatch, tmp_path):
    folder = folder_with_a_library_photo(mac, tmp_path)
    opened(monkeypatch, mac.libraries["Source"])
    result = runner.invoke(app, ["photos", "import", str(folder), "Source"])
    assert result.exit_code == 0 and "Imported 3 of 3" in result.output


def test_import_from_a_folder_with_dry_run_changes_nothing(mac, tmp_path):
    folder = folder_with_a_library_photo(mac, tmp_path)
    result = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert result.exit_code == 0 and "[dry run]" in result.output and mac.calls("import-photos.applescript") == []


def test_only_missing_judges_with_the_tolerances_and_strict_wants_exact(mac, tmp_path):
    add_photos(mac.libraries["Source"], [("2018-03-01 10:00:00", 100, "a.jpg")])
    folder = tmp_path / "backup"
    make_file(folder / "a.jpg", "2018-03-01 10:00:00", 101)
    loose = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--dry-run"])
    assert "Imported 0 of 0" in loose.output
    exact = runner.invoke(app, ["photos", "import", str(folder), "Source", "--only-missing", "--diff-mode", "exact", "--dry-run"])
    assert "Imported 0 of 1" in exact.output and "[dry run]" in exact.output


def test_only_missing_and_refresh_are_for_a_folder_and_a_bad_diff_mode_is_a_usage_error(mac, tmp_path):
    folder = tmp_path / "backup"
    make_file(folder / "a.jpg")
    listed = tmp_path / "x.csv"
    listed.write_text("date,name\n")
    for options, text in (
        ([str(listed), "Source", "--only-missing"], "are for a folder"),
        ([str(listed), "Source", "--refresh"], "are for a folder"),
        ([str(folder), "Source", "--diff-mode", "date=near"], "date="),
    ):
        result = runner.invoke(app, ["photos", "import", *options])
        assert result.exit_code == 2 and text in " ".join(result.output.replace("│", " ").split()), options


def test_import_asks_photos_to_open_the_library_and_waits_for_it(mac, monkeypatch, tmp_path):
    placeholders(mac)
    folder = found_folder(tmp_path)
    monkeypatch.setenv("FAKE_LSOF", "")
    asked = []
    monkeypatch.setattr(pp, "open_in_photos", lambda library: (asked.append(library), opened(monkeypatch, library)))
    result = import_found(found_list(tmp_path, "--folders", str(folder)))
    assert asked == [mac.libraries["Source"]] and "Opening" in result.output
    assert result.exit_code == 0 and "Imported 2 of 2" in result.output
    assert len(mac.calls("import-photos.applescript")) == 1 and mac.calls("quit-photos.applescript") == [[]]  # and closed again after


def test_a_library_photos_already_has_open_is_not_opened_again(mac, monkeypatch, tmp_path):
    placeholders(mac)
    asked = []
    monkeypatch.setattr(pp, "open_in_photos", lambda library: asked.append(library))
    opened(monkeypatch, mac.libraries["Source"])
    import_found(found_list(tmp_path, "--folders", str(found_folder(tmp_path))))
    assert asked == []
    assert mac.calls("quit-photos.applescript") == []  # it was the user's to keep open


def test_import_from_a_folder_reads_it_from_the_cache_until_refresh_reads_it_again(mac, tmp_path):
    folder = tmp_path / "backup"
    target = make_file(folder / "one.heic")
    options = ["photos", "import", str(folder), "Source", "--dry-run"]
    first = runner.invoke(app, options)
    assert "as cached" not in first.output
    stamp = target.stat().st_mtime
    target.write_bytes(b"x" * 250)  # written over in place: the folder's own time does not change
    os.utime(target, (stamp, stamp))
    assert "1 of 1 folders as cached" in runner.invoke(app, options).output
    assert "as cached" not in runner.invoke(app, [*options, "--refresh"]).output


def test_refresh_is_for_a_folder(mac, tmp_path):
    listed = tmp_path / "x.csv"
    listed.write_text("date,name\n")
    result = runner.invoke(app, ["photos", "import", str(listed), "Source", "--refresh", "--dry-run"])
    assert result.exit_code != 0 and "are for a folder" in " ".join(result.output.replace("│", " ").split())
