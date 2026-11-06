"""Where `./apple photos recover` can look for the original of a photo a library does not store.

A Source looks one photo up (find) and can import what it found. There are two kinds, by how a file is identified:
Messages (by the attachment ID the Photos database holds) and Folders (everything under a path: raw files matched by name
and time, and Photos libraries read through their database). To add a way, write a Source and give it a keyword in KINDS.
"""
import os
import shlex
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from apple.photos import powerphotos as pp
from apple.photos.progress import Progress, duration

MEDIA_FILES = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".dng", ".mov", ".mp4", ".m4v", ".3gp")
HIDDEN_DIRECTORIES = ".HFS+ Private Directory Data\r"
DEFAULT = "default"
# Folders a disk keeps for itself, that never hold an original: Synology's thumbnails, macOS's search index, change log and
# scratch folders, an archive's resource forks, and HFS+'s store of directory links (every directory in it is reached through
# the link that points at it, so reading it directly would read each twice). Not here: the recycle bins, which hold deleted
# files that may still be wanted.
IGNORED_FOLDERS = {"@eaDir", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", "__MACOSX", HIDDEN_DIRECTORIES}
TOLERANCE = 7200  # seconds a file's time may differ from the photo's capture time (the same photo is an hour out across daylight saving)


class Found(NamedTuple):
    """Where a source can get the original. source is what kind of place it is in: messages, folder (a raw file) or library
    (a file found through a library's database); root is the path that was searched, library the library that holds the file
    (for a library), files everything to import (a Live Photo's video too) and size the photo's own size in bytes."""
    source: str
    files: list
    size: int
    root: str = ""
    library: str = ""


class Source:
    """One way of recovering originals. The name is shown in the report."""
    name = ""

    def problem(self) -> Optional[str]:
        """Why this source cannot be used (a path that does not exist, say), or None."""
        return None

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        """Where the original of the photo (key is (date, size, file name)) can be had from, or None."""
        raise NotImplementedError

    def prepare(self, progress: Progress) -> None:
        """Gets ready to look photos up (a source that must read many files first does it here, reporting on progress)."""

    def note(self) -> Optional[str]:
        """Something worth saying once the search is done (libraries that could not be read), or None."""
        return None


def import_files(found: List[Found]) -> int:
    """Imports the files found into the library Photos has open; returns how many items Photos imported."""
    pp.SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pp.SCRATCH_ROOT) as scratch:
        listing = Path(scratch) / "files.txt"
        listing.write_text("\n".join(path for item in found for path in item.files) + "\n", encoding="utf-8")
        output = pp.run_applescript("import-photos.applescript", str(listing))
    return int(output.rsplit(" ", 1)[-1])


def with_pair(main: Path, folder_files) -> List[str]:
    """The photo's file and, with it, the video half of a Live Photo (the same name with another ending), so that Photos
    imports the pair as one."""
    return [str(f) for f in folder_files if f.stem.lower() == main.stem.lower()]


def captured(key: tuple) -> float:
    return datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


# Time Machine on a Linux disk -----------------------------------------------------------------------------------------

def directory_link_target(status, volume: Path) -> Optional[Path]:
    """Time Machine hard-links whole directories. Mounted on Linux, such a directory shows as an empty file whose link
    count is the id of the real directory, which HFS+ keeps in <volume>/.HFS+ Private Directory Data/dir_<id>. The real
    directory for a directory-link, or None for anything else."""
    if stat.S_ISREG(status.st_mode) and status.st_size == 0 and status.st_nlink >= 100000:
        target = volume / HIDDEN_DIRECTORIES / f"dir_{status.st_nlink}"
        return target if target.is_dir() else None
    return None


def volume_of(path: Path) -> Optional[Path]:
    """The disk root above path that holds Time Machine's hidden directory store, if any."""
    for parent in (path, *path.parents):
        if (parent / HIDDEN_DIRECTORIES).is_dir():
            return parent
    return None


def walk(root: Path, skip: Callable[[Path], bool] = lambda path: False):
    """(path, status, is_directory) for every entry under root, following Time Machine's directory links on Linux. A
    directory for which skip(path) is true is yielded and not entered. Each real directory is read once, however many
    backups link to it."""
    volume = volume_of(root)
    seen = set()

    def real_directory(path: Path, status) -> Optional[Path]:
        if stat.S_ISDIR(status.st_mode):
            return path
        return directory_link_target(status, volume) if volume else None

    def descend(folder: Path):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            real = real_directory(path, status)
            if real is None:
                yield path, status, False
                continue
            yield path, status, True
            if real not in seen and not skip(path):
                seen.add(real)
                yield from descend(real)

    try:
        top = real_directory(root, os.lstat(root)) or root
    except OSError:
        return
    yield from descend(top)


