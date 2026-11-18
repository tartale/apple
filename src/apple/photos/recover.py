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
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from apple.photos import folder_photos, matching
from apple.photos import powerphotos as pp
from apple.photos.common import is_dry_run
from apple.photos.progress import Progress, duration
from apple.photos.recover_cache import Cache
from apple.photos.library_commands import DateFormat, DiffFormat, DiffModeOption, ExplainOption, IgnoreTrashOption, Media, cell_text, count, fail, find_library, folder_listing, ids_for, known_libraries, need, parse_mode, plan_write, row_key, within, NameOption, PreferOption, trash_ids
from apple.photos import prefer as prefer_module
from apple.photos.prefer import Prefer
from apple.photos.recover_sources import DEFAULT, SNAPSHOTS, Found, build_sources, has_wildcard, import_files, parse_search
from apple.platform import require_macos

SEARCH_VARIABLE = "APPLE_PHOTOS_RECOVER_SEARCH"
MAP_VARIABLE = "APPLE_PHOTOS_RECOVER_MAPPINGS"


OPEN_WAIT_SECONDS = 60  # how long Photos is given to open a library before an import gives up


def open_in_photos(target: Path) -> bool:
    """Has Photos open target, which is what an import goes into (Photos imports into whichever library it has open): asks
    Photos to open it when it does not, waits up to OPEN_WAIT_SECONDS for that, and stops if it still is not. True when it
    had to ask."""
    def is_open() -> bool:
        return any(library_open.resolve() == target.resolve() for library_open in pp.libraries_open_in_photos())

    if is_open():
        return False
    typer.echo(f"Opening {target} in Photos ...", err=True)
    pp.open_in_photos(target)
    deadline = time.monotonic() + OPEN_WAIT_SECONDS
    while not is_open():
        if time.monotonic() >= deadline:
            opened = pp.libraries_open_in_photos()
            shown = ", ".join(str(path) for path in opened) or "none, Photos is not running"
            fail(f"Photos did not open {target} (open: {shown}). Open it in Photos yourself (hold Option while starting Photos to choose a library, or double-click it), then run this again.")
        time.sleep(1)
    return True


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
            raise typer.BadParameter("the list needs a name and a date column")
        files = record.get("files") or []
        if isinstance(files, str):
            files = [entry for entry in files.split("|") if entry]
        items.append({
            "name": str(record["name"]), "date": str(record["date"]), "hidden": is_true(record.get("hidden")),
            "shared": is_true(record.get("shared")), "identifier": str(record.get("identifier") or ""), "source": str(record.get("source") or ""),
            "ids": {kind: {str(record[kind])} for kind in ("stable", "cloud") if record.get(kind)},
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
    library: Annotated[
        Optional[str],
        typer.Option(
            "--library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The library to list: a name from `./apple photos libraries`, or the path of a .photoslibrary. Also read from "
            "APPLE_PHOTOS_DEFAULT_LIBRARY.",
        ),
    ] = None,
    name: NameOption = None,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo, with the Messages identifier of a shared photo.")] = DiffFormat.text,
    diff_mode: DiffModeOption = "",
) -> None:
    """List the photos of a library whose original it does not store. Changes nothing.

    PowerPhotos cannot list or copy these (the `missing` column of `apple photos diff`): a photo shared with you in
    Messages and never saved, or a hidden item whose file is gone. Save the list (--format csv) and give it to `apple
    photos find` to search for the originals, on this machine or another.

    A photo that is the same photo as another in the list, under --diff-mode (by default the same file name and extension,
    a date within two hours and a size within 1%), is listed once, and how many were left out is said.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if library is None:
        raise typer.BadParameter("--library is needed (or APPLE_PHOTOS_DEFAULT_LIBRARY)")
    match = parse_mode(diff_mode)
    path = Path(library) if Path(library).is_dir() else find_library(library)
    progress = Progress()
    progress.line(f"Reading the photos of {library} ...")
    try:
        photos = pp.library_photos(path)
    except pp.PowerPhotosError as error:
        fail(str(error))
    wanted = {}
    for counted in (photos.visible, photos.hidden):
        for key in pp.of_media(within(counted, since, until, name), media.value):
            wanted[key] = key in photos.hidden
    lacking = [key for key in sorted(wanted) if photos.original_missing(key)]
    # of photos that are the same photo, the first is kept, and one with a Messages identifier comes first
    kept = matching.dedupe(sorted(lacking, key=lambda key: (not photos.identifiers.get(key), key)), match, photos.ids)
    rows = [
        {"date": key[0], "name": key[2], "hidden": wanted[key], "shared": key in photos.shared, "identifier": photos.identifiers.get(key, ""),
         "stable": "|".join(sorted(photos.ids.get(key, {}).get("stable", ()))), "cloud": "|".join(sorted(photos.ids.get(key, {}).get("cloud", ())))}
        for key in sorted(kept)
    ]
    columns = ["date", "name", "hidden", "shared", "identifier", "stable", "cloud"]
    if format == DiffFormat.json:
        typer.echo(json.dumps(rows, indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns])
    elif rows:
        shown = columns[:-3]  # the identifier and the ids are for `find`
        cells = [shown] + [[cell_text(row[column]) for column in shown] for row in rows]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(shown))]
        for cell in cells:
            typer.echo("  ".join(text.ljust(width) for text, width in zip(cell, widths)).rstrip())
    typer.echo(f"{count(len(rows), 'missing file')}" + (f" ({len(lacking) - len(rows)} left out as the same photo as another, see --diff-mode)" if len(lacking) > len(rows) else ""), err=True)


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
    library: Annotated[
        Optional[str],
        typer.Option(
            "--library", envvar="APPLE_PHOTOS_DEFAULT_LIBRARY",
            help="The library the list is of, which is never searched even when a folder holds it: the path of a .photoslibrary, or a "
            "name from `./apple photos libraries` (a name is not looked up, so a path is needed to keep it out of a search). "
            "Also read from APPLE_PHOTOS_DEFAULT_LIBRARY. Needed only where the library is mounted.",
        ),
    ] = None,
    messages: Annotated[bool, typer.Option("--messages", help="Search Messages' attachments (~/Library/Messages/Attachments; Terminal needs Full Disk Access): the files of photos shared with you, found by the ID in the library.")] = False,
    messages_folder: Annotated[Optional[List[Path]], typer.Option(help="Search another folder of Messages attachments, as --messages; repeat for several.")] = None,
    folders: Annotated[
        Optional[List[Path]],
        typer.Option(help="Search everything under a path (it may have * ? or [..] in it, to name several folders, such as a Pictures folder in every Time Machine snapshot), however the originals are stored: raw files (the iCloud backup, an iPhoto or Aperture library's Masters, a Time Machine backup) are matched by file name and capture time, and the Photos libraries found on the way are read through their database. The library --library names is never searched. Repeat for several."),
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
    diff_mode: DiffModeOption = "",
    name: NameOption = None,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo.")] = DiffFormat.text,
) -> None:
    """Search for the originals of the photos in a list. Changes nothing.

    Writes each photo of LIST (`apple photos missing --format csv`) with where an original was found, or `none`. Save
    the output (--format csv) and give it to `apple photos import`, on this machine or another, to import the files found.

    A file or a library's photo matches a listed photo as --diff-mode says (by default the same file name and a time within two hours of its
    capture time), or by an iCloud fingerprint they share: the list's stable column, which `missing` writes, and the 28 characters a backup
    file's name ends in. Only the name, extension, date and identity parts apply: a list has no file to read.

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
    path = Path(library) if library and Path(library).is_dir() else Path("/nonexistent/default.photoslibrary")
    if snapshots not in SNAPSHOTS:
        raise typer.BadParameter(f"--snapshots must be one of {', '.join(SNAPSHOTS)}")
    # The translations that make the places reachable on this machine, so that one set of settings
    # (APPLE_PHOTOS_RECOVER_SEARCH and APPLE_PHOTOS_RECOVER_MAPPINGS) fits every machine.
    mapping = parse_map(map_paths)
    entries = [(kind, [translate(place, mapping) for place in places] if kind == "folders" else places) for kind, places in entries]
    sources = build_sources(entries, path, snapshots, Cache(read=not refresh), parse_mode(diff_mode))
    for source in sources:
        reason = source.problem()
        if reason:
            fail(f"Cannot search {source.name}: {reason}.")

    progress = Progress()
    items = []
    for entry in load_list(list_file):
        key = (entry["date"], "0", entry["name"])
        if not in_range(entry["date"], since, until) or not pp.of_media(Counter({key: 1}), media.value) or not pp.named(name, entry["name"]):
            continue
        items.append({"key": key, "hidden": entry["hidden"], "shared": entry["shared"], "identifier": entry["identifier"], "ids": entry["ids"]})

    rows, hits = [], []
    looked, started = 0, time.monotonic()
    for source in sources:
        source.prepare(progress)
    for item in items:
        looked += 1
        progress.update(f"Looking up originals: {looked}/{len(items)} ({len(hits)} found)")
        key = item["key"]
        found = next((hit for hit in (source.find(key, item["identifier"], item["ids"]) for source in sources) if hit), None)
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
    source: Annotated[
        str,
        typer.Argument(
            help="What to import: a folder of photo and video files (a file is its modification time as its capture time in UTC, its "
            "size and its name; --since, --until and --media limit them), or the csv or json (- for stdin) that `apple photos find "
            "--format csv` wrote, whose rows each say where their original is (the source, root, library and files columns; nothing "
            "is searched, and a row with no file is skipped).",
        ),
    ],
    destination: Annotated[
        str,
        typer.Argument(
            help="The library to import into: a name from `./apple photos libraries`, or the path of a .photoslibrary. Photos imports "
            "into whichever library it has open, so it is asked to open this one first.",
        ),
    ],
    all_files: Annotated[
        bool,
        typer.Option(
            "--all",
            help="With a folder, import every file, without working out which ones the destination lacks. Not with --explain, "
            "--ignore-trash or --prefer.",
        ),
    ] = False,
    diff_mode: DiffModeOption = "",
    explain: ExplainOption = False,
    ignore_trash: IgnoreTrashOption = False,
    prefer: PreferOption = Prefer.none,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="With a folder, read it again instead of reusing what an earlier run saved (as `diff` and `find` do), and save the result."),
    ] = False,
    map_paths: Annotated[
        Optional[List[str]],
        typer.Option("--map", help="OLD=NEW, a translation of the start of the paths the list gives (it has them as the machine that searched saw them), such as /volume1=/Volumes. Repeat for several; for each path the first one, in the order given, whose OLD starts it is used, so put specific ones before general ones. A path that exists as written is left alone. Without --map, APPLE_PHOTOS_RECOVER_MAPPINGS, which holds OLD=NEW entries separated by spaces and read as a shell reads words, so one value can be shared by the machines."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Import nothing; report what would be imported. Same as DRY_RUN=true.")] = False,
    name: NameOption = None,
    since: Annotated[Optional[datetime], typer.Option(help="Only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Photos, videos or both.")] = Media.all,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo.")] = DiffFormat.text,
) -> None:
    """Import photo and video files into a library through Photos' own import.

    The files are a folder's, or those a list says where to find (what `apple photos find` wrote, with its paths translated
    with --map or APPLE_PHOTOS_RECOVER_MAPPINGS to where they are on this machine). Photos is asked to open the
    DESTINATION, which it must have open to import into it, and is quit again afterwards when it had to be opened. The
    import makes new, ordinary photos; a placeholder stays, but `diff` no longer counts it once the copy is there. Nothing
    is deleted, and nothing is changed with --dry-run (or DRY_RUN=true).

    With a folder, only the files the DESTINATION lacks are imported, judged from its database as `copy` judges a library (see
    --diff-mode: by default a photo of the same file name, case ignored, within two hours and 1% of the size is there);
    --all imports every file. A list is imported as it says.
    """
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if source != "-" and not Path(source).expanduser().exists():
        raise typer.BadParameter(f"{source} does not exist: give a folder of photos and videos, or a list (the csv or json of `apple photos find`, or - for stdin)")
    folder = folder_photos.is_folder(source)
    if not folder and (all_files or refresh or explain or ignore_trash):
        raise typer.BadParameter("--all, --refresh, --explain and --ignore-trash are for a folder: a list says what to import")
    only_missing = folder and not all_files
    if (explain or ignore_trash or prefer != Prefer.none) and folder and all_files:
        raise typer.BadParameter("--explain, --ignore-trash and --prefer are about the files the destination lacks, which --all does not work out")
    match = parse_mode(diff_mode)
    dry = is_dry_run(dry_run)
    if Path(destination).expanduser().is_dir():
        path = Path(destination).expanduser()
    elif not dry or only_missing or prefer != Prefer.none:
        path = find_library(destination)  # a dry run of a list needs no library, so it can run where there is none
    else:
        path = Path("/nonexistent/default.photoslibrary")
    if folder:
        listed = folder_listing(source, path if only_missing else None, since, until, media, match, refresh, explain, destination, ignore_trash, name, prefer)
        import_listed(listed, source, destination, path, dry, [], since, until, media, format, name)
        return
    mapping = parse_map(map_paths)
    listed = load_list(source)
    if prefer != Prefer.none:
        listed = prefer_for_list(listed, mapping, path, prefer, match, since, until, media, name)
    import_listed(listed, source, destination, path, dry, mapping, since, until, media, format, name)


def prefer_for_list(listed: List[dict], mapping: List[tuple], library: Path, prefer: Prefer, match: matching.Mode,
                    since: Optional[datetime], until: Optional[datetime], media: Media, name_filter: Optional[List[str]]) -> List[dict]:
    """The rows of a list once a preference is applied, as it is for a folder: a row whose file is the same photo as one the library
    holds (under --diff-mode) is left out unless its file is preferred to every such photo, and then it replaces them; the other rows
    are as the list gave them. A row is compared by its file, translated with the mapping, whose size is the photo's size."""
    photos, files, ids, key_of = Counter(), {}, {}, {}
    for number, entry in enumerate(listed):
        if not in_range(entry["date"], since, until) or not pp.of_media(Counter({(entry["date"], "0", entry["name"]): 1}), media.value) or not pp.named(name_filter, entry["name"]):
            continue
        translated = [translate(f, mapping) for f in entry["files"]]
        try:
            size = Path(translated[0]).stat().st_size if translated and all(Path(f).exists() for f in translated) else None
        except OSError:
            size = None
        if size is None:
            continue
        key = (entry["date"], str(size), entry["name"])
        key_of[number] = key
        photos[key] = 1
        files.setdefault(key, translated[:1])
        if entry["ids"]:
            ids.setdefault(key, {}).update(entry["ids"])
    try:
        held = pp.library_photos(library)
    except pp.PowerPhotosError as error:
        fail(str(error))
    source = pp.Photos(Path("/"), photos, Counter(), Counter(), files, set(), Counter(), {}, {}, Counter(), ids)
    selected = [photos, held.visible + held.hidden]
    wanted, replaces = plan_write([source, held], selected, ids_for(match, [source, held], selected), since, until, media, match, False, False, str(library), prefer)
    rows, replacing = [], set()
    for number, entry in enumerate(listed):
        key = key_of.get(number)
        if key is None:
            rows.append(entry)  # not compared: as the list gave it
        elif key in wanted and row_key(key) in replaces and key not in replacing:
            replacing.add(key)  # one row for a photo the list gives twice
            rows.append({**entry, "replaces": replaces[row_key(key)]})
        elif key in wanted and row_key(key) not in replaces:
            rows.append(entry)  # nothing like it in the library
    return rows


def import_listed(listed: List[dict], list_name: str, target: Optional[str], path: Path, dry: bool, mapping: List[tuple],
                  since: Optional[datetime], until: Optional[datetime], media: Media, format: DiffFormat, name_filter: Optional[List[str]] = None) -> None:
    """Import the originals the listed rows (from read_list) say where to find, as import does, into the library target (at
    path) unless dry: a list's rows and a folder's files are imported the same way. list_name is what the rows came from."""
    progress = Progress()
    rows, hits, unreachable, denied, forced = [], [], {}, [], []
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
        if not in_range(entry["date"], since, until) or not pp.of_media(Counter({key: 1}), media.value) or not pp.named(name_filter, entry["name"]):
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
            if entry.get("replaces"):
                forced.append((found, entry["replaces"]))
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
    if forced:
        typer.echo(f"{'Would write' if dry else 'Writing'} {count(len(forced), 'preferred file')} beside the photos they are preferred to, and {'would move' if dry else 'moving'} those to Recently Deleted:", err=True)
        for found, replaces in forced:
            typer.echo(f"  {Path(found.files[0]).name}  replaces {replaces['name']}  {replaces['date']}  ({replaces['why']})", err=True)
        if not dry:
            need(known_libraries(), target)  # before anything is imported: PowerPhotos moves them to the trash
    if hits and not dry:
        require_macos()
        opened_here = open_in_photos(path)
        try:
            progress.line(f"Importing {len(hits)} photos into {target}; Photos shows no progress, so this can take a while ...")
            try:
                with pp.one_at_a_time():
                    try:
                        plain = [found for found in hits if all(found is not other for other, _ in forced)]
                        done = import_files(plain) if plain else 0
                        imported += done
                        already += max(len(plain) - done, 0)  # Photos had these: they were not missing after all
                        if forced:
                            wrote = import_files([found for found, _ in forced], force=True)
                            imported += wrote
                            if wrote >= len(forced):
                                moved, gone = trash_ids(known_libraries(), target, [identifier for _, replaces in forced for identifier in replaces["ids"]])
                                typer.echo(f"Moved {count(moved, 'photo')} to Recently Deleted" + (f"; {len(gone)} not found in {target}" if gone else ""), err=True)
                            else:
                                errors += len(forced) - wrote
                                typer.echo(f"Photos imported {wrote} of {len(forced)} preferred files, so nothing was moved to Recently Deleted", err=True)
                    except pp.PowerPhotosError as error:
                        errors += len(hits)
                        typer.echo(f"Photos failed to import: {error}", err=True)
            except pp.Busy as error:
                fail(str(error))
        finally:
            if opened_here:  # Photos was not showing this library until this command asked it to, so it is put away again
                progress.line("Closing Photos ...")
                pp.quit_photos()
    missing = len(rows) - already
    skipped = missing - imported - errors  # no original was found, or the answer was no
    typer.echo(
        f"Imported {imported} of {missing} missing files; {count(errors, 'error')}, {skipped} skipped"
        + (" [dry run]" if dry and hits else ""),
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
