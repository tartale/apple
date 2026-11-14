import fcntl
import os

import pytest
from typer.testing import CliRunner

from apple.cli import app
from conftest import JUL_2020, JUN_2020

runner = CliRunner()
ROW = "{},{},2020-06-15 12:00:00,{}"


def backup(*args):
    return runner.invoke(app, ["photos", "backup", *args])


def csv_lines(icloud):
    return (icloud.account / "albums.csv").read_text().splitlines()


def test_exits_2_and_names_the_missing_required_variables(icloud, monkeypatch):
    monkeypatch.delenv("ICLOUD_ROOT")
    monkeypatch.delenv("ICLOUD_ACCOUNTS")
    result = backup()
    assert result.exit_code == 2
    assert "ICLOUD_ROOT ICLOUD_ACCOUNTS" in result.output


def test_copies_all_photos_into_originals_and_points_albums_csv_at_them(icloud):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    icloud.cloud_file("Holiday/a.jpg", 100, JUN_2020)
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "a.jpg").is_file()
    assert not (icloud.account / "originals" / "Holiday").exists()
    assert ROW.format("Holiday/a.jpg", 100, "a.jpg") in csv_lines(icloud)
    assert csv_lines(icloud)[0] == "path,size,mtime,stored"


def test_an_album_only_photo_is_downloaded_once_to_the_first_album_path(icloud):
    icloud.cloud_file("Beach/b.jpg", 200, JUN_2020)
    icloud.cloud_file("Trip/b.jpg", 200, JUN_2020)
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "Beach" / "b.jpg").is_file()
    assert not (icloud.account / "originals" / "Trip").exists()
    assert ROW.format("Trip/b.jpg", 200, "Beach/b.jpg") in csv_lines(icloud)


def test_photos_sharing_a_name_and_size_but_not_mtime_are_different_photos(icloud):
    icloud.cloud_file("All Photos/c.jpg", 300, JUN_2020)
    icloud.cloud_file("Odd/c.jpg", 300, JUL_2020)
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "c.jpg").is_file()
    assert (icloud.account / "originals" / "Odd" / "c.jpg").is_file()


def test_a_photo_nested_under_all_photos_is_stored_once_where_rclone_puts_it(icloud):
    icloud.cloud_file("All Photos/sub/n.jpg", 400, JUN_2020)
    icloud.cloud_file("Nest/n.jpg", 400, JUN_2020)
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "sub" / "n.jpg").is_file()
    assert not (icloud.account / "originals" / "Nest").exists()
    assert ROW.format("Nest/n.jpg", 400, "sub/n.jpg") in csv_lines(icloud)


def test_album_paths_containing_a_comma_are_quoted_in_albums_csv(icloud):
    icloud.cloud_file("Trip/Day 1, morning.jpg", 500, JUN_2020)
    assert backup().exit_code == 0
    assert '"Trip/Day 1, morning.jpg",500,2020-06-15 12:00:00,"Trip/Day 1, morning.jpg"' in csv_lines(icloud)


def test_dry_run_changes_nothing_under_the_account_dir(icloud, monkeypatch):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("DRY_RUN", "true")
    assert backup().exit_code == 0
    assert not icloud.account.exists()


def test_dry_run_leaves_an_existing_albums_csv_alone(icloud):
    icloud.account.mkdir()
    (icloud.account / "albums.csv").write_text("path,size,mtime,stored\n")
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    assert backup("--dry-run").exit_code == 0
    assert (icloud.account / "albums.csv").read_text() == "path,size,mtime,stored\n"
    assert [p.name for p in icloud.account.iterdir()] == ["albums.csv"]


def test_a_failed_listing_is_an_error_and_downloads_no_album_only_photos(icloud, monkeypatch):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("FAKE_FAIL", "lsf")
    assert backup().exit_code == 1
    assert not (icloud.account / "originals" / "Holiday").exists()


def test_exits_0_without_doing_anything_while_another_run_holds_the_lock(icloud):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    with open(icloud.lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = backup()
    assert result.exit_code == 0
    assert "still active" in result.output
    assert not icloud.account.exists()


def test_the_days_runs_append_to_one_log_named_with_the_date(icloud):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    backup()
    assert backup().exit_code == 0
    assert [p.name for p in icloud.root.glob("backup*.log")] == ["backup-2026-01-02.log"]
    assert icloud.log("backup").count("backup run started") == 2


def test_when_the_album_only_download_fails_albums_csv_leaves_out_those_rows_and_the_run_fails(icloud, monkeypatch):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    icloud.cloud_file("Holiday/a.jpg", 100, JUN_2020)
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("FAKE_FAIL", "copy-album")
    assert backup().exit_code == 1
    assert ROW.format("Holiday/a.jpg", 100, "a.jpg") in csv_lines(icloud)
    assert "h.jpg" not in (icloud.account / "albums.csv").read_text()
    assert "albums.csv written without 1 entries" in icloud.log("backup")


def test_the_next_run_adds_the_rows_left_out_after_a_failed_download(icloud, monkeypatch):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("FAKE_FAIL", "copy-album")
    assert backup().exit_code == 1
    monkeypatch.delenv("FAKE_FAIL")
    assert backup().exit_code == 0
    assert ROW.format("Holiday/h.jpg", 150, "Holiday/h.jpg") in csv_lines(icloud)


def test_albums_csv_is_written_when_every_download_succeeds(icloud):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    assert backup().exit_code == 0
    assert ROW.format("Holiday/h.jpg", 150, "Holiday/h.jpg") in csv_lines(icloud)


def test_a_photo_that_left_all_photos_but_is_in_an_album_is_not_downloaded_again(icloud):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    icloud.cloud_file("Recently Deleted/a.jpg", 100, JUN_2020)
    assert backup().exit_code == 0
    (icloud.cloud / "All Photos" / "a.jpg").unlink()
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "a.jpg").is_file()
    assert not (icloud.account / "originals" / "Recently Deleted").exists()
    assert ROW.format("Recently Deleted/a.jpg", 100, "a.jpg") in csv_lines(icloud)


