"""Talks to PowerPhotos through the bundled AppleScript files, and reads library versions and contents from disk."""
import fcntl
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

T = TypeVar("T")

APPLESCRIPTS = Path(__file__).parent / "applescript"
# Where database copies are read: on local disk, since SQLite cannot use a write-ahead log on a network share, and
# TMPDIR can point into a checkout that is one.
SCRATCH_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "apple" / "scratch"
LOCK_FILE = Path(tempfile.gettempdir()) / "apple-powerphotos.lock"

# The file name endings that count as photos and as videos. The same lists are in copy-photos.applescript.
EXTENSIONS = {
    "photos": (".jpg", ".jpeg", ".png", ".heic", ".gif", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"),
    "videos": (".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mpg", ".mpeg"),
}

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


def listing() -> List[Tuple[str, Path]]:
    """Every library in PowerPhotos' list as (name, path on disk). PowerPhotos allows two with the same name."""
    found = []
    for line in run_applescript("libraries.applescript").splitlines():
        name, _, path = line.partition("\t")
        if name:
            found.append((name, Path(path)))
    return found


def duplicate_names(entries: List[Tuple[str, Path]]) -> str:
    """A sentence naming the libraries that share a name and where each is, or "" when every name is unique."""
    paths: Dict[str, List[Path]] = {}
    for name, path in entries:
        paths.setdefault(name, []).append(path)
    clashes = [f"{name} ({', '.join(str(path) for path in where)})" for name, where in paths.items() if len(where) > 1]
    return f"These PowerPhotos libraries share a name, so it is not clear which one is meant: {'; '.join(clashes)}." if clashes else ""


def libraries() -> Dict[str, Path]:
    """The libraries in PowerPhotos' list: name -> path on disk. Raises PowerPhotosError when two share a name."""
    entries = listing()
    clash = duplicate_names(entries)
    if clash:
        raise PowerPhotosError(f"{clash} Rename one in PowerPhotos.")
    return dict(entries)


PHOTOS_QUERY = """
    select datetime(a.ZDATECREATED + 978307200, 'unixepoch'), x.ZORIGINALFILESIZE, coalesce(x.ZORIGINALFILENAME, a.ZFILENAME)
    from ZASSET a left join ZADDITIONALASSETATTRIBUTES x on x.ZASSET = a.Z_PK
    where a.ZTRASHEDSTATE = 0
"""
READ_ERRORS = (sqlite3.Error, KeyError, TypeError, ValueError, plistlib.InvalidFileException)


def read_file(database: Path, reader: Callable[[sqlite3.Connection], T], mode: str = "ro") -> T:
    connection = sqlite3.connect(f"file:{database}?mode={mode}", uri=True)
    try:
        return reader(connection)
    finally:
        connection.close()


def read_database(library: Path, reader: Callable[[sqlite3.Connection], T]) -> T:
    """Runs reader on a library's Photos database, opened read-only; raises OSError or a READ_ERRORS error.

    The value Photos just wrote can still be in the write-ahead log (Photos.sqlite-wal) and not yet in the database
    file, so the log is read too: the database is opened where it is, or, where that cannot work (a read-only location,
    a network share), a copy of the database with its log is read.
    """
    database = library / "database" / "Photos.sqlite"
    if not database.exists():
        raise FileNotFoundError(f"{database} does not exist")

    try:
        return read_file(database, reader)
    except READ_ERRORS as in_place:
        first = in_place
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH_ROOT) as scratch:
        for suffix in ("", "-wal"):  # the -shm index is rebuilt from the log
            source = database.with_name(database.name + suffix)
            if source.exists():
                shutil.copyfile(source, Path(scratch) / source.name)
        # the copy is ours to write to: SQLite needs that to build the log's index (-shm) beside a database in WAL mode
        try:
            return read_file(Path(scratch) / database.name, reader, mode="rw")
        except READ_ERRORS as copied:
            raise type(copied)(f"{copied} (reading the copy in {scratch}); in place: {first}") from copied


def model_version(library: Path, exact: bool = True) -> Optional[int]:
    """The Photos database version (PLModelVersion) of a library, or None when it cannot be read.

    A library made by an older Photos has a lower number, and Photos must upgrade it before it accepts photos.
    exact reads the write-ahead log too, which on a network share means copying the database. Without it the database
    file is read where it is, without locking: instant, but a version Photos just wrote and has not yet checkpointed
    out of the log is missed.
    """
    def read(db: sqlite3.Connection):
        return db.execute("select Z_PLIST from Z_METADATA").fetchone()

    try:
        row = read_database(library, read) if exact else read_file(library / "database" / "Photos.sqlite", read, mode="ro&immutable=1")
        return int(plistlib.loads(row[0])["PLModelVersion"])
    except (OSError, *READ_ERRORS):
        return None


def of_media(photos: Counter, media: str) -> Counter:
    """The photos (see photos()) whose file name ends like a photo or video, as `media` says; all keeps everything."""
    if media == "all":
        return photos
    return Counter({key: count for key, count in photos.items() if key[2].lower().endswith(EXTENSIONS[media])})


def photos(library: Path) -> Counter:
    """What a library holds (not in the trash), as a count of (date, size, file name) triples. Read from the database;
    PowerPhotos is not involved. Raises PowerPhotosError when the database cannot be read."""
    try:
        rows = read_database(library, lambda db: db.execute(PHOTOS_QUERY).fetchall())
    except (OSError, *READ_ERRORS) as error:
        raise PowerPhotosError(f"Could not read the photos of {library}: {error}")
    return Counter((when, str(size), name) for when, size, name in rows)


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
