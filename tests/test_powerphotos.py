import csv
import fcntl
import io
import json
import os
import plistlib
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple import platform as apple_platform
from apple.cli import app
from apple.photos import library_commands as lc
from apple.photos import powerphotos as pp

FAKES = Path(__file__).resolve().parent / "fakes"
runner = CliRunner()


def make_library(root: Path, name: str, version: int) -> Path:
    library = root / f"{name}.photoslibrary"
    (library / "database").mkdir(parents=True)
    connection = sqlite3.connect(library / "database" / "Photos.sqlite")
    connection.execute("create table Z_METADATA (Z_VERSION integer, Z_PLIST blob)")
    connection.execute("insert into Z_METADATA values (1, ?)", (plistlib.dumps({"PLModelVersion": version}),))
    connection.commit()
    connection.close()
    return library


@pytest.fixture(autouse=True)
def scratch_root(tmp_path, monkeypatch):
    """Database copies are made under the cache directory; keep them out of the real one."""
    monkeypatch.setattr(pp, "SCRATCH_ROOT", tmp_path / "scratch")
    return tmp_path / "scratch"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """macOS with a fake osascript, a current source library and current destinations for 2018 and 2019."""
    libs = {"Source": make_library(tmp_path, "Source", 19607)}
    for year in (2018, 2019):
        libs[f"photos-{year}"] = make_library(tmp_path, f"photos-{year}", 19607)
    monkeypatch.setattr(apple_platform, "is_macos", lambda: True)
    monkeypatch.setenv("PATH", f"{FAKES}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(pp, "LOCK_FILE", tmp_path / "lock")
    log = tmp_path / "osascript.log"
    monkeypatch.setenv("FAKE_OSASCRIPT_LOG", str(log))
    monkeypatch.delenv("FAKE_COPY_ERROR", raising=False)

    def publish(libraries):
        monkeypatch.setenv("FAKE_LIBRARIES", "\n".join(f"{n}\t{p}/" for n, p in libraries.items()))

    publish(libs)

    class Mac:
        libraries = libs
        republish = staticmethod(publish)
        make = staticmethod(lambda name, version: libs.update({name: make_library(tmp_path, name, version)}) or publish(libs))

        @staticmethod
        def calls(script):
            if not log.exists():
                return []
            return [c[1:] for c in map(json.loads, log.read_text().splitlines()) if c[0] == script]

    return Mac


def test_the_powerphotos_commands_refuse_to_run_off_macos(monkeypatch):
    monkeypatch.setattr(apple_platform, "is_macos", lambda: False)
    for args in (["libraries"], ["copy", "--from", "a", "--to", "b"], ["split", "--from", "a", "--media", "photos", "--years", "2018"]):
        result = runner.invoke(app, ["photos", *args])
        assert result.exit_code == 2
        assert "macOS only" in result.output


def test_libraries_lists_names_and_paths(mac):
    result = runner.invoke(app, ["photos", "libraries"])
    assert result.exit_code == 0
    assert "photos-2018\t" in result.output
    assert result.output.splitlines()[0].endswith("\t19607")  # the version of the fake libraries


def test_libraries_shows_unknown_for_a_library_whose_version_cannot_be_read(mac, tmp_path):
    (tmp_path / "Source.photoslibrary" / "database" / "Photos.sqlite").unlink()
    result = runner.invoke(app, ["photos", "libraries"])
    assert any(line.startswith("Source\t") and line.endswith("\tunknown") for line in result.output.splitlines())


def test_copy_passes_the_range_and_media_and_makes_until_inclusive(mac):
    result = runner.invoke(
        app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--since", "2018-03-01",
              "--until", "2018-12-31", "--media", "videos"],
    )
    assert result.exit_code == 0
    assert mac.calls("copy-photos.applescript") == [["Source", "photos-2018", "2018-03-01", "2019-01-01", "videos"]]
    assert "photos-2018: asked PowerPhotos to copy 3" in result.output


def test_copy_with_no_range_passes_dashes_and_all(mac):
    runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018"])
    assert mac.calls("copy-photos.applescript") == [["Source", "photos-2018", "-", "-", "all"]]


def test_an_unknown_library_is_named_and_the_known_ones_are_not_listed(mac):
    result = runner.invoke(app, ["photos", "copy", "--from", "Nope", "--to", "photos-2018"])
    assert result.exit_code == 1
    assert "No library named Nope in PowerPhotos" in result.output and "photos-2019" not in result.output and "photos-2018" not in result.output.split("Nope")[1]
    assert "./apple photos libraries" in result.output  # where to see them
    assert mac.calls("copy-photos.applescript") == []


def test_a_bad_date_or_reversed_range_is_a_usage_error(mac):
    bad = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--since", "last week"])
    assert bad.exit_code == 2
    reversed_ = runner.invoke(
        app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--since", "2018-05-01", "--until", "2018-01-01"]
    )
    assert reversed_.exit_code == 2


def test_an_older_destination_is_skipped_before_any_copy(mac):
    mac.make("photos-2017", 19320)
    result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2017"])
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_stop_ends_with_an_error_for_an_older_destination(mac):
    mac.make("photos-2017", 19320)
    result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2017", "--on-upgrade", "stop"])
    assert result.exit_code == 1
    assert "needs a Photos upgrade" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_ask_without_a_terminal_skips(mac):
    mac.make("photos-2017", 19320)
    result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2017", "--on-upgrade", "ask"])
    assert result.exit_code == 0 and "skipped" in result.output


def test_a_refused_copy_with_error_5_is_handled_like_an_old_destination(mac, monkeypatch):
    monkeypatch.setenv("FAKE_COPY_ERROR", "PowerPhotos got an error: Library needs upgrade (5)")
    skipped = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018"])
    assert skipped.exit_code == 0 and "skipped" in skipped.output
    stopped = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--on-upgrade", "stop"])
    assert stopped.exit_code == 1


def test_any_other_powerphotos_error_fails_the_command(mac, monkeypatch):
    monkeypatch.setenv("FAKE_COPY_ERROR", "PowerPhotos got an error: Can't get library (-1728)")
    result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018"])
    assert result.exit_code == 1
    assert "-1728" in result.output


def test_only_one_run_at_a_time(mac, tmp_path):
    with open(tmp_path / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018"])
    assert result.exit_code == 1
    assert "already running" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_split_copies_each_year_into_its_own_library(mac):
    result = runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2018-2019", "--check-duplicates"])
    assert result.exit_code == 0
    assert mac.calls("copy-photos.applescript") == [
        ["Source", "photos-2018", "2018-01-01", "2019-01-01", "photos"],
        ["Source", "photos-2019", "2019-01-01", "2020-01-01", "photos"],
    ]


def test_split_rejects_a_bad_year_range(mac):
    assert runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "x"]).exit_code == 2
    assert runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2019-2018"]).exit_code == 2


def test_model_version_reads_the_database_and_tolerates_a_missing_one(tmp_path):
    assert pp.model_version(make_library(tmp_path, "x", 19320)) == 19320
    assert pp.model_version(tmp_path / "missing.photoslibrary") is None


def wal_library(root: Path, before: int, after: int):
    """A library whose upgrade (before -> after) is only in the write-ahead log, as while Photos still has it open.

    Returns the library and the open connection, which has to stay open for the log to remain.
    """
    library = root / "w.photoslibrary"
    (library / "database").mkdir(parents=True)
    connection = sqlite3.connect(library / "database" / "Photos.sqlite")
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma wal_autocheckpoint=0")
    connection.execute("create table Z_METADATA (Z_VERSION integer, Z_PLIST blob)")
    connection.execute("insert into Z_METADATA values (1, ?)", (plistlib.dumps({"PLModelVersion": before}),))
    connection.commit()
    connection.execute("pragma wal_checkpoint(truncate)")
    connection.execute("update Z_METADATA set Z_PLIST = ?", (plistlib.dumps({"PLModelVersion": after}),))
    connection.commit()
    return library, connection


def test_model_version_sees_an_upgrade_that_is_still_in_the_write_ahead_log(tmp_path):
    library, connection = wal_library(tmp_path, 19320, 19607)
    assert pp.model_version(library) == 19607
    connection.close()


def test_model_version_reads_a_copy_with_its_log_where_the_database_cannot_be_opened_in_place(tmp_path, monkeypatch, scratch_root):
    library, connection = wal_library(tmp_path, 19320, 19607)
    real = pp.read_file
    opened = []

    def read_file(database, reader, mode="ro"):
        opened.append(database)
        if len(opened) == 1:
            raise sqlite3.OperationalError("unable to open database file")  # e.g. a network share
        return real(database, reader, mode)

    monkeypatch.setattr(pp, "read_file", read_file)
    assert pp.model_version(library) == 19607
    assert opened[1] != opened[0]  # the second read was of a copy
    assert scratch_root in opened[1].parents  # on local disk, not in TMPDIR
    connection.close()


def test_a_version_read_that_is_not_exact_reads_the_database_where_it_is_without_the_log(tmp_path, monkeypatch):
    library, connection = wal_library(tmp_path, 19320, 19607)
    opened = []
    real = pp.read_file
    monkeypatch.setattr(pp, "read_file", lambda database, reader, mode="ro": opened.append(mode) or real(database, reader, mode))
    assert pp.model_version(library, exact=False) == 19320  # the upgrade is only in the log
    assert opened == ["ro&immutable=1"]
    connection.close()


def test_the_listing_does_not_read_the_log_but_outdated_does(mac):
    library, connection = wal_library(mac.libraries["Source"].parent, 19320, 19607)
    mac.libraries["old"] = library
    mac.republish(mac.libraries)
    listed = runner.invoke(app, ["photos", "libraries"])
    assert "old\t" in listed.output and listed.output.count("19607") == 3 and "\t19320" in listed.output
    outdated = runner.invoke(app, ["photos", "libraries", "--outdated"])
    assert outdated.stdout == ""  # exact: old is 19607 like the rest
    connection.close()


def lines(text):
    return [line.split("\t")[0] for line in text.splitlines() if line]


def test_outdated_lists_only_libraries_older_than_the_newest(mac):
    mac.make("photos-2017", 19320)
    mac.make("videos-henry", 15323)
    result = runner.invoke(app, ["photos", "libraries", "--outdated"])
    assert result.exit_code == 0
    assert lines(result.stdout) == ["photos-2017", "videos-henry"]
    assert result.stdout.splitlines()[0].endswith("\t19320")
    assert "Reference: 19607 (the newest library). 2 older." in result.stderr


def test_outdated_against_a_named_library_uses_its_version_as_the_reference(mac):
    mac.make("photos-2017", 19320)
    mac.make("videos-henry", 15323)
    result = runner.invoke(app, ["photos", "libraries", "--outdated", "--against", "photos-2017"])
    assert lines(result.stdout) == ["videos-henry"]
    assert "Reference: 19320 (photos-2017). 1 older." in result.stderr


def test_outdated_with_nothing_older_prints_nothing_to_stdout(mac):
    result = runner.invoke(app, ["photos", "libraries", "--outdated"])
    assert result.exit_code == 0 and result.stdout == ""
    assert "0 older" in result.stderr


def test_outdated_names_unreadable_libraries_on_stderr_and_does_not_list_them(mac, tmp_path):
    mac.make("photos-2017", 19320)
    (tmp_path / "photos-2018.photoslibrary" / "database" / "Photos.sqlite").unlink()
    result = runner.invoke(app, ["photos", "libraries", "--outdated"])
    assert lines(result.stdout) == ["photos-2017"]
    assert "Version cannot be read: photos-2018" in result.stderr


def test_outdated_against_an_unknown_or_unreadable_library_fails(mac, tmp_path):
    unknown = runner.invoke(app, ["photos", "libraries", "--outdated", "--against", "Nope"])
    assert unknown.exit_code == 1 and "No library named Nope" in unknown.output
    (tmp_path / "photos-2018.photoslibrary" / "database" / "Photos.sqlite").unlink()
    unreadable = runner.invoke(app, ["photos", "libraries", "--outdated", "--against", "photos-2018"])
    assert unreadable.exit_code == 1 and "cannot be the reference" in unreadable.output


def test_against_without_outdated_is_a_usage_error(mac):
    assert runner.invoke(app, ["photos", "libraries", "--against", "Source"]).exit_code == 2


