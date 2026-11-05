"""`./apple photos recover`: find where the original of a photo that a library does not store can be had, and import it.

A library keeps a placeholder for a photo whose original it does not hold (a photo shared with you in Messages and never
saved, for one). Each way of getting the original back is a Source: it looks a photo up, and can import what it found.
To add a way, write a Source and put it in SOURCES.
"""
import csv
import json
import os
import sys
import tempfile
from datetime import datetime
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
    """Where a source can get the original: the files to import, and anything worth knowing about them."""
    source: str
    files: list
    note: str


class Source:
    """One way of recovering originals."""
    name = ""
    description = ""

    def find(self, photos: pp.Photos, key: tuple) -> Optional[Found]:
        """Where the original of the photo (key is (date, size, file name)) can be had from this source, or None."""
        raise NotImplementedError

    def apply(self, found: List[Found]) -> int:
        """Imports what find returned into the library Photos has open; returns how many items were imported."""
        raise NotImplementedError


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
            wanted = [f for f in candidates if f.name.lower() == key[2].lower()] or candidates[:1]
            main = wanted[0]
            # the video half of a Live Photo has the same name, so Photos imports the pair as one
            files = [f for f in candidates if f.stem.lower() == main.stem.lower()]
            return Found(self.name, [str(f) for f in files], f"{main.stat().st_size} bytes")
        return None

    def apply(self, found: List[Found]) -> int:
        pp.SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pp.SCRATCH_ROOT) as scratch:
            listing = Path(scratch) / "files.txt"
            listing.write_text("\n".join(path for item in found for path in item.files) + "\n", encoding="utf-8")
            output = pp.run_applescript("import-photos.applescript", str(listing))
        return int(output.rsplit(" ", 1)[-1])


SOURCES = {source.name: source for source in (Messages(),)}
SourceName = Enum("SourceName", {name: name for name in SOURCES}, type=str)


class Apply(str, Enum):
    yes = "yes"
    no = "no"
    ask = "ask"


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
    chosen = [SOURCES[name.value] for name in source] if source else list(SOURCES.values())
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
            "date": key[0], "size": int(key[1]) if key[1].isdigit() else None, "name": key[2], "hidden": wanted[key],
            "shared": key in photos.shared, "source": found.source if found else "none",
            "file": found.files[0] if found else "", "note": found.note if found else "",
        })
    print_recover(rows, format)
    recoverable = sum(len(hits) for hits in found_by_source.values())
    detail = ", ".join(f"{len(hits)} from {name}" for name, hits in found_by_source.items())
    stuck = len(rows) - recoverable
    if not rows:
        message = f"No photo in {default_library} is missing its original: nothing to recover."
    else:
        message = f"{count(len(rows), 'photo')} in {default_library} {'has' if len(rows) == 1 else 'have'} no original stored. "
        message += (f"An original was found for {recoverable} ({detail}). " if recoverable else "No original was found. ")
        message += "" if not recoverable or not stuck else f"None was found for the other {stuck}."
        message = message.strip()
    typer.echo(message, err=True)
    if not recoverable:
        return
    mode = Apply.no if is_dry_run(dry_run) else apply
    if mode == Apply.no:
        typer.echo("Nothing was imported. Use --apply yes (or ask) to import what was found.", err=True)
        return
    require_macos()
    check_open_in_photos(path)
    if mode == Apply.ask:
        if not sys.stdin.isatty():
            typer.echo("Nothing was imported: --apply ask needs a terminal.", err=True)
            return
        if not typer.confirm(f"Import {recoverable} photos into the library Photos has open (it should be {default_library})?", default=False):
            typer.echo("Nothing was imported.", err=True)
            return
    try:
        with pp.one_at_a_time():
            for name, hits in found_by_source.items():
                typer.echo(f"{name}: imported {SOURCES[name].apply(hits)} of {len(hits)}")
    except pp.Busy as error:
        fail(str(error))
    except pp.PowerPhotosError as error:
        fail(f"Photos failed: {error}")


def abbreviate(file: str) -> str:
    """A path short enough for a table: the home folder as ~, and a long run in the middle as an ellipsis, keeping the end
    (the folder, with a long identifier cut short, and the file name)."""
    if not file:
        return ""
    path = Path(file)
    try:
        parts = ["~", *path.relative_to(Path.home()).parts]
    except ValueError:
        parts = list(path.parts)
    if len(parts) > 5:
        parts = parts[:3] + ["..."] + parts[-2:]
    if len(parts) >= 2 and len(parts[-2]) > 16:
        parts[-2] = parts[-2][:13] + "..."
    return "/".join(parts).replace("//", "/")


def print_recover(rows: list, format: DiffFormat) -> None:
    columns = ["date", "size", "name", "hidden", "shared", "source", "file", "note"]
    if format == DiffFormat.json:
        typer.echo(json.dumps([{column: row[column] for column in columns} for row in rows], indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns])
    elif rows:
        cells = [columns] + [
            [abbreviate(row[column]) if column == "file" else cell_text(row[column]) for column in columns] for row in rows
        ]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(columns))]
        for cell in cells:
            typer.echo("  ".join((text.rjust(width) if columns[i] == "size" else text.ljust(width)) for i, (text, width) in enumerate(zip(cell, widths))).rstrip())
