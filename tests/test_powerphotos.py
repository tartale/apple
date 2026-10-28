import fcntl
import json
import plistlib
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