def add_photos(library: Path, photos) -> Path:
    """Gives a fake library the tables and original files diff reads.

    photos: (date as 'yyyy-mm-dd hh:mm:ss', size, name[, flags]) with flags among "trashed", "hidden", "burst" (a hidden
    burst member) and "shared" (it came from Messages). An item of size 0 is a placeholder: it has no original file.
    """
    connection = sqlite3.connect(library / "database" / "Photos.sqlite")
    connection.execute("drop table if exists ZASSET")
    connection.execute("drop table if exists ZADDITIONALASSETATTRIBUTES")
    connection.execute("create table ZASSET (Z_PK integer primary key, ZUUID text, ZDATECREATED real, ZFILENAME text, ZDIRECTORY text, ZTRASHEDSTATE integer, ZVISIBILITYSTATE integer, ZAVALANCHEKIND integer)")
    connection.execute("create table ZADDITIONALASSETATTRIBUTES (Z_PK integer primary key, ZASSET integer, ZORIGINALFILESIZE integer, ZORIGINALFILENAME text, ZSYNDICATIONIDENTIFIER text)")
    (library / "originals" / "A").mkdir(parents=True, exist_ok=True)
    for number, (when, size, name, *flags) in enumerate(photos, 1):
        seconds = connection.execute("select strftime('%s', ?) - 978307200", (when,)).fetchone()[0]
        stored_as = f"UUID{number}.jpg"
        connection.execute(
            "insert into ZASSET values (?, ?, ?, ?, 'A', ?, ?, ?)",
            (number, f"UUID{number}", seconds, stored_as, int("trashed" in flags), 2 if {"hidden", "burst"} & set(flags) else 0, int("burst" in flags)),
        )
        connection.execute(
            "insert into ZADDITIONALASSETATTRIBUTES values (?, ?, ?, ?, ?)",
            (number, number, size, name, f"at_0_{number}" if "shared" in flags else None),
        )
        original = library / "originals" / "A" / stored_as
        if size:
            original.write_bytes(b"x")
        elif original.exists():
            original.unlink()
    connection.commit()
    connection.close()
    return library


def diff(mac, left, right, *options):
    """Runs diff on two fake libraries, showing just date, size and name unless --fields says otherwise."""
    for name, photos in (("Source", left), ("photos-2018", right)):
        add_photos(mac.libraries[name], photos)
    columns = [] if "--fields" in options else ["--fields", "date,size,name"]
    return runner.invoke(app, ["photos", "diff", "Source", "photos-2018", *columns, *options])


def shown(result):
    """The rows of diff's text output, without the header and with each run of padding squeezed to two spaces."""
    return [re.sub(r" {2,}", "  ", line) for line in result.stdout.splitlines()[1:]]


A = ("2018-03-01 10:00:00", 100, "a.jpg")
B = ("2018-03-02 11:00:00", 100, "b.jpg")
C = ("2018-03-03 12:00:00", 100, "c.mov")
D = ("2018-03-04 09:00:00", 100, "d.jpg")


def test_diff_lists_what_is_in_only_one_library_and_exits_1(mac):
    result = diff(mac, [A, B, C], [B, D])
    assert result.exit_code == 1
    assert shown(result) == [
        "<  2018-03-01 10:00:00  100  a.jpg", "<  2018-03-03 12:00:00  100  c.mov", ">  2018-03-04 09:00:00  100  d.jpg",
    ]
    assert "Only in Source: 2. Only in photos-2018: 1. In both: 1." in result.output
    assert mac.calls("list-photos.applescript") == []  # read from the database, not through PowerPhotos


def test_diff_of_matching_libraries_prints_nothing_and_exits_0(mac):
    result = diff(mac, [A], [A])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_diff_counts_repeated_photos_only_with_include_duplicates(mac):
    assert shown(diff(mac, [A, A], [A], "--include-duplicates")) == ["<  2018-03-01 10:00:00  100  a.jpg"]


def test_diff_ignores_the_trash(mac):
    assert diff(mac, [A, (*B, "trashed")], [A]).exit_code == 0


def test_diff_tells_apart_photos_that_differ_only_in_size(mac):
    other = ("2018-03-01 10:00:00", 200, "a.jpg")
    assert shown(diff(mac, [A], [other])) == ["<  2018-03-01 10:00:00  100  a.jpg", ">  2018-03-01 10:00:00  200  a.jpg"]


def test_diff_summary_prints_only_the_counts(mac):
    result = diff(mac, [A], [], "--summary")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Only in Source: 1." in result.output


def test_diff_takes_library_paths_and_needs_no_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_platform, "is_macos", lambda: False)
    one = add_photos(make_library(tmp_path, "one", 19607), [A, B])
    two = add_photos(make_library(tmp_path, "two", 19607), [B])
    result = runner.invoke(app, ["photos", "diff", str(one), str(two), "--fields", "date,size,name"])
    assert shown(result) == ["<  2018-03-01 10:00:00  100  a.jpg"]


def test_diff_names_an_unknown_library(mac):
    result = runner.invoke(app, ["photos", "diff", "Source", "Nope"])
    assert result.exit_code == 1
    assert "No library named Nope" in result.output


def test_diff_text_has_a_header_and_aligned_columns_with_sizes_on_the_right(mac):
    long = ("2018-03-03 12:00:00", 25000, "longer-name.mov")
    result = diff(mac, [A, long], [])
    assert result.stdout.splitlines() == [
        "   date                  size  name",
        "<  2018-03-01 10:00:00    100  a.jpg",
        "<  2018-03-03 12:00:00  25000  longer-name.mov",
    ]
    assert diff(mac, [A], [D], "--format", "csv").stdout.startswith("side,date,size,name\n")
    assert diff(mac, [A, A], [], "--only", "left").stdout.count("date") == 1  # the header is printed once, and only when there is something to show
    assert diff(mac, [A], [A]).stdout == ""


def test_diff_only_shows_one_side_without_the_marks(mac):
    assert shown(diff(mac, [A, B], [B, D], "--only", "left")) == ["2018-03-01 10:00:00  100  a.jpg"]
    assert shown(diff(mac, [A, B], [B, D], "--only", "right")) == ["2018-03-04 09:00:00  100  d.jpg"]


def test_diff_csv_and_json_carry_the_side(mac):
    csv_out = diff(mac, [A], [D], "--format", "csv").stdout.splitlines()
    assert csv_out == ["side,date,size,name", "left,2018-03-01 10:00:00,100,a.jpg", "right,2018-03-04 09:00:00,100,d.jpg"]
    records = json.loads(diff(mac, [A], [D], "--format", "json").stdout)
    assert records[0] == {"side": "left", "date": "2018-03-01 10:00:00", "size": 100, "name": "a.jpg"}
    assert diff(mac, [A], [D], "--format", "csv", "--only", "left").stdout.splitlines()[0] == "date,size,name"


def test_diff_fields_chooses_and_orders_the_columns(mac):
    assert shown(diff(mac, [A], [], "--fields", "name,size", "--only", "left")) == ["a.jpg  100"]
    bad = diff(mac, [A], [], "--fields", "name,name")
    assert bad.exit_code == 2 and "at most once" in bad.output
    assert diff(mac, [A], [], "--fields", "colour").exit_code == 2


def test_diff_sort_orders_by_date_name_or_size(mac):
    big = ("2018-03-05 08:00:00", 900, "a-late.jpg")
    small = ("2018-03-02 08:00:00", 5, "z-early.jpg")
    names = lambda *o: [line.split()[-1] for line in shown(diff(mac, [big, small, A], [], "--only", "left", *o))]
    assert names() == ["a.jpg", "z-early.jpg", "a-late.jpg"]
    assert names("--sort", "name") == ["a-late.jpg", "a.jpg", "z-early.jpg"]
    assert names("--sort", "size") == ["z-early.jpg", "a.jpg", "a-late.jpg"]


def copy_list(args, text, **options):
    return runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", *args], input=text, **options)


def test_copy_list_copies_the_names_in_diff_csv_output(mac):
    result = copy_list(["--list", "-"], "side,date,size,name\nleft,2018-03-01 10:00:00,100,\"a, b.jpg\"\nleft,x,1,c.mov\n")
    assert result.exit_code == 0 and "photos-2018: copied 3 (no duplicate check)" in result.output
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["a, b.jpg", "c.mov"], "false"]]
    assert mac.calls("copy-photos.applescript") == []


def test_copy_list_reads_json_plain_names_and_files(mac, tmp_path):
    copy_list(["--list", "-"], '[{"side": "left", "name": "a.jpg"}, {"name": "b.mov"}]')
    copy_list(["--list", "-"], "c.jpg\n\nd.mov\n")
    listing = tmp_path / "names.txt"
    listing.write_text("e.jpg\n")
    copy_list(["--list", str(listing)], "")
    assert [call[2] for call in mac.calls("copy-named.applescript")] == [["a.jpg", "b.mov"], ["c.jpg", "d.mov"], ["e.jpg"]]


def test_copy_list_without_a_name_column_is_an_error(mac):
    result = copy_list(["--list", "-"], "side,date\nleft,2018-03-01 10:00:00\n")
    assert result.exit_code == 2 and "no name column" in result.output
    assert mac.calls("copy-named.applescript") == []


def test_an_empty_list_copies_nothing_and_succeeds(mac):
    for text in ("", "\n", "side,name\n", "[]"):
        result = copy_list(["--list", "-"], text)
        assert result.exit_code == 0 and "no files copied" in result.output
    assert mac.calls("copy-named.applescript") == []


def test_copy_list_cannot_be_combined_with_a_range_or_media(mac):
    for option in (["--since", "2018-01-01"], ["--until", "2018-12-31"], ["--media", "videos"]):
        assert copy_list(["--list", "-", *option], "a.jpg\n").exit_code == 2


def test_names_the_source_lacks_are_reported(mac, monkeypatch):
    monkeypatch.setenv("FAKE_NAMED_MISSING", "gone.jpg,gone2.jpg")
    result = copy_list(["--list", "-"], "a.jpg\ngone.jpg\ngone2.jpg\n")
    assert "2 not copied: the database of Source has them, but PowerPhotos lists no photo with these names" in result.output
    assert "    gone.jpg" in result.output and "    gone2.jpg" in result.output


def test_a_list_is_copied_without_the_duplicate_check_unless_it_is_asked_for(mac):
    copy_list(["--list", "-"], "a.jpg\n")
    assert mac.calls("copy-named.applescript")[0][3] == "false"
    copy_list(["--list", "-", "--check-duplicates"], "a.jpg\n")
    assert mac.calls("copy-named.applescript")[1][3] == "true"
    assert copy_list(["--check-duplicates"], "").exit_code == 2  # a date range is always compared against the destination
    assert len(mac.calls("copy-photos.applescript")) == 0


def test_the_extension_lists_match_the_applescript():
    script = (pp.APPLESCRIPTS / "copy-photos.applescript").read_text()
    for kind, key in (("photoExtensions", "photos"), ("videoExtensions", "videos")):
        listed = re.search(rf"property {kind} : \{{([^}}]*)\}}", script).group(1)
        assert tuple(re.findall(r'"([^"]+)"', listed)) == pp.EXTENSIONS[key]


def test_copy_named_applescript_passes_the_duplicate_setting_explicitly():
    script = (pp.APPLESCRIPTS / "copy-named.applescript").read_text()
    assert "with skip duplicates" in script and "without skip duplicates" in script


def year_libraries(mac, source, **destinations):
    add_photos(mac.libraries["Source"], source)
    for name, photos in destinations.items():
        add_photos(mac.libraries[name.replace("_", "-")], photos)


def split(*options):
    return runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2018", *options])


def test_split_skips_a_year_whose_library_already_has_everything(mac):
    year_libraries(mac, [A, B], photos_2018=[A, B])
    result = split()
    assert result.exit_code == 0 and "photos-2018: up to date" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_split_copies_a_year_that_is_missing_something(mac):
    year_libraries(mac, [A, B], photos_2018=[A])
    assert "photos-2018: asked PowerPhotos to copy" in split("--check-duplicates").output
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_split_ignores_photos_of_the_other_media_and_the_trash_when_judging_a_year(mac):
    year_libraries(mac, [A, C, (*B, "trashed")], photos_2018=[A])
    assert "up to date" in split().output


def test_split_no_skip_complete_copies_even_a_complete_year(mac):
    year_libraries(mac, [A], photos_2018=[A])
    split("--no-skip-complete", "--check-duplicates")
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_split_copies_when_a_year_library_cannot_be_read(mac):
    year_libraries(mac, [A])  # photos-2018 has no photo tables
    split("--check-duplicates")
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_a_photo_near_new_year_may_be_in_the_neighbouring_years_library(mac):
    new_year = ("2018-01-01 05:00:00", 100, "ny.jpg")
    mac.make("photos-2017", 19607)
    year_libraries(mac, [A, new_year], photos_2018=[A], photos_2017=[new_year])
    assert "up to date" in split("--check-duplicates").output
    year_libraries(mac, [A, new_year], photos_2018=[A], photos_2017=[])
    assert "photos-2018: asked PowerPhotos to copy" in split("--check-duplicates").output


def test_split_by_default_copies_only_the_missing_names_without_the_check(mac):
    new_year = ("2018-01-01 05:00:00", 100, "ny.jpg")
    year_libraries(mac, [A, B, new_year], photos_2018=[A])
    result = split()
    assert "photos-2018: copied 3 (no duplicate check)" in result.output
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID3", "UUID2"], "false"]]  # b.jpg and ny.jpg (near New Year too), by Photos identifier
    assert mac.calls("copy-photos.applescript") == []


def test_split_by_default_leaves_a_complete_year_alone(mac):
    year_libraries(mac, [A], photos_2018=[A])
    assert "up to date" in split().output
    assert mac.calls("copy-named.applescript") == []


