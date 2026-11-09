"""`./apple photos libraries|diff|copy|split`: compare and move photos between Photos libraries with PowerPhotos (macOS only)."""
import csv
import json
import re
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, NamedTuple, Optional

import typer

from apple.platform import require_macos
from apple.photos import powerphotos as pp
from apple.photos import split_plan
from apple.photos.common import is_dry_run


class Media(str, Enum):
    all = "all"
    photos = "photos"
    videos = "videos"


class OnMissing(str, Enum):
    stop = "stop"
    skip = "skip"


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


def count(number: int, noun: str) -> str:
    """'1 photo', '3 photos'."""
    return f"{number} {noun}" + ("" if number == 1 else "s")


def is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def parse_list(text: str) -> tuple:
    """(file names to copy, hidden items) from the output of `./apple photos diff` (json or csv, with a name column and
    optionally true/false hidden, burst and missing columns) or one name per line. Returns the names to copy, the hidden
    items as a dict of name -> whether it is a burst member, and the items whose original is not stored in the library as a
    dict of name -> whether it came from Messages.
    PowerPhotos lists neither kind, so neither is copied."""
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            records = [{key: value for key, value in record.items()} | {"name": str(record["name"])} for record in json.loads(stripped)]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise typer.BadParameter("the list is not json from `apple photos diff --format json`")
    else:
        lines = stripped.splitlines()
        header = next(csv.reader([lines[0]])) if lines else []
        if "name" in header:
            records = list(csv.DictReader(lines))
        elif header and set(header) <= {"side", "date", "size", "hidden", "burst"}:
            raise typer.BadParameter("the list has no name column; run diff with --fields including name")
        else:
            records = [{"name": line.strip()} for line in lines if line.strip()]
    return split_records(records)


def split_records(records: list) -> tuple:
    """(names to copy, hidden items, items with no stored original) from records with a name and optionally true/false
    hidden, burst, missing and shared values, as parse_list describes."""
    names, hidden, missing = [], {}, {}
    for record in records:
        if is_true(record.get("hidden")):
            hidden[record["name"]] = is_true(record.get("burst"))
        elif is_true(record.get("missing")):
            missing[record["name"]] = is_true(record.get("shared"))
        else:
            names.append(record["name"])
    return names, hidden, missing


def report_missing(missing: dict, source: str) -> None:
    """Says on stderr which wanted photos were left out because their original is not stored in the source. missing is
    a dict of name -> whether the photo came from Messages (Shared with You)."""
    groups = (
        ([name for name, shared in missing.items() if shared],
         "shared with you in Messages and not saved to the library {source}, so there is no original to copy. Save them "
         "to the library in Photos, then run this again:"),
        ([name for name, shared in missing.items() if not shared],
         "the original is not stored in {source} (it may live only in iCloud), so PowerPhotos does not list it and "
         "cannot copy it:"),
    )
    for names, text in groups:
        if names:
            typer.echo(f"  {len(names)} not copied: " + text.format(source=source), err=True)
            for name in names[:20]:
                typer.echo(f"    {name}", err=True)
            if len(names) > 20:
                typer.echo(f"    ... and {len(names) - 20} more", err=True)


def report_hidden(hidden: dict) -> None:
    """Says on stderr which listed items were left out because Photos hides them (PowerPhotos cannot copy those)."""
    if len(hidden) <= 20:
        for name, burst in hidden.items():
            typer.echo(f"Not copied: {name}: hidden in Photos{' (a burst member)' if burst else ''}, so PowerPhotos does not list it", err=True)
    typer.echo(f"{count(len(hidden), 'hidden item')} ({count(sum(hidden.values()), 'burst member')}) {'was' if len(hidden) == 1 else 'were'} not copied: PowerPhotos cannot see them.", err=True)


class CopyResult(NamedTuple):
    """sent: how many source photos were handed to PowerPhotos to copy, or None when the destination was skipped;
    unlisted: the file names asked for that PowerPhotos lists no photo for in the source."""
    sent: Optional[int]
    unlisted: list


def describe_copy(result: CopyResult, check_duplicates: bool = True) -> str:
    """What happened, for the line that names the destination."""
    if result.sent is None:
        return "skipped (needs a Photos upgrade)"
    if check_duplicates:
        return f"asked PowerPhotos to copy {result.sent} (it skips any the library already has)"
    return f"copied {result.sent} (no duplicate check)"


def report_unlisted(unlisted: list, source: str) -> None:
    """Says on stderr which wanted photos PowerPhotos could not find in the source, and why that matters."""
    if not unlisted:
        return
    typer.echo(
        f"  {len(unlisted)} not copied: the database of {source} has them, but PowerPhotos lists no photo with "
        "these names, so it cannot copy them:", err=True,
    )
    for name in unlisted[:20]:
        typer.echo(f"    {name}", err=True)
    if len(unlisted) > 20:
        typer.echo(f"    ... and {len(unlisted) - 20} more", err=True)


