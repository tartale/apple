import os
import plistlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple import platform as apple_platform
from apple.cli import app

FAKES = Path(__file__).resolve().parent / "fakes"
runner = CliRunner()
SPLIT = ["photos", "split", "--from", "Default Library", "--media", "photos", "--years", "2018-2020"]


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_platform, "is_macos", lambda: True)
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("LAUNCH_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("HOME", str(tmp_path))
    log = tmp_path / "launchctl.log"
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
    monkeypatch.delenv("FAKE_LAUNCHCTL_FAIL", raising=False)

    class Mac:
        agents = tmp_path / "agents"
        plist = tmp_path / "agents" / "local.apple.nightly.plist"
        calls = staticmethod(lambda: log.read_text().splitlines() if log.exists() else [])

    return Mac


def add(*extra, command=SPLIT):
    return runner.invoke(app, ["schedule", "add", "nightly", *extra, "--", *command])


def test_the_schedule_commands_refuse_to_run_off_macos(monkeypatch):
    monkeypatch.setattr(apple_platform, "is_macos", lambda: False)
    for args in (["add", "n", "--", "photos", "libraries"], ["remove", "n"], ["list"]):
        result = runner.invoke(app, ["schedule", *args])
        assert result.exit_code == 2 and "macOS only" in result.output


def test_add_writes_a_plist_that_runs_the_command_at_the_chosen_time_and_loads_it(mac):
    result = add("--at", "04:05")
    assert result.exit_code == 0
    plist = plistlib.loads(mac.plist.read_bytes())
    assert plist["Label"] == "local.apple.nightly"
    assert plist["ProgramArguments"][-len(SPLIT):] == SPLIT
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 5}
    assert any(c.startswith("bootstrap gui/") and str(mac.plist) in c for c in mac.calls())
    assert not list(mac.agents.glob("*.new"))


def test_the_default_time_is_three_in_the_morning(mac):
    add()
    assert plistlib.loads(mac.plist.read_bytes())["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}


def test_dry_run_prints_the_agent_and_installs_nothing(mac):
    result = add("--dry-run")
    assert result.exit_code == 0 and "local.apple.nightly" in result.output
    assert not mac.agents.exists() and mac.calls() == []


def test_a_name_added_again_replaces_it(mac):
    add("--at", "01:00")
    add("--at", "02:00")
    assert plistlib.loads(mac.plist.read_bytes())["StartCalendarInterval"]["Hour"] == 2
    assert len(list(mac.agents.glob("*.plist"))) == 1
    verbs = [c.split()[0] for c in mac.calls()]
    assert verbs[-2:] == ["bootout", "bootstrap"]  # the old agent is unloaded before the new one is loaded


@pytest.mark.parametrize("extra,command", [
    (["--at", "24:00"], SPLIT), (["--at", "3pm"], SPLIT), (["--at", "03:60"], SPLIT),
    ([], ["bogus"]), ([], ["photos"]), ([], []),
])
def test_bad_input_exits_2_and_installs_nothing(mac, extra, command):
    assert add(*extra, command=command).exit_code == 2
    assert not mac.agents.exists()


def test_a_bad_name_is_rejected(mac):
    result = runner.invoke(app, ["schedule", "add", "a b/c", "--", "doctor"])
    assert result.exit_code == 2 and not mac.agents.exists()


def test_a_launchctl_failure_is_an_error(mac, monkeypatch):
    monkeypatch.setenv("FAKE_LAUNCHCTL_FAIL", "bootstrap")
    result = add()
    assert result.exit_code == 1 and "could not load" in result.output


def test_list_shows_name_time_and_command(mac):
    add("--at", "04:05")
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0
    assert result.output.startswith("nightly\t04:05\t")
    assert "photos split" in result.output


def test_remove_unloads_and_deletes(mac):
    add()
    result = runner.invoke(app, ["schedule", "remove", "nightly"])
    assert result.exit_code == 0 and "Removed" in result.output
    assert not mac.plist.exists()
    assert any(c.startswith("bootout") for c in mac.calls())
    assert "No schedule named" in runner.invoke(app, ["schedule", "remove", "nightly"]).output
