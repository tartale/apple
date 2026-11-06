"""`./apple photos recover`: find where the original of a photo that a library does not store can be had, and import it.

A library keeps a placeholder for a photo whose original it does not hold (a photo shared with you in Messages and never
saved, for one). The places to look are in recover_sources.py; the caller says which to search.
"""
import csv
import json
import os
import sys
from collections import Counter
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
    """What to search: the options if any is given, else APPLE_PHOTOS_RECOVER_SEARCH, else nothing (there are no defaults,
    and with nothing to search the command just lists the photos whose original is missing)."""
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
    return entries


def read_list(text: str) -> List[dict]:
    """The photos to look for, from csv or json with a name and a date column (the output of this command, or of `apple
    photos diff` with its date and name columns). When the list also says where an original was found (the source, root,
    library and files columns of this command's output), that is kept. files is an array in json and separated by | in csv."""
    stripped = text.strip()
    try:
        if stripped.startswith("["):
            records = [dict(record) for record in json.loads(stripped)]
        else:
            records = list(csv.DictReader(stripped.splitlines()))
    except (ValueError, TypeError, csv.Error):
        raise typer.BadParameter("--list is not csv or json from `apple photos recover` or `apple photos diff`")
    items = []
    for record in records:
        if "name" not in record or "date" not in record:
            raise typer.BadParameter("--list needs a name and a date column")
        files = record.get("files") or []
        if isinstance(files, str):
            files = [entry for entry in files.split("|") if entry]
        items.append({
            "name": str(record["name"]), "date": str(record["date"]), "hidden": is_true(record.get("hidden")),
            "shared": is_true(record.get("shared")), "source": str(record.get("source") or ""),
            "root": str(record.get("root") or ""), "library": str(record.get("library") or ""), "files": [str(f) for f in files],
        })
    return items


def parse_map(entries: Optional[List[str]]) -> dict:
    mapping = {}
    for entry in entries or []:
        old, equals, new = entry.partition("=")
        if not equals or not old or not new:
            raise typer.BadParameter(f"--map {entry!r}: use OLD=NEW, the path in the list and where it is here")
        mapping[old.rstrip("/")] = new.rstrip("/")
    return mapping


def translate(path: str, mapping: dict) -> str:
    """The path with the longest matching start in the mapping replaced."""
    for old in sorted(mapping, key=len, reverse=True):
        if path == old or path.startswith(old + "/"):
            return mapping[old] + path[len(old):]
    return path


