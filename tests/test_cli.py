import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple.cli import app

REPO = Path(__file__).resolve().parent.parent
JUN_2020 = 1592222400  # 2020-06-15 12:00 UTC


@pytest.fixture
def icloud(tmp_path, monkeypatch):
    """A fake iCloud with one photo, an empty backup root and the fake rclone."""
    cloud = tmp_path / "cloud"
    photo = cloud / "tom" / "PrimarySync" / "All Photos" / "a.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 100)
    os.utime(photo, (JUN_2020, JUN_2020))
    root = tmp_path / "root"
    root.mkdir()
    for name, value in {
        "TZ": "UTC",
        "FAKE_CLOUD": cloud,
        "ICLOUD_ROOT": root,
        "ICLOUD_ACCOUNTS": "tom=tom",
        "ICLOUD_RCLONE": REPO / "test" / "fake-rclone",
        "ICLOUD_LOCK_FILE": tmp_path / "lock",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("DRY_RUN", raising=False)
    return root


runner = CliRunner()


def test_help_lists_the_photos_group():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "photos" in result.output


def test_photos_help_lists_backup_and_organize():
    result = runner.invoke(app, ["photos", "--help"])
    assert result.exit_code == 0
    assert "backup" in result.output and "organize" in result.output


def test_backup_copies_the_photo(icloud):
    result = runner.invoke(app, ["photos", "backup"])
    assert result.exit_code == 0
    assert (icloud / "tom" / "originals" / "a.jpg").exists()


def test_backup_dry_run_changes_nothing(icloud):
    result = runner.invoke(app, ["photos", "backup", "--dry-run"])
    assert result.exit_code == 0
    assert not (icloud / "tom").exists()


def test_the_dry_run_environment_variable_still_works(icloud, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    result = runner.invoke(app, ["photos", "backup"])
    assert result.exit_code == 0
    assert not (icloud / "tom").exists()


def test_organize_builds_by_date(icloud):
    runner.invoke(app, ["photos", "backup"])
    result = runner.invoke(app, ["photos", "organize"])
    assert result.exit_code == 0
    assert (icloud / "tom" / "by-date" / "2020" / "06" / "a.jpg").exists()


def test_a_script_failure_is_the_exit_status(icloud, monkeypatch):
    monkeypatch.delenv("ICLOUD_ROOT")
    result = runner.invoke(app, ["photos", "backup"])
    assert result.exit_code == 2
