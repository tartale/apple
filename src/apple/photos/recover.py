"""`./apple photos missing`, `find` and `import`: list the photos whose original a library does not store, find where the original can be had, and import it.

A library keeps a placeholder for a photo whose original it does not hold (a photo shared with you in Messages and never
saved, for one). The places to look are in recover_sources.py; the caller says which to search.
"""
import csv
import json
import os
import shlex
import sys
import time
from collections import Counter
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from apple.photos import powerphotos as pp
from apple.photos.common import is_dry_run
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache
from apple.photos.library_commands import DateFormat, DiffFormat, Media, cell_text, count, fail, find_library, within
from apple.photos.recover_sources import DEFAULT, SNAPSHOTS, Found, build_sources, has_wildcard, import_files, parse_search
from apple.platform import require_macos

SEARCH_VARIABLE = "APPLE_PHOTOS_RECOVER_SEARCH"
MAP_VARIABLE = "APPLE_PHOTOS_RECOVER_MAPPINGS"


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
        fail(f"Photos does not have {target} open (open: {shown}). Open it in Photos (hold Option while starting Photos to choose a library, or double-click it), then run this again.")


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
        raise typer.BadParameter("the list is not csv or json from `apple photos missing`, `find` or `diff`")
    items = []
    for record in records:
        if "name" not in record or "date" not in record:
            raise typer.BadParameter("--list needs a name and a date column")
        files = record.get("files") or []
        if isinstance(files, str):
            files = [entry for entry in files.split("|") if entry]
        items.append({
            "name": str(record["name"]), "date": str(record["date"]), "hidden": is_true(record.get("hidden")),
            "shared": is_true(record.get("shared")), "identifier": str(record.get("identifier") or ""), "source": str(record.get("source") or ""),
            "root": str(record.get("root") or ""), "library": str(record.get("library") or ""), "files": [str(f) for f in files],
        })
    return items


def load_list(list_file: str) -> List[dict]:
    """The rows of the list in the file (- for stdin)."""
    try:
        text = sys.stdin.read() if list_file == "-" else Path(list_file).read_text(encoding="utf-8")
    except OSError as error:
        fail(f"Cannot read {list_file}: {error.strerror or error}.")
    return read_list(text)


def parse_map(entries: Optional[List[str]]) -> List[tuple]:
    """[(old, new)] in the order given. With no --map, APPLE_PHOTOS_RECOVER_MAPPINGS: OLD=NEW entries separated by spaces and
    read as a shell reads words, so that one value can be shared by the machines (a path with a space is quoted)."""
    where = "--map"
    if not entries:
        where = MAP_VARIABLE
        try:
            entries = shlex.split(os.environ.get(MAP_VARIABLE, ""))
        except ValueError as error:
            raise typer.BadParameter(f"{MAP_VARIABLE}: {error}")
    mapping = []
    for entry in entries:
        old, equals, new = entry.partition("=")
        if not equals or not old or not new:
            raise typer.BadParameter(f"{where} {entry!r}: use OLD=NEW, the start of a path in the list and what it is here")
        mapping.append((old.rstrip("/") or "/", new.rstrip("/")))
    return mapping


def translate(path: str, mapping: List[tuple]) -> str:
    """Where the file is here: the path with its start replaced by the first translation, in the order given, whose old
    start it has (at a folder boundary), if a file exists there (for a place to search with wildcards in it, the folders
    before the first wildcard); otherwise the path as written, which is right on the
    machine that made the list. That way one set of translations can be shared by every machine, and each uses the ones
    that apply to it."""
    for old, new in mapping:
        if path == old or path.startswith(old.rstrip("/") + "/"):
            translated = new + path[len(old.rstrip("/")):]
            if exists(translated) or not exists(path):
                return translated
            break
    return path


def exists(path: str) -> bool:
    """Whether the path exists, as far as it names anything fixed: a place to search may have wildcards in it, so only the
    folders before the first one are looked at. One that cannot be looked at (permission denied) is taken as not existing."""
    fixed = []
    for part in Path(path).parts:
        if has_wildcard(part):
            break
        fixed.append(part)
    try:
        return Path(*fixed).exists() if fixed else False
    except PermissionError:
        return False