def copy_one(
    libraries: dict, source: str, destination: str, since: Optional[date], before: Optional[date],
    media: str, mode: OnUpgrade, names: Optional[list] = None, check_duplicates: bool = True, ids: Optional[list] = None,
) -> CopyResult:
    """Copies one date range, or the photos with the given file names, or the photos with the given Photos identifiers."""
    source_path = need(libraries, source)
    destination_path = need(libraries, destination)
    source_version = pp.model_version(source_path)
    destination_version = pp.model_version(destination_path)
    if source_version is not None and destination_version is not None and destination_version < source_version:
        reason = f"It is in an older format than {source}."
        if not resolve_upgrade(destination, destination_path, source_version, mode, reason):
            return CopyResult(None, [])
    pp.SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pp.SCRATCH_ROOT) as scratch:
        if ids is not None:
            script = "copy-ids.applescript"
            listing = Path(scratch) / "ids.txt"
            listing.write_text("\n".join(ids) + "\n", encoding="utf-8")
            args = [source, destination, str(listing), "true" if check_duplicates else "false"]
        elif names is None:
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
                unlisted = [line.partition("\t")[2] for line in output.splitlines() if line.startswith("missing\t")]
                return CopyResult(int(output.rsplit(" ", 1)[-1]), unlisted)
            except pp.PowerPhotosError as error:
                if error.number != pp.UPGRADE_NEEDED:
                    fail(f"PowerPhotos failed: {error}")
                if not resolve_upgrade(destination, destination_path, source_version, mode, str(error)):
                    return CopyResult(None, [])


def trash_ids(libraries: dict, library: str, ids: list) -> tuple:
    """Moves the photos with these Photos identifiers to the library's Recently Deleted. Returns (how many were moved, the
    identifiers no photo of the library has: PowerPhotos does not list hidden items)."""
    need(libraries, library)
    pp.SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pp.SCRATCH_ROOT) as scratch:
        listing = Path(scratch) / "ids.txt"
        listing.write_text("\n".join(ids) + "\n", encoding="utf-8")
        try:
            output = pp.run_applescript("trash-ids.applescript", library, str(listing))
        except pp.PowerPhotosError as error:
            fail(f"PowerPhotos failed: {error}")
    return int(output.rsplit(" ", 1)[-1]), [line.partition("\t")[2] for line in output.splitlines() if line.startswith("missing\t")]


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


DIFF_FIELDS = ("date", "size", "name", "hidden", "burst", "missing", "shared")
DEFAULT_FIELDS = ["date", "size", "name"]


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


def diff_rows(only_left: Counter, only_right: Counter, which: Optional[Side], sort: DiffSort, hidden: dict, bursts: dict, libraries: dict, with_missing: bool) -> list:
    """The photos to show as dicts of side, date, size (int or None), name, hidden, burst and missing (true or false), in
    the order asked for. hidden and bursts give, for each side's value, the (date, size, name) keys of that side's hidden
    items and of its burst members; libraries gives its pp.Photos, which says whether an original is not stored there
    (found by looking at the files, so only when with_missing), and shared whether the item came from Messages."""
    rows = []
    for side, counted in ((Side.left, only_left), (Side.right, only_right)):
        if which in (None, side):
            for key in counted.elements():
                when, size, name = key
                rows.append({
                    "side": side.value, "date": when, "size": int(size) if size.isdigit() else None, "name": name,
                    "hidden": key in hidden[side.value], "burst": key in bursts[side.value],
                    "missing": libraries[side.value].original_missing(key) if with_missing else False,
                    "shared": key in libraries[side.value].shared,
                })
    order = {
        DiffSort.date: lambda r: (r["date"], r["name"], r["size"] or 0, r["side"]),
        DiffSort.name: lambda r: (r["name"], r["date"], r["size"] or 0, r["side"]),
        DiffSort.size: lambda r: (r["size"] or 0, r["date"], r["name"], r["side"]),
    }[sort]
    return sorted(rows, key=order)


def cell_text(value) -> str:
    """A value as text: true and false for booleans, nothing for a missing value."""
    return "" if value is None else ("true" if value else "false") if isinstance(value, bool) else str(value)


def print_diff(rows: list, fields: list, show_side: bool, format: DiffFormat) -> None:
    columns = (["side"] if show_side else []) + fields
    if format == DiffFormat.json:
        typer.echo(json.dumps([{column: row[column] for column in columns} for row in rows], indent=2))
    elif format == DiffFormat.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell_text(row[column]) for column in columns])
    else:
        cells = [
            [{"left": "<", "right": ">"}[row["side"]] if column == "side" else cell_text(row[column]) for column in columns]
            for row in rows
        ]
        if cells:
            cells.insert(0, ["" if column == "side" else column for column in columns])
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


