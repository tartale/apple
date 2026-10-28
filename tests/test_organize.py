import fcntl
import os

from typer.testing import CliRunner

from apple.cli import app
from conftest import JUL_2020, JUN_2020

runner = CliRunner()


def organize(*args):
    return runner.invoke(app, ["photos", "organize", *args])


def albums(icloud, text):
    icloud.account.mkdir(exist_ok=True)
    (icloud.account / "albums.csv").write_text(text)


def same(a, b):
    return os.path.samefile(a, b)


def test_exits_2_and_names_the_missing_required_variables(icloud, monkeypatch):
    monkeypatch.delenv("ICLOUD_ROOT")
    monkeypatch.delenv("ICLOUD_ACCOUNTS")
    result = organize()
    assert result.exit_code == 2 and "ICLOUD_ROOT ICLOUD_ACCOUNTS" in result.output


def test_by_date_holds_a_hardlink_to_each_original_by_modification_month(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    icloud.original("b.jpg", 100, JUL_2020)
    assert organize().exit_code == 0
    assert same(icloud.account / "by-date/2020/06/a.jpg", icloud.account / "originals/a.jpg")
    assert same(icloud.account / "by-date/2020/07/b.jpg", icloud.account / "originals/b.jpg")


def test_a_different_photo_with_the_same_name_in_the_same_month_is_linked_with_its_size_appended(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    icloud.original("sub/a.jpg", 250, JUN_2020)
    assert organize().exit_code == 0
    month = icloud.account / "by-date/2020/06"
    assert (month / "a.jpg").is_file()
    assert (month / "a-100.jpg").is_file() or (month / "a-250.jpg").is_file()
    assert len(list(month.iterdir())) == 2


def test_a_name_without_an_extension_gets_the_size_appended(icloud):
    icloud.original("a", 100, JUN_2020)
    icloud.original("sub/a", 250, JUN_2020)
    assert organize().exit_code == 0
    assert sorted(p.name for p in (icloud.account / "by-date/2020/06").iterdir()) in (["a", "a-100"], ["a", "a-250"])


def test_synology_eadir_folders_are_skipped(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    icloud.original("@eaDir/a.jpg/SYNOPHOTO_THUMB.jpg", 10, JUN_2020)
    assert organize().exit_code == 0
    assert len([p for p in (icloud.account / "by-date").rglob("*") if p.is_file()]) == 1


def test_albums_are_hardlinks_to_the_stored_file_named_in_albums_csv(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    icloud.original("Beach/b.jpg", 200, JUN_2020)
    albums(icloud, "path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n"
                   "Trip/b.jpg,200,2020-06-15 12:00:00,Beach/b.jpg\n")
    assert organize().exit_code == 0
    assert same(icloud.account / "albums/Holiday/a.jpg", icloud.account / "originals/a.jpg")
    assert same(icloud.account / "albums/Trip/b.jpg", icloud.account / "originals/Beach/b.jpg")


def test_quoted_album_entries_with_a_comma_are_linked_under_their_real_name(icloud):
    icloud.original("Trip/Day 1, morning.jpg", 500, JUN_2020)
    albums(icloud, 'path,size,mtime,stored\n"Trip/Day 1, morning.jpg",500,2020-06-15 12:00:00,"Trip/Day 1, morning.jpg"\n')
    assert organize().exit_code == 0
    assert same(icloud.account / "albums/Trip/Day 1, morning.jpg", icloud.account / "originals/Trip/Day 1, morning.jpg")


def test_a_second_run_creates_nothing_new_and_still_succeeds(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    albums(icloud, "path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n")
    assert organize().exit_code == 0
    assert organize().exit_code == 0
    log = icloud.log("organize")
    assert "by-date done: new=0 existing=1" in log and "albums done: new=0 existing=1" in log


def test_dry_run_creates_no_links(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    albums(icloud, "path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n")
    assert organize("--dry-run").exit_code == 0
    assert not (icloud.account / "by-date").exists() and not (icloud.account / "albums").exists()


def test_an_account_with_no_originals_dir_is_an_error(icloud):
    assert organize().exit_code == 1


def test_the_days_runs_append_to_one_log_named_with_the_date(icloud):
    (icloud.account / "originals").mkdir(parents=True)
    organize()
    assert organize().exit_code == 0
    assert [p.name for p in icloud.root.glob("organize*.log")] == ["organize-2026-01-02.log"]
    assert icloud.log("organize").count("link run started") == 2


def test_an_album_entry_whose_stored_file_is_missing_fails_the_run_but_the_other_links_are_made(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    albums(icloud, "path,size,mtime,stored\nHoliday/a.jpg,100,2020-06-15 12:00:00,a.jpg\n"
                   "Holiday/gone.jpg,200,2020-06-15 12:00:00,gone.jpg\n")
    assert organize().exit_code == 1
    assert same(icloud.account / "albums/Holiday/a.jpg", icloud.account / "originals/a.jpg")
    assert "not in originals: Holiday/gone.jpg" in icloud.log("organize")


def test_exits_0_while_a_backup_holds_the_lock(icloud):
    icloud.original("a.jpg", 100, JUN_2020)
    with open(icloud.lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = organize()
    assert result.exit_code == 0 and "Backup still running" in result.output
    assert not (icloud.account / "by-date").exists()


def test_a_photo_whose_name_and_size_name_are_both_taken_by_other_photos_fails_the_run(icloud):
    month = icloud.account / "by-date/2020/06"
    month.mkdir(parents=True)
    (month / "a.jpg").write_bytes(b"other")
    (month / "a-100.jpg").write_bytes(b"another")
    icloud.original("a.jpg", 100, JUN_2020)
    assert organize().exit_code == 1
    assert "FAILED to link" in icloud.log("organize")
