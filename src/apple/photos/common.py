"""Setup shared by `apple photos backup` and `organize`: the environment, the lock, the dry-run flag and the log."""
import fcntl
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import typer

from apple.doctor import install_command


def today() -> date:
    return date.today()


def require_env() -> None:
    """Exits 2 naming every required environment variable that is not set."""
    missing = [name for name in ("ICLOUD_ROOT", "ICLOUD_ACCOUNTS") if not os.environ.get(name)]
    if missing:
        typer.echo(f"Missing required environment variables: {' '.join(missing)}", err=True)
        raise typer.Exit(2)


def require_rclone() -> str:
    """Returns the rclone to run, or exits 2 with a command that installs it."""
    rclone = os.environ.get("ICLOUD_RCLONE") or "rclone"
    if shutil.which(rclone):
        return rclone
    package = rclone.rsplit("/", 1)[-1]
    command = install_command({manager: package for manager in ("brew", "apt-get", "dnf", "apk")})
    typer.echo(f"Missing required programs: {rclone}", err=True)
    if command:
        typer.echo(f"Install them with:\n  {command}", err=True)
    else:
        typer.echo(f"Install the packages that provide: {package}", err=True)
    raise typer.Exit(2)


def is_dry_run(flag: bool) -> bool:
    """True with --dry-run, or when DRY_RUN is true, 1, yes or on in any case; any other value is a real run."""
    return flag or os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes", "on")


@contextmanager
def exclusive_lock(message: str) -> Iterator[None]:
    """Holds the lock shared by backup and organize; when another run holds it, prints the message and exits 0."""
    path = os.environ.get("ICLOUD_LOCK_FILE") or "/tmp/rclone-icloudphotos.lock"
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            typer.echo(message)
            raise typer.Exit(0)
        yield


class Log:
    """The day's log file, ICLOUD_ROOT/<name>-YYYY-MM-DD.log (the date of the run's start), shared by that day's runs."""

    def __init__(self, name: str) -> None:
        root = Path(os.environ["ICLOUD_ROOT"])
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{name}-{today().isoformat()}.log"

    def __call__(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        with open(self.path, "a") as handle:
            handle.write(line + "\n")
        if sys.stdout.isatty():
            print(line)