def ignoring_date(first: Counter, second: Counter) -> tuple:
    """(only in first, only in second, count in both) when a photo is its size and file name, whatever its date: the same
    clip imported twice with a shifted time is one photo, and the extra copies are the ones left over."""
    def grouped(photos: Counter) -> dict:
        groups = {}
        for (when, size, name), count in sorted(photos.items()):
            groups.setdefault((size, name), []).extend([(when, size, name)] * count)
        return groups

    left_groups, right_groups = grouped(first), grouped(second)
    only_left, only_right, both = Counter(), Counter(), 0
    for key in left_groups.keys() | right_groups.keys():
        left_items, right_items = left_groups.get(key, []), right_groups.get(key, [])
        shared = min(len(left_items), len(right_items))
        both += shared
        only_left.update(left_items[shared:])
        only_right.update(right_items[shared:])
    return only_left, only_right, both


def diff_command(
    left: Annotated[str, typer.Argument(help="First library: a name from `./apple photos libraries`, or the path of a .photoslibrary.")],
    right: Annotated[str, typer.Argument(help="Second library, named the same way.")],
    summary: Annotated[bool, typer.Option("--summary", help="Print only the counts.")] = False,
    only: Annotated[Optional[Side], typer.Option(help="Show only the photos that are in just the left, or just the right, library; the side column is left out.")] = None,
    format: Annotated[DiffFormat, typer.Option(help="text: aligned columns. csv and json: one record per photo, with a side column (left or right) unless --only is given.")] = DiffFormat.text,
    fields: Annotated[
        Optional[str],
        typer.Option(help="The columns to show and their order: any of date, size, name, hidden, burst, missing, shared, comma-separated. hidden, burst, missing and shared are true or false: missing means the original file is not stored in the library, so PowerPhotos cannot list or copy it; shared means the photo came from Messages (Shared with You), and when it is also missing, saving it to the library in Photos makes it copyable. The default is date,size,name,hidden,burst,missing,shared; with --exclude-hidden, without hidden and burst, which would only ever say false."),
    ] = None,
    sort: Annotated[DiffSort, typer.Option(help="Order of the photos.")] = DiffSort.date,
    since: Annotated[Optional[datetime], typer.Option(help="Compare only photos from this date on (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Compare only photos up to and including this date (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Compare photos, videos or both, by file name ending as `copy --media` does.")] = Media.all,
    ignore_date: Annotated[
        bool,
        typer.Option("--ignore-date", help="Treat photos with the same size and file name as the same photo, whatever their dates (finds a clip imported twice with a shifted time). Repeated names from different cameras can match wrongly."),
    ] = False,
    exclude_hidden: Annotated[
        bool,
        typer.Option("--exclude-hidden", help="Leave out the items Photos keeps out of view (mostly burst members), which PowerPhotos does not list and so cannot copy: compare only what it can see. How many were left out is reported on stderr."),
    ] = False,
) -> None:
    """Show which photos and videos are in one library and not the other.

    Reads the libraries' databases directly, so it is fast, and needs PowerPhotos only to look up names (a path to a
    .photoslibrary works anywhere). A photo is its date, size and file name; photos in the trash are ignored. Items Photos
    keeps out of view (hidden: mostly burst members) are compared too and marked in the hidden and burst columns, though
    PowerPhotos does not list them and cannot copy them; --exclude-hidden leaves them out and reports how many on stderr. --since and
    --until limit both libraries to a range of dates, in UTC as the databases keep them, and --media to photos or videos,
    so the counts are for that selection too. --ignore-date matches photos on size and file name alone. Text output
    marks each photo `<` when it is only in LEFT and `>` when it is only in RIGHT, in aligned columns. The counts go to
    stderr. Exits 1 when the libraries differ, 0 when they match. Changes nothing.
    """
    columns = parse_fields(fields) if fields is not None else DEFAULT_FIELDS + ([] if exclude_hidden else ["hidden", "burst"]) + ["missing", "shared"]
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    try:
        left_path, right_path = find_library(left), find_library(right)
        data = [pp.library_photos(left_path), pp.library_photos(right_path)]
    except pp.PowerPhotosError as error:
        fail(str(error))

    def selected(counted: Counter) -> Counter:
        return pp.of_media(within(counted, since, until), media.value)

    superseded = [selected(d.superseded) for d in data]
    visible = [selected(d.visible) for d in data]
    hidden = [selected(d.hidden) for d in data]
    bursts = [selected(d.bursts) for d in data]
    first, second = (visible[i] if exclude_hidden else visible[i] + hidden[i] for i in (0, 1))
    if ignore_date:
        only_left, only_right, both = ignoring_date(first, second)
    else:
        only_left, only_right, both = first - second, second - first, sum((first & second).values())
    if not summary:
        rows = diff_rows(only_left, only_right, only, sort, {"left": set(hidden[0]), "right": set(hidden[1])}, {"left": set(bursts[0]), "right": set(bursts[1])}, {"left": data[0], "right": data[1]}, "missing" in columns)
        print_diff(rows, columns, only is None, format)
    typer.echo(f"Only in {left}: {sum(only_left.values())}. Only in {right}: {sum(only_right.values())}. In both: {both}.", err=True)
    if sum(superseded[0].values()) or sum(superseded[1].values()):
        typer.echo(
            "Not counted, shared placeholders that have an imported copy in the same library: "
            f"{sum(superseded[0].values())} in {left}, {sum(superseded[1].values())} in {right}.",
            err=True,
        )
    if exclude_hidden and (sum(hidden[0].values()) or sum(hidden[1].values())):
        typer.echo(
            "Not compared, hidden in Photos and not listed by PowerPhotos: "
            f"{sum(hidden[0].values())} in {left} ({count(sum(bursts[0].values()), 'burst member')}), "
            f"{sum(hidden[1].values())} in {right} ({count(sum(bursts[1].values()), 'burst member')}).",
            err=True,
        )
    raise typer.Exit(1 if only_left or only_right else 0)