def media_files(root: Path, skip: Callable[[Path], bool] = lambda path: False):
    for path, status, is_directory in walk(root, skip):
        if not is_directory and stat.S_ISREG(status.st_mode) and path.suffix.lower() in MEDIA_FILES:
            yield path, status


# The three kinds ------------------------------------------------------------------------------------------------------

def messages_attachments() -> Path:
    return Path.home() / "Library" / "Messages" / "Attachments"


class Messages(Source):
    """The attachment of a photo shared with you, which Messages may still hold (Terminal needs Full Disk Access)."""
    name = "messages"

    def __init__(self, folders: List[Path]):
        self.folders = folders

    def problem(self) -> Optional[str]:
        missing = [str(folder) for folder in self.folders if not folder.is_dir()]
        return f"{', '.join(missing)} is not a folder" if missing else None

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        identifier = photos.identifiers.get(key)
        if not identifier:
            return None
        for attachments in self.folders:
            for folder in attachments.glob(f"*/*/{identifier}"):
                candidates = sorted(f for f in folder.iterdir() if f.suffix.lower() in MEDIA_FILES)
                if candidates:
                    main = ([f for f in candidates if f.name.lower() == key[2].lower()] or candidates[:1])[0]
                    return Found("messages", with_pair(main, candidates), main.stat().st_size, str(attachments))
        return None


class ByNameAndTime(Source):
    """Files that are known by name: a photo matches by file name and a time within two hours of its capture time. A name
    alone is not enough, since the numbers in IMG_nnnn.JPG repeat."""

    def __init__(self):
        self._index = None
        self.progress = Progress()

    def files(self):
        """(name, time, path, kind, root, library) of each file that could be an original: the name it is known by, its
        time, where it is, what kind of place it is in, the path that was searched and the library that holds it."""
        raise NotImplementedError

    def prepare(self, progress: Progress) -> None:
        self.progress = progress
        self.index()

    def index(self) -> dict:
        if self._index is None:
            self._index = {}
            count, started = 0, time.monotonic()
            for name, modified, path, kind, root, library in self.files():
                self._index.setdefault(name.lower(), []).append((modified, path, kind, root, library))
                count += 1
                if count % 100 == 0:
                    self.progress.update(self.status(count, started))
            self.finished(count, started)
        return self._index

    def status(self, count: int, started: float) -> str:
        """The progress text while indexing: count files so far, since started."""
        return f"Indexing: {count:,} files so far ({duration(time.monotonic() - started)})"

    def finished(self, count: int, started: float) -> None:
        self.progress.line(f"Indexed {count:,} files in {duration(time.monotonic() - started)}")

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        taken = captured(key)
        near = [(abs(entry[0] - taken), entry) for entry in self.index().get(key[2].lower(), []) if abs(entry[0] - taken) <= TOLERANCE]
        if not near:
            return None
        _, path, kind, root, library = min(near, key=lambda pair: pair[0])[1]
        return Found(kind, with_pair(path, sorted(p for p in path.parent.iterdir() if p.suffix.lower() in MEDIA_FILES)), path.stat().st_size, root, library)


def is_current_library_originals(path: Path) -> bool:
    """The originals folder of a current-format Photos library: its files are named by random ID, so a search by name
    cannot match them (Folders reads the library's database instead)."""
    return path.name == "originals" and path.parent.name.endswith(".photoslibrary")


def has_database(path: Path) -> bool:
    try:
        return (path / "database" / "Photos.sqlite").exists()
    except OSError:
        return False


def database_originals(library: Path):
    """(name, time, path) for each photo whose original is in a Photos library: the name is the one its database gives,
    since the file is named by an ID."""
    database = library / "database" / "Photos.sqlite"
    db = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        rows = db.execute(
            "select coalesce(x.ZORIGINALFILENAME, a.ZFILENAME), a.ZDATECREATED + 978307200, a.ZDIRECTORY, a.ZFILENAME "
            "from ZASSET a left join ZADDITIONALASSETATTRIBUTES x on x.ZASSET = a.Z_PK where a.ZTRASHEDSTATE = 0"
        ).fetchall()
    finally:
        db.close()
    for name, taken, directory, stored_as in rows:
        if name and taken is not None and directory is not None and stored_as:
            original = library / "originals" / str(directory) / stored_as
            if original.exists():
                yield name, taken, original


