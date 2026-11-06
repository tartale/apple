"""Where `./apple photos recover` can look for the original of a photo a library does not store.

A Source looks one photo up (find) and can import what it found. There are two kinds, by how a file is identified:
Messages (by the attachment ID the Photos database holds) and Folders (everything under a path: raw files matched by name
and time, and Photos libraries read through their database). To add a way, write a Source and give it a keyword in KINDS.
"""
import fnmatch
import os
import queue
import re
import shlex
import sqlite3
import stat
import tempfile
import threading
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
SNAPSHOT = re.compile(r"\d{4}-\d{2}-\d{2}-\d{6}$")  # the folder of a finished Time Machine snapshot (an unfinished one ends in .inProgress)
SNAPSHOTS = ("all", "ends", "newest", "oldest")
WORKERS = 8  # directories read at once: a disk on a slow link spends its time waiting
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


def with_pair(logical: Path, real: Path, folder_files) -> List[str]:
    """The photo's file and, with it, the video half of a Live Photo (the same name with another ending), so that Photos
    imports the pair as one. real is where the folder can be read, folder_files the files in it, and logical where the
    photo is as the backup shows it, which is what is reported."""
    return [str(logical.parent / f.name) for f in folder_files if f.stem.lower() == real.stem.lower()]


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


def step(real: Path, name: str, volume: Optional[Path] = None) -> Optional[Path]:
    """Where real/name can be read: itself, or when it is one of Time Machine's directory links (an empty file on Linux) the
    real folder behind it. None if it does not exist."""
    candidate = real / name
    try:
        status = os.lstat(candidate)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_size:
        return candidate
    volume = volume or volume_of(real)
    return (directory_link_target(status, volume) if volume else None) or candidate


def resolve(path: Path) -> Optional[Path]:
    """Where path can be read, stepping through Time Machine's directory links on Linux; None if it does not exist."""
    parts = path.parts
    real = Path(parts[0])
    for name in parts[1:]:
        found = step(real, name)
        if found is None:
            return None
        real = found
    return real


def has_wildcard(text: str) -> bool:
    return any(character in text for character in "*?[")


PACKAGES = (".photoslibrary", ".photolibrary", ".aplibrary", ".migratedphotolibrary")


