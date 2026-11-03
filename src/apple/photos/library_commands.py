"""`./apple photos libraries|diff|copy|split`: compare and move photos between Photos libraries with PowerPhotos (macOS only)."""
import csv
import json
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer

from apple.platform import require_macos
from apple.photos import powerphotos as pp


class Media(str, Enum):
    all = "all"
    photos = "photos"
    videos = "videos"


class SplitMedia(str, Enum):
    photos = "photos"
    videos = "videos"


class OnUpgrade(str, Enum):
    skip = "skip"
    ask = "ask"
    stop = "stop"


SourceOption = Annotated[str, typer.Option("--from", help="Source library, as named by `./apple photos libraries`.")]
NoDuplicateCheckOption = Annotated[
    bool,
    typer.Option(
        "--no-duplicate-check",
        help="Copy even photos the destination already has, instead of letting PowerPhotos compare against it first "
        "(faster into a large library). Needs a list of what to copy, so it does not duplicate what is there.",
    ),
]
OnUpgradeOption = Annotated[
    OnUpgrade,
    typer.Option(
        help="When the destination was made by an older Photos and must be upgraded first: "
        "skip it and go on, ask in the terminal (skips when there is no terminal), or stop with an error."
    ),
]
DateFormat = ["%Y-%m-%d"]


def fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def known_libraries() -> dict:
    try:
        return pp.libraries()
    except pp.PowerPhotosError as error:
        fail(f"Could not list the PowerPhotos libraries: {error}")


def need(libraries: dict, name: str) -> Path:
    if name not in libraries:
        fail(f"No library named {name} in PowerPhotos. Libraries: {', '.join(sorted(libraries))}")
    return libraries[name]


def find_library(name_or_path: str) -> Path:
    """A library given as the path of a .photoslibrary, or by its name in PowerPhotos (macOS only)."""
    if Path(name_or_path).is_dir():
        return Path(name_or_path)
    require_macos()
    return need(known_libraries(), name_or_path)


def resolve_upgrade(name: str, path: Path, source_version: Optional[int], mode: OnUpgrade, reason: str) -> bool:
    """Decides what to do with a destination that needs a Photos upgrade. True: carry on; False: skip it."""
    if mode == OnUpgrade.stop:
        fail(f"{name} needs a Photos upgrade before it accepts photos. {reason}")
    if mode == OnUpgrade.skip or not sys.stdin.isatty():
        return False
    typer.echo(f"{name} needs a Photos upgrade before it accepts photos. {reason}", err=True)
    while True:
        answer = typer.prompt("[s]kip, [q]uit, or [o]pen it in Photos to upgrade it", default="s").strip().lower()[:1]
        if answer == "q":
            fail("Stopped.")
        if answer != "o":
            return False
        pp.open_in_photos(path)
        typer.prompt(f"Upgrade {name} in Photos, quit Photos, then press Enter to retry", default="", show_default=False)
        upgraded = pp.model_version(path)
        if source_version is None or upgraded is None or upgraded >= source_version:
            return True
        typer.echo(f"{name} still needs the upgrade.", err=True)


def parse_names(text: str) -> list:
    """File names from the output of `./apple photos diff` (json or csv, with a name column) or one name per line."""
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            return [str(record["name"]) for record in json.loads(stripped)]
        except (ValueError, KeyError, TypeError):
            raise typer.BadParameter("the list is not json from `apple photos diff --format json`")
    lines = stripped.splitlines()
    header = next(csv.reader([lines[0]])) if lines else []
    if "name" in header:
        return [record["name"] for record in csv.DictReader(lines)]
    if header and set(header) <= {"side", "date", "size"}:
        raise typer.BadParameter("the list has no name column; run diff with --fields including name")
    return [line.strip() for line in lines if line.strip()]


