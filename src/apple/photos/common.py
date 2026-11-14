"""Setup shared by `apple photos backup` and `organize`: the environment, the lock, the dry-run flag and the log."""
import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional

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


STALL_VARIABLE = "ICLOUD_STALL_MINUTES"
DEFAULT_STALL_MINUTES = 20  # rclone's own quiet stretches are long: a cold album listing of a large library was silent for 7 minutes
POLL_SECONDS = 5  # how often a running rclone is looked at


class Finished(NamedTuple):
    returncode: int
    stdout: bytes


def stall_seconds() -> Optional[float]:
    """How long rclone may show no sign of life (no output, no line in the log) before it is killed: ICLOUD_STALL_MINUTES, 20 by
    default; None (never) when it is 0. A value that is not a number is an error."""
    text = os.environ.get(STALL_VARIABLE, "")
    try:
        minutes = float(text) if text.strip() else DEFAULT_STALL_MINUTES
    except ValueError:
        typer.echo(f"{STALL_VARIABLE} must be a number of minutes, not {text!r}", err=True)
        raise typer.Exit(2)
    return minutes * 60 if minutes > 0 else None


def run_watched(command: List[str], log: "Log", label: str, capture: bool = False, attempts: int = 3) -> Finished:
    """Runs a command that talks to iCloud and kills it, and runs it again, when it shows no sign of life for stall_seconds(): rclone
    can wait on one request for hours, with no error and no timeout. Life is anything on its output (when capture holds it, the
    output is returned) or a new line in the log file; each kill is logged with the label. After the last attempt the result is a
    failure (1)."""
    limit = stall_seconds()
    for attempt in range(1, attempts + 1):
        process = subprocess.Popen(command, stdout=subprocess.PIPE if capture else None)
        chunks: List[bytes] = []
        seen = {"at": time.monotonic()}

        def read() -> None:
            for block in iter(lambda: process.stdout.read(65536), b""):
                chunks.append(block)
                seen["at"] = time.monotonic()

        reader = threading.Thread(target=read, daemon=True) if capture else None
        if reader:
            reader.start()
        size = log.path.stat().st_size if log.path.exists() else 0
        stalled = False
        while True:
            try:
                process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                grown = log.path.stat().st_size if log.path.exists() else 0
                if grown != size:
                    size, seen["at"] = grown, time.monotonic()
                if limit is not None and time.monotonic() - seen["at"] > limit:
                    stalled = True
                    break
        if stalled:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if reader:
            reader.join()
        if not stalled:
            return Finished(process.returncode, b"".join(chunks))
        log(f"{label}: no sign of life for {limit / 60:g} minutes, killed (attempt {attempt} of {attempts})" + ("; trying again" if attempt < attempts else ""))
    return Finished(1, b"")
