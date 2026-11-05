"""`./apple photos recover`: find where the original of a photo that a library does not store can be had, and import it.

A library keeps a placeholder for a photo whose original it does not hold (a photo shared with you in Messages and never
saved, for one). Each way of getting the original back is a Source: it looks a photo up, and can import what it found.
To add a way, write a Source and put it in SOURCES.
"""
import csv
import json
import os
import sys
import stat
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, List, NamedTuple, Optional

import typer

from apple.photos import powerphotos as pp
from apple.photos.common import is_dry_run
from apple.photos.library_commands import DateFormat, DiffFormat, Media, cell_text, count, fail, find_library, within
from apple.platform import require_macos

MEDIA_FILES = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".dng", ".mov", ".mp4", ".m4v", ".3gp")


class Found(NamedTuple):
    """Where a source can get the original: the files to import, and the size in bytes of the photo's own file."""
    source: str
    files: list
    size: int


class Source:
    """One way of recovering originals."""
    name = ""
    description = ""

    def available(self) -> Optional[str]:
        """Why this source cannot be used right now, or None when it can."""
        return None

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        """Where the original of the photo (key is (date, size, file name)) can be had from this source, or None."""
        raise NotImplementedError

    def apply(self, found: List[Found]) -> int:
        """Imports what find returned into the library Photos has open; returns how many items were imported."""
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


def messages_attachments() -> Path:
    return Path.home() / "Library" / "Messages" / "Attachments"


class Messages(Source):
    name = "messages"
    description = "the attachment of a photo shared with you, which Messages may still hold (Terminal needs Full Disk Access)"

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        identifier = photos.identifiers.get(key)
        if not identifier or not messages_attachments().is_dir():
            return None
        for folder in messages_attachments().glob(f"*/*/{identifier}"):
            candidates = sorted(f for f in folder.iterdir() if f.suffix.lower() in MEDIA_FILES)
            if not candidates:
                continue
            main = ([f for f in candidates if f.name.lower() == key[2].lower()] or candidates[:1])[0]
            return Found(self.name, with_pair(main, candidates), main.stat().st_size)
        return None


HIDDEN_DIRECTORIES = ".HFS+ Private Directory Data\r"


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


def media_files(root: Path):
    """(path, status) of every photo or video file under root, following Time Machine's directory links on Linux. Each
    real directory is read once, however many backups link to it."""
    volume = volume_of(root)
    seen = set()

    def walk(folder: Path):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            real = Path(entry.path) if stat.S_ISDIR(status.st_mode) else (directory_link_target(status, volume) if volume else None)
            if real is not None:
                if real not in seen:
                    seen.add(real)
                    yield from walk(real)
            elif stat.S_ISREG(status.st_mode) and Path(entry.name).suffix.lower() in MEDIA_FILES:
                yield Path(entry.path), status

    top = root
    if volume:
        try:
            link = directory_link_target(os.lstat(root), volume)
        except OSError:
            link = None
        top = link or root
    yield from walk(top)


class ByNameAndTime(Source):
    """A source that finds an original among files kept by name: a photo matches by file name and a modification time
    within two hours of its capture time (the same photo is an hour out where daylight saving differs). A name alone is
    not enough, since the numbers in IMG_nnnn.JPG repeat. Subclasses say where the files are."""
    tolerance = 7200  # seconds

    def __init__(self):
        self._roots = None
        self._index = None

    def roots(self) -> List[Path]:
        raise NotImplementedError

    def index(self) -> dict:
        """lower-case file name -> [(modification time, path)] of every photo or video file under the roots."""
        roots = self.roots()
        if self._index is None or self._roots != roots:
            self._roots, self._index = roots, {}
            for root in roots:
                for path, status in media_files(root):
                    self._index.setdefault(path.name.lower(), []).append((status.st_mtime, path))
        return self._index

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        taken = datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        near = [(abs(modified - taken), path) for modified, path in self.index().get(key[2].lower(), []) if abs(modified - taken) <= self.tolerance]
        if not near:
            return None
        main = min(near)[1]
        return Found(self.name, with_pair(main, sorted(p for p in main.parent.iterdir() if p.suffix.lower() in MEDIA_FILES)), main.stat().st_size)


