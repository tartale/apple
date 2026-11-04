import fcntl
import json
import plistlib
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple import platform as apple_platform
from apple.cli import app
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
    assert "photos-2018: found 3" in result.output


def test_copy_with_no_range_passes_dashes_and_all(mac):
    runner.invoke(app, ["photos", "copy", "--from", "Source", "--to", "photos-2018"])
    assert mac.calls("copy-photos.applescript") == [["Source", "photos-2018", "-", "-", "all"]]


def test_an_unknown_library_lists_the_known_ones(mac):
    result = runner.invoke(app, ["photos", "copy", "--from", "Nope", "--to", "photos-2018"])
    assert result.exit_code == 1
    assert "No library named Nope" in result.output and "photos-2019" in result.output
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
    result = runner.invoke(app, ["photos", "split", "--from", "Source", "--media", "photos", "--years", "2018-2019"])
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
    """Gives a fake library the tables diff reads. photos: (date as 'yyyy-mm-dd hh:mm:ss', size, name[, "trashed" or "hidden"])."""
    connection = sqlite3.connect(library / "database" / "Photos.sqlite")
    connection.execute("drop table if exists ZASSET")
    connection.execute("drop table if exists ZADDITIONALASSETATTRIBUTES")
    connection.execute("create table ZASSET (Z_PK integer primary key, ZDATECREATED real, ZFILENAME text, ZTRASHEDSTATE integer, ZVISIBILITYSTATE integer, ZAVALANCHEKIND integer)")
    connection.execute("create table ZADDITIONALASSETATTRIBUTES (Z_PK integer primary key, ZASSET integer, ZORIGINALFILESIZE integer, ZORIGINALFILENAME text)")
    for number, (when, size, name, *flags) in enumerate(photos, 1):
        seconds = connection.execute("select strftime('%s', ?) - 978307200", (when,)).fetchone()[0]
        connection.execute(
            "insert into ZASSET values (?, ?, ?, ?, ?, ?)",
            (number, seconds, f"UUID{number}.jpg", int("trashed" in flags), 2 if {"hidden", "burst"} & set(flags) else 0, int("burst" in flags)),
        )
        connection.execute("insert into ZADDITIONALASSETATTRIBUTES values (?, ?, ?, ?)", (number, number, size, name))
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


def test_diff_counts_repeated_photos(mac):
    assert shown(diff(mac, [A, A], [A])) == ["<  2018-03-01 10:00:00  100  a.jpg"]


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
    assert diff(mac, [A, A], [A]).stdout.count("a.jpg") == 1  # the header is printed once, and only when there is something to show
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
    assert result.exit_code == 0 and "photos-2018: found 3" in result.output
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["a, b.jpg", "c.mov"], "true"]]
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
    assert "PowerPhotos does not list gone.jpg in Source" in result.output and "PowerPhotos does not list gone2.jpg in Source" in result.output


def test_no_duplicate_check_needs_a_list_and_is_passed_through_with_one(mac):
    assert copy_list(["--no-duplicate-check"], "").exit_code == 2
    assert mac.calls("copy-photos.applescript") == []
    copy_list(["--list", "-", "--no-duplicate-check"], "a.jpg\n")
    assert mac.calls("copy-named.applescript")[0][3] == "false"


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
    assert "photos-2018: found" in split().output
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_split_ignores_photos_of_the_other_media_and_the_trash_when_judging_a_year(mac):
    year_libraries(mac, [A, C, (*B, "trashed")], photos_2018=[A])
    assert "up to date" in split().output


def test_split_no_skip_complete_copies_even_a_complete_year(mac):
    year_libraries(mac, [A], photos_2018=[A])
    split("--no-skip-complete")
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_split_copies_when_a_year_library_cannot_be_read(mac):
    year_libraries(mac, [A])  # photos-2018 has no photo tables
    split()
    assert len(mac.calls("copy-photos.applescript")) == 1


def test_a_photo_near_new_year_may_be_in_the_neighbouring_years_library(mac):
    new_year = ("2018-01-01 05:00:00", 100, "ny.jpg")
    mac.make("photos-2017", 19607)
    year_libraries(mac, [A, new_year], photos_2018=[A], photos_2017=[new_year])
    assert "up to date" in split().output
    year_libraries(mac, [A, new_year], photos_2018=[A], photos_2017=[])
    assert "photos-2018: found" in split().output


def test_split_no_duplicate_check_copies_only_the_missing_names_without_the_check(mac):
    new_year = ("2018-01-01 05:00:00", 100, "ny.jpg")
    year_libraries(mac, [A, B, new_year], photos_2018=[A])
    result = split("--no-duplicate-check")
    assert "photos-2018: found" in result.output
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["b.jpg", "ny.jpg"], "false"]]  # near New Year too
    assert mac.calls("copy-photos.applescript") == []


def test_split_no_duplicate_check_leaves_a_complete_year_alone(mac):
    year_libraries(mac, [A], photos_2018=[A])
    assert "up to date" in split("--no-duplicate-check").output
    assert mac.calls("copy-named.applescript") == []