def test_split_by_default_needs_the_databases_and_the_skip_check(mac):
    year_libraries(mac, [A])  # photos-2018 cannot be read
    result = split()
    assert result.exit_code == 1 and "cannot be read" in result.output
    assert split("--no-skip-complete").exit_code == 2
    assert mac.calls("copy-named.applescript") == []


def two_videos_2016(mac, monkeypatch, tmp_path):
    other = make_library(tmp_path / "elsewhere", "videos-2016", 19607)
    monkeypatch.setenv("FAKE_LIBRARIES", "\n".join(f"{n}\t{p}/" for n, p in [*mac.libraries.items(), ("photos-2018", other)]))


def test_libraries_sharing_a_name_stop_copy_and_diff_and_outdated(mac, monkeypatch, tmp_path):
    two_videos_2016(mac, monkeypatch, tmp_path)
    for args in (["copy", "--from", "Source", "--to", "photos-2018"], ["diff", "Source", "photos-2018"], ["libraries", "--outdated"]):
        result = runner.invoke(app, ["photos", *args])
        assert result.exit_code == 1 and "share a name" in result.output and "photos-2018" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_the_listing_shows_libraries_that_share_a_name_and_says_so(mac, monkeypatch, tmp_path):
    two_videos_2016(mac, monkeypatch, tmp_path)
    result = runner.invoke(app, ["photos", "libraries"])
    assert result.exit_code == 0
    assert result.stdout.count("photos-2018\t") == 2
    assert "share a name" in result.output


def test_diff_compares_hidden_items_unless_told_to_leave_them_out(mac):
    result = diff(mac, [A, (*B, "hidden")], [A])
    assert result.exit_code == 1 and shown(result) == ["<  2018-03-02 11:00:00  100  b.jpg"]
    left_out = diff(mac, [A, (*B, "burst")], [A], "--exclude-hidden")
    assert left_out.exit_code == 0
    assert "Not compared, hidden in Photos and not listed by PowerPhotos: 1 in Source (1 burst member), 0 in photos-2018 (0 burst members)." in left_out.output


def test_diff_since_and_until_limit_both_libraries_to_a_range_of_days(mac):
    result = diff(mac, [A, B, C, D], [B], "--since", "2018-03-02", "--until", "2018-03-03")
    assert shown(result) == ["<  2018-03-03 12:00:00  100  c.mov"]
    assert "Only in Source: 1. Only in photos-2018: 0. In both: 1." in result.output  # b is in both; a and d are out of range
    assert shown(diff(mac, [A, D], [], "--since", "2018-03-04")) == ["<  2018-03-04 09:00:00  100  d.jpg"]
    assert shown(diff(mac, [A, D], [], "--until", "2018-03-01")) == ["<  2018-03-01 10:00:00  100  a.jpg"]
    assert diff(mac, [A], [], "--since", "2018-03-05", "--until", "2018-03-01").exit_code == 2


def test_diff_media_compares_only_photos_or_only_videos(mac):
    assert shown(diff(mac, [A, C], [], "--media", "videos", "--only", "left")) == ["2018-03-03 12:00:00  100  c.mov"]
    assert shown(diff(mac, [A, C], [], "--media", "photos", "--only", "left")) == ["2018-03-01 10:00:00  100  a.jpg"]
    assert len(shown(diff(mac, [A, C], [], "--media", "all", "--only", "left"))) == 2


def test_the_named_copy_script_treats_jpeg_as_jpg():
    script = (pp.APPLESCRIPTS / "copy-named.applescript").read_text()
    assert 'ends with ".jpeg"' in script and '& ".jpg"' in script


def test_parse_size_takes_decimal_units():
    from apple.photos.split_plan import format_size, parse_size
    assert parse_size("200GB") == 200 * 10**9 and parse_size("1.5 tb") == 1_500_000_000_000
    assert parse_size("500mb") == 500 * 10**6 and parse_size("250") == 250
    for bad in ("", "GB", "0", "-5GB", "12 parsecs"):
        with pytest.raises(ValueError):
            parse_size(bad)
    assert format_size(1_500_000_000) == "1.5 GB" and format_size(999) == "999 B"


def day(date_text, size, name="x.jpg"):
    return (f"{date_text} 12:00:00", str(size), name)


def test_a_year_under_the_cap_is_one_library_with_the_plain_name():
    from collections import Counter
    from apple.photos.split_plan import plan_year
    photos = Counter([day("2018-03-01", 100), day("2018-03-02", 100)])
    parts = plan_year(photos, 2018, "photos", "photos-2018", 1000)
    assert [(p.name, p.start.isoformat(), p.end.isoformat(), p.size) for p in parts] == [("photos-2018", "2018-01-01", "2019-01-01", 200)]
    assert plan_year(photos, 2018, "photos", "photos-2018", None) == parts
    assert plan_year(None, 2018, "photos", "photos-2018", 1000)[0].size == 0


def test_a_year_over_the_cap_is_divided_into_runs_of_whole_days():
    from collections import Counter
    from apple.photos.split_plan import plan_year
    photos = Counter([day("2018-03-01", 100, "a.jpg"), day("2018-03-01", 100, "b.jpg"), day("2018-03-02", 100), day("2018-03-03", 100), day("2018-12-30", 100), day("2019-01-05", 9999)])
    parts = plan_year(photos, 2018, "photos", "photos-2018", 250)
    # day 1 holds two photos (200), so day 2 would make 300: it starts the next library
    assert [(p.name, p.start.isoformat(), p.end.isoformat(), p.size) for p in parts] == [
        ("photos-2018-1", "2018-01-01", "2018-03-02", 200),
        ("photos-2018-2", "2018-03-02", "2018-12-30", 200),
        ("photos-2018-3", "2018-12-30", "2019-01-01", 100),
    ]
    assert sum(p.size for p in parts) == 500  # the photo of 2019 and other media are not counted


def test_a_single_day_over_the_cap_is_a_library_of_its_own():
    from collections import Counter
    from apple.photos.split_plan import plan_year
    parts = plan_year(Counter([day("2018-03-01", 900), day("2018-03-02", 10)]), 2018, "photos", "p", 100)
    assert [(p.name, p.size) for p in parts] == [("p-1", 900), ("p-2", 10)]


def test_the_cap_counts_only_the_media_asked_for():
    from collections import Counter
    from apple.photos.split_plan import plan_year
    photos = Counter([day("2018-03-01", 500, "v.mov"), day("2018-03-02", 100)])
    assert len(plan_year(photos, 2018, "photos", "p", 250)) == 1


def capped_split(*options):
    return split("--max-size", "250", *options)


def three_days(mac, **destinations):
    day_a, day_b, day_c = ("2018-03-01 10:00:00", 100, "a.jpg"), ("2018-03-02 10:00:00", 100, "b.jpg"), ("2018-03-03 10:00:00", 100, "c.jpg")
    year_libraries(mac, [day_a, day_b, day_c], **destinations)
    return day_a, day_b, day_c


def test_split_over_the_cap_copies_each_part_by_its_date_range(mac):
    three_days(mac)
    mac.make("photos-2018-1", 19607)
    mac.make("photos-2018-2", 19607)
    result = capped_split("--check-duplicates")
    assert result.exit_code == 0 and "3 libraries" not in result.output and "in 2 libraries" in result.output
    assert mac.calls("copy-photos.applescript") == [
        ["Source", "photos-2018-1", "2018-01-01", "2018-03-03", "photos"],
        ["Source", "photos-2018-2", "2018-03-03", "2019-01-01", "photos"],
    ]


def test_split_under_the_cap_keeps_the_plain_library_name(mac):
    three_days(mac)
    split("--max-size", "10GB", "--check-duplicates")
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["photos-2018"]


def test_split_stops_before_copying_when_a_part_library_is_missing(mac):
    three_days(mac)
    mac.make("photos-2018-1", 19607)  # photos-2018-2 is not in PowerPhotos
    result = capped_split()
    assert result.exit_code == 1 and "Not in PowerPhotos: photos-2018-2" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_split_on_missing_skip_copies_the_libraries_that_exist(mac):
    three_days(mac)
    mac.make("photos-2018-1", 19607)
    result = capped_split("--on-missing", "skip", "--check-duplicates")
    assert result.exit_code == 0 and "photos-2018-2 (2018-03-03 to 2018-12-31" in result.output and "skipped (not in PowerPhotos)" in result.output
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["photos-2018-1"]


def test_split_dry_run_copies_nothing_and_says_what_each_library_needs(mac):
    day_a, day_b, day_c = three_days(mac)
    mac.make("photos-2018-1", 19607)
    add_photos(mac.libraries["photos-2018-1"], [day_a, day_b, day_c])
    result = capped_split("--dry-run")
    assert result.exit_code == 0
    assert "photos-2018-1 (2018-01-01 to 2018-03-02, 200 B): up to date" in result.output
    assert "photos-2018-2 (2018-03-03 to 2018-12-31, 100 B): library missing" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_the_dry_run_environment_variable_is_a_dry_run_too(mac):
    three_days(mac)
    mac.make("photos-2018-1", 19607)
    mac.make("photos-2018-2", 19607)
    result = runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2018", "--max-size", "250"], env={"DRY_RUN": "true"})
    assert result.exit_code == 0 and "would copy" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_split_skips_a_part_that_is_complete_and_copies_the_other(mac):
    day_a, day_b, day_c = three_days(mac)
    day_d = ("2018-09-01 10:00:00", 100, "d.jpg")
    add_photos(mac.libraries["Source"], [day_a, day_b, day_c, day_d])
    mac.make("photos-2018-1", 19607)
    mac.make("photos-2018-2", 19607)
    add_photos(mac.libraries["photos-2018-1"], [day_a, day_b])
    add_photos(mac.libraries["photos-2018-2"], [day_c])  # c is near the boundary: either library may hold it
    result = capped_split("--check-duplicates")
    assert "photos-2018-1 (2018-01-01 to 2018-03-02, 200 B): up to date" in result.output
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["photos-2018-2"]


def test_split_by_default_copies_what_a_part_lacks_and_photos_near_its_edges(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    day_a, day_b, day_c = three_days(mac)
    mac.make("photos-2018-1", 19607)
    mac.make("photos-2018-2", 19607)
    add_photos(mac.libraries["photos-2018-1"], [day_a])
    add_photos(mac.libraries["photos-2018-2"], [])
    capped_split()
    # b and c are within a day of the boundary between the parts, so either library might be theirs (a, in part 1, is not)
    assert mac.calls("copy-ids.applescript") == [
        ["Source", "photos-2018-1", ["UUID2", "UUID3"], "false"], ["Source", "photos-2018-2", ["UUID2", "UUID3"], "false"],
    ]


def test_split_max_size_must_be_a_size_and_needs_the_source_database(mac):
    assert capped_split("--max-size", "lots").exit_code == 2
    assert split("--max-size", "lots").exit_code == 2
    result = split("--max-size", "1GB", "--check-duplicates")  # Source has no photo tables in this fixture
    assert result.exit_code == 1 and "size cannot be worked out" in result.output


def test_diff_default_columns_are_true_or_false_flags_after_the_name(mac):
    add_photos(mac.libraries["Source"], [A, (*B, "burst"), (*C, "hidden"), ("2018-05-01 10:00:00", 0, "p.jpg", "shared")])
    add_photos(mac.libraries["photos-2018"], [])
    text = runner.invoke(app, ["photos", "diff", "Source", "photos-2018"]).stdout.splitlines()
    assert text[0].split() == ["date", "size", "name", "hidden", "burst", "missing", "shared"]
    assert [line.split()[4:] for line in text[1:]] == [
        ["a.jpg", "false", "false", "false", "false"], ["b.jpg", "true", "true", "false", "false"],
        ["c.mov", "true", "false", "false", "false"], ["p.jpg", "false", "false", "true", "true"],
    ]
    records = json.loads(runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--format", "json"]).stdout)
    assert [(r["name"], r["hidden"], r["burst"], r["missing"], r["shared"]) for r in records] == [
        ("a.jpg", False, False, False, False), ("b.jpg", True, True, False, False),
        ("c.mov", True, False, False, False), ("p.jpg", False, False, True, True),
    ]
    csv_lines = runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--format", "csv"]).stdout.splitlines()
    assert csv_lines[0] == "side,date,size,name,hidden,burst,missing,shared" and csv_lines[2].endswith(",b.jpg,true,true,false,false")
    columns = runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--exclude-hidden", "--format", "csv"]).stdout.splitlines()
    assert columns[0] == "side,date,size,name,missing,shared" and len(columns) == 3  # a.jpg and the placeholder; the hidden ones are left out


def test_diff_ignore_date_matches_the_same_clip_imported_with_a_shifted_time(mac):
    shifted = ("2018-03-01 11:00:00", 100, "a.jpg")
    assert diff(mac, [A], [shifted]).exit_code == 1
    result = diff(mac, [A], [shifted], "--ignore-date")
    assert result.exit_code == 0 and "In both: 1." in result.output
    twice = diff(mac, [A], [A, shifted], "--ignore-date")  # the destination holds the clip twice: one extra
    assert twice.exit_code == 1 and shown(twice) == [">  2018-03-01 11:00:00  100  a.jpg"]
    different = diff(mac, [A], [("2018-03-01 11:00:00", 999, "a.jpg")], "--ignore-date")
    assert different.exit_code == 1  # a different size is a different photo


def test_a_list_from_diff_leaves_hidden_items_out_and_says_so(mac):
    result = copy_list(["--list", "-"], "side,name,hidden,burst\nleft,a.jpg,false,false\nleft,b.jpg,true,true\nleft,c.jpg,true,false\n")
    assert result.exit_code == 0
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["a.jpg"], "false"]]
    assert "Not copied: b.jpg: hidden in Photos (a burst member)" in result.output
    assert "Not copied: c.jpg: hidden in Photos, so" in result.output
    assert "2 hidden items (1 burst member) were not copied" in result.output
    as_json = copy_list(["--list", "-"], '[{"name": "d.jpg", "hidden": true, "burst": true}]')
    assert as_json.exit_code == 0 and "no files copied (everything listed is hidden in Photos or has no original in Source)" in as_json.output
    assert len(mac.calls("copy-named.applescript")) == 1