def copy_one(
    libraries: dict, source: str, destination: str, since: Optional[date], before: Optional[date],
    media: str, mode: OnUpgrade, names: Optional[list] = None, check_duplicates: bool = True,
) -> Optional[int]:
    """Copies one date range, or the photos with the given file names; returns how many photos matched, or None when the
    destination was skipped."""
    source_path = need(libraries, source)
    destination_path = need(libraries, destination)
    source_version = pp.model_version(source_path)
    destination_version = pp.model_version(destination_path)
    if source_version is not None and destination_version is not None and destination_version < source_version:
        reason = f"It is in an older format than {source}."
        if not resolve_upgrade(destination, destination_path, source_version, mode, reason):
            return None
    pp.SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pp.SCRATCH_ROOT) as scratch:
        if names is None:
            script = "copy-photos.applescript"
            args = [source, destination, since.isoformat() if since else "-", before.isoformat() if before else "-", media]
        else:
            script = "copy-named.applescript"
            listing = Path(scratch) / "names.txt"
            listing.write_text("\n".join(names) + "\n", encoding="utf-8")
            args = [source, destination, str(listing), "true" if check_duplicates else "false"]
        while True:
            try:
                output = pp.run_applescript(script, *args)
                for line in output.splitlines():
                    if line.startswith("missing\t"):
                        typer.echo(f"Not in {source}: {line.partition(chr(9))[2]}", err=True)
                return int(output.rsplit(" ", 1)[-1])
            except pp.PowerPhotosError as error:
                if error.number != pp.UPGRADE_NEEDED:
                    fail(f"PowerPhotos failed: {error}")
                if not resolve_upgrade(destination, destination_path, source_version, mode, str(error)):
                    return None


def libraries_command(
    outdated: Annotated[
        bool,
        typer.Option("--outdated", help="List only the libraries older than the reference, which Photos must upgrade before they accept photos."),
    ] = False,
    against: Annotated[
        Optional[str],
        typer.Option(help="With --outdated: the library whose version is the reference. The default is the newest version among the libraries."),
    ] = None,
) -> None:
    """[macOS only] List the libraries in PowerPhotos with where each is on disk and its Photos database version.

    A library with a lower version than the one you copy from must be upgraded by Photos before it accepts photos.
    The listing reads each database as it is on disk, which is instant but can miss a version Photos wrote moments ago;
    --outdated reads the write-ahead log too, which is exact but copies each database, slow over a network share.
    With --outdated, only those are listed (one per line on stdout, so it can be used in a script); libraries whose version
    cannot be read, such as iPhoto or Aperture libraries, are named on stderr.
    """
    require_macos()
    if against and not outdated:
        raise typer.BadParameter("--against needs --outdated")
    try:
        entries = pp.listing()
    except pp.PowerPhotosError as error:
        fail(f"Could not list the PowerPhotos libraries: {error}")
    clash = pp.duplicate_names(entries)
    if clash and outdated:
        fail(f"{clash} Rename one in PowerPhotos.")
    with ThreadPoolExecutor(max_workers=8) as pool:  # an exact read can mean copying a database over the network; overlap them
        read = list(pool.map(lambda entry: pp.model_version(entry[1], exact=outdated), entries))
    if not outdated:
        for (name, path), version in zip(entries, read):
            typer.echo(f"{name}\t{path}\t{version if version is not None else 'unknown'}")
        if clash:
            typer.echo(clash, err=True)
        return
    libraries = dict(entries)
    versions = {name: version for (name, _), version in zip(entries, read)}

    if against:
        need(libraries, against)
        reference = versions[against]
        if reference is None:
            fail(f"The version of {against} cannot be read, so it cannot be the reference.")
        described = against
    else:
        readable = [version for version in versions.values() if version is not None]
        if not readable:
            fail("No library's version can be read.")
        reference = max(readable)
        described = "the newest library"
    older = [name for name, version in versions.items() if version is not None and version < reference]
    for name in older:
        typer.echo(f"{name}\t{libraries[name]}\t{versions[name]}")
    unreadable = [name for name, version in versions.items() if version is None]
    typer.echo(f"Reference: {reference} ({described}). {len(older)} older.", err=True)
    if unreadable:
        typer.echo(f"Version cannot be read: {', '.join(unreadable)}", err=True)


DIFF_FIELDS = ("date", "size", "name")


class Side(str, Enum):
    left = "left"
    right = "right"


