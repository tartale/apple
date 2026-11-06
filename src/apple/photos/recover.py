"""`./apple photos recover`: find where the original of a photo that a library does not store can be had, and import it.

A library keeps a placeholder for a photo whose original it does not hold (a photo shared with you in Messages and never
saved, for one). The places to look are in recover_sources.py; the caller says which to search.
"""
import csv
import json
import os
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from apple.photos import powerphotos as pp
from apple.photos.common import is_dry_run
from apple.photos.library_commands import DateFormat, DiffFormat, Media, cell_text, count, fail, find_library, within
from apple.photos.recover_sources import DEFAULT, Found, build_sources, import_files, parse_search
from apple.platform import require_macos

SEARCH_VARIABLE = "APPLE_PHOTOS_RECOVER_SEARCH"


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


def search_entries(messages: bool, messages_folder: Optional[List[Path]], folders: Optional[List[Path]]) -> list:
    """What to search: the options if any is given, else APPLE_PHOTOS_RECOVER_SEARCH, else nothing (there are no defaults)."""
    given = [
        ("messages", ([DEFAULT] if messages else []) + [str(path) for path in messages_folder or []]),
        ("folders", [str(path) for path in folders or []]),
    ]
    given = [(kind, paths) for kind, paths in given if paths]
    if given:
        return given
    try:
        entries = parse_search(os.environ.get(SEARCH_VARIABLE, ""))
    except ValueError as error:
        raise typer.BadParameter(f"{SEARCH_VARIABLE}: {error}")
    if not entries:
        raise typer.BadParameter(f"nothing to search: pass --messages or --folders, or set {SEARCH_VARIABLE}")
    return entries


def recover_command(
    default_library: Annotated[
        str,
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The default library, the one whose missing originals are looked for and the only one photos are imported into: "
            "a name from `./apple photos libraries`, or the path of a .photoslibrary. Also read from APPLE_PHOTOS_DEFAULT_LIBRARY.",
        ),
    ],
    messages: Annotated[bool, typer.Option("--messages", help="Search Messages' attachments (~/Library/Messages/Attachments; Terminal needs Full Disk Access): the files of photos shared with you, found by the ID in the library.")] = False,
    messages_folder: Annotated[Optional[List[Path]], typer.Option(help="Search another folder of Messages attachments, as --messages; repeat for several.")] = None,
    folders: Annotated[
        Optional[List[Path]],
        typer.Option(help="Search everything under a path, however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture library's Masters, a Time Machine backup) are matched by file name and capture time, and the Photos libraries found on the way are read through their database. The default library is never searched. Repeat for several."),
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

    You must say where to search: --messages and --folders (each can be repeated), or the variable
    APPLE_PHOTOS_RECOVER_SEARCH when neither is given, which holds space-separated entries read as a shell reads words,
    each a key or key=paths with paths separated like PATH, for example:
    messages folders=/Volumes/icloud/photos:"/Volumes/My Disk/Libraries". A bare messages is Messages' default folder,
    and messages=default:/other adds another. Places are searched in the order written; with options, messages first,
    then folders; the first to find an original wins.

    Photos are imported only into the default library (--default-library, or APPLE_PHOTOS_DEFAULT_LIBRARY), and only
    while Photos has that library open; otherwise nothing is imported.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    entries = search_entries(messages, messages_folder, folders)
    path = find_library(default_library)
    sources = build_sources(entries, path)
    for source in sources:
        reason = source.problem()
        if reason:
            fail(f"Cannot search {source.name}: {reason}.")
    try:
        photos = pp.library_photos(path)
    except pp.PowerPhotosError as error:
        fail(str(error))

    wanted = {}
    for counted in (photos.visible, photos.hidden):
        for key in pp.of_media(within(counted, since, until), media.value):
            wanted[key] = key in photos.hidden
    rows, hits = [], []
    for key in sorted(wanted):
        if not photos.original_missing(key):
            continue
        found = next((hit for hit in (source.find(photos, key) for source in sources) if hit), None)
        if found:
            hits.append(found)
        rows.append({
            "date": key[0], "size": found.size if found else None, "name": key[2], "hidden": wanted[key],
            "shared": key in photos.shared, "source": found.source if found else "none",
            "file": found.files[0] if found else "",
        })
    print_recover(rows, format)
    for source in sources:
        if source.note():
            typer.echo(source.note(), err=True)
    mode = Apply.no if is_dry_run(dry_run) else apply
    imported = errors = already = 0
    if hits and mode != Apply.no:
        require_macos()
        check_open_in_photos(path)
        go = True
        if mode == Apply.ask:
            if not has_terminal():
                typer.echo("Nothing was imported: --apply ask needs a terminal.", err=True)
                go = False
            else:
                go = typer.confirm(f"Import {len(hits)} photos into the library Photos has open (it should be {default_library})?", default=False)
        if go:
            try:
                with pp.one_at_a_time():
                    try:
                        done = import_files(hits)
                        imported += done
                        already += max(len(hits) - done, 0)  # Photos had these: they were not missing after all
                    except pp.PowerPhotosError as error:
                        errors += len(hits)
                        typer.echo(f"Photos failed to import: {error}", err=True)
            except pp.Busy as error:
                fail(str(error))
    missing = len(rows) - already
    skipped = missing - imported - errors  # no original was found, or the answer was no
    typer.echo(
        f"Imported {imported} of {missing} missing files; {count(errors, 'error')}, {skipped} skipped"
        + (" [dry run]" if mode == Apply.no and hits else ""),
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