def is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def recover_command(
    default_library: Annotated[
        Optional[str],
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The default library, the one whose missing originals are looked for and the only one photos are imported into: "
            "a name from `./apple photos libraries`, or the path of a .photoslibrary. Also read from APPLE_PHOTOS_DEFAULT_LIBRARY. "
            "Needed unless --list says which photos to look for and nothing is imported.",
        ),
    ] = None,
    list_file: Annotated[
        Optional[str],
        typer.Option(
            "--list",
            help="Look for the photos in this csv or json file (- for stdin) instead of reading the default library, so the "
            "search can run where the library is not mounted. It needs name and date columns: the output of this command or of "
            "`apple photos diff --format csv` will do. A row that says where its original is (this command's output does) is "
            "used as it is, once --map has translated the paths, without searching; a row whose files cannot be reached here is "
            "searched for only if you gave places to search. Not with --messages, which finds files by an ID that only the library holds.",
        ),
    ] = None,
    messages: Annotated[bool, typer.Option("--messages", help="Search Messages' attachments (~/Library/Messages/Attachments; Terminal needs Full Disk Access): the files of photos shared with you, found by the ID in the library.")] = False,
    messages_folder: Annotated[Optional[List[Path]], typer.Option(help="Search another folder of Messages attachments, as --messages; repeat for several.")] = None,
    folders: Annotated[
        Optional[List[Path]],
        typer.Option(help="Search everything under a path, however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture library's Masters, a Time Machine backup) are matched by file name and capture time, and the Photos libraries found on the way are read through their database. The default library is never searched. Repeat for several."),
    ] = None,
    map_paths: Annotated[
        Optional[List[str]],
        typer.Option("--map", help="With --list: OLD=NEW, where a path the list gives is mounted here (the list has the paths as the machine that searched saw them). Repeat for each root; if a root cannot be reached, the message says which to map."),
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

    Say where to search with --messages and --folders (each can be repeated), or the variable
    APPLE_PHOTOS_RECOVER_SEARCH when neither is given; with neither, nothing is searched and the command only lists the
    photos whose original is missing, which is what to save as a --list for a search elsewhere. The variable holds
    space-separated entries read as a shell reads words, each a key or key=paths with paths separated like PATH, for example:
    messages folders=/Volumes/icloud/photos:"/Volumes/My Disk/Libraries". A bare messages is Messages' default folder,
    and messages=default:/other adds another. Places are searched in the order written; with options, messages first,
    then folders; the first to find an original wins.

    Photos are imported only into the default library (--default-library, or APPLE_PHOTOS_DEFAULT_LIBRARY), and only
    while Photos has that library open; otherwise nothing is imported.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    entries = search_entries(messages, messages_folder, folders)
    if not entries:
        typer.echo(f"Not searching anywhere, so no original is looked for: pass --messages or --folders, or set {SEARCH_VARIABLE}.", err=True)
    mode = Apply.no if is_dry_run(dry_run) else apply
    if list_file is not None and any(kind == "messages" for kind, _ in entries):
        raise typer.BadParameter("--list cannot be used with Messages: it finds files by an ID that only the library holds")
    if default_library is None and (list_file is None or mode != Apply.no):
        raise typer.BadParameter("--default-library is needed (or APPLE_PHOTOS_DEFAULT_LIBRARY)" + ("" if list_file is None else " to import"))
    # The library is looked up only when it is needed: to read the missing photos, or to import. A name is looked up in
    # PowerPhotos, which exists only on a Mac, so a search from a --list elsewhere (a NAS) must not do it.
    needed = list_file is None or mode != Apply.no
    if default_library and Path(default_library).is_dir():
        path = Path(default_library)
    elif default_library and needed:
        path = find_library(default_library)
    else:
        path = Path("/nonexistent/default.photoslibrary")
    sources = build_sources(entries, path)
    for source in sources:
        reason = source.problem()
        if reason:
            fail(f"Cannot search {source.name}: {reason}.")

    if list_file is None:
        try:
            photos = pp.library_photos(path)
        except pp.PowerPhotosError as error:
            fail(str(error))
        wanted = {}
        for counted in (photos.visible, photos.hidden):
            for key in pp.of_media(within(counted, since, until), media.value):
                wanted[key] = key in photos.hidden
        items = [
            {"key": key, "hidden": wanted[key], "shared": key in photos.shared}
            for key in sorted(wanted) if photos.original_missing(key)
        ]
    else:
        photos = None
        items = []
        for entry in read_list(sys.stdin.read() if list_file == "-" else Path(list_file).read_text(encoding="utf-8")):
            key = (entry["date"], "0", entry["name"])
            if (since and entry["date"][:10] < since.date().isoformat()) or (until and entry["date"][:10] > until.date().isoformat()):
                continue
            if not pp.of_media(Counter({key: 1}), media.value):
                continue
            items.append({"key": key, **{name: entry[name] for name in ("hidden", "shared", "source", "root", "library", "files")}})

    mapping = parse_map(map_paths)
    rows, hits, unreachable = [], [], {}
    for item in items:
        key = item["key"]
        found = None
        if item.get("files"):  # the list says where the original is
            files = [translate(f, mapping) for f in item["files"]]
            if all(Path(f).exists() for f in files):
                found = Found(item["source"] or "list", files, Path(files[0]).stat().st_size, item.get("root", ""), item.get("library", ""))
            else:
                unreachable[item.get("root") or str(Path(item["files"][0]).parent)] = unreachable.get(item.get("root") or str(Path(item["files"][0]).parent), 0) + 1
        if not found:
            found = next((hit for hit in (source.find(photos, key) for source in sources) if hit), None)
        if found:
            hits.append(found)
        rows.append({
            "date": key[0], "size": found.size if found else None, "name": key[2], "hidden": item["hidden"],
            "shared": item["shared"], "source": found.source if found else "none",
            "root": found.root if found else "", "library": found.library if found else "", "files": found.files if found else [],
        })
    print_recover(rows, format)
    if unreachable:
        roots = ", ".join(f"{root} ({count(n, 'photo')})" for root, n in sorted(unreachable.items()))
        typer.echo(f"The list says where {sum(unreachable.values())} originals are, but they cannot be reached from here: {roots}.", err=True)
        typer.echo("Map each root to where it is mounted here: " + " ".join(f"--map {root}=<path here>" for root in sorted(unreachable)), err=True)
    for source in sources:
        if source.note():
            typer.echo(source.note(), err=True)
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
        + (" [list only]" if not entries else " [dry run]" if mode == Apply.no and hits else ""),
        err=True,
    )
    if errors:
        raise typer.Exit(1)


def print_recover(rows: list, format: DiffFormat) -> None:
    """The table has date, size (of the original found), name, hidden, shared and the kind of place it was found in. csv and
    json also say the root searched, the library, and every file to import (a | separated list in csv, an array in json)."""
    columns = ["date", "size", "name", "hidden", "shared", "source"]
    extra = ["root", "library", "files"]
    if format == DiffFormat.json:
        typer.echo(json.dumps([{column: row[column] for column in columns + extra} for row in rows], indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns + extra)
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns] + [row["root"], row["library"], "|".join(row["files"])])
    elif rows:
        cells = [columns] + [[cell_text(row[column]) for column in columns] for row in rows]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(columns))]
        for cell in cells:
            typer.echo("  ".join((text.rjust(width) if columns[i] == "size" else text.ljust(width)) for i, (text, width) in enumerate(zip(cell, widths))).rstrip())