class DiffFormat(str, Enum):
    text = "text"
    csv = "csv"
    json = "json"


class DiffSort(str, Enum):
    date = "date"
    name = "name"
    size = "size"


def parse_fields(text: str) -> list:
    fields = [field.strip() for field in text.split(",") if field.strip()]
    if not fields or any(field not in DIFF_FIELDS for field in fields) or len(set(fields)) != len(fields):
        raise typer.BadParameter(f"use each of {', '.join(DIFF_FIELDS)} at most once, comma-separated")
    return fields


def diff_rows(only_left: Counter, only_right: Counter, which: Optional[Side], sort: DiffSort) -> list:
    """The photos to show as dicts of side, date, size (int or None) and name, in the order asked for."""
    rows = []
    for side, counted in ((Side.left, only_left), (Side.right, only_right)):
        if which in (None, side):
            for when, size, name in counted.elements():
                rows.append({"side": side.value, "date": when, "size": int(size) if size.isdigit() else None, "name": name})
    order = {
        DiffSort.date: lambda r: (r["date"], r["name"], r["size"] or 0, r["side"]),
        DiffSort.name: lambda r: (r["name"], r["date"], r["size"] or 0, r["side"]),
        DiffSort.size: lambda r: (r["size"] or 0, r["date"], r["name"], r["side"]),
    }[sort]
    return sorted(rows, key=order)


def print_diff(rows: list, fields: list, show_side: bool, format: DiffFormat) -> None:
    columns = (["side"] if show_side else []) + fields
    if format == DiffFormat.json:
        typer.echo(json.dumps([{column: row[column] for column in columns} for row in rows], indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[column] is None else row[column] for column in columns])
    else:
        cells = [
            [{"left": "<", "right": ">"}[row["side"]] if column == "side" else "" if row[column] is None else str(row[column]) for column in columns]
            for row in rows
        ]
        widths = [max(len(cell[i]) for cell in cells) for i in range(len(columns))] if cells else []
        for cell in cells:
            typer.echo("  ".join(
                (text.rjust(width) if columns[i] == "size" else text.ljust(width)) for i, (text, width) in enumerate(zip(cell, widths))
            ).rstrip())


def within(photos: Counter, since: Optional[datetime], until: Optional[datetime]) -> Counter:
    """The photos dated from since to until, both days included (either may be None for no limit)."""
    first = since.date().isoformat() if since else ""
    last = until.date().isoformat() if until else "9999-99-99"
    return Counter({key: count for key, count in photos.items() if first <= key[0][:10] <= last})