def test_a_long_list_of_hidden_items_is_summarised(mac):
    rows = "".join(f"left,h{n}.jpg,true,true\n" for n in range(30))
    result = copy_list(["--list", "-"], "side,name,hidden,burst\n" + rows)
    assert "Not copied: h0.jpg" not in result.output and "30 hidden items (30 burst members) were not copied" in result.output


def test_split_says_how_many_hidden_items_it_cannot_copy(mac):
    year_libraries(mac, [A, (*B, "burst"), (*C, "hidden"), ("2018-05-01 10:00:00", 100, "p.jpg", "burst")], photos_2018=[A])
    result = split()
    assert "Source: 2 hidden photos (2 burst members) not copied" in result.output  # c is a movie, so not counted for photos


def split_all(*options):
    return runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "all", "--years", "2018", *options])


def test_split_media_all_copies_photos_and_videos_each_into_its_own_library(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    year_libraries(mac, [A, C], photos_2018=[])
    mac.make("videos-2018", 19607)
    add_photos(mac.libraries["videos-2018"], [])
    result = split_all("--check-duplicates")
    assert result.exit_code == 0
    assert mac.calls("copy-photos.applescript") == [
        ["Source", "photos-2018", "2018-01-01", "2019-01-01", "photos"],
        ["Source", "videos-2018", "2018-01-01", "2019-01-01", "videos"],
    ]
    assert "all-2018" not in result.output


def test_split_media_all_checks_both_kinds_for_missing_libraries_before_copying_either(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    year_libraries(mac, [A, C], photos_2018=[])  # videos-2018 is not in PowerPhotos
    result = split_all("--check-duplicates")
    assert result.exit_code == 1 and "Not in PowerPhotos: videos-2018" in result.output
    assert mac.calls("copy-photos.applescript") == []
    skipped = split_all("--on-missing", "skip", "--check-duplicates")
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["photos-2018"]
    assert "videos-2018: skipped (not in PowerPhotos)" in skipped.output


def test_split_media_all_skips_only_the_kind_that_is_complete(mac):
    year_libraries(mac, [A, C], photos_2018=[A])
    mac.make("videos-2018", 19607)
    add_photos(mac.libraries["videos-2018"], [])
    result = split_all("--check-duplicates")
    assert "photos-2018: up to date" in result.output
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["videos-2018"]


def test_each_split_library_line_says_how_many_hidden_items_its_dates_leave_out(mac):
    year_libraries(mac, [A, (*B, "burst"), (*C, "hidden")], photos_2018=[A])
    assert "photos-2018: up to date; 1 hidden item in Source not copied" in split().output  # b is a hidden photo; the movie is another kind
    year_libraries(mac, [A], photos_2018=[A])
    assert "hidden not copied" not in split().output


def test_count_in_range_counts_the_media_and_dates_asked_for():
    from collections import Counter
    from datetime import date
    from apple.photos.split_plan import count_in_range
    items = Counter({("2018-03-01 10:00:00", "1", "a.jpg"): 2, ("2018-03-02 10:00:00", "1", "v.mov"): 1, ("2019-01-01 00:00:00", "1", "b.jpg"): 1})
    assert count_in_range(items, date(2018, 1, 1), date(2019, 1, 1), "photos") == 2
    assert count_in_range(items, date(2018, 1, 1), date(2019, 1, 1), "all") == 3
    assert count_in_range(items, date(2018, 3, 2), date(2018, 3, 3), "videos") == 1


PLACEHOLDER = ("2018-03-05 10:00:00", 0, "shared.heic", "shared")  # shared with you, never saved: no original
NO_ORIGINAL = ("2018-03-06 10:00:00", 0, "gone.heic", "hidden")  # not shared, and its original is not stored either


def test_a_photo_whose_original_is_not_stored_is_marked_missing_and_shared_when_it_came_from_messages(mac):
    result = diff(mac, [A, PLACEHOLDER, NO_ORIGINAL], [], "--fields", "name,missing,shared", "--format", "json")
    assert [(r["name"], r["missing"], r["shared"]) for r in json.loads(result.stdout)] == [
        ("a.jpg", False, False), ("shared.heic", True, True), ("gone.heic", True, False),
    ]


def test_a_library_without_an_originals_folder_is_never_said_to_miss_files(tmp_path):
    library = make_library(tmp_path, "old", 15323)
    add_photos(library, [A, PLACEHOLDER])
    import shutil
    shutil.rmtree(library / "originals")
    assert not any(pp.library_photos(library).original_missing(key) for key in pp.library_photos(library).visible)


def test_copy_list_does_not_send_photos_without_an_original_and_tells_shared_ones_from_the_rest(mac):
    csv_text = "side,name,missing,shared\nleft,a.jpg,false,false\nleft,shared.heic,true,true\nleft,gone.heic,true,false\n"
    result = copy_list(["--list", "-"], csv_text)
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["a.jpg"], "false"]]
    assert "1 not copied: shared with you in Messages and not saved to the library Source" in result.output
    assert "Save them to the library in Photos, then run this again:\n    shared.heic" in result.output
    assert "1 not copied: the original is not stored in Source (it may live only in iCloud)" in result.output
    assert result.output.index("(no duplicate check)") < result.output.index("shared with you in Messages")  # after the library's own line
    only = copy_list(["--list", "-"], "name,missing,shared\nshared.heic,true,true\n")
    assert only.exit_code == 0 and "everything listed is hidden in Photos or has no original in Source" in only.output
    assert len(mac.calls("copy-named.applescript")) == 1


def test_split_by_default_leaves_out_photos_that_have_no_original_and_says_why(mac):
    year_libraries(mac, [A, PLACEHOLDER, (*NO_ORIGINAL[:3],)], photos_2018=[A])
    result = split()
    assert mac.calls("copy-named.applescript") == [] or all("shared.heic" not in call[2] and "gone.heic" not in call[2] for call in mac.calls("copy-named.applescript"))
    assert "shared with you in Messages and not saved to the library Source" in result.output
    assert "the original is not stored in Source" in result.output
    assert "photos-2018: nothing to copy" in result.output
    assert result.output.index("nothing to copy") < result.output.index("shared with you in Messages")


def test_a_placeholder_with_an_imported_copy_is_not_counted_and_the_copy_is(mac):
    imported = ("2018-03-05 10:00:05", 4321, "shared.heic")  # the same photo, imported: a real file, no link to Messages
    result = diff(mac, [PLACEHOLDER, imported], [])
    assert shown(result) == ["<  2018-03-05 10:00:05  4321  shared.heic"]
    assert "Not counted, shared placeholders that have an imported copy in the same library: 1 in Source, 0 in photos-2018." in result.output
    alone = diff(mac, [PLACEHOLDER], [])
    assert len(shown(alone)) == 1 and "Not counted" not in alone.output  # nothing replaces it
    far = diff(mac, [PLACEHOLDER, ("2018-03-09 10:00:00", 4321, "shared.heic")], [])
    assert len(shown(far)) == 2  # a photo of the same name days later is not its copy
    other = diff(mac, [PLACEHOLDER, ("2018-03-05 10:00:05", 4321, "other.heic")], [])
    assert len(shown(other)) == 2


def test_split_does_not_count_a_superseded_placeholder_when_judging_a_library(mac):
    imported = ("2018-03-05 10:00:05", 4321, "shared.heic")
    year_libraries(mac, [A, PLACEHOLDER, imported], photos_2018=[A, imported])
    result = split()
    assert "photos-2018: up to date" in result.output
    assert "Source: 1 shared placeholder with an imported copy in the same library not counted." in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_an_older_library_without_the_newer_columns_can_still_be_read(tmp_path):
    library = tmp_path / "old.photoslibrary"
    (library / "database").mkdir(parents=True)
    connection = sqlite3.connect(library / "database" / "Photos.sqlite")
    connection.execute("create table ZASSET (Z_PK integer primary key, ZDATECREATED real, ZFILENAME text, ZDIRECTORY text, ZTRASHEDSTATE integer, ZAVALANCHEUUID text)")
    connection.execute("create table ZADDITIONALASSETATTRIBUTES (Z_PK integer primary key, ZASSET integer, ZORIGINALFILESIZE integer, ZORIGINALFILENAME text)")
    connection.execute("insert into ZASSET values (1, 0, 'U1.jpg', 'A', 0, null)")
    connection.execute("insert into ZASSET values (2, 0, 'U2.jpg', 'A', 0, 'burst-1')")
    connection.executemany("insert into ZADDITIONALASSETATTRIBUTES values (?, ?, ?, ?)", [(1, 1, 10, "a.jpg"), (2, 2, 10, "b.jpg")])
    connection.commit()
    connection.close()
    found = pp.library_photos(library)
    assert sum(found.visible.values()) == 2 and not found.shared and not found.hidden
    assert sum(found.bursts.values()) == 0  # no visibility column, so nothing is hidden and a burst is only a hidden burst member


def test_counts_say_item_once_and_items_otherwise():
    from apple.photos.library_commands import count
    assert count(1, "hidden item") == "1 hidden item" and count(0, "burst member") == "0 burst members" and count(3, "photo") == "3 photos"


def copy_only_missing(mac, left, right, *options):
    for name, photos in (("Source", left), ("photos-2018", right)):
        add_photos(mac.libraries[name], photos)
    return runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing", *options])


def test_only_missing_copies_one_copy_of_each_missing_photo_by_its_identifier(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    add_photos(mac.libraries["Source"], [A, B, B, C, ("2018-03-04 09:00:00", 100, "h.jpg", "hidden"), ("2018-03-05 09:00:00", 0, "s.jpg", "shared")])
    add_photos(mac.libraries["photos-2018"], [A])
    result = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing"])
    assert result.exit_code == 0 and mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID2", "UUID4"], "false"]]  # b.jpg once (not twice) and c.mov
    assert mac.calls("copy-named.applescript") == [] and "h.jpg" in result.output and "s.jpg" in result.output  # hidden and shared are reported, not copied


def test_only_missing_limits_by_dates_and_says_when_nothing_is_missing(mac):
    copy_only_missing(mac, [A, B, C], [A], "--since", "2018-03-03")
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID3"], "false"]]
    nothing = copy_only_missing(mac, [A, A], [A])  # a lack of duplicates is not a lack
    assert nothing.exit_code == 0 and "no files copied (nothing is missing from photos-2018)" in nothing.output
    assert len(mac.calls("copy-ids.applescript")) == 1


def test_only_missing_cannot_be_combined_with_a_list(mac):
    for option in (["--list", "-"],):
        result = copy_only_missing(mac, [A], [], *option)
        assert result.exit_code == 2 and "--only-missing" in result.output


def test_split_check_passes_a_library_that_holds_its_year_and_copies_nothing(mac):
    year_libraries(mac, [A, B], photos_2018=[A, B])
    result = split("--check")
    assert result.exit_code == 0 and "photos-2018: in order" in result.output
    assert mac.calls("copy-photos.applescript") == [] and mac.calls("copy-ids.applescript") == []


def test_split_check_lists_what_is_outside_the_year_and_what_is_missing(mac):
    year_libraries(mac, [A, B], photos_2018=[A, ("2017-06-01 10:00:00", 50, "old.jpg"), ("2019-02-01 10:00:00", 50, "new.jpg")])
    result = split("--check")
    assert result.exit_code == 1 and "photos-2018: 2 outside 2018 (2017 1, 2019 1); 1 missing" in result.output
    assert "    outside: 2017-06-01 10:00:00 old.jpg" in result.output and "    missing: 2018-03-02 11:00:00 b.jpg" in result.output
    assert mac.calls("copy-photos.applescript") == []


def test_split_check_does_not_call_an_item_within_a_day_of_the_year_a_stray(mac):
    year_libraries(mac, [A], photos_2018=[A, ("2017-12-31 23:00:00", 50, "edge.jpg")])
    result = split("--check")
    assert result.exit_code == 0 and "in order (1 within a day of the year's ends)" in result.output


def test_split_check_fails_for_a_library_that_is_not_in_powerphotos(mac):
    year_libraries(mac, [A], photos_2018=[A])
    result = split("--check", "--to", "nothing-{year}", "--on-missing", "skip")
    assert result.exit_code == 1 and "nothing-2018: library missing" in result.output


def test_split_check_cannot_be_combined_with_what_it_does_not_do(mac):
    year_libraries(mac, [A], photos_2018=[A])
    for option in ("--check-duplicates", "--no-skip-complete"):
        assert split("--check", option).exit_code == 2


def split_check_without_a_source(*options):
    return runner.invoke(app, ["photos", "split", "--check", "--media", "photos", "--years", "2017-2018", *options])


def test_split_check_without_a_source_checks_the_libraries_that_exist_for_the_years(mac):
    add_photos(mac.libraries["photos-2018"], [A, ("2017-06-01 10:00:00", 50, "old.jpg")])
    result = split_check_without_a_source()
    assert result.exit_code == 1 and "photos-2018: 1 outside 2018 (2017 1)" in result.output
    assert "No library, and none needed: photos-2017" in result.output
    add_photos(mac.libraries["photos-2018"], [A])
    assert split_check_without_a_source().exit_code == 0
    add_photos(mac.libraries["photos-2018"], [])
    empty = split_check_without_a_source()
    assert empty.exit_code == 1 and "photos-2018: holds no photos from 2018" in empty.output


def test_split_check_with_a_source_does_not_ask_for_libraries_of_years_it_has_no_photos_in(mac):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A, B])
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2017-2018"])
    assert result.exit_code == 0 and "photos-2018: in order" in result.output and "No library, and none needed: photos-2017" in result.output


