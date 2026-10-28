"""Checks that a command can run on this machine."""
import shutil
import sys

import typer


def is_macos() -> bool:
    return sys.platform == "darwin"


def require_macos() -> None:
    """Exits with status 2 unless this is macOS with osascript, which the PowerPhotos commands need."""
    if not is_macos():
        typer.echo("This command runs on macOS only (it drives PowerPhotos).", err=True)
        raise typer.Exit(2)
    if not shutil.which("osascript"):
        typer.echo("osascript was not found; it ships with macOS.", err=True)
        raise typer.Exit(2)
