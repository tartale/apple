import os
import shutil

import pytest
from typer.testing import CliRunner

from apple import doctor
from apple import platform as apple_platform
from apple.cli import app

runner = CliRunner()


@pytest.fixture
def everything(monkeypatch):
    """Every program present, the iCloud variables set, not macOS."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "runs", lambda command: True)
    monkeypatch.setattr(apple_platform, "is_macos", lambda: False)
    monkeypatch.setattr(doctor, "is_macos", lambda: False)
    monkeypatch.setenv("ICLOUD_ROOT", "/data")
    monkeypatch.setenv("ICLOUD_ACCOUNTS", "a=a.icloud")
    monkeypatch.delenv("ICLOUD_RCLONE", raising=False)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.strip()


def test_everything_present_passes_and_skips_powerphotos_off_macos(everything):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "MISSING" not in result.output
    assert "skipped: macOS only" in result.output


def test_a_missing_program_is_reported_with_an_install_command(everything, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "rclone" else f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "package_manager", lambda: "apt-get")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "MISSING rclone" in result.output
    assert "apt-get install -y rclone" in result.output


def test_the_install_command_uses_sudo_unless_root(monkeypatch):
    monkeypatch.setattr(doctor, "package_manager", lambda: "apt-get")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert doctor.install_command({"apt-get": "x"}) == "sudo apt-get install -y x"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert doctor.install_command({"apt-get": "x"}) == "apt-get install -y x"


def test_unset_environment_is_reported_with_the_line_to_run(everything, monkeypatch):
    monkeypatch.delenv("ICLOUD_ROOT")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "MISSING ICLOUD_ROOT is set" in result.output
    assert 'export ICLOUD_ROOT="/path/to/backup"' in result.output


def test_a_custom_rclone_is_the_one_checked(everything, monkeypatch):
    monkeypatch.setenv("ICLOUD_RCLONE", "/opt/rclone")
    seen = []
    monkeypatch.setattr(shutil, "which", lambda name: seen.append(name) or f"/x/{name}")
    runner.invoke(app, ["doctor"])
    assert "/opt/rclone" in seen


def test_on_macos_powerphotos_is_checked(everything, monkeypatch):
    monkeypatch.setattr(doctor, "is_macos", lambda: True)
    monkeypatch.setattr(doctor, "runs", lambda command: "osascript" not in command[0] or "PowerPhotos" not in command[-1])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "MISSING PowerPhotos is installed" in result.output


def test_homebrew_gnu_tools_installed_as_g_prefixed_get_a_path_fix(everything, monkeypatch):
    # find and stat are the BSD ones, but gfind and gstat (Homebrew's names for the GNU ones) are installed
    monkeypatch.setattr(doctor, "runs", lambda command: command[0] not in ("find", "stat"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "MISSING GNU find" in result.output and "installed as gfind" in result.output
    assert "installed as gstat" in result.output
    assert "libexec/gnubin" in result.output


def test_missing_gnu_tools_get_an_install_command(everything, monkeypatch):
    monkeypatch.setattr(doctor, "runs", lambda command: command[0] not in ("find", "stat"))
    monkeypatch.setattr(shutil, "which", lambda name: None if name in ("gfind", "gstat") else f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "package_manager", lambda: "brew")
    result = runner.invoke(app, ["doctor"])
    assert "brew install findutils" in result.output and "brew install coreutils" in result.output