def test_split_needs_from_unless_it_only_checks(mac):
    result = runner.invoke(app, ["photos", "split", "--media", "photos", "--years", "2018"])
    assert result.exit_code == 2 and "--from is needed" in result.output


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch, tmp_path):
    """--fix looks at a library being copied into every few seconds: do not really wait. PowerPhotos' reports and the list of
    the blacklist are the test's own, never the real ones."""
    monkeypatch.setenv("APPLE_PHOTOS_POWERPHOTOS_REPORTS_DIR", str(tmp_path / "Reports"))
    monkeypatch.setenv("APPLE_PHOTOS_BLACKLIST_FILE", str(tmp_path / "blacklist.json"))
    monkeypatch.setenv("APPLE_PHOTOS_CACHE_FILE", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(lc, "POLL_SECONDS", 0)
    monkeypatch.setattr(lc, "SECONDS_PER_MINUTE", 1)  # so that --timeout N is N looks at an unchanged library


OLD = ("2017-06-01 10:00:00", 50, "old.jpg")


def fix(*options):
    return runner.invoke(app, ["photos", "split", "--fix", "--media", "photos", "--years", "2018", *options])


def trashing_updates_the_database(monkeypatch, mac):
    """PowerPhotos' delete moves photos to Recently Deleted, which the database shows as trashed: the fake does not write, so do it."""
    real = lc.trash_ids

    def trash(libraries, library, ids):
        result = real(libraries, library, ids)
        connection = sqlite3.connect(mac.libraries[library] / "database" / "Photos.sqlite")
        connection.execute("update ZASSET set ZTRASHEDSTATE = 1 where ZUUID in (%s)" % ",".join("?" * len(ids)), ids)
        connection.commit()
        connection.close()
        return result

    monkeypatch.setattr(lc, "trash_ids", trash)


def write_report(destination, results=None, error=None, finished=True, operation_log=None):
    """The report PowerPhotos writes for a copy (a .ppreport bundle with Assets.db, as its AGENTS.md documents).
    results: {photo identifier: (IPAssetResult, description)}; error: the operation's error as JSON text."""
    reports = Path(os.environ["APPLE_PHOTOS_POWERPHOTOS_REPORTS_DIR"])
    bundle = reports / f"2026-11-12-{len(list(reports.glob('*.ppreport')) if reports.exists() else []):06d} Copy {len(results or {})} photos to library \u201c{destination}\u201d.ppreport"
    bundle.mkdir(parents=True)
    connection = sqlite3.connect(bundle / "Assets.db")
    connection.execute("create table assets (libraryIdentifier text, uuid text, filename text, status integer, result integer, resultDescription text)")
    connection.execute("create table reportMedatata (operationType integer, wasCancelled integer, startDate real, endDate real, error blob)")
    for uuid, (result, description) in (results or {}).items():
        connection.execute("insert into assets values ('L', ?, 'x.jpg', 1, ?, ?)", (uuid, result, description))
    connection.execute("insert into reportMedatata values (1, 0, 1.0, ?, ?)", (2.0 if finished else None, error.encode() if error else None))
    connection.commit()
    connection.close()
    if operation_log is not None:
        (bundle / "operation.log").write_text(operation_log, encoding="utf-8")


def blacklisted():
    """The entries of the blacklist file the test run wrote."""
    return json.loads(Path(os.environ["APPLE_PHOTOS_BLACKLIST_FILE"]).read_text())["entries"]


IMPORT_FAILED = 'Photos failed to import the photo \u201c{name}\u201d: uuid {uuid}/L0/001 (Error Domain=PHPhotosErrorDomain Code=3302 "Invalid resource (Invalid resource (code 3302))")'


def copying_updates_the_database(monkeypatch, mac, arrives=None, fails=(), error=None, report=True):
    """The fake PowerPhotos copies nothing, so put the copied photos into the destination's database, and write the report
    PowerPhotos would. arrives: the names that arrive (default: all). fails: names it records a failed import for, which do
    not arrive. error: an error (JSON text) it ends the whole operation with, copying nothing. report=False: it writes none."""
    real = lc.copy_one

    def copy(libraries, source, destination, since, before, media, mode, names=None, check_duplicates=True, ids=None):
        result = real(libraries, source, destination, since, before, media, mode, names, check_duplicates, ids)
        held = pp.library_photos(mac.libraries[destination])
        have = list((key[0], key[1] and int(key[1]), key[2]) for key in held.visible.elements())
        photos = pp.library_photos(mac.libraries[source])
        results = {}
        for key in photos.visible.elements():
            if ids is not None:
                copied = any(uuid in ids for uuid in photos.uuids.get(key, []))
            elif names is not None:
                copied = pp.normalized_name(key[2]) in {pp.normalized_name(name) for name in names}
            else:  # a date range of one media
                copied = (since is None or key[0][:10] >= since.isoformat()) and (before is None or key[0][:10] < before.isoformat()) and bool(pp.of_media(Counter({key: 1}), media))
            if not copied or key in held.visible:
                continue
            uuid = photos.uuids[key][0]
            if key[2] in fails:
                results[uuid + "/L0/001"] = (8, IMPORT_FAILED.format(name=key[2], uuid=uuid))
            elif arrives is None or key[2] in arrives:
                results[uuid + "/L0/001"] = (6, "")
                have.append((key[0], int(key[1]), key[2]))
        if report:
            write_report(destination, results, error=error)
        if error is None:
            add_photos(mac.libraries[destination], have)
        return result

    monkeypatch.setattr(lc, "copy_one", copy)


def test_fix_copies_a_stray_the_year_library_lacks_without_the_duplicate_check_then_moves_it_out(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [["photos-2018", "photos-2017", ["UUID2"], "false"]]  # the stray's Photos identifier; PowerPhotos' duplicate check off
    assert "photos-2017: 0 items already there, copied 1 item; 1 of 1 confirmed in its database" in result.output
    assert mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]]
    assert "photos-2018: moved 1 to its Recently Deleted: old.jpg" in result.output
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_fix_does_not_copy_a_stray_the_year_library_already_has_and_only_removes_it(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [OLD])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [] and "photos-2017: 1 item already there, copied 0 items; 1 of 1 confirmed" in result.output
    assert mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]] and result.exit_code == 0


def test_fix_moves_a_stray_that_shares_its_file_name_with_another_photo_by_identifier(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    in_year = ("2018-05-05 10:00:00", 50, "old.jpg")  # the same name as the stray, and where it belongs
    add_photos(mac.libraries["photos-2018"], [A, OLD, in_year])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert mac.calls("copy-ids.applescript")[0][2] == ["UUID2"] and mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]]
    assert result.exit_code == 0 and "photos-2018 (after the fix): in order" in result.output


def test_fix_does_not_move_an_item_out_unless_the_other_library_has_it(mac):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])  # the fake PowerPhotos copies nothing, so it does not arrive
    result = fix()
    assert "0 of 1 confirmed" in result.output and "NOT confirmed in photos-2017, so it stays in photos-2018: 2017-06-01 10:00:00 old.jpg" in result.output
    assert mac.calls("trash-ids.applescript") == [] and result.exit_code == 1


def test_fix_says_what_powerphotos_would_not_move_out(mac, monkeypatch):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [OLD])
    monkeypatch.setenv("FAKE_TRASH_MISSING", "UUID2")
    monkeypatch.setenv("FAKE_TRASH_DELETED", "0")
    result = fix()
    assert "not moved out of photos-2018 (PowerPhotos lists no photo with that identifier" in result.output and "old.jpg" in result.output and result.exit_code == 1


def test_fix_leaves_alone_what_it_cannot_place(mac):
    add_photos(mac.libraries["photos-2018"], [A, ("2016-01-01 10:00:00", 50, "x.jpg"), OLD])
    result = fix()
    assert "not moved: 2016-01-01 10:00:00 x.jpg (no library for 2016)" in result.output
    assert "not moved: 2017-06-01 10:00:00 old.jpg (no library for 2017)" in result.output
    assert mac.calls("copy-ids.applescript") == [] and result.exit_code == 1


def test_fix_dry_run_copies_nothing(mac):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    result = fix("--dry-run")
    assert "would copy 1 item into photos-2017, then move them out of photos-2018: old.jpg" in result.output
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == []


def test_fix_names_a_year_library_that_is_planned_but_not_in_powerphotos(mac):
    add_photos(mac.libraries["Source"], [A, OLD])
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    result = fix("--from", "Source")
    assert "not moved: 2017-06-01 10:00:00 old.jpg (photos-2017 is not in PowerPhotos)" in result.output and mac.calls("copy-ids.applescript") == []


def test_the_id_applescripts_do_not_read_or_filter_the_whole_library():
    """Reading the id of every photo of a large library into AppleScript and looping over them, or filtering the library by id,
    makes PowerPhotos examine every photo and the copy looks hung. copy-ids reaches each photo by its id (photo id "..." of
    library ...), which PowerPhotos looks up directly; trash-ids, which must delete one specifier, uses one filter for a batch."""
    copy = (pp.APPLESCRIPTS / "copy-ids.applescript").read_text()
    assert 'photo id ((contents of anId) & "/L0/001") of sourceLibrary' in copy and "duplicate wanted" in copy and '"missing"' in copy
    code = "\n".join(line for line in copy.splitlines() if not line.lstrip().startswith("--"))
    assert " whose " not in code and "run script" not in code
    trash = (pp.APPLESCRIPTS / "trash-ids.applescript").read_text()
    assert "run script" in trash and 'whose " & tests' in trash and '"/L0/001"' in trash and '"missing"' in trash
    assert "id of photos of" not in trash and "count of allIds" not in trash and "photos of theLibrary" not in trash


def test_trash_ids_deletes_a_batch_with_one_filter_because_powerphotos_cannot_delete_a_list():
    """delete {photo, photo} is an error ("Can't make ... into type specifier") and one delete per photo asks for confirmation each
    time: a filter (every photo whose id is "a" or id is "b") is one specifier, and so one confirmation."""
    script = (pp.APPLESCRIPTS / "trash-ids.applescript").read_text()
    assert "delete toDelete" not in script and "delete (contents of" not in script and "then delete " in script and '" or "' in script


def test_fix_removes_the_strays_of_every_year_in_one_delete_after_all_are_copied(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    older = ("2016-02-02 10:00:00", 50, "older.jpg")
    for name in ("photos-2017", "photos-2016"):
        mac.make(name, 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD, older])
    add_photos(mac.libraries["photos-2017"], [])
    add_photos(mac.libraries["photos-2016"], [older])  # already there
    result = fix()
    assert [call[1] for call in mac.calls("copy-ids.applescript")] == ["photos-2017"]
    trash = mac.calls("trash-ids.applescript")
    assert len(trash) == 1 and trash[0][0] == "photos-2018" and sorted(trash[0][1]) == ["UUID2", "UUID3"]  # one delete, for both years
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_fix_stops_at_the_first_library_a_copy_fails_in_and_removes_nothing(mac):
    older = ("2016-02-02 10:00:00", 50, "older.jpg")
    unlucky = ("2015-03-03 10:00:00", 50, "unlucky.jpg")
    for name in ("photos-2017", "photos-2016", "photos-2015"):
        mac.make(name, 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD, older, unlucky])
    add_photos(mac.libraries["photos-2017"], [OLD])  # already there: only the removal is left
    add_photos(mac.libraries["photos-2016"], [])
    add_photos(mac.libraries["photos-2015"], [])  # the fake PowerPhotos copies nothing, as when PowerPhotos fails
    result = fix()
    assert [call[1] for call in mac.calls("copy-ids.applescript")] == ["photos-2015"]  # the first, in order; 2016 was not tried
    assert mac.calls("trash-ids.applescript") == []  # nothing was removed, not even what was confirmed
    assert "Stopped: the copy into photos-2015 did not complete" in result.output and "(after the fix)" not in result.output and result.exit_code == 1