def only_in_left(left: str, right: str, since: Optional[datetime], until: Optional[datetime], media: Media) -> list:
    """The records of `diff LEFT RIGHT --only left` for the dates and media given, read from the databases."""
    try:
        data = [pp.library_photos(find_library(left)), pp.library_photos(find_library(right))]
    except pp.PowerPhotosError as error:
        fail(str(error))
    selected = [pp.of_media(within(d.visible + d.hidden, since, until), media.value) for d in data]
    hidden = [set(pp.of_media(within(d.hidden, since, until), media.value)) for d in data]
    bursts = [set(pp.of_media(within(d.bursts, since, until), media.value)) for d in data]
    return diff_rows(selected[0] - selected[1], Counter(), Side.left, DiffSort.date, {"left": hidden[0], "right": hidden[1]},
                     {"left": bursts[0], "right": bursts[1]}, {"left": data[0], "right": data[1]}, True)


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
    only_missing: Annotated[
        bool,
        typer.Option("--only-missing", help="Copy just the photos the source has and the destination lacks, worked out from the two databases as `apple photos diff SOURCE DESTINATION --only left` lists them, without PowerPhotos' duplicate check: the same as piping that diff into --list - with --no-duplicate-check. --since, --until and --media limit what is compared. Not with --list."),
    ] = False,
    no_duplicate_check: NoDuplicateCheckOption = False,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
) -> None:
    """[macOS only] Copy the photos and videos in a date range, or the ones in a list, from one library to another with PowerPhotos.

    Photos already in the destination are skipped, so reruns add only what is new. Nothing is removed from the source.
    With --list, every photo in the source with a listed file name is copied, so a name that more than one photo has
    copies all of them. Names PowerPhotos does not list in the source are reported on stderr, and so are hidden items in the list
    (the hidden column of `diff`), which are left out. An empty list copies nothing and succeeds.
    --no-duplicate-check copies the listed photos even when the destination has them, which avoids PowerPhotos'
    comparison against a large library; use it only with a list of what is missing, such as from `apple photos diff`.
    --only-missing does that for you: it works out what the destination lacks and copies exactly that.
    """
    require_macos()
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if only_missing and (list_file is not None or no_duplicate_check):
        raise typer.BadParameter("--only-missing works out what to copy itself, so it cannot be used with --list or --no-duplicate-check")
    if no_duplicate_check and list_file is None:
        raise typer.BadParameter("--no-duplicate-check needs --list: without a list it would copy the whole range again")
    names, hidden, no_original = None, {}, {}
    if only_missing:
        no_duplicate_check = True
        names, hidden, no_original = split_records(only_in_left(source, destination, since, until, media))
        if not names:
            reason = f"nothing is missing from {destination}" if not (hidden or no_original) else "everything missing is hidden in Photos or has no original in " + source
            typer.echo(f"{destination}: no files copied ({reason})")
            if hidden:
                report_hidden(hidden)
            report_missing(no_original, source)
            return
    elif list_file is not None:
        if since or until or media != Media.all:
            raise typer.BadParameter("--list cannot be combined with --since, --until or --media")
        names, hidden, no_original = parse_list(sys.stdin.read() if list_file == "-" else Path(list_file).read_text(encoding="utf-8"))
        if not names:  # e.g. a diff that found nothing: there is nothing to copy, which is not an error
            reason = "the list is empty" if not (hidden or no_original) else "everything listed is hidden in Photos or has no original in " + source
            typer.echo(f"{destination}: no files copied ({reason})")
            if hidden:
                report_hidden(hidden)
            report_missing(no_original, source)
            return
    try:
        with pp.one_at_a_time():
            result = copy_one(
                known_libraries(), source, destination,
                since.date() if since else None,
                until.date() + timedelta(days=1) if until else None,
                media.value, on_upgrade, names, not no_duplicate_check,
            )
    except pp.Busy as error:
        fail(str(error))
    typer.echo(f"{destination}: {describe_copy(result, not no_duplicate_check)}")
    if names is not None and hidden:
        report_hidden(hidden)
    if names is not None:
        report_missing(no_original, source)
    report_unlisted(result.unlisted, source)