def is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def missing_command(
    default_library: Annotated[
        Optional[str],
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The default library: a name from `./apple photos libraries`, or the path of a .photoslibrary. Also read from "
            "APPLE_PHOTOS_DEFAULT_LIBRARY.",
        ),
    ] = None,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo, with the Messages identifier of a shared photo.")] = DiffFormat.text,
) -> None:
    """List the photos of the default library whose original it does not store. Changes nothing.

    PowerPhotos cannot list or copy these (the `missing` column of `apple photos diff`): a photo shared with you in
    Messages and never saved, or a hidden item whose file is gone. Save the list (--format csv) and give it to `apple
    photos find` to search for the originals, on this machine or another.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if default_library is None:
        raise typer.BadParameter("--default-library is needed (or APPLE_PHOTOS_DEFAULT_LIBRARY)")
    path = Path(default_library) if Path(default_library).is_dir() else find_library(default_library)
    progress = Progress()
    progress.line(f"Reading the photos of {default_library} ...")
    try:
        photos = pp.library_photos(path)
    except pp.PowerPhotosError as error:
        fail(str(error))
    wanted = {}
    for counted in (photos.visible, photos.hidden):
        for key in pp.of_media(within(counted, since, until), media.value):
            wanted[key] = key in photos.hidden
    rows = [
        {"date": key[0], "name": key[2], "hidden": wanted[key], "shared": key in photos.shared, "identifier": photos.identifiers.get(key, "")}
        for key in sorted(wanted) if photos.original_missing(key)
    ]
    columns = ["date", "name", "hidden", "shared", "identifier"]
    if format == DiffFormat.json:
        typer.echo(json.dumps(rows, indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns])
    elif rows:
        cells = [columns[:-1]] + [[cell_text(row[column]) for column in columns[:-1]] for row in rows]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(columns) - 1)]
        for cell in cells:
            typer.echo("  ".join(text.ljust(width) for text, width in zip(cell, widths)).rstrip())
    typer.echo(f"{count(len(rows), 'missing file')}", err=True)


def find_command(
    list_file: Annotated[
        str,
        typer.Argument(
            metavar="LIST",
            help="The photos to look for: a csv or json file (- for stdin) with name and date columns, such as `apple photos "
            "missing --format csv` or `apple photos diff --format csv`. A Messages search also needs the identifier column of "
            "`missing`; any other column is ignored.",
        ),
    ],
    default_library: Annotated[
        Optional[str],
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The default library, which is never searched even when a folder holds it: the path of a .photoslibrary. "
            "Needed only where the library is mounted.",
        ),
    ] = None,
    messages: Annotated[bool, typer.Option("--messages", help="Search Messages' attachments (~/Library/Messages/Attachments; Terminal needs Full Disk Access): the files of photos shared with you, found by the ID in the library.")] = False,
    messages_folder: Annotated[Optional[List[Path]], typer.Option(help="Search another folder of Messages attachments, as --messages; repeat for several.")] = None,
    folders: Annotated[
        Optional[List[Path]],
        typer.Option(help="Search everything under a path (it may have * ? or [..] in it, to name several folders, such as a Pictures folder in every Time Machine snapshot), however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture library's Masters, a Time Machine backup) are matched by file name and capture time, and the Photos libraries found on the way are read through their database. The default library is never searched. Repeat for several."),
    ] = None,
    map_paths: Annotated[
        Optional[List[str]],
        typer.Option("--map", help="OLD=NEW, a translation of the start of the places to search, for one set of places shared by machines that mount them differently, such as /volume1=/Volumes. Repeat for several; for each place the first one, in the order given, whose OLD starts it is used, so put specific ones before general ones. A place that exists as written is left alone. Without --map, APPLE_PHOTOS_RECOVER_MAPPINGS, which holds OLD=NEW entries separated by spaces and read as a shell reads words."),
    ] = None,
    snapshots: Annotated[
        str,
        typer.Option(help=f"Of the snapshots in a Time Machine Backups.backupdb/<machine> folder under a --folders path, which to read: {', '.join(SNAPSHOTS)}. all (the default) is every one, so nothing that was only in between is missed; ends is the oldest and newest, much faster when there are many snapshots and a path below the snapshot is not given."),
    ] = "all",
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Read the places again instead of reusing what an earlier run saved: library databases are kept by their size and modification time, and Time Machine backup folders by the names of their snapshots. The new result is saved."),
    ] = False,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo.")] = DiffFormat.text,
) -> None:
    """Search for the originals of the photos in a list. Changes nothing.

    Writes each photo of LIST (`apple photos missing --format csv`) with where an original was found, or `none`. Save
    the output (--format csv) and give it to `apple photos import`, on this machine or another, to import the files found.

    Say where to search with --messages and --folders (each can be repeated), or the variable APPLE_PHOTOS_RECOVER_SEARCH
    when neither is given; with neither, it is an error. The variable holds space-separated entries read as a shell reads
    words, each a key or key=paths with paths separated like PATH, for example:
    messages folders=/Volumes/icloud/photos:"/Volumes/My Disk/Libraries". A bare messages is Messages' default folder,
    and messages=default:/other adds another. Places are searched in the order written; with options, messages first,
    then folders; the first to find an original wins.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    entries = search_entries(messages, messages_folder, folders)
    if not entries:
        raise typer.BadParameter(f"nothing to search: pass --messages or --folders, or set {SEARCH_VARIABLE}")
    path = Path(default_library) if default_library and Path(default_library).is_dir() else Path("/nonexistent/default.photoslibrary")
    if snapshots not in SNAPSHOTS:
        raise typer.BadParameter(f"--snapshots must be one of {', '.join(SNAPSHOTS)}")
    # The translations that make the places reachable on this machine, so that one set of settings
    # (APPLE_PHOTOS_RECOVER_SEARCH and APPLE_PHOTOS_RECOVER_MAPPINGS) fits every machine.
    mapping = parse_map(map_paths)
    entries = [(kind, [translate(place, mapping) for place in places] if kind == "folders" else places) for kind, places in entries]
    sources = build_sources(entries, path, snapshots, Cache(read=not refresh))
    for source in sources:
        reason = source.problem()
        if reason:
            fail(f"Cannot search {source.name}: {reason}.")

    progress = Progress()
    items = []
    for entry in load_list(list_file):
        key = (entry["date"], "0", entry["name"])
        if not in_range(entry["date"], since, until) or not pp.of_media(Counter({key: 1}), media.value):
            continue
        items.append({"key": key, "hidden": entry["hidden"], "shared": entry["shared"], "identifier": entry["identifier"]})

    rows, hits = [], []
    looked, started = 0, time.monotonic()
    for source in sources:
        source.prepare(progress)
    for item in items:
        looked += 1
        progress.update(f"Looking up originals: {looked}/{len(items)} ({len(hits)} found)")
        key = item["key"]
        found = next((hit for hit in (source.find(key, item["identifier"]) for source in sources) if hit), None)
        if found:
            hits.append(found)
        rows.append(found_row(key, item, found))
    if items:
        progress.line(f"Looked up {len(items)} photos: {len(hits)} found ({duration(time.monotonic() - started)})")
    print_recover(rows, format)
    for source in sources:
        if source.note():
            typer.echo(source.note(), err=True)
    typer.echo(f"Found {len(hits)} of {len(rows)} missing files", err=True)


