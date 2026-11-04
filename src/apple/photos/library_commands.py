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


def is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def parse_list(text: str) -> tuple:
    """(file names to copy, hidden items) from the output of `./apple photos diff` (json or csv, with a name column and
    optionally true/false hidden, burst and missing columns) or one name per line. Returns the names to copy, the hidden
    items as a dict of name -> whether it is a burst member, and the names whose original is not stored in the library.
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
    names, hidden, missing = [], {}, []
    for record in records:
        if is_true(record.get("hidden")):
            hidden[record["name"]] = is_true(record.get("burst"))
        elif is_true(record.get("missing")):
            missing.append(record["name"])
        else:
            names.append(record["name"])
    return names, hidden, missing


def report_missing(names: list, source: str) -> None:
    """Says on stderr which wanted photos were left out because their original is not stored in the source."""
    if not names:
        return
    typer.echo(
        f"  {len(names)} not copied: the original is not stored in {source} (a photo shared with you in Messages, or one "
        "whose original lives only in iCloud), so PowerPhotos does not list it and cannot copy it:", err=True,
    )
    for name in names[:20]:
        typer.echo(f"    {name}", err=True)
    if len(names) > 20:
        typer.echo(f"    ... and {len(names) - 20} more", err=True)


def report_hidden(hidden: dict) -> None:
    """Says on stderr which listed items were left out because Photos hides them (PowerPhotos cannot copy those)."""
    if len(hidden) <= 20:
        for name, burst in hidden.items():
            typer.echo(f"Not copied: {name}: hidden in Photos{' (a burst member)' if burst else ''}, so PowerPhotos does not list it", err=True)
    typer.echo(f"{len(hidden)} hidden items ({sum(hidden.values())} burst members) were not copied: PowerPhotos cannot see them.", err=True)


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
    media: str, mode: OnUpgrade, names: Optional[list] = None, check_duplicates: bool = True,
) -> CopyResult:
    """Copies one date range, or the photos with the given file names."""
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
                unlisted = [line.partition("\t")[2] for line in output.splitlines() if line.startswith("missing\t")]
                return CopyResult(int(output.rsplit(" ", 1)[-1]), unlisted)
            except pp.PowerPhotosError as error:
                if error.number != pp.UPGRADE_NEEDED:
                    fail(f"PowerPhotos failed: {error}")
                if not resolve_upgrade(destination, destination_path, source_version, mode, str(error)):
                    return CopyResult(None, [])


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


DIFF_FIELDS = ("date", "size", "name", "hidden", "burst", "missing")
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
    (found by looking at the files, so only when with_missing)."""
    rows = []
    for side, counted in ((Side.left, only_left), (Side.right, only_right)):
        if which in (None, side):
            for key in counted.elements():
                when, size, name = key
                rows.append({
                    "side": side.value, "date": when, "size": int(size) if size.isdigit() else None, "name": name,
                    "hidden": key in hidden[side.value], "burst": key in bursts[side.value],
                    "missing": libraries[side.value].original_missing(key) if with_missing else False,
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
        typer.Option(help="The columns to show and their order: any of date, size, name, hidden, burst, missing, comma-separated. hidden, burst and missing are true or false: missing means the original file is not stored in the library (a photo shared with you in Messages, or one whose original lives only in iCloud), so PowerPhotos cannot list or copy it. The default is date,size,name,hidden,burst,missing; with --exclude-hidden, without hidden and burst, which would only ever say false."),
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
    columns = parse_fields(fields) if fields is not None else DEFAULT_FIELDS + ([] if exclude_hidden else ["hidden", "burst"]) + ["missing"]
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    try:
        left_path, right_path = find_library(left), find_library(right)
        data = [pp.library_photos(left_path), pp.library_photos(right_path)]
    except pp.PowerPhotosError as error:
        fail(str(error))

    def selected(counted: Counter) -> Counter:
        return pp.of_media(within(counted, since, until), media.value)

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
    if exclude_hidden and (sum(hidden[0].values()) or sum(hidden[1].values())):
        typer.echo(
            "Not compared, hidden in Photos and not listed by PowerPhotos: "
            f"{sum(hidden[0].values())} in {left} ({sum(bursts[0].values())} burst members), "
            f"{sum(hidden[1].values())} in {right} ({sum(bursts[1].values())} burst members).",
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
    copies all of them. Names PowerPhotos does not list in the source are reported on stderr, and so are hidden items in the list
    (the hidden column of `diff`), which are left out. An empty list copies nothing and succeeds.
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
        names, hidden, no_original = parse_list(sys.stdin.read() if list_file == "-" else Path(list_file).read_text(encoding="utf-8"))
        if hidden:
            report_hidden(hidden)
        report_missing(no_original, source)
        if not names:  # e.g. a diff that found nothing: there is nothing to copy, which is not an error
            reason = "the list is empty" if not (hidden or no_original) else "everything listed is hidden in Photos or has no original in " + source
            typer.echo(f"{destination}: no files copied ({reason})")
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


def split_command(
    source: SourceOption,
    media: Annotated[Media, typer.Option(help="Split photos, videos or both. With all, photos and videos go to their own libraries (photos-2018 and videos-2018), in the same run.")],
    years: Annotated[str, typer.Option(help="One year or a range, such as 2018 or 2018-2020.")],
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
        max_bytes = split_plan.parse_size(max_size) if max_size is not None else None
    except ValueError as error:
        raise typer.BadParameter(str(error))
    dry = is_dry_run(dry_run)
    kinds = ["photos", "videos"] if media == Media.all else [media.value]
    try:
        with pp.one_at_a_time():
            libraries = known_libraries()
            source_data = read_library(libraries, source) if skip_complete or max_bytes else None
            source_photos = None if source_data is None else source_data.visible
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
                            f"{source}: {sum(hidden_here.values())} hidden {kind} ({sum(bursts_here.values())} burst members) "
                            "are not copied: Photos keeps them out of view and PowerPhotos does not list them.",
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
                            note = f"; {left_out} hidden items in {source} not copied (Photos keeps them out of view)" if left_out else ""
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
                        no_original = []
                        if no_duplicate_check:
                            if found is None:
                                fail(f"The photos of {part.name} cannot be read from its database, so what is missing cannot be worked out.")
                            keys_by_name = {}
                            for key in list(found[0] + found[1]):  # near the ends too: any library will do
                                keys_by_name.setdefault(key[2], []).append(key)
                            no_original = sorted(
                                name for name, keys in keys_by_name.items()
                                if source_data is not None and all(source_data.original_missing(key) for key in keys)
                            )
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