def read_library(libraries: dict, name: str) -> Optional[pp.Photos]:
    """The photos of a library in PowerPhotos, read from its database, or None when it is unknown or cannot be read."""
    if name not in libraries:
        return None
    try:
        return pp.library_photos(libraries[name])
    except pp.PowerPhotosError:
        return None


def read_photos(libraries: dict, name: str) -> Optional[Counter]:
    """The visible photos of a library (see read_library), or None."""
    found = read_library(libraries, name)
    return None if found is None else found.visible


def check_part(libraries: dict, part: split_plan.Part, kind: str, year: int, source_data: Optional[pp.Photos], nearby: list, read_destination, label: Optional[str] = None) -> tuple:
    """Says whether the library of a part holds only photos of its year and, with a source, all its photos of the part's
    dates; without one, that it holds some photos of the year. Lists what is outside the year and what is missing (see
    split_command, --check). Returns (whether it is in order, the items outside the year, the library's photos or None)."""
    label = label or part.name
    if part.name not in libraries:
        typer.echo(f"{label}: library missing")
        return False, Counter(), None
    own = read_library(libraries, part.name)
    if own is None:
        typer.echo(f"{label}: cannot be read from its database")
        return False, Counter(), None
    held = own.visible + own.hidden
    outside, near_ends = split_plan.outside_year(held, year, kind)
    if source_data is None:
        lacking = uncertain = Counter()
    else:
        found = split_plan.missing_in_range(source_data.visible, part.start, part.end, kind, read_destination(part.name), [read_destination(name) for name in nearby])
        lacking, uncertain = found if found is not None else (Counter(), Counter())
    problems = []
    if source_data is None and not split_plan.count_in_range(held, date(year, 1, 1), date(year + 1, 1, 1), kind):
        problems.append(f"holds no {kind} from {year}")
    if outside:
        by_year = Counter()
        for key, number in outside.items():
            by_year[key[0][:4]] += number
        problems.append(f"{sum(outside.values())} outside {year} ({', '.join(f'{when} {number}' for when, number in sorted(by_year.items()))})")
    if lacking:
        problems.append(f"{sum(lacking.values())} missing")
    notes = []
    if near_ends:
        notes.append(f"{near_ends} within a day of the year's ends")
    if uncertain:
        notes.append(f"{sum(uncertain.values())} of the source's within a day of its dates' ends are in no library")
    typer.echo(f"{label}: " + ("; ".join(problems) if problems else "in order") + (f" ({'; '.join(notes)})" if notes else ""))
    for key in sorted(outside.elements()):
        typer.echo(f"    outside: {key[0]} {key[2]}")
    for key in sorted(lacking.elements()):
        typer.echo(f"    missing: {key[0]} {key[2]}")
    return not problems, outside, own