def test_an_album_only_photo_downloaded_earlier_is_reused_by_another_album(icloud):
    icloud.cloud_file("Beach/b.jpg", 200, JUN_2020)
    assert backup().exit_code == 0
    icloud.cloud_file("Trip/b.jpg", 200, JUN_2020)
    (icloud.cloud / "Beach" / "b.jpg").unlink()
    assert backup().exit_code == 0
    assert not (icloud.account / "originals" / "Trip").exists()
    assert ROW.format("Trip/b.jpg", 200, "Beach/b.jpg") in csv_lines(icloud)


def test_a_different_photo_with_the_same_name_and_size_but_another_mtime_is_still_downloaded(icloud):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    backup()
    icloud.cloud_file("Odd/a.jpg", 100, JUL_2020)
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "Odd" / "a.jpg").is_file()


def test_exits_2_with_an_install_command_when_rclone_is_missing(icloud, monkeypatch):
    monkeypatch.setenv("ICLOUD_RCLONE", "/nonexistent/rclone")
    result = backup()
    assert result.exit_code == 2
    assert "Missing required programs: /nonexistent/rclone" in result.output
    assert "rclone" in result.output.split("Install", 1)[1]
    assert not (icloud.root / "backup-2026-01-02.log").exists()


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "On"])
def test_dry_run_accepts_true_1_yes_and_on_in_any_case(icloud, monkeypatch, value):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    monkeypatch.setenv("DRY_RUN", value)
    assert backup().exit_code == 0
    assert not icloud.account.exists()


def test_any_other_dry_run_value_including_false_is_a_real_run(icloud, monkeypatch):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    monkeypatch.setenv("DRY_RUN", "false")
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "a.jpg").is_file()


def test_the_all_photos_copy_is_preferred_to_an_older_copy_elsewhere_in_originals(icloud):
    # n.jpg was downloaded earlier as an album-only photo (Beach/n.jpg); now All Photos has it too (sub/n.jpg)
    icloud.cloud_file("Beach/n.jpg", 400, JUN_2020)
    assert backup().exit_code == 0
    icloud.cloud_file("All Photos/sub/n.jpg", 400, JUN_2020)
    assert backup().exit_code == 0
    assert ROW.format("Beach/n.jpg", 400, "sub/n.jpg") in csv_lines(icloud)


@pytest.fixture
def quick_watchdog(monkeypatch):
    """A watchdog that gives up on a silent rclone after a second."""
    monkeypatch.setenv("ICLOUD_STALL_MINUTES", "0.02")
    monkeypatch.setattr("apple.photos.common.POLL_SECONDS", 0.1)


def test_a_listing_that_stalls_is_killed_and_tried_again(icloud, monkeypatch, quick_watchdog):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("FAKE_HANG", "lsf")  # the first listing waits; the second one answers
    result = backup()
    assert result.exit_code == 0
    log = next(icloud.root.glob("backup-*.log")).read_text()
    assert "listing the albums: no sign of life" in log and "killed (attempt 1 of 3); trying again" in log
    assert (icloud.account / "originals" / "Holiday" / "h.jpg").exists() and (icloud.account / "albums.csv").exists()


def test_a_listing_that_always_stalls_fails_the_run_after_three_attempts(icloud, monkeypatch, quick_watchdog):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    monkeypatch.setenv("FAKE_HANG", "lsf")
    monkeypatch.setenv("FAKE_HANG_TIMES", "9")
    result = backup()
    assert result.exit_code == 1
    log = next(icloud.root.glob("backup-*.log")).read_text()
    assert "killed (attempt 3 of 3)" in log and "listing failed; album-only photos not copied" in log
    assert not (icloud.account / "originals" / "Holiday").exists()


def test_a_copy_that_stalls_is_killed_and_tried_again_too(icloud, monkeypatch, quick_watchdog):
    icloud.cloud_file("All Photos/a.jpg", 100, JUN_2020)
    monkeypatch.setenv("FAKE_HANG", "copy-all")
    assert backup().exit_code == 0
    assert (icloud.account / "originals" / "a.jpg").exists()
    assert "copying All Photos: no sign of life" in next(icloud.root.glob("backup-*.log")).read_text()


def test_the_log_says_when_the_album_listing_starts(icloud):
    icloud.cloud_file("Holiday/h.jpg", 150, JUN_2020)
    backup()
    assert "listing the albums (a large library can take several minutes without output)" in next(icloud.root.glob("backup-*.log")).read_text()


def test_the_stall_time_is_set_by_the_environment_and_a_bad_value_is_an_error(icloud, monkeypatch):
    from apple.photos import common
    monkeypatch.delenv("ICLOUD_STALL_MINUTES", raising=False)
    assert common.stall_seconds() == 20 * 60
    monkeypatch.setenv("ICLOUD_STALL_MINUTES", "1.5")
    assert common.stall_seconds() == 90
    monkeypatch.setenv("ICLOUD_STALL_MINUTES", "0")
    assert common.stall_seconds() is None  # off
    monkeypatch.setenv("ICLOUD_STALL_MINUTES", "soon")
    result = backup()
    assert result.exit_code == 2 and "ICLOUD_STALL_MINUTES must be a number of minutes" in result.output