def in_range(date: str, since: Optional[datetime], until: Optional[datetime]) -> bool:
    return not ((since and date[:10] < since.date().isoformat()) or (until and date[:10] > until.date().isoformat()))


def found_row(key: tuple, item: dict, found: Optional[Found]) -> dict:
    return {
        "date": key[0], "size": found.size if found else None, "name": key[2], "hidden": item["hidden"],
        "shared": item["shared"], "source": found.source if found else "none",
        "root": found.root if found else "", "library": found.library if found else "", "files": found.files if found else [],
    }


def import_found_command(
    list_file: Annotated[
        str,
        typer.Argument(
            metavar="LIST",
            help="The csv or json (- for stdin) that `apple photos find --format csv` wrote: each row says where its original "
            "is (the source, root, library and files columns). Nothing is searched; a row with no file is skipped.",
        ),
    ],
    default_library: Annotated[
        Optional[str],
        typer.Option(
            "--default-library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The library to import into unless --library names another: a name from `./apple photos libraries`, or the path of a "
            ".photoslibrary. Also read from APPLE_PHOTOS_DEFAULT_LIBRARY. Photos must have it open.",
        ),
    ] = None,
    library: Annotated[
        Optional[str],
        typer.Option(
            "--library",
            help="Import into this library instead of the default library: a name from `./apple photos libraries`, or the path of a "
            ".photoslibrary. Photos imports into whichever library it has open, so it must have this one open.",
        ),
    ] = None,
    map_paths: Annotated[
        Optional[List[str]],
        typer.Option("--map", help="OLD=NEW, a translation of the start of the paths the list gives (it has them as the machine that searched saw them), such as /volume1=/Volumes. Repeat for several; for each path the first one, in the order given, whose OLD starts it is used, so put specific ones before general ones. A path that exists as written is left alone. Without --map, APPLE_PHOTOS_RECOVER_MAPPINGS, which holds OLD=NEW entries separated by spaces and read as a shell reads words, so one value can be shared by the machines."),
    ] = None,
    apply: Annotated[
        Apply,
        typer.Option(help="Import the files into the library Photos has open: yes, no (only report what would be), or ask once in the terminal."),
    ] = Apply.no,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Import nothing, as --apply no. Same as DRY_RUN=true.")] = False,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo.")] = DiffFormat.text,
) -> None:
    """Import the originals that `apple photos find` found, without searching.

    Reads the list recover wrote, translates its paths with --map (or APPLE_PHOTOS_RECOVER_MAPPINGS) to where the files
    are on this machine, and with --apply yes (or ask) imports them into the library that Photos has open, so open the
    library in Photos first. The import makes a new, ordinary photo; the placeholder stays, but `diff` no longer counts
    it once the copy is there. Nothing is deleted, and nothing is changed unless --apply says so (--dry-run, or
    DRY_RUN=true, always wins). Photos go into the default library, or the one --library names, and only while Photos has
    exactly that library open: with another one open nothing is imported.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    mode = Apply.no if is_dry_run(dry_run) else apply
    target = library or default_library  # the library to import into
    if target is None and mode != Apply.no:
        raise typer.BadParameter("--library or --default-library is needed (or APPLE_PHOTOS_DEFAULT_LIBRARY) to import")
    if target and Path(target).expanduser().is_dir():
        path = Path(target).expanduser()
    elif target and mode != Apply.no:
        path = find_library(target)
    else:
        path = Path("/nonexistent/default.photoslibrary")
    import_listed(load_list(list_file), list_file, target, path, mode, parse_map(map_paths), since, until, media, format)


def import_listed(listed: List[dict], list_name: str, target: Optional[str], path: Path, mode: Apply, mapping: List[tuple],
                  since: Optional[datetime], until: Optional[datetime], media: Media, format: DiffFormat) -> None:
    """Import the originals the listed rows (from read_list) say where to find, as import does, into the library target (at
    path) when mode says so: what `import` and `copy --only-missing` from a folder share. list_name is what the rows came from."""
    progress = Progress()
    rows, hits, unreachable, denied = [], [], {}, []
    if listed and not any(entry["files"] for entry in listed):
        if not any(entry["source"] for entry in listed):  # no source column: nobody has looked for these originals
            raise typer.BadParameter(
                f"{list_name} lists photos but not where their originals are (it has no source and files columns), as the output of `apple photos "
                f"missing`, or of `diff` between two libraries, does: search for them first, `apple photos find {list_name} --folders PATH --format csv > found.csv`, "
                "and import found.csv; or `diff LIBRARY FOLDER --only right --format csv`, which lists a folder's files with their paths, can be imported as it is"
            )
        typer.echo(f"None of the {count(len(listed), 'photo')} in {list_name} has an original that was found, so there is nothing to import: run `apple photos find` again with other places to search.", err=True)
        return
    for entry in listed:
        key = (entry["date"], "0", entry["name"])
        if not in_range(entry["date"], since, until) or not pp.of_media(Counter({key: 1}), media.value):
            continue
        found = None
        if entry["files"]:
            files = [translate(f, mapping) for f in entry["files"]]
            try:
                reachable = all(Path(f).exists() for f in files)
                size = Path(files[0]).stat().st_size if reachable else 0
            except PermissionError as error:
                denied.append(str(error.filename or files[0]))
                reachable = None
            if reachable:
                found = Found(entry["source"] or "list", files, size, entry["root"], entry["library"])
            elif reachable is not None:
                where = entry["root"] or str(Path(entry["files"][0]).parent)
                unreachable[where] = unreachable.get(where, 0) + 1
        if found:
            hits.append(found)
        rows.append(found_row(key, entry, found))
    print_recover(rows, format)
    if unreachable:
        roots = ", ".join(f"{root} ({count(n, 'photo')})" for root, n in sorted(unreachable.items()))
        typer.echo(f"The list says where {sum(unreachable.values())} originals are, but they cannot be reached from here: {roots}.", err=True)
        typer.echo("Translate the start of those paths to where they are here, for example --map " + sorted(unreachable)[0] + "=<path here>; the first --map in the order given that matches a path is used.", err=True)
    if denied:
        typer.echo(f"{len(denied)} file{'' if len(denied) == 1 else 's'} cannot be read (permission denied), so nothing was imported for them:\n" + "\n".join(f"  {path}" for path in denied), err=True)
    imported = already = 0
    errors = len(denied)
    if hits and mode != Apply.no:
        require_macos()
        check_open_in_photos(path)
        go = True
        if mode == Apply.ask:
            if not has_terminal():
                typer.echo("Nothing was imported: --apply ask needs a terminal.", err=True)
                go = False
            else:
                go = typer.confirm(f"Import {len(hits)} photos into the library Photos has open (it should be {target})?", default=False)
        if go:
            progress.line(f"Importing {len(hits)} photos into {target}; Photos shows no progress, so this can take a while ...")
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