def fix_strays(libraries: dict, part: split_plan.Part, kind: str, outside: Counter, own: pp.Photos, parts_of, on_upgrade: OnUpgrade, dry: bool) -> bool:
    """--fix: copies the items outside the year of a library into the library of their own year (PowerPhotos skips any that
    library already has), then re-reads that library's database to confirm each one is there, and only then moves the
    confirmed ones out of this library to its Recently Deleted (PowerPhotos' delete), all in one delete after every copy has
    been confirmed, so that PowerPhotos asks once however many years the strays come from. Photos are named by their Photos
    identifiers, so a file name another photo shares does not matter. An item is left alone, and said so, when its year
    has no library, when it cannot be told which part of a capped year it belongs to, or when the database gives it no
    identifier. Returns whether anything was removed from this library."""
    wanted: dict = {}
    left_alone = []
    for key in sorted(outside.elements()):
        item_date = date.fromisoformat(key[0][:10])
        candidates = parts_of(kind, item_date.year)
        target = candidates[0] if len(candidates) == 1 else next((c for c in candidates if c.start <= item_date < c.end), None)
        if target is None:
            left_alone.append((key, f"no library for {item_date.year}" if not candidates else f"cannot tell which part of {item_date.year} it belongs in"))
        elif target.name not in libraries:
            left_alone.append((key, f"{target.name} is not in PowerPhotos"))
        elif not own.uuids.get(key):
            left_alone.append((key, "the database gives it no identifier"))
        else:
            wanted.setdefault(target.name, []).append(key)
    for key, why in left_alone:
        typer.echo(f"    not moved: {key[0]} {key[2]} ({why})")
    confirmed_everywhere: list = []
    names_of: dict = {}
    for target, keys in sorted(wanted.items()):
        keys = sorted(set(keys))
        identifiers = {key: own.uuids[key] for key in keys}
        if dry:
            typer.echo(f"    would copy {count(len(keys), 'item')} into {target}, then move them out of {part.name}: {', '.join(key[2] for key in keys)}")
            continue
        result = copy_one(libraries, part.name, target, None, None, kind, on_upgrade, check_duplicates=True, ids=[uuid for key in keys for uuid in identifiers[key]])
        if result.sent is None:
            typer.echo(f"    {target}: skipped (needs a Photos upgrade)")
            continue
        names_of.update({uuid: key[2] for key in keys for uuid in identifiers[key]})
        report_unlisted([names_of.get(uuid, uuid) for uuid in result.unlisted], part.name)
        there = read_library(libraries, target)
        confirmed = [key for key in keys if there is not None and key in there.visible + there.hidden]
        typer.echo(f"    {target}: asked PowerPhotos to copy {count(len(keys), 'item')}; {len(confirmed)} confirmed in its database")
        for key in keys:
            if key not in confirmed:
                typer.echo(f"      NOT confirmed in {target}, so it stays in {part.name}: {key[0]} {key[2]}")
        confirmed_everywhere += [(key, identifiers[key]) for key in confirmed]
    if not confirmed_everywhere:
        return False
    # Removed once, after every copy is confirmed: PowerPhotos asks to confirm each delete, and a library with strays of
    # several years would otherwise ask once per year.
    deleted, missing = trash_ids(libraries, part.name, [uuid for _, uuids in confirmed_everywhere for uuid in uuids])
    typer.echo(f"    {part.name}: moved {deleted} to its Recently Deleted: " + ", ".join(key[2] for key, _ in confirmed_everywhere))
    for uuid in missing:
        typer.echo(f"      not moved out of {part.name} (PowerPhotos lists no photo with that identifier, as it does not list hidden items): {names_of.get(uuid, uuid)}")
    return deleted > 0


def check_years(libraries: dict, kinds: list, first_year: int, last_year: int, destination_name: str, source_data: Optional[pp.Photos], max_bytes: Optional[int], fix: Optional[dict] = None) -> bool:
    """--check: for each year and kind, the libraries photos would be split into (those the source's photos need, or without
    a source those that exist: the name, or name-1, name-2 ...), each checked by check_part. A year with no library
    that needs none (the source has nothing in it) or, without a source, has none, is named at the end. True when every
    library is in order."""
    cache: dict = {}

    def read_destination(name: str):
        if name not in cache:
            cache[name] = read_photos(libraries, name)
        return cache[name]

    def parts_of(kind: str, year: int) -> list:
        name = destination_name.format(media=kind, year=year)
        if source_data is not None:
            return split_plan.plan_year(source_data.visible, year, kind, name, max_bytes)
        found = [existing for existing in sorted(libraries) if existing == name or re.fullmatch(re.escape(name) + r"-\d+", existing)]
        return [split_plan.Part(existing, date(year, 1, 1), date(year + 1, 1, 1), 0) for existing in found]

    in_order, nothing = True, []
    for kind in kinds:
        for year in range(first_year, last_year + 1):
            parts = parts_of(kind, year)
            if not parts:
                nothing.append(destination_name.format(media=kind, year=year))
            for part in parts:
                if part.name not in libraries and source_data is not None and not split_plan.count_in_range(source_data.visible, part.start, part.end, kind):
                    nothing.append(part.name)  # the source has no photo for it, so it is not needed
                    continue
                nearby = [p.name for y in (year - 1, year, year + 1) for p in parts_of(kind, y) if p.name != part.name]
                ok, outside, own = check_part(libraries, part, kind, year, source_data, nearby, read_destination)
                if fix is not None and outside and own is not None and fix_strays(libraries, part, kind, outside, own, parts_of, fix["on_upgrade"], fix["dry"]):
                    cache.pop(part.name, None)  # it has changed
                    ok, _, _ = check_part(libraries, part, kind, year, source_data, nearby, read_destination, f"{part.name} (after the fix)")
                in_order = ok and in_order
    if nothing:
        typer.echo(f"No library, and none needed: {', '.join(sorted(set(nothing)))}")
    return in_order