class Backup(ByNameAndTime):
    name = "backup"
    description = "the iCloud backup made by `apple photos backup` under ICLOUD_ROOT, matched by file name and capture time"

    def available(self) -> Optional[str]:
        root = os.environ.get("ICLOUD_ROOT")
        if not root:
            return "ICLOUD_ROOT is not set"
        if not Path(root).is_dir():
            return f"{root} is not a folder"
        return None

    def roots(self) -> List[Path]:
        return sorted(Path(os.environ.get("ICLOUD_ROOT", "")).glob("*/originals"))


class Folders(ByNameAndTime):
    name = "folders"
    description = (
        "any folders you name with --folder (or APPLE_PHOTOS_RECOVER_FOLDERS, separated like PATH), such as an iPhoto "
        "library's Masters or a Time Machine backup, matched by file name and capture time"
    )

    def __init__(self):
        super().__init__()
        self.folders: List[Path] = []

    def available(self) -> Optional[str]:
        if not self.folders:
            return "no folder is named: give --folder, or set APPLE_PHOTOS_RECOVER_FOLDERS"
        missing = [str(folder) for folder in self.folders if not folder.exists()]
        return f"{', '.join(missing)} does not exist" if missing else None

    def roots(self) -> List[Path]:
        return self.folders


SOURCES = {source.name: source for source in (Messages(), Backup(), Folders())}
SourceName = Enum("SourceName", {name: name for name in SOURCES}, type=str)


class Apply(str, Enum):
    yes = "yes"
    no = "no"
    ask = "ask"


def has_terminal() -> bool:
    return sys.stdin.isatty()


def check_open_in_photos(target: Path) -> None:
    """Stops unless Photos has target open: Photos imports into whichever library it has open."""
    opened = pp.libraries_open_in_photos()
    if not any(library_open.resolve() == target.resolve() for library_open in opened):
        shown = ", ".join(str(path) for path in opened) or "none, Photos is not running"
        fail(f"Photos does not have {target} open (open: {shown}). Open it in Photos, then run this again.")


