"""Talks to PowerPhotos through the bundled AppleScript files, and reads library versions from disk."""
import fcntl
import plistlib
import re
import shutil
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


def read_version(database: Path, uri_options: str) -> int:
    connection = sqlite3.connect(f"file:{database}?{uri_options}", uri=True)
    try:
        row = connection.execute("select Z_PLIST from Z_METADATA").fetchone()
    finally:
        connection.close()
    return int(plistlib.loads(row[0])["PLModelVersion"])


def model_version(library: Path, exact: bool = True) -> Optional[int]:
    """The Photos database version (PLModelVersion) of a library, or None when it cannot be read.

    A library made by an older Photos has a lower number, and Photos must upgrade it before it accepts photos.
    Without exact the database file is read where it is, without locking: instant even on a network share, but a version
    Photos just wrote and has not yet checkpointed out of the log is missed.
    With exact, the value Photos just wrote, which can still be in the write-ahead log (Photos.sqlite-wal) and not yet
    in the database file, is found too: the database is opened read-only, or, where that cannot work (a read-only location, a
    network share), a copy of the database with its log is read.
    """
    database = library / "database" / "Photos.sqlite"
    errors = (sqlite3.Error, KeyError, TypeError, ValueError, plistlib.InvalidFileException)
    if not exact:
        try:
            return read_version(database, "mode=ro&immutable=1")
        except errors:
            return None
    try:
        return read_version(database, "mode=ro")
    except errors:
        pass
    try:
        with tempfile.TemporaryDirectory() as scratch:
            for suffix in ("", "-wal", "-shm"):
                source = database.with_name(database.name + suffix)
                if source.exists():
                    shutil.copyfile(source, Path(scratch) / source.name)
            return read_version(Path(scratch) / database.name, "mode=ro")
    except (OSError, *errors):
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