def split_command(
    media: Annotated[Media, typer.Option(help="Split photos, videos or both. With all, photos and videos go to their own libraries (photos-2018 and videos-2018), in the same run.")],
    years: Annotated[str, typer.Option(help="One year or a range, such as 2018 or 2018-2020.")],
    source: Annotated[Optional[str], typer.Option("--from", help="Source library, as named by `./apple photos libraries`. Needed unless --check is given without one.")] = None,
    destination_name: Annotated[
        str, typer.Option("--to", help="Destination library name for each year; {media} and {year} are filled in.")
    ] = "{media}-{year}",
    max_size: Annotated[
        Optional[str],
        typer.Option(
            help="A cap on the size of each library, such as 200GB (decimal units, the sum of the original files). A year over "
            "the cap goes into NAME-1, NAME-2 ... each a run of whole days from January 1 on, filled up to the cap in turn."
        ),
    ] = None,
    skip_complete: Annotated[
        bool,
        typer.Option(
            "--skip-complete/--no-skip-complete",
            help="Leave out a library that already holds all the photos of its dates, judged from the databases "
            "(a few seconds, where each PowerPhotos copy into a large library makes it reindex).",
        ),
    ] = True,
    no_duplicate_check: NoDuplicateCheckOption = False,
    on_missing: Annotated[
        OnMissing,
        typer.Option(help="When a destination library is not in PowerPhotos: stop before copying anything, or skip it and go on."),
    ] = OnMissing.stop,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Copy nothing; check that each library holds only the photos of its year and every photo of its dates that "
            "the source has: list what is outside the year and what is missing. Exits 1 when a library is not in order.",
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="With --check (implied): copy each item outside its library's year into the library of its own year, confirm it is "
            "there from that library's database, then move it out of the wrong library to that library's Recently Deleted. "
            "--dry-run shows what would be moved.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Copy nothing; show the libraries each year goes into and what each needs. Same as DRY_RUN=true.")
    ] = False,
) -> None:
    """[macOS only] Copy a library into one library per year (photos-2018, photos-2019 ...) with PowerPhotos.

    The destination libraries must already exist in PowerPhotos. A library that already has every photo of its dates
    is reported as up to date and not copied again.

    --max-size caps each library: a year over the cap is divided into runs of whole days, chronologically, into
    photos-2018-1, photos-2018-2 ... A rerun after new photos arrive can move a boundary, and a photo near one may be
    filed in either part.

    --no-duplicate-check catches stragglers: it works out from the databases which photos of each library's dates it
    lacks and copies just those, matched by file name and without PowerPhotos' duplicate check. A missing photo whose
    file name another photo in the source shares copies that one too. A photo within a day of the start or end of a
    library's dates, whose library the databases cannot settle, is copied into every library it could belong to that
    lacks it, so it can end up in two.

    --check copies nothing: it reads the databases and, for each library, lists the photos dated outside its year
    (strays) and the source's photos of its dates that it lacks, so it can be run to see that photos-2005 really holds
    the photos of 2005 and only those. A photo within a day of the year's ends is not a stray, since the databases keep
    UTC dates and PowerPhotos files by local date; one that close that the library lacks is reported as near the ends
    and does not fail the check. Exits 1 when any library has strays, lacks photos or is not in PowerPhotos.

    --fix (it implies --check) then moves the strays to where they belong, as far as it safely can: each is copied into
    the library of its own year (by file name, PowerPhotos skipping any that library has), and that library's database is
    read again to confirm it arrived. Only then is it moved out of the wrong library, to that library's Recently Deleted
    (PowerPhotos' delete; it can be put back for 30 days, and nothing is removed from disk). The check is then run again
    on that library. A stray is left alone, and said so, when its year has no
    library, when it is not clear which part of a capped year it belongs to, or when another photo in the library shares
    its file name (the copy is made by name, so that photo would be copied too).
    """
    require_macos()
    check = check or fix
    if source is None and not check:
        raise typer.BadParameter("--from is needed (it is optional only with --check)")
    first, _, last = years.partition("-")
    if not (first.isdigit() and (last.isdigit() or not last)):
        raise typer.BadParameter("--years must be a year or a range such as 2018-2020")
    first_year, last_year = int(first), int(last or first)
    if last_year < first_year:
        raise typer.BadParameter("--years ends before it starts")
    if no_duplicate_check and not skip_complete:
        raise typer.BadParameter("--no-duplicate-check works out what is missing, so it cannot be used with --no-skip-complete")
    if check and (no_duplicate_check or not skip_complete):
        raise typer.BadParameter("--check works out what is missing and copies nothing, so it cannot be used with --no-duplicate-check or --no-skip-complete")
    try:
        max_bytes = split_plan.parse_size(max_size) if max_size is not None else None
    except ValueError as error:
        raise typer.BadParameter(str(error))
    dry = is_dry_run(dry_run)
    kinds = ["photos", "videos"] if media == Media.all else [media.value]
    try:
        with pp.one_at_a_time():
            libraries = known_libraries()
            source_data = read_library(libraries, source) if source and (skip_complete or max_bytes or check) else None
            if check:
                if source and source_data is None:
                    fail(f"The photos of {source} cannot be read from its database, so what is missing cannot be worked out.")
                if not check_years(libraries, kinds, first_year, last_year, destination_name, source_data, max_bytes,
                                   {"on_upgrade": on_upgrade, "dry": dry} if fix else None):
                    raise typer.Exit(1)
                return
            source_photos = None if source_data is None else source_data.visible
            if source_data is not None and sum(source_data.superseded.values()):
                typer.echo(
                    f"{source}: {count(sum(source_data.superseded.values()), 'shared placeholder')} with an imported copy in the same "
                    "library not counted.", err=True,
                )
            if (no_duplicate_check or max_bytes) and source_photos is None:
                fail(f"The photos of {source} cannot be read from its database, so " + (
                    "what is missing cannot be worked out." if no_duplicate_check else "its size cannot be worked out."))
            plans = {}

            def plan(kind: str, year: int) -> list:
                if (kind, year) not in plans:
                    name = destination_name.format(media=kind, year=year)
                    plans[(kind, year)] = split_plan.plan_year(source_photos, year, kind, name, max_bytes)
                return plans[(kind, year)]

            cache = {}

            def read_destination(name: str):
                if name not in cache:
                    cache[name] = read_photos(libraries, name)
                return cache[name]

            def nearby_names(kind: str, year: int, own: str) -> list:
                return [p.name for y in (year - 1, year, year + 1) for p in plan(kind, y) if p.name != own]

            work = [part for kind in kinds for year in range(first_year, last_year + 1) for part in plan(kind, year)]
            absent = [part.name for part in work if part.name not in libraries]
            if absent and on_missing == OnMissing.stop and not dry:
                fail(f"Not in PowerPhotos: {', '.join(absent)}. Create them there, or use --on-missing skip.")
            for kind in kinds:
                if source_data is not None:
                    hidden_here = pp.of_media(source_data.hidden, kind)
                    if hidden_here:
                        bursts_here = pp.of_media(source_data.bursts, kind)
                        typer.echo(
                            f"{source}: {count(sum(hidden_here.values()), 'hidden ' + kind[:-1])} ({count(sum(bursts_here.values()), 'burst member')}) "
                            "not copied: Photos keeps them out of view and PowerPhotos does not list them.",
                            err=True,
                        )
                for year in range(first_year, last_year + 1):
                    parts = plan(kind, year)
                    if len(parts) > 1:
                        typer.echo(f"{parts[0].name.rsplit('-', 1)[0]}: {split_plan.format_size(sum(p.size for p in parts))} over the "
                                   f"{split_plan.format_size(max_bytes)} cap, in {len(parts)} libraries")
                    for part in parts:
                        label = f"{part.name} ({part.start.isoformat()} to {(part.end - timedelta(days=1)).isoformat()}, {split_plan.format_size(part.size)})" if max_bytes else part.name
                        note = ""
                        if source_data is not None:
                            left_out = split_plan.count_in_range(source_data.hidden, part.start, part.end, kind)
                            note = f"; {count(left_out, 'hidden item')} in {source} not copied (Photos keeps them out of view)" if left_out else ""
                        if part.name not in libraries:
                            typer.echo(f"{label}: " + ("library missing" if dry else "skipped (not in PowerPhotos)") + note)
                            continue
                        found = None
                        if source_photos is not None:
                            near = [read_destination(name) for name in nearby_names(kind, year, part.name)]
                            found = split_plan.missing_in_range(source_photos, part.start, part.end, kind, read_destination(part.name), near)
                            if found is not None and not found[0] and not found[1]:
                                typer.echo(f"{label}: up to date{note}")
                                continue
                        if dry:
                            typer.echo(f"{label}: would copy{note}")
                            continue
                        no_original = {}
                        if no_duplicate_check:
                            if found is None:
                                fail(f"The photos of {part.name} cannot be read from its database, so what is missing cannot be worked out.")
                            keys_by_name = {}
                            for key in list(found[0] + found[1]):  # near the ends too: any library will do
                                keys_by_name.setdefault(key[2], []).append(key)
                            no_original = {
                                name: any(key in source_data.shared for key in keys) for name, keys in sorted(keys_by_name.items())
                                if source_data is not None and all(source_data.original_missing(key) for key in keys)
                            }
                            names = sorted(set(keys_by_name) - set(no_original))
                            if not names:
                                typer.echo(f"{label}: nothing to copy{note}")
                                report_missing(no_original, source)
                                continue
                            result = copy_one(
                                libraries, source, part.name, None, None, kind, on_upgrade, names, check_duplicates=False
                            )
                        else:
                            result = copy_one(libraries, source, part.name, part.start, part.end, kind, on_upgrade)
                        typer.echo(f"{label}: {describe_copy(result, not no_duplicate_check)}{note}")
                        report_missing(no_original, source)
                        report_unlisted(result.unlisted, source)
    except pp.Busy as error:
        fail(str(error))