def test_fix_waits_for_powerphotos_to_finish_copying_before_it_confirms_and_deletes(mac, monkeypatch):
    """PowerPhotos can hand back control while it is still writing: the library is looked at until the copy has arrived."""
    trashing_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:  # the copy lands after the third look
            add_photos(mac.libraries["photos-2017"], [OLD])

    monkeypatch.setattr(lc.time, "sleep", sleep)
    result = fix()
    assert len(waits) == 3 and "waiting for PowerPhotos to finish copying into photos-2017" in result.output
    assert "1 of 1 confirmed" in result.output and mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]] and result.exit_code == 0


def test_fix_keeps_waiting_while_the_library_is_still_changing_and_gives_up_when_it_is_not(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) <= 5:  # other photos keep arriving first: it is busy, so it is not given up on
            add_photos(mac.libraries["photos-2017"], [("2017-01-0%d 10:00:00" % len(waits), 50, "other%d.jpg" % len(waits))])
        elif len(waits) == 6:
            add_photos(mac.libraries["photos-2017"], [OLD])

    monkeypatch.setattr(lc.time, "sleep", sleep)
    busy = fix("--timeout", "2")
    assert len(waits) == 6 and "1 of 1 confirmed" in busy.output
    waits.clear()
    monkeypatch.setattr(lc.time, "sleep", lambda seconds: waits.append(seconds))
    add_photos(mac.libraries["photos-2018"], [A, OLD])  # the first run moved it out
    add_photos(mac.libraries["photos-2017"], [])
    still = fix("--timeout", "2")
    assert len(waits) == 2 and "0 of 1 confirmed" in still.output  # gives up after 2 unchanged looks


def test_fix_waits_for_the_delete_to_land_before_it_checks_the_library_again(mac, monkeypatch):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [OLD])
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:  # the delete lands after the second look
            connection = sqlite3.connect(mac.libraries["photos-2018"] / "database" / "Photos.sqlite")
            connection.execute("update ZASSET set ZTRASHEDSTATE = 1 where ZUUID = 'UUID2'")
            connection.commit()
            connection.close()

    monkeypatch.setattr(lc.time, "sleep", sleep)
    result = fix()
    assert len(waits) == 2 and "waiting for PowerPhotos to finish deleting from photos-2018" in result.output
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_check_lists_a_video_in_a_photos_library_and_a_file_of_neither_kind(mac):
    add_photos(mac.libraries["photos-2018"], [A, C, ("2018-04-04 10:00:00", 50, "note.webp")])
    result = runner.invoke(app, ["photos", "split", "--check", "--media", "photos", "--years", "2018"])
    assert result.exit_code == 1 and "1 video in a photos library; 1 of a type that is neither photo nor video" in result.output
    assert "    wrong kind: 2018-03-03 12:00:00 c.mov" in result.output and "    unknown type: 2018-04-04 10:00:00 note.webp" in result.output


def test_fix_moves_a_video_out_of_a_photos_library_into_the_videos_library_of_its_year(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    mac.make("videos-2018", 19607)
    add_photos(mac.libraries["photos-2018"], [A, C])
    add_photos(mac.libraries["videos-2018"], [])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [["photos-2018", "videos-2018", ["UUID2"], "false"]]
    assert mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]]
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_fix_leaves_a_file_of_neither_kind_where_it_is(mac):
    add_photos(mac.libraries["photos-2018"], [A, ("2018-04-04 10:00:00", 50, "note.webp")])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == []
    assert "unknown type: 2018-04-04 10:00:00 note.webp" in result.output and result.exit_code == 1


def test_a_3g2_file_is_a_video():
    kept = pp.of_media(Counter({("2018-03-01 10:00:00", "5", "clip.3G2"): 1, ("2018-03-01 10:00:00", "5", "a.jpg"): 1}), "videos")
    assert list(kept) == [("2018-03-01 10:00:00", "5", "clip.3G2")]


def test_fix_says_why_powerphotos_could_not_copy_an_item(mac, monkeypatch):
    mac.make("photos-2017", 19607)
    hidden = ("2017-06-02 10:00:00", 50, "hidden.jpg", "hidden")
    gone = ("2017-06-03 10:00:00", 0, "gone.jpg")  # no stored original
    add_photos(mac.libraries["photos-2018"], [A, OLD, hidden, gone])
    add_photos(mac.libraries["photos-2017"], [])
    monkeypatch.setenv("FAKE_NAMED_MISSING", "UUID2,UUID3,UUID4")
    result = fix()
    assert "3 not copied: the database of photos-2018 has them, but PowerPhotos lists no photo" in result.output
    assert "2017-06-02 10:00:00 hidden.jpg (hidden in Photos)" in result.output
    assert "2017-06-03 10:00:00 gone.jpg (its original is not stored in the library)" in result.output
    assert "2017-06-01 10:00:00 old.jpg (no reason found in the database; if Photos lists it under Recovered, restore it there)" in result.output


def test_fix_with_a_source_also_copies_what_the_library_lacks(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID2"], "false"]]  # b.jpg, by its Photos identifier, with no duplicate check
    assert "photos-2018: asked PowerPhotos to copy 1 item from Source" in result.output
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_fix_dry_run_says_what_it_would_copy_from_the_source(mac):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source", "--dry-run")
    assert "would copy 1 item that photos-2018 lacks from Source" in result.output and mac.calls("copy-ids.applescript") == []


def test_fix_leaves_a_photo_with_no_stored_original_out_of_the_copy_and_says_so(mac):
    add_photos(mac.libraries["Source"], [A, ("2018-03-02 11:00:00", 0, "b.jpg")])  # a placeholder: no stored original
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert mac.calls("copy-ids.applescript") == []  # nothing PowerPhotos could copy is sent
    assert "1 not copied: the original is not stored in Source (it may live only in iCloud)" in result.output and "    b.jpg" in result.output and result.exit_code == 1


def test_a_photo_the_source_holds_twice_is_not_missing_from_a_library_that_has_it_once(mac):
    add_photos(mac.libraries["Source"], [A, A, B])
    add_photos(mac.libraries["photos-2018"], [A, B])
    check = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert check.exit_code == 0 and "photos-2018: in order" in check.output
    assert "up to date" in split().output and mac.calls("copy-named.applescript") == []


def test_a_photo_the_source_holds_twice_is_missing_once_from_a_library_that_lacks_it(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    add_photos(mac.libraries["Source"], [A, A, B])
    add_photos(mac.libraries["photos-2018"], [B])
    check = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert check.exit_code == 1 and "photos-2018: 1 missing" in check.output
    fixed = fix("--from", "Source")
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID1"], "false"]]  # one copy: duplicates are not carried over
    assert "photos-2018 (after the fix): in order" in fixed.output and fixed.exit_code == 0


def test_diff_counts_a_photo_once_unless_told_to_include_duplicates(mac):
    ignored = diff(mac, [A, A, B], [A, B])
    assert ignored.exit_code == 0 and shown(ignored) == [] and "Only in Source: 0. Only in photos-2018: 0. In both: 2." in ignored.output
    counted = diff(mac, [A, A, B], [A, B], "--include-duplicates")
    assert counted.exit_code == 1 and shown(counted) == ["<  2018-03-01 10:00:00  100  a.jpg"] and "Only in Source: 1. Only in photos-2018: 0. In both: 2." in counted.output
    still = diff(mac, [A, B], [B])
    assert still.exit_code == 1 and shown(still) == ["<  2018-03-01 10:00:00  100  a.jpg"]  # a photo the other library lacks altogether is still a difference

def test_fix_moves_one_copy_of_a_stray_held_several_times_and_removes_every_copy(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD, OLD, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [["photos-2018", "photos-2017", ["UUID2"], "false"]]  # one copy goes across
    assert mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2", "UUID3", "UUID4"]]]  # and all three leave
    assert "photos-2018 (after the fix): in order" in result.output and result.exit_code == 0


def test_fix_copies_nothing_for_a_stray_the_right_library_has_but_still_removes_every_copy(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD, OLD])
    add_photos(mac.libraries["photos-2017"], [OLD])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2", "UUID3"]]]
    assert result.exit_code == 0


def test_split_by_default_copies_one_copy_of_the_photo_and_not_another_that_shares_its_name(mac):
    other_year = ("2019-05-05 10:00:00", 100, "b.jpg")  # the same file name, in the next year's range
    year_libraries(mac, [B, B, other_year], photos_2018=[])
    split()
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID1"], "false"]] and mac.calls("copy-named.applescript") == []


def test_a_photo_that_is_hidden_in_the_library_is_not_missing_from_it(mac):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A, (*B, "hidden")])
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert result.exit_code == 0 and "photos-2018: in order" in result.output
    assert "up to date" in split().output and mac.calls("copy-ids.applescript") == []


def test_check_says_what_a_library_holds_under_the_name_of_a_photo_it_lacks(mac):
    add_photos(mac.libraries["Source"], [A, B, C])
    add_photos(mac.libraries["photos-2018"], [A, ("2018-03-02 14:00:00", 100, "B.JPG"), ("2018-03-03 12:00:00", 90, "c.mov")])
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "all", "--years", "2018", "--to", "photos-{year}"])
    assert "    missing: 2018-03-02 11:00:00 b.jpg (the library has this name: dated 2018-03-02 14:00:00)" in result.output
    assert "    missing: 2018-03-03 12:00:00 c.mov (the library has this name: 90 bytes)" in result.output
    assert "    missing: 2018-03-05" not in result.output


def test_a_copy_that_differs_by_an_hour_or_a_little_in_size_is_the_same_photo(mac):
    source = [("2011-04-10 10:57:49", 1967270, "IMG_4254.JPG"), ("2011-04-10 11:57:49", 1967270, "IMG_4254.JPG"), ("2011-05-30 00:53:30", 1864453, "IMAG0020.jpg")]
    dest = [("2011-04-10 10:57:49", 1968640, "IMG_4254.JPG"), ("2011-05-29 23:53:30", 1865823, "imag0020.JPG")]  # 1370 bytes more, an hour earlier
    add_photos(mac.libraries["Source"], source)
    add_photos(mac.libraries["photos-2018"], dest)
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2011", "--to", "photos-2018"])
    assert result.exit_code == 0 and "photos-2018: in order" in result.output  # both source copies of IMG_4254, and the lower-cased name


def test_a_copy_further_off_in_time_or_size_is_not_the_same_photo(mac):
    source = [("2011-04-10 10:00:00", 1000000, "a.jpg"), ("2011-04-10 10:00:00", 1000000, "b.jpg"), ("2011-04-10 10:00:00", 1000000, "c.jpg")]
    dest = [("2011-04-10 13:00:00", 1000000, "a.jpg"), ("2011-04-10 10:00:00", 1100000, "b.jpg"), ("2011-04-10 11:59:00", 1005000, "c.jpg")]
    add_photos(mac.libraries["Source"], source)
    add_photos(mac.libraries["photos-2018"], dest)
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2011", "--to", "photos-2018"])
    assert "photos-2018: 2 missing" in result.output and "missing: 2011-04-10 10:00:00 a.jpg" in result.output and "missing: 2011-04-10 10:00:00 b.jpg" in result.output
    assert "missing: 2011-04-10 10:00:00 c.jpg" not in result.output  # under two hours and half a percent


def test_fix_does_not_copy_a_stray_the_right_library_has_a_near_copy_of(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    stray = ("2017-06-01 10:00:00", 1967270, "old.jpg")
    near = ("2017-06-01 11:00:00", 1968640, "OLD.JPG")  # an hour later, 1370 bytes more, another case
    add_photos(mac.libraries["photos-2018"], [A, stray])
    add_photos(mac.libraries["photos-2017"], [near])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == [["photos-2018", ["UUID2"]]]
    assert "photos-2017: 1 item already there, copied 0 items; 1 of 1 confirmed" in result.output and result.exit_code == 0


def test_fix_copies_a_stray_when_the_right_library_only_has_something_further_off(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, ("2017-06-01 10:00:00", 1967270, "old.jpg")])
    add_photos(mac.libraries["photos-2017"], [("2017-06-01 14:00:00", 1967270, "old.jpg")])  # four hours off
    result = fix()
    assert mac.calls("copy-ids.applescript") == [["photos-2018", "photos-2017", ["UUID2"], "false"]] and result.exit_code == 0