def recover_command(
    default_library: Annotated[
        str,
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The default library, the one whose missing originals are looked for and the only one photos are imported into: "
            "a name from `./apple photos libraries`, or the path of a .photoslibrary. Also read from APPLE_PHOTOS_DEFAULT_LIBRARY.",
        ),
    ],
    source: Annotated[
        Optional[List[SourceName]],
        typer.Option(help=f"Where to look, in this order; repeat to use several. The default is all: {', '.join(f'{n} ({s.description})' for n, s in SOURCES.items())}."),
    ] = None,
    apply: Annotated[
        Apply,
        typer.Option(help="Import what was found into the library Photos has open: yes, no (only report), or ask once in the terminal."),
    ] = Apply.no,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Import nothing, as --apply no. Same as DRY_RUN=true.")] = False,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    folder: Annotated[
        Optional[List[Path]],
        typer.Option(help="A folder for the folders source to search for originals, such as an iPhoto library's Masters or a Time Machine backup; repeat for several. Also read from APPLE_PHOTOS_RECOVER_FOLDERS, separated like PATH."),
    ] = None,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo.")] = DiffFormat.text,
) -> None:
    """Find where the originals of photos a library does not store can be had from, and optionally import them.

    A photo whose original is not in the library cannot be listed or copied by PowerPhotos. This lists each one (its
    `missing` column in `apple photos diff`) with where an original was found, or `none`. With --apply yes (or ask),
    the files found are imported into the library that Photos has open, so open the library in Photos first. The import
    makes a new, ordinary photo; the placeholder stays, but `diff` no longer counts it once the copy is there. Nothing
    is deleted, and nothing is changed unless --apply says so (--dry-run, or DRY_RUN=true, always wins).

    Photos are imported only into the default library (--default-library, or APPLE_PHOTOS_DEFAULT_LIBRARY), and only
    while Photos has that library open; otherwise nothing is imported.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    named = folder or [Path(entry) for entry in os.environ.get("APPLE_PHOTOS_RECOVER_FOLDERS", "").split(os.pathsep) if entry]
    SOURCES["folders"].folders = named
    chosen = []
    for candidate in ([SOURCES[name.value] for name in source] if source else list(SOURCES.values())):
        reason = candidate.available()
        if reason and source:
            fail(f"The {candidate.name} source cannot be used: {reason}.")
        if reason:
            typer.echo(f"Not looking in {candidate.name}: {reason}.", err=True)
        else:
            chosen.append(candidate)
    path = find_library(default_library)
    try:
        photos = pp.library_photos(path)
    except pp.PowerPhotosError as error:
        fail(str(error))

    wanted = {}
    for counted in (photos.visible, photos.hidden):
        for key in pp.of_media(within(counted, since, until), media.value):
            wanted[key] = key in photos.hidden
    rows, found_by_source = [], {}
    for key in sorted(wanted):
        if not photos.original_missing(key):
            continue
        found = next((hit for hit in (s.find(photos, key) for s in chosen) if hit), None)
        if found:
            found_by_source.setdefault(found.source, []).append(found)
        rows.append({
            "date": key[0], "size": found.size if found else None, "name": key[2], "hidden": wanted[key],
            "shared": key in photos.shared, "source": found.source if found else "none",
            "file": found.files[0] if found else "",
        })
    print_recover(rows, format)
    recoverable = sum(len(hits) for hits in found_by_source.values())
    mode = Apply.no if is_dry_run(dry_run) else apply
    imported = errors = already = 0
    if recoverable and mode != Apply.no:
        require_macos()
        check_open_in_photos(path)
        go = True
        if mode == Apply.ask:
            if not has_terminal():
                typer.echo("Nothing was imported: --apply ask needs a terminal.", err=True)
                go = False
            else:
                go = typer.confirm(f"Import {recoverable} photos into the library Photos has open (it should be {default_library})?", default=False)
        if go:
            try:
                with pp.one_at_a_time():
                    for name, hits in found_by_source.items():
                        try:
                            done = SOURCES[name].apply(hits)
                            imported += done
                            already += max(len(hits) - done, 0)  # Photos had these: they were not missing after all
                        except pp.PowerPhotosError as error:
                            errors += len(hits)
                            typer.echo(f"Photos failed to import from {name}: {error}", err=True)
            except pp.Busy as error:
                fail(str(error))
    missing = len(rows) - already
    skipped = missing - imported - errors  # no original was found, or the answer was no
    typer.echo(
        f"Imported {imported} of {missing} missing files; {count(errors, 'error')}, {skipped} skipped"
        + (" [dry run]" if mode == Apply.no and recoverable else ""),
        err=True,
    )
    if errors:
        raise typer.Exit(1)


def print_recover(rows: list, format: DiffFormat) -> None:
    """size is the size of the original that was found. The file it was found at is left out of the table, since the name
    and the source say enough, but csv and json keep it."""
    columns = ["date", "size", "name", "hidden", "shared", "source"]
    if format == DiffFormat.json:
        typer.echo(json.dumps([{column: row[column] for column in columns + ["file"]} for row in rows], indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns + ["file"])
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns + ["file"]])
    elif rows:
        cells = [columns] + [[cell_text(row[column]) for column in columns] for row in rows]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(columns))]
        for cell in cells:
            typer.echo("  ".join((text.rjust(width) if columns[i] == "size" else text.ljust(width)) for i, (text, width) in enumerate(zip(cell, widths))).rstrip())