def diff_command(
    left: Annotated[str, typer.Argument(help="First library: a name from `./apple photos libraries`, or the path of a .photoslibrary.")],
    right: Annotated[str, typer.Argument(help="Second library, named the same way.")],
    summary: Annotated[bool, typer.Option("--summary", help="Print only the counts.")] = False,
    only: Annotated[Optional[Side], typer.Option(help="Show only the photos that are in just the left, or just the right, library; the side column is left out.")] = None,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo, with a side column (left or right) unless --only is given.")] = DiffFormat.text,
    fields: Annotated[str, typer.Option(help="The columns to show and their order: any of date, size, name, comma-separated.")] = ",".join(DIFF_FIELDS),
    sort: Annotated[DiffSort, typer.Option(help="Order of the photos.")] = DiffSort.date,
    since: Annotated[Optional[datetime], typer.Option(help="Compare only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Compare only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
) -> None:
    """Show which photos and videos are in one library and not the other.

    Reads the libraries' databases directly, so it is fast, and needs PowerPhotos only to look up names (a path to a
    .photoslibrary works anywhere). A photo is its date, size and file name; photos in the trash are ignored. --since and
    --until limit both libraries to a range of dates, in UTC as the databases keep them, so the counts are for that range
    too. Text output
    marks each photo `<` when it is only in LEFT and `>` when it is only in RIGHT, in aligned columns. The counts go to
    stderr. Exits 1 when the libraries differ, 0 when they match. Changes nothing.
    """
    columns = parse_fields(fields)
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    try:
        left_path, right_path = find_library(left), find_library(right)
        first, second = pp.photos(left_path), pp.photos(right_path)
    except pp.PowerPhotosError as error:
        fail(str(error))
    first, second = (within(photos, since, until) for photos in (first, second))
    only_left, only_right = first - second, second - first
    if not summary:
        print_diff(diff_rows(only_left, only_right, only, sort), columns, only is None, format)
    typer.echo(
        f"Only in {left}: {sum(only_left.values())}. Only in {right}: {sum(only_right.values())}. "
        f"In both: {sum((first & second).values())}.",
        err=True,
    )
    raise typer.Exit(1 if only_left or only_right else 0)


def copy_command(
    source: SourceOption,
    destination: Annotated[str, typer.Option("--to", help="Destination library; it must already exist in PowerPhotos.")],
    since: Annotated[Optional[datetime], typer.Option(help="First date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Last date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Copy photos, videos or both.")] = Media.all,
    list_file: Annotated[
        Optional[str],
        typer.Option("--list", help="Copy only the photos with the file names in this file (- for stdin): the output of `apple photos diff --format csv|json`, or one name per line. Not with --since, --until or --media."),
    ] = None,
    no_duplicate_check: NoDuplicateCheckOption = False,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
) -> None:
    """[macOS only] Copy the photos and videos in a date range, or the ones in a list, from one library to another with PowerPhotos.

    Photos already in the destination are skipped, so reruns add only what is new. Nothing is removed from the source.
    With --list, every photo in the source with a listed file name is copied, so a name that more than one photo has
    copies all of them. Names the source does not have are listed on stderr. An empty list copies nothing and succeeds.
    --no-duplicate-check copies the listed photos even when the destination has them, which avoids PowerPhotos'
    comparison against a large library; use it only with a list of what is missing, such as from `apple photos diff`.
    """
    require_macos()
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if no_duplicate_check and list_file is None:
        raise typer.BadParameter("--no-duplicate-check needs --list: without a list it would copy the whole range again")
    names = None
    if list_file is not None:
        if since or until or media != Media.all:
            raise typer.BadParameter("--list cannot be combined with --since, --until or --media")
        names = parse_names(sys.stdin.read() if list_file == "-" else Path(list_file).read_text(encoding="utf-8"))
        if not names:  # e.g. a diff that found nothing: there is nothing to copy, which is not an error
            typer.echo(f"{destination}: no files copied (the list is empty)")
            return
    try:
        with pp.one_at_a_time():
            found = copy_one(
                known_libraries(), source, destination,
                since.date() if since else None,
                until.date() + timedelta(days=1) if until else None,
                media.value, on_upgrade, names, not no_duplicate_check,
            )
    except pp.Busy as error:
        fail(str(error))
    typer.echo(f"{destination}: skipped (needs a Photos upgrade)" if found is None else f"{destination}: found {found}")


def read_photos(libraries: dict, name: str) -> Optional[Counter]:
    """The photos of a library in PowerPhotos, read from its database, or None when it is unknown or cannot be read."""
    if name not in libraries:
        return None
    try:
        return pp.photos(libraries[name])
    except pp.PowerPhotosError:
        return None


def missing_in_year(source_photos: Counter, year: int, media: str, read) -> Optional[tuple]:
    """What the library for a year lacks of that year's photos in the source, judged from the databases.

    Returns (missing, uncertain), or None when that year's library is missing or cannot be read. The databases keep UTC
    dates and PowerPhotos files by local date, so a photo within a day of New Year may belong to the year before or
    after: missing holds the photos of the year proper that its library lacks, and uncertain the ones near New Year that
    are in none of this year's library and its neighbours'. read(year) gives that year's library as pp.photos does, or
    None.
    """
    destination = read(year)
    if destination is None:
        return None
    first_day, last_day = f"{year}-01-02", f"{year}-12-31"
    core, edge = Counter(), Counter()
    for (when, size, name), count in source_photos.items():
        if not name.lower().endswith(pp.EXTENSIONS[media]):
            continue
        if first_day <= when < last_day:
            core[(when, size, name)] = count
        elif f"{year - 1}-12-31" <= when < f"{year + 1}-01-02":
            edge[(when, size, name)] = count
    nearby = destination + (read(year - 1) or Counter()) + (read(year + 1) or Counter())
    return core - destination, edge - nearby


def complete_year(source_photos: Counter, year: int, media: str, read) -> bool:
    """True when the library for a year holds every photo of that year in the source (see missing_in_year)."""
    found = missing_in_year(source_photos, year, media, read)
    return found is not None and not found[0] and not found[1]


def split_command(
    source: SourceOption,
    media: Annotated[SplitMedia, typer.Option(help="Split photos or videos.")],
    years: Annotated[str, typer.Option(help="One year or a range, such as 2018 or 2018-2020.")],
    destination_name: Annotated[
        str, typer.Option("--to", help="Destination library name for each year; {media} and {year} are filled in.")
    ] = "{media}-{year}",
    skip_complete: Annotated[
        bool,
        typer.Option(
            "--skip-complete/--no-skip-complete",
            help="Leave out a year whose library already holds all of that year's photos, judged from the databases "
            "(a few seconds, where each PowerPhotos copy into a large library makes it reindex).",
        ),
    ] = True,
    no_duplicate_check: NoDuplicateCheckOption = False,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
) -> None:
    """[macOS only] Copy a library into one library per year (photos-2018, photos-2019 ...) with PowerPhotos.

    The destination libraries must already exist in PowerPhotos. A year whose library already has every photo is
    reported as up to date and not copied again.

    --no-duplicate-check catches stragglers: it works out from the databases which photos of each year the library
    lacks and copies just those, matched by file name and without PowerPhotos' duplicate check. A missing photo whose
    file name another photo in the source shares copies that one too. A photo within a day of New Year, whose year the
    databases cannot settle, is copied into the library of every year in --years if no library for that year or the
    ones beside it has it, so it can end up in two year libraries.
    """
    require_macos()
    first, _, last = years.partition("-")
    if not (first.isdigit() and (last.isdigit() or not last)):
        raise typer.BadParameter("--years must be a year or a range such as 2018-2020")
    first_year, last_year = int(first), int(last or first)
    if last_year < first_year:
        raise typer.BadParameter("--years ends before it starts")
    if no_duplicate_check and not skip_complete:
        raise typer.BadParameter("--no-duplicate-check works out what is missing, so it cannot be used with --no-skip-complete")
    try:
        with pp.one_at_a_time():
            libraries = known_libraries()
            source_photos = read_photos(libraries, source) if skip_complete else None
            if no_duplicate_check and source_photos is None:
                fail(f"The photos of {source} cannot be read from its database, so what is missing cannot be worked out.")
            cache = {}

            def read_year(year: int):
                name = destination_name.format(media=media.value, year=year)
                if name not in cache:
                    cache[name] = read_photos(libraries, name)
                return cache[name]

            for year in range(first_year, last_year + 1):
                name = destination_name.format(media=media.value, year=year)
                if source_photos is not None and complete_year(source_photos, year, media.value, read_year):
                    typer.echo(f"{name}: up to date")
                    continue
                if no_duplicate_check:
                    found = missing_in_year(source_photos, year, media.value, read_year)
                    if found is None:
                        fail(f"The photos of {name} cannot be read from its database, so what is missing cannot be worked out.")
                    names = sorted({file_name for _, _, file_name in found[0] + found[1]})  # near New Year too: any year will do
                    if not names:
                        typer.echo(f"{name}: nothing to copy")
                        continue
                    found = copy_one(
                        libraries, source, name, None, None, media.value, on_upgrade, names, check_duplicates=False
                    )
                    typer.echo(f"{name}: skipped (needs a Photos upgrade)" if found is None else f"{name}: found {found}")
                    continue
                found = copy_one(
                    libraries, source, name, date(year, 1, 1), date(year + 1, 1, 1), media.value, on_upgrade
                )
                typer.echo(f"{name}: skipped (needs a Photos upgrade)" if found is None else f"{name}: found {found}")
    except pp.Busy as error:
        fail(str(error))
