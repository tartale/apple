"""`apple doctor`: checks what the commands need and prints a copy/paste fix for anything missing."""
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import typer

from apple.platform import is_macos


@dataclass
class Check:
    name: str
    ok: bool
    fix: Optional[str] = None


def package_manager() -> Optional[str]:
    for manager in ("brew", "apt-get", "dnf", "apk"):
        if shutil.which(manager):
            return manager
    return None


def install_command(packages: dict) -> Optional[str]:
    """The command that installs a program; packages maps a package manager to its package name."""
    manager = package_manager()
    if manager is None or manager not in packages:
        return None
    command = {"brew": "brew install", "apt-get": "apt-get install -y", "dnf": "dnf install -y", "apk": "apk add"}[manager]
    if manager != "brew" and os.geteuid() != 0:
        command = "sudo " + command
    return f"{command} {packages[manager]}"


def runs(command: List[str]) -> bool:
    try:
        return subprocess.run(command, capture_output=True).returncode == 0
    except OSError:
        return False


def program(name: str, packages: dict, label: Optional[str] = None) -> Check:
    if shutil.which(name):
        return Check(label or name, True)
    return Check(label or name, False, install_command(packages) or f"install {name} with your package manager")


def environment(name: str, example: str) -> Check:
    return Check(f"{name} is set", bool(os.environ.get(name)), f'export {name}="{example}"')


def backup_checks() -> List[Check]:
    rclone = os.environ.get("ICLOUD_RCLONE", "rclone")
    return [
        program(rclone, {"brew": "rclone", "apt-get": "rclone", "dnf": "rclone", "apk": "rclone"}, "rclone"),
        environment("ICLOUD_ROOT", "/path/to/backup"),
        environment("ICLOUD_ACCOUNTS", "alice=alice.icloud"),
    ]


def powerphotos_checks() -> List[Check]:
    checks = [program("osascript", {})]
    installed = checks[0].ok and runs(["osascript", "-e", 'id of application "PowerPhotos"'])
    checks.append(Check("PowerPhotos is installed", installed, "install PowerPhotos from https://www.fatcatsoftware.com/powerphotos/"))
    return checks


def report(title: str, checks: List[Check]) -> bool:
    typer.echo(title)
    for check in checks:
        typer.echo(f"  {'ok     ' if check.ok else 'MISSING'} {check.name}")
        if not check.ok and check.fix:
            typer.echo(f"          {check.fix}")
    return all(check.ok for check in checks)


def doctor_command() -> None:
    """Check what the apple commands need and show how to fix anything missing."""
    ok = report("iCloud backup (apple photos backup, organize)", backup_checks())
    typer.echo()
    if is_macos():
        ok = report("PowerPhotos (apple photos libraries, copy, split)", powerphotos_checks()) and ok
    else:
        typer.echo("PowerPhotos (apple photos libraries, copy, split)\n  skipped: macOS only")
    raise typer.Exit(0 if ok else 1)