NEAR_SOURCE = [("2011-04-10 10:57:49", 1967270, "IMG_4254.JPG")]
NEAR_COPY = [("2011-04-10 11:57:49", 1968640, "IMG_4254.JPG")]  # an hour later, 1370 bytes more


def test_strict_counts_a_photo_as_there_only_with_its_exact_date_size_and_name(mac):
    add_photos(mac.libraries["Source"], NEAR_SOURCE)
    add_photos(mac.libraries["photos-2018"], NEAR_COPY)
    options = ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2011", "--to", "photos-2018"]
    assert runner.invoke(app, options).exit_code == 0
    strict = runner.invoke(app, [*options, "--strict"])
    assert strict.exit_code == 1 and "photos-2018: 1 missing" in strict.output


def test_strict_fix_copies_a_stray_whose_near_copy_is_in_the_right_library(mac, monkeypatch):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, ("2017-06-01 10:00:00", 1967270, "old.jpg")])
    add_photos(mac.libraries["photos-2017"], [("2017-06-01 11:00:00", 1968640, "old.jpg")])
    assert fix().exit_code in (0, 1) and mac.calls("copy-ids.applescript") == []
    fix("--strict")
    assert [call[:2] for call in mac.calls("copy-ids.applescript")] == [["photos-2018", "photos-2017"]]


def test_only_missing_strict_copies_a_photo_whose_near_copy_the_destination_has(mac):
    result = copy_only_missing(mac, NEAR_SOURCE, NEAR_COPY)
    assert "nothing is missing from photos-2018" in result.output and mac.calls("copy-ids.applescript") == []
    add_photos(mac.libraries["photos-2018"], NEAR_COPY)
    strict = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing", "--strict"])
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID1"], "false"]]
    assert runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--strict", "--since", "2011-01-01"]).exit_code == 2


def test_the_id_applescripts_do_not_use_applescript_words_as_variable_names():
    """`set lines to ""` fails ("Can't set every line to"): lines, selection and the like are terms, not variables."""
    import re
    for name in ("copy-ids", "trash-ids"):
        script = (pp.APPLESCRIPTS / f"{name}.applescript").read_text()
        assigned = set(re.findall(r"^\s*set (\w+) to", script, re.M))
        assert not assigned & {"lines", "line", "selection", "words", "characters", "paragraphs", "photos", "text", "item", "items", "result", "properties"}


def test_fix_says_when_none_of_the_photos_it_copied_arrived(mac):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])  # the fake PowerPhotos copies nothing, as when PowerPhotos fails every photo
    result = fix("--from", "Source")
    assert "none of the 1 photo arrived in photos-2018: PowerPhotos probably failed to copy them" in result.output
    assert "Reports section" in result.output and "killall photolibraryd" in result.output and result.exit_code == 1


def test_fix_says_how_many_of_the_photos_it_copied_did_not_arrive(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, arrives={"b.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "1 of the 2 photos did not arrive in photos-2018: see PowerPhotos' Reports section for why." in result.output
    assert "killall" not in result.output


def test_fix_does_not_copy_a_stray_that_has_no_stored_original_and_leaves_it_where_it_is(mac, monkeypatch):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, ("2017-06-01 10:00:00", 0, "old.jpg")])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert mac.calls("copy-ids.applescript") == [] and mac.calls("trash-ids.applescript") == []
    assert "not copied to photos-2017, so it stays in photos-2018: 2017-06-01 10:00:00 old.jpg (its original is not stored in photos-2018)" in result.output
    assert "none of the" not in result.output and result.exit_code == 1


def test_fix_says_when_none_of_the_strays_it_copied_arrived(mac):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert "none of the 1 photo arrived in photos-2017" in result.output and "NOT confirmed in photos-2017, so it stays in photos-2018" in result.output


def test_fix_stops_before_the_next_year_when_a_copy_from_the_source_fails(mac):
    add_photos(mac.libraries["Source"], [A, ("2019-03-01 10:00:00", 100, "b19.jpg")])
    add_photos(mac.libraries["photos-2018"], [])
    add_photos(mac.libraries["photos-2019"], [])
    result = runner.invoke(app, ["photos", "split", "--fix", "--from", "Source", "--media", "photos", "--years", "2018-2019"])
    assert [call[1] for call in mac.calls("copy-ids.applescript")] == ["photos-2018"]  # photos-2019 was not tried
    assert "Stopped: the copy into photos-2018 did not complete" in result.output and "photos-2019" not in result.output.split("Stopped:")[1] and result.exit_code == 1