class Folders(ByNameAndTime):
    """Everything under the paths, however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture
    library's Masters, a Time Machine backup) are matched by file name and time; a Photos library found on the way is
    read through its database, since its files are named by ID. The library being repaired is never searched."""
    name = "folders"

    def __init__(self, folders: List[Path], default: Path):
        super().__init__()
        self.folders = folders
        self.default = default
        self.unreadable: List[str] = []
        self.in_folder = 0  # files indexed so far in the folder being searched, and how many libraries were read there
        self.libraries_in_folder = 0
        self.folder_started = 0.0

    def problem(self) -> Optional[str]:
        missing = [str(folder) for folder in self.folders if not folder.exists()]
        return f"{', '.join(missing)} does not exist" if missing else None

    def status(self, count: int, started: float) -> str:
        return f"  Indexing: {self.in_folder:,} files so far ({duration(time.monotonic() - self.folder_started)})"

    def finished(self, count: int, started: float) -> None:
        if len(self.folders) > 1:
            self.progress.line(f"Indexed {count:,} files in {duration(time.monotonic() - started)}")

    def files(self):
        seen = set()  # databases already read: the default library, and the same file reached through another backup
        try:
            status = os.stat(self.default / "database" / "Photos.sqlite")
            seen.add((status.st_dev, status.st_ino))
        except OSError:
            pass

        def library(path: Path, root: str):
            try:
                status = os.stat(path / "database" / "Photos.sqlite")
            except OSError:
                return
            if (status.st_dev, status.st_ino) in seen:
                return
            seen.add((status.st_dev, status.st_ino))
            self.libraries_in_folder += 1
            self.progress.update(f"  Indexing: reading library {path}")
            try:
                for name, taken, original in database_originals(path):
                    yield name, taken, original, "library", root, str(path)
            except sqlite3.Error:
                self.unreadable.append(str(path))

        def skip(path: Path) -> bool:  # libraries with a database are read through it; the rest of the walk is for raw files
            return path.name in IGNORED_FOLDERS or has_database(path) or is_current_library_originals(path)

        def search(folder: Path):
            if has_database(folder):
                yield from library(folder, str(folder))
                return
            for path, status, is_directory in walk(folder, skip):
                if is_directory:
                    if has_database(path):
                        yield from library(path, str(folder))
                    elif path.name.endswith(".photoslibrary") and not (path / "Masters").exists():
                        self.unreadable.append(str(path))  # a current-format library whose database is not there
                elif stat.S_ISREG(status.st_mode) and path.suffix.lower() in MEDIA_FILES and not path.name.startswith("._"):
                    yield path.name, status.st_mtime, path, "folder", str(folder), ""

        for folder in self.folders:
            self.in_folder = self.libraries_in_folder = 0
            self.folder_started = time.monotonic()
            self.progress.line(f"Searching {folder}")
            for item in search(folder):
                self.in_folder += 1
                yield item
            libraries = f", {self.libraries_in_folder} librar{'y' if self.libraries_in_folder == 1 else 'ies'} read" if self.libraries_in_folder else ""
            self.progress.line(f"  {self.in_folder:,} files{libraries} ({duration(time.monotonic() - self.folder_started)})")

    def note(self) -> Optional[str]:
        if self.unreadable:
            heading = f"{len(self.unreadable)} librar{'y' if len(self.unreadable) == 1 else 'ies'} could not be read:"
            return "\n".join([heading] + [f"  {library}" for library in self.unreadable])
        return None


# The search specification ---------------------------------------------------------------------------------------------

KINDS = ("messages", "folders")


def parse_search(text: str) -> List[Tuple[str, List[str]]]:
    """APPLE_PHOTOS_RECOVER_SEARCH: space-separated entries, read as a shell reads words (so a path with a space is
    quoted), each key or key=paths with paths separated like PATH. Returns [(kind, [paths])] in the order written."""
    entries = []
    for word in shlex.split(text):
        kind, equals, value = word.partition("=")
        if kind not in KINDS:
            hint = "" if equals else f" (if {kind!r} is part of a path with a space in it, put quotes around that path, such as folders='/a b')"
            raise ValueError(f"unknown place to search {kind!r}; use {', '.join(KINDS)}{hint}")
        paths = [entry for entry in value.split(os.pathsep) if entry] if equals else [DEFAULT]
        if equals and not paths:
            raise ValueError(f"{kind}= names no path")
        if kind != "messages" and DEFAULT in paths:
            raise ValueError(f"{kind} needs a path; {DEFAULT!r} is only for messages")
        entries.append((kind, paths))
    return entries


def build_sources(entries: List[Tuple[str, List[str]]], default_library: Path) -> List[Source]:
    """The sources for a search specification, in order. Entries of the same kind are joined."""
    joined: Dict[str, List[str]] = {}
    order: List[str] = []
    for kind, paths in entries:
        if kind not in joined:
            order.append(kind)
        joined.setdefault(kind, []).extend(paths)
    sources: List[Source] = []
    for kind in order:
        folders = [messages_attachments() if path == DEFAULT else Path(path).expanduser() for path in joined[kind]]
        if kind == "messages":
            sources.append(Messages(folders))
        else:
            sources.append(Folders(folders, default_library))
    return sources