def test_split_no_duplicate_check_needs_the_databases_and_the_skip_check(mac):
    year_libraries(mac, [A])  # photos-2018 cannot be read
    result = split("--no-duplicate-check")
    assert result.exit_code == 1 and "cannot be read" in result.output
    assert split("--no-duplicate-check", "--no-skip-complete").exit_code == 2
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
    assert "Not compared, hidden in Photos and not listed by PowerPhotos: 1 in Source (1 burst members), 0 in photos-2018 (0 burst members)." in left_out.output


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
    result = capped_split()
    assert result.exit_code == 0 and "3 libraries" not in result.output and "in 2 libraries" in result.output
    assert mac.calls("copy-photos.applescript") == [
        ["Source", "photos-2018-1", "2018-01-01", "2018-03-03", "photos"],
        ["Source", "photos-2018-2", "2018-03-03", "2019-01-01", "photos"],
    ]


def test_split_under_the_cap_keeps_the_plain_library_name(mac):
    three_days(mac)
    split("--max-size", "10GB")
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
    result = capped_split("--on-missing", "skip")
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
    result = capped_split()
    assert "photos-2018-1 (2018-01-01 to 2018-03-02, 200 B): up to date" in result.output
    assert [call[1] for call in mac.calls("copy-photos.applescript")] == ["photos-2018-2"]


def test_split_no_duplicate_check_copies_what_a_part_lacks_and_photos_near_its_edges(mac):
    day_a, day_b, day_c = three_days(mac)
    mac.make("photos-2018-1", 19607)
    mac.make("photos-2018-2", 19607)
    add_photos(mac.libraries["photos-2018-1"], [day_a])
    add_photos(mac.libraries["photos-2018-2"], [])
    capped_split("--no-duplicate-check")
    # b and c are within a day of the boundary between the parts, so either library might be theirs (a, in part 1, is not)
    assert mac.calls("copy-named.applescript") == [
        ["Source", "photos-2018-1", ["b.jpg", "c.jpg"], "false"], ["Source", "photos-2018-2", ["b.jpg", "c.jpg"], "false"],
    ]


def test_split_max_size_must_be_a_size_and_needs_the_source_database(mac):
    assert capped_split("--max-size", "lots").exit_code == 2
    assert split("--max-size", "lots").exit_code == 2
    result = split("--max-size", "1GB")  # Source has no photo tables in this fixture
    assert result.exit_code == 1 and "size cannot be worked out" in result.output


def test_diff_default_columns_include_hidden_and_burst_as_true_or_false(mac):
    add_photos(mac.libraries["Source"], [A, (*B, "burst"), (*C, "hidden")])
    add_photos(mac.libraries["photos-2018"], [])
    text = runner.invoke(app, ["photos", "diff", "Source", "photos-2018"]).stdout.splitlines()
    assert text[0].split() == ["date", "size", "name", "hidden", "burst"]
    assert [line.split()[-3:] for line in text[1:]] == [["a.jpg", "false", "false"], ["b.jpg", "true", "true"], ["c.mov", "true", "false"]]
    records = json.loads(runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--format", "json"]).stdout)
    assert [(r["name"], r["hidden"], r["burst"]) for r in records] == [("a.jpg", False, False), ("b.jpg", True, True), ("c.mov", True, False)]
    csv_lines = runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--format", "csv"]).stdout.splitlines()
    assert csv_lines[0] == "side,date,size,name,hidden,burst" and csv_lines[2].endswith(",b.jpg,true,true")
    columns = runner.invoke(app, ["photos", "diff", "Source", "photos-2018", "--exclude-hidden", "--format", "csv"]).stdout.splitlines()
    assert columns == ["side,date,size,name", "left,2018-03-01 10:00:00,100,a.jpg"]


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
    assert mac.calls("copy-named.applescript") == [["Source", "photos-2018", ["a.jpg"], "true"]]
    assert "Not copied: b.jpg: hidden in Photos (a burst member)" in result.output
    assert "Not copied: c.jpg: hidden in Photos, so" in result.output
    assert "2 hidden items (1 burst members) were not copied" in result.output
    as_json = copy_list(["--list", "-"], '[{"name": "d.jpg", "hidden": true, "burst": true}]')
    assert as_json.exit_code == 0 and "no files copied (everything listed is hidden in Photos)" in as_json.output
    assert len(mac.calls("copy-named.applescript")) == 1


def test_a_long_list_of_hidden_items_is_summarised(mac):
    rows = "".join(f"left,h{n}.jpg,true,true\n" for n in range(30))
    result = copy_list(["--list", "-"], "side,name,hidden,burst\n" + rows)
    assert "Not copied: h0.jpg" not in result.output and "30 hidden items (30 burst members) were not copied" in result.output


def test_split_says_how_many_hidden_items_it_cannot_copy(mac):
    year_libraries(mac, [A, (*B, "burst"), (*C, "hidden"), ("2018-05-01 10:00:00", 100, "p.jpg", "burst")], photos_2018=[A])
    result = split()
    assert "Source: 2 hidden photos (2 burst members) are not copied" in result.output  # c is a movie, so not counted for photos