def test_fix_stops_when_only_some_of_the_photos_arrive(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, arrives={"b.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D, ("2019-03-01 10:00:00", 100, "b19.jpg")])
    add_photos(mac.libraries["photos-2018"], [A])
    add_photos(mac.libraries["photos-2019"], [])
    result = runner.invoke(app, ["photos", "split", "--fix", "--from", "Source", "--media", "photos", "--years", "2018-2019"])
    assert "1 of the 2 photos did not arrive in photos-2018" in result.output and "Stopped:" in result.output
    assert [call[1] for call in mac.calls("copy-ids.applescript")] == ["photos-2018"]


def test_copy_waits_for_the_photos_to_arrive_and_says_when_they_do_not(mac):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    options = ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing"]
    result = runner.invoke(app, options)  # the fake PowerPhotos copies nothing
    assert result.exit_code == 1 and "waiting for PowerPhotos to finish copying into photos-2018" in result.output
    assert "none of the 1 photo arrived in photos-2018: PowerPhotos probably failed to copy them" in result.output
    assert "still waiting: 0 of 1 photos have arrived in photos-2018" in result.output  # progress while it waits


def test_copy_waits_for_a_list_and_a_date_range_too(mac, monkeypatch):
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [])
    listed = copy_list(["--list", "-"], "a.jpg\nb.jpg\n")
    assert listed.exit_code == 1 and "none of the 2 photos arrived in photos-2018" in listed.output
    ranged = runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--since", "2018-03-01", "--until", "2018-03-02", "--media", "photos"])
    assert ranged.exit_code == 1 and "none of the 2 photos arrived in photos-2018" in ranged.output
    copying_updates_the_database(monkeypatch, mac)
    assert copy_list(["--list", "-"], "a.jpg\nb.jpg\n").exit_code == 0


def test_timeout_says_how_long_an_unchanged_library_is_waited_for(mac, monkeypatch):
    add_photos(mac.libraries["Source"], [A])
    add_photos(mac.libraries["photos-2018"], [])
    looks = []
    monkeypatch.setattr(lc.time, "sleep", lambda seconds: looks.append(seconds))
    options = ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing"]
    runner.invoke(app, [*options, "--timeout", "3"])
    assert len(looks) == 3  # three unchanged looks, a "minute" each in this test
    looks.clear()
    runner.invoke(app, [*options, "--timeout", "7"])
    assert len(looks) == 7
    assert runner.invoke(app, [*options, "--timeout", "0"]).exit_code == 2  # at least a few seconds


def test_the_default_timeout_is_fifteen_minutes_and_split_has_the_option_too(mac, monkeypatch):
    assert lc.DEFAULT_TIMEOUT == 15.0
    looks = []
    monkeypatch.setattr(lc.time, "sleep", lambda seconds: looks.append(seconds))
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2018", "--timeout", "4"])
    assert len(looks) == 4


def test_a_photo_powerphotos_reports_a_failed_import_for_is_recorded_as_uncopyable_and_the_run_goes_on(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, fails={"d.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "PowerPhotos could not copy 2018-03-04 09:00:00 d.jpg (Photos failed to import the photo \u201cd.jpg\u201d: uuid UUID3/L0/001" in result.output and "Code=3302" in result.output
    assert "it is blacklisted and left out from now on" in result.output and "Stopped:" not in result.output  # a logged failure of one photo does not stop the run
    assert "none of the" not in result.output and "did not arrive" not in result.output
    recorded = blacklisted()
    assert [(entry["library"], entry["uuid"], entry["name"], entry["date"], entry["kind"]) for entry in recorded] == [("Source", "UUID3", "d.jpg", "2018-03-04 09:00:00", "uncopyable")]
    assert "(after the fix): 1 missing" in result.output and result.exit_code == 1  # it is still missing, but nothing is left to try


def test_an_uncopyable_photo_is_left_out_of_later_copies_and_tagged_on_its_missing_line(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, fails={"d.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    first = fix("--from", "Source")
    assert "    missing: 2018-03-04 09:00:00 d.jpg\n" in first.output  # not blacklisted yet when it is first looked at
    before = len(mac.calls("copy-ids.applescript"))
    again = fix("--from", "Source")
    assert len(mac.calls("copy-ids.applescript")) == before  # b arrived, d is left out: nothing to ask PowerPhotos for
    assert f"    missing: 2018-03-04 09:00:00 d.jpg [blacklisted on {date.today().isoformat()}]" in again.output
    assert "Photos failed to import" not in again.output and "left out, blacklisted" not in again.output and "Stopped:" not in again.output  # the error is not printed again


def test_ignore_blacklist_shows_the_plain_missing_line_and_copies_it_again(mac, monkeypatch):
    add_photos(mac.libraries["Source"], [A, D])
    add_photos(mac.libraries["photos-2018"], [A])
    Path(os.environ["APPLE_PHOTOS_BLACKLIST_FILE"]).write_text(json.dumps({"version": 1, "entries": [
        {"kind": "uncopyable", "library": "Source", "uuid": "UUID2", "name": "d.jpg", "date": "2018-03-04 09:00:00", "reason": "r", "recorded": "2026-11-11T10:00:00"}]}))
    tagged = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert "    missing: 2018-03-04 09:00:00 d.jpg [blacklisted on 2026-11-11]" in tagged.output
    plain = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018", "--ignore-blacklist"])
    assert "    missing: 2018-03-04 09:00:00 d.jpg\n" in plain.output and "blacklisted" not in plain.output


def test_ignore_blacklist_tries_a_listed_photo_again_and_forgets_it_when_it_copies(mac, monkeypatch):
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A, B])
    Path(os.environ["APPLE_PHOTOS_BLACKLIST_FILE"]).write_text(json.dumps({"version": 1, "entries": [
        {"kind": "uncopyable", "library": "Source", "uuid": "UUID3", "name": "d.jpg", "date": "2018-03-04 09:00:00", "reason": "Photos failed to import it", "recorded": "2026-11-12T10:00:00"}]}))
    copying_updates_the_database(monkeypatch, mac)  # this time it copies
    result = fix("--from", "Source", "--ignore-blacklist")
    assert mac.calls("copy-ids.applescript") == [["Source", "photos-2018", ["UUID3"], "false"]] and "left out, recorded" not in result.output
    assert blacklisted() == [] and result.exit_code == 0


def test_photos_that_do_not_arrive_with_no_logged_failure_still_stop_the_run(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, arrives={"b.jpg"})  # d.jpg does not arrive, and PowerPhotos logs nothing
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "1 of the 2 photos did not arrive in photos-2018" in result.output and "Stopped:" in result.output
    assert not Path(os.environ["APPLE_PHOTOS_BLACKLIST_FILE"]).exists()  # nothing is recorded without evidence


def test_copy_only_missing_records_and_skips_an_uncopyable_photo_too(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, fails={"d.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    options = ["photos", "copy", "--from", "Source", "--to", "photos-2018", "--only-missing"]
    first = runner.invoke(app, options)
    assert "PowerPhotos could not copy 2018-03-04 09:00:00 d.jpg" in first.output and first.exit_code == 0
    second = runner.invoke(app, options)
    assert "no files copied (everything missing is left out, blacklisted)" in second.output and "Photos failed to import" not in second.output
    assert len(mac.calls("copy-ids.applescript")) == 1


def test_the_default_split_records_and_skips_an_uncopyable_photo(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, fails={"d.jpg"})
    add_photos(mac.libraries["Source"], [A, B, D])
    add_photos(mac.libraries["photos-2018"], [A])
    first = split()
    assert "PowerPhotos could not copy 2018-03-04 09:00:00 d.jpg" in first.output and "Stopped:" not in first.output
    again = split()
    assert "blacklisted (--ignore-blacklist tries them again)" in again.output and len(mac.calls("copy-ids.applescript")) == 1


def test_a_stray_that_failed_to_copy_stays_where_it_is_and_is_recorded(mac, monkeypatch):
    trashing_updates_the_database(monkeypatch, mac)
    copying_updates_the_database(monkeypatch, mac, fails={"old.jpg"})
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["photos-2018"], [A, OLD])
    add_photos(mac.libraries["photos-2017"], [])
    result = fix()
    assert "PowerPhotos could not copy 2017-06-01 10:00:00 old.jpg" in result.output and "NOT confirmed in photos-2017, so it stays in photos-2018" in result.output
    assert mac.calls("trash-ids.applescript") == [] and [(entry["library"], entry["uuid"]) for entry in blacklisted()] == [("photos-2018", "UUID2")]
    assert "Stopped:" not in result.output


def test_an_error_that_ended_the_whole_copy_is_shown_and_stops_the_run_at_once(mac, monkeypatch):
    error = '{"code":10,"domain":"BWFoundation.BWCodableError","errorDescription":"The contents of the library \u201cphotos-2018\u201d could not be read.","recoverySuggestion":"Please try opening this library in Photos to make sure it is intact, then open it in PowerPhotos again."}'
    copying_updates_the_database(monkeypatch, mac, error=error)
    waits = []
    monkeypatch.setattr(lc.time, "sleep", lambda seconds: waits.append(seconds))
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "PowerPhotos could not copy into photos-2018: The contents of the library \u201cphotos-2018\u201d could not be read. Please try opening this library in Photos" in result.output
    assert "Stopped: the copy into photos-2018 did not complete" in result.output and result.exit_code == 1
    assert waits == [] and not Path(os.environ["APPLE_PHOTOS_BLACKLIST_FILE"]).exists()  # no waiting for a copy that has ended, and nothing recorded


def test_an_error_shown_in_the_operation_log_ends_the_copy_too(mac, monkeypatch):
    real = lc.copy_one

    def copy(*args, **kwargs):
        result = real(*args, **kwargs)
        write_report("photos-2018", {}, operation_log="2026-11-12 10:00:00.000  Session \u201cCopy 1 photo\u201d started\n2026-11-12 10:00:02.000  Error shown: Photos is open. Quit Photos and try again.\n")
        return result

    monkeypatch.setattr(lc, "copy_one", copy)
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "PowerPhotos could not copy into photos-2018: Photos is open. Quit Photos and try again." in result.output and result.exit_code == 1


def test_a_failed_photo_is_matched_by_its_photos_identifier_and_not_by_its_file_name(mac, monkeypatch):
    """Two photos with the same name: only the one the report names is recorded, and the other, which arrived, is not."""
    real = lc.copy_one

    def copy(libraries, source, destination, *args, **kwargs):
        result = real(libraries, source, destination, *args, **kwargs)
        write_report(destination, {"UUID2/L0/001": (8, "failed"), "UUID3/L0/001": (6, "")})
        add_photos(mac.libraries[destination], [A, ("2018-03-04 09:00:00", 100, "twin.jpg")])
        return result

    monkeypatch.setattr(lc, "copy_one", copy)
    add_photos(mac.libraries["Source"], [A, ("2018-03-02 11:00:00", 100, "twin.jpg"), ("2018-03-04 09:00:00", 100, "twin.jpg")])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    recorded = blacklisted()
    assert [(entry["uuid"], entry["date"]) for entry in recorded] == [("UUID2", "2018-03-02 11:00:00")] and "Stopped:" not in result.output


def test_without_a_report_a_copy_that_arrived_is_fine(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, report=False)
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    assert fix("--from", "Source").exit_code == 0


def test_without_a_report_a_copy_that_did_not_arrive_is_said_and_nothing_says_why(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac, arrives=set(), report=False)
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix("--from", "Source")
    assert "none of the 1 photo arrived in photos-2018" in result.output and "Stopped:" in result.output


NEW_YEAR = ("2018-01-01 05:00:00", 100, "ny.jpg")  # within a day of the end of 2017 and of the start of 2018


def fix_years(*options):
    return runner.invoke(app, ["photos", "split", "--fix", "--from", "Source", "--media", "photos", "--years", "2017-2018", *options])


def test_fix_copies_a_photo_near_new_year_that_no_library_holds_into_each_library_it_could_belong_to(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["Source"], [A, NEW_YEAR])
    add_photos(mac.libraries["photos-2017"], [])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix_years()
    assert [(call[0], call[1], call[2]) for call in mac.calls("copy-ids.applescript")] == [("Source", "photos-2017", ["UUID2"]), ("Source", "photos-2018", ["UUID2"])]  # in both, as split does
    assert result.exit_code == 0 and "photos-2017 (after the fix): in order" in result.output and "photos-2018 (after the fix): in order" in result.output


def test_fix_does_not_copy_a_photo_near_new_year_that_the_library_either_side_holds(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["Source"], [A, NEW_YEAR])
    add_photos(mac.libraries["photos-2017"], [NEW_YEAR])
    add_photos(mac.libraries["photos-2018"], [A])
    result = fix_years()
    assert mac.calls("copy-ids.applescript") == [] and result.exit_code == 0


def test_check_without_fix_still_only_notes_a_photo_near_new_year(mac):
    mac.make("photos-2017", 19607)
    add_photos(mac.libraries["Source"], [A, NEW_YEAR])
    add_photos(mac.libraries["photos-2017"], [])
    add_photos(mac.libraries["photos-2018"], [A])
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2017-2018"])
    assert result.exit_code == 0 and "1 of the source's within a day of its dates' ends are in no library" in result.output and mac.calls("copy-ids.applescript") == []


def counting_reads(monkeypatch):
    """The names of the libraries read, in order (the source included)."""
    real = lc.read_library
    names = []

    def counted(libraries, name):
        names.append(name)
        return real(libraries, name)

    monkeypatch.setattr(lc, "read_library", counted)
    return names


def test_a_check_reads_only_the_year_asked_for_when_nothing_is_near_its_ends(mac, monkeypatch):
    add_photos(mac.libraries["Source"], [A])
    for name in ("photos-2017", "photos-2018", "photos-2019"):
        if name not in mac.libraries:
            mac.make(name, 19607)
        add_photos(mac.libraries[name], [])
    add_photos(mac.libraries["photos-2018"], [A])
    names = counting_reads(monkeypatch)
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert result.exit_code == 0 and sorted(names) == ["Source", "photos-2018"]  # not 2017 or 2019: nothing is within a day of 2018's ends


def test_a_library_either_side_is_read_only_for_the_end_a_photo_is_near(mac, monkeypatch):
    for name in ("photos-2017", "photos-2018", "photos-2019"):
        if name not in mac.libraries:
            mac.make(name, 19607)
    add_photos(mac.libraries["Source"], [A, NEW_YEAR])  # within a day of the start of 2018 (and the end of 2017)
    add_photos(mac.libraries["photos-2017"], [NEW_YEAR])
    add_photos(mac.libraries["photos-2018"], [A])
    names = counting_reads(monkeypatch)
    runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2018"])
    assert sorted(names) == ["Source", "photos-2017", "photos-2018"]  # the start's neighbor, not the end's


def test_each_library_is_read_once_in_a_run_of_several_years_and_the_reads_are_announced(mac, monkeypatch):
    for name in ("photos-2017",):
        mac.make(name, 19607)
    add_photos(mac.libraries["Source"], [A, ("2017-06-01 10:00:00", 50, "old.jpg")])
    add_photos(mac.libraries["photos-2017"], [("2017-06-01 10:00:00", 50, "old.jpg")])
    add_photos(mac.libraries["photos-2018"], [A])
    names = counting_reads(monkeypatch)
    result = runner.invoke(app, ["photos", "split", "--check", "--from", "Source", "--media", "photos", "--years", "2017-2018"])
    assert result.exit_code == 0 and sorted(names) == ["Source", "photos-2017", "photos-2018"]  # once each
    assert "Read 2 library databases" in result.output


def test_a_fix_reads_a_library_again_only_after_it_was_changed(mac, monkeypatch):
    copying_updates_the_database(monkeypatch, mac)
    add_photos(mac.libraries["Source"], [A, B])
    add_photos(mac.libraries["photos-2018"], [A])
    names = counting_reads(monkeypatch)
    result = fix("--from", "Source")
    assert result.exit_code == 0
    # the source once; photos-2018 at the start, while waiting for the copy (until it arrived) and after it, not once for every look
    assert names.count("Source") == 1 and names.count("photos-2018") <= 4


def touch(path, when, size=100):
    """A file whose modification time is the capture time (UTC) and whose size is as given, as a backup keeps it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_diff_compares_a_library_with_a_folder_of_files(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A, B, C])
    folder = tmp_path / "backup"
    touch(folder / "a.jpg", A[0])  # the same photo as A
    touch(folder / "sub" / "c.mov", C[0])  # in a subfolder
    touch(folder / "d.jpg", D[0])  # only in the folder
    touch(folder / "notes.txt", A[0])  # not a photo
    touch(folder / "@eaDir" / "a.jpg", "2020-01-01 00:00:00")  # what a NAS keeps for itself
    result = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--fields", "date,size,name"])
    assert result.exit_code == 1 and shown(result) == ["<  2018-03-02 11:00:00  100  b.jpg", ">  2018-03-04 09:00:00  100  d.jpg"]
    assert f"Only in Source: 1. Only in {folder}: 1. In both: 2." in result.output


def test_a_folder_can_be_the_left_side_and_a_difference_in_size_or_time_is_a_difference(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A, B])
    folder = tmp_path / "pics"
    touch(folder / "a.jpg", A[0], size=101)  # another size
    touch(folder / "b.jpg", "2018-03-02 12:00:00")  # an hour later: the time of a re-saved file
    result = runner.invoke(app, ["photos", "diff", str(folder), "Source", "--fields", "date,size,name", "--format", "csv"])
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert result.exit_code == 1 and sorted((r["side"], r["name"]) for r in rows) == [("left", "a.jpg"), ("left", "b.jpg"), ("right", "a.jpg"), ("right", "b.jpg")]


def test_a_library_is_still_a_library_and_a_folder_with_one_in_it_does_not_read_it(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A])
    folder = tmp_path / "archive"
    touch(folder / "a.jpg", A[0])
    other = make_library(folder, "inner", 19607)
    add_photos(other, [B])  # a library inside the folder: its photos are not the folder's files
    path_of_library = runner.invoke(app, ["photos", "diff", str(mac.libraries["Source"]), str(folder), "--fields", "date,size,name"])
    assert path_of_library.exit_code == 0 and "In both: 1" in path_of_library.output  # a.jpg only; the library's b.jpg is not counted


def test_the_media_option_and_dates_limit_a_folder_too(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A])
    folder = tmp_path / "f"
    touch(folder / "a.jpg", A[0])
    touch(folder / "c.mov", C[0])
    touch(folder / "old.jpg", "2010-01-01 00:00:00")
    only_photos = runner.invoke(app, ["photos", "diff", "Source", str(folder), "--media", "photos", "--since", "2018-01-01", "--fields", "date,size,name"])
    assert only_photos.exit_code == 0 and "In both: 1" in only_photos.output


def test_a_folder_is_cached_with_its_files_sizes_and_refresh_reads_it_again(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A])
    folder = tmp_path / "pics"
    target = touch(folder / "a.jpg", A[0])
    options = ["photos", "diff", "Source", str(folder), "--fields", "date,size,name"]
    first = runner.invoke(app, options)
    assert first.exit_code == 0 and "as cached" not in first.output
    stamp = target.stat().st_mtime
    target.write_bytes(b"x" * 250)  # written over in place: the folder's own time does not change
    os.utime(target, (stamp, stamp))
    cached = runner.invoke(app, options)
    assert cached.exit_code == 0 and "1 of 1 folders as cached" in cached.output  # the size in the cache is still 100
    fresh = runner.invoke(app, [*options, "--refresh"])
    assert fresh.exit_code == 1 and shown(fresh)[0].startswith("<") and "as cached" not in fresh.output  # now 250 bytes: a difference


def test_a_changed_folder_is_listed_again_and_a_cached_size_is_used_for_the_files_that_did_not_change(mac, tmp_path):
    add_photos(mac.libraries["Source"], [A, B])
    folder = tmp_path / "pics"
    touch(folder / "a.jpg", A[0], size=100)
    options = ["photos", "diff", "Source", str(folder), "--fields", "date,size,name", "--only", "left"]
    assert shown(runner.invoke(app, options)) == ["2018-03-02 11:00:00  100  b.jpg"]
    touch(folder / "b.jpg", B[0], size=100)  # a new file changes the directory's time: it is listed again
    again = runner.invoke(app, options)
    assert again.exit_code == 0 and shown(again) == []


def test_a_target_written_as_a_path_that_does_not_exist_is_not_looked_up_as_a_library_name(mac, monkeypatch, tmp_path):
    looked_up = []
    real = lc.need
    monkeypatch.setattr(lc, "need", lambda libraries, name: looked_up.append(name) or real(libraries, name))
    for missing in (str(tmp_path / "gone.photoslibrary"), "./missing", "~/no-such-folder", "sub/dir", "../elsewhere"):
        for sides in ((missing, "Source"), ("Source", missing)):
            result = runner.invoke(app, ["photos", "diff", *sides])
            assert result.exit_code == 1 and f"{missing} does not exist." in result.output, (missing, sides)
            assert "No library named" not in result.output and missing not in looked_up  # only "Source" was ever looked up


def test_a_plain_name_that_is_not_a_library_is_still_an_unknown_library(mac):
    result = runner.invoke(app, ["photos", "diff", "Nope", "Source"])
    assert result.exit_code == 1 and "No library named Nope in PowerPhotos" in result.output and "does not exist" not in result.output
