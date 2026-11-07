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
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple, TypeVar

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


def photos_query(db: sqlite3.Connection) -> str:
    """The query for a library's photos. Older Photos databases lack some columns (a burst is marked by ZAVALANCHEUUID
    where ZAVALANCHEKIND does not exist yet), so each is used only when it is there."""
    asset = {row[1] for row in db.execute("pragma table_info(ZASSET)")}
    attributes = {row[1] for row in db.execute("pragma table_info(ZADDITIONALASSETATTRIBUTES)")}
    hidden = "a.ZVISIBILITYSTATE != 0" if "ZVISIBILITYSTATE" in asset else "0"
    burst = "a.ZAVALANCHEKIND != 0" if "ZAVALANCHEKIND" in asset else "a.ZAVALANCHEUUID is not null" if "ZAVALANCHEUUID" in asset else "0"
    shared = "x.ZSYNDICATIONIDENTIFIER" if "ZSYNDICATIONIDENTIFIER" in attributes else "null"
    return f"""
        select datetime(a.ZDATECREATED + 978307200, 'unixepoch'), x.ZORIGINALFILESIZE, coalesce(x.ZORIGINALFILENAME, a.ZFILENAME),
               {hidden}, {burst}, a.ZDIRECTORY, a.ZFILENAME, {shared}, a.ZDATECREATED
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


class Photos(NamedTuple):
    """What a library holds, not counting the trash, each a count of (date, size, file name) triples: the visible items,
    the hidden ones (Photos keeps them out of view and PowerPhotos does not list them), and which of the hidden ones are
    members of a burst. files says where each item's original should be, as paths below the library. shared holds the
    keys of the items that came from Messages (Shared with You): until they are saved to the library, Photos keeps a
    placeholder with no original; identifiers maps each of those keys to its Messages identifier, which names the attachment
    folder in Messages. superseded counts the stand-ins left out of visible and hidden: shared placeholders, and hidden items whose original file is gone, that
    the library also has a real visible photo for with the same file name taken within two days (an imported copy). The
    picture is there, and the stand-in can never be saved."""
    library: Path
    visible: Counter
    hidden: Counter
    bursts: Counter
    files: dict
    shared: set
    superseded: Counter
    identifiers: dict

    def original_missing(self, key: tuple) -> bool:
        """True when the item's original file is not stored in the library (Photos keeps a placeholder for photos whose
        original lives only elsewhere, and PowerPhotos does not list those). A library with no originals folder, such as
        one from an older Photos, is never reported as missing a file."""
        if not (self.library / "originals").is_dir():
            return False
        paths = self.files.get(key, [])
        return bool(paths) and not any((self.library / path).exists() for path in paths)


def library_photos(library: Path) -> Photos:
    """Reads a library's photos from its database; PowerPhotos is not involved. Raises PowerPhotosError when the
    database cannot be read."""
    try:
        rows = read_database(library, lambda db: db.execute(photos_query(db)).fetchall())
    except (OSError, *READ_ERRORS) as error:
        raise PowerPhotosError(f"Could not read the photos of {library}: {error}")
    visible, hidden, bursts, files, shared, superseded, identifiers = Counter(), Counter(), Counter(), {}, set(), Counter(), {}
    real = {}  # lower-case file name -> capture times (seconds) of the visible photos that have their original
    for _, size, name, is_hidden, *_, taken in rows:
        if size and taken is not None and not is_hidden:
            real.setdefault(name.lower(), []).append(taken)
    has_originals = (library / "originals").is_dir()
    for when, size, name, is_hidden, is_burst, directory, stored_as, is_shared, taken in rows:
        key = (when, str(size), name)
        if taken is not None and any(abs(taken - other) < 172800 for other in real.get(name.lower(), [])):
            # a stand-in without its original (a shared placeholder, or a hidden item whose file is gone) whose picture
            # has been imported as a real photo
            no_file = has_originals and directory is not None and stored_as and not (library / "originals" / str(directory) / stored_as).exists()
            if (is_shared and not size) or (is_hidden and no_file):
                superseded[key] += 1
                continue
        (hidden if is_hidden else visible)[key] += 1
        if is_hidden and is_burst:
            bursts[key] += 1
        if is_shared:
            shared.add(key)
            identifiers[key] = is_shared
        if directory is not None and stored_as:
            files.setdefault(key, []).append(f"originals/{directory}/{stored_as}")
    return Photos(library, visible, hidden, bursts, files, shared, superseded, identifiers)


def photos(library: Path) -> Counter:
    """The visible items of a library (see library_photos): the ones PowerPhotos lists."""
    return library_photos(library).visible


def libraries_open_in_photos() -> List[Path]:
    """The libraries the running Photos app has open, found from the database file it holds (lsof). Photos also keeps
    its own Syndication library (Shared with You), which is not one you choose, so it is left out. Empty when Photos is
    not running."""
    result = subprocess.run(["lsof", "-c", "Photos", "-Fn"], capture_output=True, text=True)
    found = []
    for line in result.stdout.splitlines():
        if line.startswith("n") and line.endswith(".photoslibrary/database/Photos.sqlite"):
            library = Path(line[1:]).parent.parent
            if library.name != "Syndication.photoslibrary" and library not in found:
                found.append(library)
    return found


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
