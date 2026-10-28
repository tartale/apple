"""Talks to PowerPhotos through the bundled AppleScript files, and reads library versions from disk."""
import fcntl
import plistlib
import re
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

APPLESCRIPTS = Path(__file__).parent / "applescript"
LOCK_FILE = Path(tempfile.gettempdir()) / "apple-powerphotos.lock"

UPGRADE_NEEDED = 5  # PowerPhotos' error number for "Library needs upgrade"


class PowerPhotosError(Exception):
    def __init__(self, message: str, number: Optional[int] = None):
        super().__init__(message)
        self.number = number


class Busy(Exception):
    pass


def run_applescript(name: str, *args: str) -> str:
    """Runs a bundled AppleScript with osascript; returns its output or raises PowerPhotosError."""
    result = subprocess.run(
        ["osascript", str(APPLESCRIPTS / name), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        message = result.stderr.strip()
        match = re.search(r"\((-?\d+)\)\s*$", message)
        raise PowerPhotosError(message, int(match.group(1)) if match else None)
    return result.stdout.strip()


def libraries() -> Dict[str, Path]:
    """The libraries in PowerPhotos' list: name -> path on disk."""
    found = {}
    for line in run_applescript("libraries.applescript").splitlines():
        name, _, path = line.partition("\t")
        if name:
            found[name] = Path(path)
    return found


def model_version(library: Path) -> Optional[int]:
    """The Photos database version (PLModelVersion) of a library, or None when it cannot be read.

    A library made by an older Photos has a lower number, and Photos must upgrade it before it accepts photos.
    """
    database = library / "database" / "Photos.sqlite"
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        try:
            row = connection.execute("select Z_PLIST from Z_METADATA").fetchone()
        finally:
            connection.close()
        return int(plistlib.loads(row[0])["PLModelVersion"])
    except (sqlite3.Error, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        return None


def open_in_photos(library: Path) -> None:
    subprocess.run(["open", "-a", "Photos", str(library)], check=False)


@contextmanager
def one_at_a_time() -> Iterator[None]:
    """Holds a lock for the whole operation; the system drops it if this process dies. Raises Busy if it is taken."""
    with open(LOCK_FILE, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("Another apple photos copy or split is already running.")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