def folders_below(logical: Path, real: Path, seen: set, counted: Optional[Callable[[], None]] = None):
    """(logical, real) of this folder and of every folder below it, stepping through Time Machine's directory links on
    Linux. It does not go into a library package or a folder a disk keeps for itself, which hold no folders anyone means.
    A folder whose real location is in seen (a set that can be shared by calls) is left out, and the set is filled in: a
    folder that Time Machine shares between snapshots is then read once."""
    volume = volume_of(real)
    if real in seen:
        return
    seen.add(real)
    pending = [(logical, real)]
    while pending:
        here = pending.pop()
        if counted:
            counted()
        yield here
        try:
            entries = sorted(os.scandir(here[1]), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in reversed(entries):
            if entry.name in IGNORED_FOLDERS or entry.name.endswith(PACKAGES):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    target = Path(entry.path)
                elif volume and Path(entry.name).suffix.lower() not in MEDIA_FILES:
                    target = directory_link_target(entry.stat(follow_symlinks=False), volume)
                else:
                    target = None
            except OSError:
                continue
            if target is not None and target not in seen:
                seen.add(target)
                pending.append((here[0] / entry.name, target))


def expand(path: Path, progress: Optional[Callable[[str], None]] = None) -> List[Path]:
    """The paths a pattern with * ? [..] or ** in it names, sorted, stepping through Time Machine's directory links on Linux.
    ** stands for any number of folders, none included (folders_below says where it does not go); at the end of a pattern
    it adds nothing, since a search of a folder covers what is below it. A path that exists as written is taken as it is, so
    a folder whose name has a bracket in it still works. A path with no wildcard is returned as it is. A ** can look at a
    great many folders, so progress(text), if given, is told how far it is."""
    if not has_wildcard(str(path)) or resolve(path) is not None:
        return [path]
    candidates = [(Path(path.parts[0]), Path(path.parts[0]))]  # (logical, real)
    below = set()  # the folders already listed for a **, so that one shared by many snapshots is read once
    looked = [0]

    def counted() -> None:
        looked[0] += 1
        if progress and looked[0] % 200 == 0:
            progress(f"  Expanding {path.name if path.name != '**' else '**'}: {looked[0]:,} folders looked at so far")

    last = len(path.parts) - 1
    for position, name in enumerate(path.parts[1:], 1):
        following = []
        for logical, real in candidates:
            if name == "**":
                following.extend([(logical, real)] if position == last else folders_below(logical, real, below, counted))
                continue
            if has_wildcard(name):
                try:
                    names = sorted(fnmatch.filter(os.listdir(real), name))
                except OSError:
                    continue
            else:
                names = [name]
            for match in names:
                found = step(real, match)
                if found is not None:
                    following.append((logical / match, found))
        candidates = following
    seen, result = set(), []
    for logical, _ in candidates:
        if logical not in seen:
            seen.add(logical)
            result.append(logical)
    return result


class Entry(NamedTuple):
    """Something found by walk. logical is where it is as the backup shows it and real where it can be read from; they differ
    inside Time Machine's linked folders on Linux. status is None for a directory."""
    logical: Path
    real: Path
    status: Optional[os.stat_result]
    is_directory: bool


def walk(
    root: Path,
    skip: Callable[[Path, Path], bool] = lambda logical, real: False,
    snapshots: str = "all",
    notes: Callable[[str], None] = lambda text: None,
    workers: int = WORKERS,
    logical_root: Optional[Path] = None,
    seen_directories: Optional[set] = None,
    seen_files: Optional[set] = None,
):
    """Every directory and photo or video file under root, as Entry values in no particular order. Several directories are
    read at once. On Linux Time Machine's linked folders are followed, each real folder once however many snapshots link to
    it; a file hard-linked into many snapshots (the same inode) is reported once. A directory for which skip(logical, real) is
    true is reported and not entered. Of the snapshots in a Backups.backupdb/<machine> folder only those that snapshots
    ('all', 'ends': the oldest and newest, 'newest' or 'oldest') names are read; notes(text) says which. root is where the
    folder can be read and logical_root, if different, where it is as the backup shows it. A directory or file already in
    seen_directories or seen_files (sets that several walks can share) is skipped, and those sets are filled in."""
    volume = volume_of(root)
    try:
        top = os.lstat(root)
    except OSError:
        return
    start = (directory_link_target(top, volume) if volume else None) or root
    seen_directories = set() if seen_directories is None else seen_directories  # pass the same sets to several walks to read
    seen_files = set() if seen_files is None else seen_files  # each directory and file once between them
    lock = threading.Lock()
    tasks: "queue.Queue" = queue.Queue()
    results: "queue.Queue" = queue.Queue()
    pending = [0]
    stop = threading.Event()
    finished = object()

    def enter(real: Path, logical: Path) -> bool:
        with lock:
            if real in seen_directories:
                return False
            seen_directories.add(real)
            pending[0] += 1
        tasks.put((real, logical))
        return True

    def chosen(entries: list, logical: Path) -> list:
        names = sorted(entry.name for entry in entries if SNAPSHOT.match(entry.name))
        if snapshots == "all" or len(names) <= 1:
            return entries
        keep = {"ends": {names[0], names[-1]}, "newest": {names[-1]}, "oldest": {names[0]}}[snapshots]
        if len(keep) < len(names):
            notes(f"  {logical.name}: reading the snapshots {', '.join(sorted(keep))}; {len(names) - len(keep)} others are "
                  "skipped (--snapshots all reads every one)")
        return [entry for entry in entries if not SNAPSHOT.match(entry.name) or entry.name in keep]

    def process(real: Path, logical: Path) -> None:
        try:
            entries = list(os.scandir(real))
        except OSError:
            return
        if logical.parent.name == "Backups.backupdb":
            entries = chosen(entries, logical)
        device = None
        for entry in entries:
            if stop.is_set():
                return
            name, child_logical, child_real = entry.name, logical / entry.name, Path(entry.path)
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_directory:
                results.put(Entry(child_logical, child_real, None, True))
                if not skip(child_logical, child_real):
                    enter(child_real, child_logical)
            elif child_real.suffix.lower() in MEDIA_FILES:
                if name.startswith("._"):  # an archive's resource fork, not a photo
                    continue
                try:
                    if device is None:
                        device = os.stat(real).st_dev
                    key = (device, entry.inode())
                except OSError:
                    continue
                with lock:
                    if key in seen_files:
                        continue
                    seen_files.add(key)
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(status.st_mode):
                    results.put(Entry(child_logical, child_real, status, False))
            elif volume:  # on a Time Machine disk a directory may look like an empty file with a huge link count
                try:
                    target = directory_link_target(entry.stat(follow_symlinks=False), volume)
                except OSError:
                    continue
                if target:
                    results.put(Entry(child_logical, target, None, True))
                    if not skip(child_logical, target):
                        enter(target, child_logical)

    def work() -> None:
        while True:
            task = tasks.get()
            if task is None:
                return
            try:
                process(*task)
            except Exception as error:  # reported to the caller, who is waiting for results
                results.put(error)
            finally:
                with lock:
                    pending[0] -= 1
                    last = pending[0] == 0
                if last:
                    results.put(finished)

    if not enter(start, logical_root or root):  # already read by an earlier walk: nothing to do
        return
    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    try:
        while True:
            item = results.get()
            if item is finished:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()
        for _ in threads:
            tasks.put(None)


# The kinds of place ------------------------------------------------------------------------------------------------------

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
                    return Found("messages", with_pair(main, main, candidates), main.stat().st_size, str(attachments))
        return None


class ByNameAndTime(Source):
    """Files that are known by name: a photo matches by file name and a time within two hours of its capture time. A name
    alone is not enough, since the numbers in IMG_nnnn.JPG repeat."""

    def __init__(self):
        self._index = None
        self.progress = Progress()

    def files(self):
        """(name, time, real, logical, kind, root, library) of each file that could be an original: the name it is known
        by, its time, where it can be read and where it is as the backup shows it, what kind of place it is in, the path
        that was searched and the library that holds it."""
        raise NotImplementedError

    def prepare(self, progress: Progress) -> None:
        self.progress = progress
        self.index()

    def index(self) -> dict:
        if self._index is None:
            self._index = {}
            count, started = 0, time.monotonic()
            for name, modified, real, logical, kind, root, library in self.files():
                self._index.setdefault(name.lower(), []).append((modified, real, logical, kind, root, library))
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
        _, real, logical, kind, root, library = min(near, key=lambda pair: pair[0])[1]
        siblings = sorted(p for p in real.parent.iterdir() if p.suffix.lower() in MEDIA_FILES)
        return Found(kind, with_pair(logical, real, siblings), real.stat().st_size, root, library)


def is_current_library_originals(path: Path) -> bool:
    """The originals folder of a current-format Photos library: its files are named by random ID, so a search by name
    cannot match them (Folders reads the library's database instead)."""
    return path.name == "originals" and path.parent.name.endswith(".photoslibrary")


def has_database(path: Path) -> bool:
    try:
        return (path / "database" / "Photos.sqlite").exists()
    except OSError:
        return False


def database_originals(library: Path, logical: Path):
    """(name, time, real path, logical path) for each photo whose original is in a Photos library (library is where it can
    be read, logical where it is as the backup shows it): the name is the one its database gives, since the file is named
    by an ID."""
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
                yield name, taken, original, logical / "originals" / str(directory) / stored_as


class Folders(ByNameAndTime):
    """Everything under the paths, however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture
    library's Masters, a Time Machine backup) are matched by file name and time; a Photos library found on the way is
    read through its database, since its files are named by ID. The library being repaired is never searched."""
    name = "folders"

    def __init__(self, folders: List[Path], default: Path, snapshots: str = "all"):
        super().__init__()
        self.patterns = folders
        self.folders = [folder for folder in folders if not has_wildcard(str(folder)) or resolve(folder) is not None]  # patterns are expanded when the search starts
        self.unmatched: List[str] = []
        self.default = default
        self.snapshots = snapshots
        self.unreadable: List[str] = []
        self.in_folder = 0  # files indexed so far in the folder being searched, and how many libraries were read there
        self.libraries_in_folder = 0
        self.folder_started = 0.0

    def problem(self) -> Optional[str]:
        missing = [str(folder) for folder in self.patterns if not has_wildcard(str(folder)) and resolve(folder) is None]
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

        def library(path: Path, logical: Path, root: str):
            try:
                status = os.stat(path / "database" / "Photos.sqlite")
            except OSError:
                return
            if (status.st_dev, status.st_ino) in seen:
                return
            seen.add((status.st_dev, status.st_ino))
            self.libraries_in_folder += 1
            self.progress.update(f"  Indexing: reading library {logical}")
            try:
                for name, taken, original, original_logical in database_originals(path, logical):
                    yield name, taken, original, original_logical, "library", root, str(logical)
            except sqlite3.Error:
                self.unreadable.append(str(logical))

        def skip(logical: Path, real: Path) -> bool:  # libraries with a database are read through it; the rest is for raw files
            return logical.name in IGNORED_FOLDERS or has_database(real) or is_current_library_originals(logical)

        seen_directories, seen_files = set(), set()  # shared by all the folders: a file in many snapshots is indexed once

        def search(folder: Path):
            real = resolve(folder) or folder
            if has_database(real):
                yield from library(real, folder, str(folder))
                return
            for entry in walk(real, skip, self.snapshots, lambda text: self.progress.line(text), logical_root=folder, seen_directories=seen_directories, seen_files=seen_files):
                if entry.is_directory:
                    if has_database(entry.real):
                        yield from library(entry.real, entry.logical, str(folder))
                    elif entry.logical.name.endswith(".photoslibrary") and not (entry.real / "Masters").exists():
                        self.unreadable.append(str(entry.logical))  # a current-format library whose database is not there
                else:
                    yield entry.logical.name, entry.status.st_mtime, entry.real, entry.logical, "folder", str(folder), ""

        folders = []
        for pattern in self.patterns:
            if has_wildcard(str(pattern)) and resolve(pattern) is None:
                self.progress.line(f"Expanding {pattern}")
                matches = expand(pattern, self.progress.update)
                if not matches:
                    self.unmatched.append(str(pattern))
                else:
                    self.progress.line(f"  {len(matches):,} folders")
                folders.extend(matches)
            else:
                folders.append(pattern)
        self.folders = folders
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
        lines = []
        if self.unmatched:
            lines += [f"{len(self.unmatched)} pattern{'' if len(self.unmatched) == 1 else 's'} matched no folder:"] + [f"  {pattern}" for pattern in self.unmatched]
        if self.unreadable:
            lines += [f"{len(self.unreadable)} librar{'y' if len(self.unreadable) == 1 else 'ies'} could not be read:"] + [f"  {library}" for library in self.unreadable]
        return "\n".join(lines) or None


# The search specification ---------------------------------------------------------------------------------------------

KINDS = ("messages", "folders")


def parse_search(text: str) -> List[Tuple[str, List[str]]]:
    """APPLE_PHOTOS_RECOVER_SEARCH: space-separated entries, read as a shell reads words (so a path with a space is
    quoted), each key or key=paths with paths separated like PATH. Returns [(kind, [paths])] in the order written."""
    entries = []
    for word in shlex.split(text):
        kind, equals, value = word.partition("=")
        if kind not in KINDS:
            raise ValueError(f"unknown place to search {kind!r}; use {', '.join(KINDS)}")
        paths = [entry for entry in value.split(os.pathsep) if entry] if equals else [DEFAULT]
        if equals and not paths:
            raise ValueError(f"{kind}= names no path")
        if kind != "messages" and DEFAULT in paths:
            raise ValueError(f"{kind} needs a path; {DEFAULT!r} is only for messages")
        entries.append((kind, paths))
    return entries


def build_sources(entries: List[Tuple[str, List[str]]], default_library: Path, snapshots: str = "all") -> List[Source]:
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
            sources.append(Folders(folders, default_library, snapshots))
    return sources
