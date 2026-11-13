"""`./apple photos libraries|diff|copy|split`: compare and move photos between Photos libraries with PowerPhotos (macOS only)."""
import csv
import json
import re
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, NamedTuple, Optional

import typer

from apple.platform import require_macos
from apple.photos import powerphotos as pp
from apple.photos import blacklist, folder_photos, matching, powerphotos_reports, split_plan
from apple.photos.common import is_dry_run
from apple.photos.progress import Progress, duration


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


SourceArgument = Annotated[str, typer.Argument(help="Source library, as named by `./apple photos libraries`.")]
CheckDuplicatesOption = Annotated[
    bool,
    typer.Option(
        "--check-duplicates",
        help="Let PowerPhotos compare what it copies against the destination and skip the photos it already has. Off by "
        "default: what is missing is worked out from the databases and only that is copied, which is faster into a large library.",
    ),
]
DiffModeOption = Annotated[str, typer.Option("--diff-mode", envvar="APPLE_PHOTOS_DIFF_MODE", help=matching.HELP)]
ExplainOption = Annotated[
    bool,
    typer.Option(
        "--explain",
        help="For each photo that is reported as having no match, say on stderr which photo of the other library is closest to it "
        "and what differs under --diff-mode: why it is not a match.",
    ),
]
POLL_SECONDS = 5  # how often a library being copied into is looked at
SECONDS_PER_MINUTE = 60
DEFAULT_TIMEOUT = 15.0  # minutes a library may stay unchanged, while a copy into it is waited for, before the copy is given up on

TimeoutOption = Annotated[
    float,
    typer.Option(
        "--timeout", min=0.1,
        help="How many minutes to wait, while PowerPhotos copies, for the destination library to change before giving up on "
        "the copy: it can work a long time on large photos and videos without writing to the library. The wait ends as soon as "
        "the photos have arrived.",
    ),
]
IgnoreBlacklistOption = Annotated[
    bool,
    typer.Option(
        "--ignore-blacklist",
        help="Try again the photos on the blacklist (a photo goes on it when a copy of it fails with an error of its own in "
        "PowerPhotos' report), which are otherwise left out of copies made by identifier; one that copies is taken off it.",
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
        fail(f"No library named {name} in PowerPhotos (`./apple photos libraries` lists them).")
    return libraries[name]


def looks_like_a_path(text: str) -> bool:
    """Whether text is written as a path (it has a / in it, or starts with ~ or .) and so cannot be a library's name."""
    return "/" in text or text.startswith(("~", "."))


def find_library(name_or_path: str) -> Path:
    """A library given as the path of a .photoslibrary, or by its name in PowerPhotos (macOS only). Something written as a
    path that does not exist is an error, not a name to look up."""
    path = Path(name_or_path).expanduser()
    if path.is_dir():
        return path
    if looks_like_a_path(name_or_path):
        fail(f"{name_or_path} does not exist.")
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


DIFF_FIELDS = ("date", "size", "name", "hidden", "burst", "missing", "shared", "files")
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


def diff_rows(only_left: Counter, only_right: Counter, which: Optional[Side], sort: DiffSort, hidden: dict, bursts: dict, libraries: dict, with_missing: bool, folders: Optional[dict] = None) -> list:
    """The photos to show as dicts of side, date, size (int or None), name, hidden, burst and missing (true or false), in
    the order asked for. hidden and bursts give, for each side's value, the (date, size, name) keys of that side's hidden
    items and of its burst members; libraries gives its pp.Photos, which says whether an original is not stored there
    (found by looking at the files, so only when with_missing), and shared whether the item came from Messages. folders says
    which sides are a folder of files: for those, each row also has the path of its file (each copy of a photo its own, in
    order) as files, a list; for a library it is empty."""
    rows = []
    for side, counted in ((Side.left, only_left), (Side.right, only_right)):
        if which in (None, side):
            copy_of = Counter()
            for key in counted.elements():
                when, size, name = key
                paths = libraries[side.value].files.get(key, []) if folders and folders[side.value] else []
                files = [paths[min(copy_of[key], len(paths) - 1)]] if paths else []
                copy_of[key] += 1
                rows.append({
                    "side": side.value, "date": when, "size": int(size) if size.isdigit() else None, "name": name,
                    "hidden": key in hidden[side.value], "burst": key in bursts[side.value],
                    "missing": libraries[side.value].original_missing(key) if with_missing else False,
                    "shared": key in libraries[side.value].shared, "files": files,
                })
    order = {
        DiffSort.date: lambda r: (r["date"], r["name"], r["size"] or 0, r["side"]),
        DiffSort.name: lambda r: (r["name"], r["date"], r["size"] or 0, r["side"]),
        DiffSort.size: lambda r: (r["size"] or 0, r["date"], r["name"], r["side"]),
    }[sort]
    return sorted(rows, key=order)


def cell_text(value) -> str:
    """A value as text: true and false for booleans, nothing for a missing value."""
    if isinstance(value, list):
        return "|".join(str(item) for item in value)  # the paths of a row, as import reads them
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


def diff_command(
    left: Annotated[str, typer.Argument(help="First library: a name from `./apple photos libraries`, or the path of a .photoslibrary; or a folder of photo and video files.")],
    right: Annotated[str, typer.Argument(help="Second library or folder, named the same way.")],
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
    diff_mode: DiffModeOption = "",
    explain: ExplainOption = False,
    include_duplicates: Annotated[
        bool,
        typer.Option("--include-duplicates", help="Count every copy of a photo (the same date, size and file name) a library holds, so a photo held twice in one library and once in the other is a difference. By default a photo is counted once."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="For a folder: read it again instead of reusing what an earlier run (of `diff` or `find`) saved. A directory whose modification time is unchanged is not listed again, so a file written over in place is not noticed until then."),
    ] = False,
    exclude_hidden: Annotated[
        bool,
        typer.Option("--exclude-hidden", help="Leave out the items Photos keeps out of view (mostly burst members), which PowerPhotos does not list and so cannot copy: compare only what it can see. How many were left out is reported on stderr."),
    ] = False,
) -> None:
    """Show which photos and videos are in one library and not the other.

    Either side can be a folder of photo and video files instead of a library: a file is its modification time (taken as its
    capture time, in UTC), size and name, the same triple a library's database gives a photo, so a folder that kept its files'
    times (a backup, a copy) compares exactly, and one whose files were re-saved does not. Subfolders are read, Time Machine's
    linked folders are followed, and libraries inside the folder and what a disk keeps for itself are skipped. With a folder
    on either side the output also has a files column, the path of each row's file (empty for a library's photos), so that
    `diff LIBRARY FOLDER --only right --format csv` lists the folder's files the library lacks, in the form `apple photos import
    -` reads to import them into the library.

    Reads the libraries' databases directly, so it is fast, and needs PowerPhotos only to look up names (a path to a
    .photoslibrary works anywhere). A photo is its date, size and file name; photos in the trash are ignored. Items Photos
    keeps out of view (hidden: mostly burst members) are compared too and marked in the hidden and burst columns, though
    PowerPhotos does not list them and cannot copy them; --exclude-hidden leaves them out and reports how many on stderr. --since and
    --until limit both libraries to a range of dates, in UTC as the databases keep them, and --media to photos or videos,
    so the counts are for that selection too. A photo is counted once however many copies of it (the same date, size and file name) a library
    holds; --include-duplicates counts every copy. --diff-mode says how closely two photos must agree to be the same photo (their file name, date and size; the default allows a date two hours apart and a size 1% apart), and `date=ignore` matches a clip imported twice with a shifted time. Text output
    marks each photo `<` when it is only in LEFT and `>` when it is only in RIGHT, in aligned columns. The counts go to
    stderr. Exits 1 when the libraries differ, 0 when they match. Changes nothing.
    """
    folders = {name: folder_photos.is_folder(side) for name, side in (("left", left), ("right", right))}
    columns = parse_fields(fields) if fields is not None else DEFAULT_FIELDS + ([] if exclude_hidden else ["hidden", "burst"]) + ["missing", "shared"] + (["files"] if any(folders.values()) else [])
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    match = parse_mode(diff_mode)
    try:
        sides = [Path(side) if folders[name] else find_library(side) for name, side in (("left", left), ("right", right))]  # a name that is not known fails before anything is read
        data = [folder_photos.folder_photos(side, refresh=refresh) if folders[name] else pp.library_photos(side) for name, side in zip(("left", "right"), sides)]
    except pp.PowerPhotosError as error:
        fail(str(error))

    def selected(counted: Counter) -> Counter:
        return pp.of_media(within(counted, since, until), media.value)

    superseded = [selected(d.superseded) for d in data]
    visible = [selected(d.visible) for d in data]
    hidden = [selected(d.hidden) for d in data]
    bursts = [selected(d.bursts) for d in data]
    first, second = (visible[i] if exclude_hidden else visible[i] + hidden[i] for i in (0, 1))
    if not include_duplicates:
        first, second = (Counter(dict.fromkeys(counted, 1)) for counted in (first, second))
    only_left, only_right, both = matching.pair(first, second, match)
    if explain:
        explain_unmatched(only_left, second, match, right, data[1].trashed)
        explain_unmatched(only_right, first, match, left, data[0].trashed)
    if not summary:
        rows = diff_rows(only_left, only_right, only, sort, {"left": set(hidden[0]), "right": set(hidden[1])}, {"left": set(bursts[0]), "right": set(bursts[1])}, {"left": data[0], "right": data[1]}, "missing" in columns, folders)
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


IgnoreTrashOption = Annotated[
    bool,
    typer.Option(
        "--ignore-trash",
        help="With --only-missing, do not count what the destination holds in Recently Deleted as there. By default a photo that "
        "matches one there is left out, as Photos itself would call it a duplicate; with this it is listed, imported or copied.",
    ),
]


def lacking_from(wanted: Counter, held: Counter, library: pp.Photos, since: Optional[datetime], until: Optional[datetime],
                 media: Media, match: matching.Mode, ignore_trash: bool) -> Counter:
    """The photos of wanted that the library (whose live photos, in range, are held) has none of; unless ignore_trash, one
    that matches a photo in its Recently Deleted is there too, and how many that left out is said."""
    live = matching.absent(wanted, held, match)
    if ignore_trash:
        return live
    trash = pp.of_media(within(library.trashed, since, until), media.value)
    missing = matching.absent(live, held + trash, match)
    if len(missing) < len(live):
        typer.echo(f"{len(live) - len(missing)} left out as the destination has them in Recently Deleted (--ignore-trash lists them)", err=True)
    return missing


def explain_unmatched(unmatched: Counter, others: Counter, mode: matching.Mode, name: str, trashed: Optional[Counter] = None) -> None:
    """Says on stderr, for each photo of unmatched, which photo of others (the library or folder called name) is closest to it and
    what differs: what is wrong with the match, as far as the mode can tell. Also the photos of the same name, however much
    they differ, and those in Recently Deleted (trashed), which Photos looks at when it looks for a duplicate."""
    if not unmatched:
        return
    finder = matching.Finder(mode, list(others))
    trash = matching.Finder(mode, list(trashed)) if trashed else None
    typer.echo(f"Why {count(len(unmatched), 'photo')} did not match a photo of {name} (the closest one, and what differs under the mode):", err=True)
    for photo in sorted(unmatched):
        typer.echo(f"  {photo[2]}  {photo[0]}  {photo[1]}", err=True)
        near = finder.closest(photo)
        if near is None:
            typer.echo("      nothing like it", err=True)
        else:
            typer.echo(f"      closest: {near.photo[2]}  {near.photo[0]}  {near.photo[1]}", err=True)
            typer.echo(f"      differs: {', '.join(near.differs) or 'nothing the mode looks at'}", err=True)
            alike = [
                part for part, differs in (
                    ("date " + ("the same" if not near.gap else f"{matching.span(near.gap)} apart"), any(d.startswith("date") for d in near.differs)),
                    ("size " + ("the same" if not near.bytes_apart else f"{near.bytes_apart:,} bytes apart"), any(d.startswith("size") for d in near.differs)),
                ) if not differs
            ]
            if alike:
                typer.echo(f"      within the mode: {', '.join(alike)}", err=True)
            if near.differs and all(part in ("name", "extension") for part in near.differs) and not near.gap and not near.bytes_apart:
                typer.echo("      the same date and size under another name: Photos compares the content, so it may call this a duplicate", err=True)
        same = finder.same_name(photo)
        for item in same:
            if not near or item.photo != near.photo:
                typer.echo(f"      same name: {item.photo[2]}  {item.photo[0]}  {item.photo[1]}  (differs: {', '.join(item.differs) or 'nothing the mode looks at'})", err=True)
        if not same:
            typer.echo(f"      no photo of that name in {name}", err=True)
        for item in (trash.same_name(photo) if trash else []):
            typer.echo(f"      in Recently Deleted: {item.photo[2]}  {item.photo[0]}  {item.photo[1]}  (differs: {', '.join(item.differs) or 'nothing the mode looks at: Photos may count this one'})", err=True)


def parse_mode(text: str) -> matching.Mode:
    """The mode --diff-mode (or APPLE_PHOTOS_DIFF_MODE) says, as a usage error when it is not one."""
    try:
        return matching.parse(text)
    except ValueError as error:
        raise typer.BadParameter(str(error))


def only_in_left(left: str, right: str, since: Optional[datetime], until: Optional[datetime], media: Media, match: matching.Mode = matching.TOLERANT, explain: bool = False, ignore_trash: bool = False) -> list:
    """The records of `diff LEFT RIGHT --only left` for the dates and media given, read from the databases."""
    try:
        data = [pp.library_photos(find_library(left)), pp.library_photos(find_library(right))]
    except pp.PowerPhotosError as error:
        fail(str(error))
    selected = [pp.of_media(within(d.visible + d.hidden, since, until), media.value) for d in data]
    hidden = [set(pp.of_media(within(d.hidden, since, until), media.value)) for d in data]
    bursts = [set(pp.of_media(within(d.bursts, since, until), media.value)) for d in data]
    missing = lacking_from(selected[0], selected[1], data[1], since, until, media, match, ignore_trash)
    if explain:
        explain_unmatched(missing, selected[1], match, right, data[1].trashed)
    rows = diff_rows(missing, Counter(), Side.left, DiffSort.date, {"left": hidden[0], "right": hidden[1]},
                     {"left": bursts[0], "right": bursts[1]}, {"left": data[0], "right": data[1]}, True)
    ids = {(key[0], key[2], int(key[1]) if key[1].isdigit() else None): data[0].uuids[key][0] for key in missing if data[0].uuids.get(key)}
    for row in rows:
        row["id"] = ids.get((row["date"], row["name"], row["size"]))  # the Photos identifier of one copy of it
    return rows


def folder_listing(folder: str, library: Optional[Path], since: Optional[datetime], until: Optional[datetime], media: Media, match: matching.Mode = matching.TOLERANT, refresh: bool = False, explain: bool = False, library_name: str = "the library", ignore_trash: bool = False) -> list:
    """The rows `import` reads for the photo and video files below folder (read again, not from the cache, with refresh), one for each photo, with the path of its file: all
    of them, or with a library only those the library lacks (judged as `copy --only-missing` judges, by the match mode); of
    files that are the same photo under the mode, one."""
    try:
        data = [folder_photos.folder_photos(Path(folder), refresh=refresh)] + ([pp.library_photos(library)] if library else [])
    except pp.PowerPhotosError as error:
        fail(str(error))
    selected = [pp.of_media(within(d.visible + d.hidden, since, until), media.value) for d in data]
    wanted = lacking_from(selected[0], selected[1], data[1], since, until, media, match, ignore_trash) if library else Counter(dict.fromkeys(selected[0], 1))
    # of files that are the same photo under the mode only one is imported, the one with the shortest name (the original, not
    # a copy that a service gave a longer name)
    kept = matching.dedupe(sorted(wanted, key=lambda photo: (len(photo[2]), photo[2], photo[0], photo[1])), match)
    if len(kept) < len(wanted):
        typer.echo(f"{len(wanted) - len(kept)} left out as the same photo as another file of {folder}, see --diff-mode", err=True)
    wanted = Counter({photo: 1 for photo in kept})
    if explain and library:  # of the photos that are left, not of the ones left out as the same photo
        explain_unmatched(wanted, selected[1], match, library_name, data[1].trashed)
    rows = diff_rows(wanted, Counter(), Side.left, DiffSort.date, {"left": set(), "right": set()}, {"left": set(), "right": set()},
                     {"left": data[0], "right": data[0]}, False, {"left": True, "right": False})
    return [
        {"name": row["name"], "date": row["date"], "hidden": False, "shared": False, "identifier": "", "source": "folder",
         "root": folder, "library": "", "files": row["files"]}
        for row in rows
    ]


def copy_command(
    source: SourceArgument,
    destination: Annotated[str, typer.Argument(help="Destination library; it must already exist in PowerPhotos.")],
    since: Annotated[Optional[datetime], typer.Option(help="First date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Last date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Copy photos, videos or both.")] = Media.all,
    list_file: Annotated[
        Optional[str],
        typer.Option("--list", help="Copy only the photos with the file names in this file (- for stdin): the output of `apple photos diff --format csv|json`, or one name per line. Not with --since, --until or --media."),
    ] = None,
    only_missing: Annotated[
        bool,
        typer.Option("--only-missing", help="Copy just the photos the source has and the destination lacks, worked out from the two databases as `apple photos diff SOURCE DESTINATION --only left` lists them, without PowerPhotos' duplicate check: the same as piping that diff into --list -. --since, --until and --media limit what is compared. Not with --list."),
    ] = False,
    check_duplicates: CheckDuplicatesOption = False,
    diff_mode: DiffModeOption = "",
    explain: ExplainOption = False,
    ignore_trash: IgnoreTrashOption = False,
    timeout: TimeoutOption = DEFAULT_TIMEOUT,
    ignore_blacklist: IgnoreBlacklistOption = False,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Copy nothing; say what would be copied. Same as DRY_RUN=true.")
    ] = False,
) -> None:
    """[macOS only] Copy the photos and videos in a date range, or the ones in a list, from one library to another with PowerPhotos.

    Photos already in the destination are skipped, so reruns add only what is new. Nothing is removed from the source.
    With --list, every photo in the source with a listed file name is copied, so a name that more than one photo has
    copies all of them. Names PowerPhotos does not list in the source are reported on stderr, and so are hidden items in the list
    (the hidden column of `diff`), which are left out. An empty list copies nothing and succeeds.
    A list is copied without PowerPhotos' comparison against the destination (--check-duplicates turns it on), so give it
    a list of what is missing, such as from `apple photos diff --only left`; a listed photo the destination has is copied
    again. --only-missing does that for you: it works out what the destination lacks and copies exactly that. A date range
    is always compared against the destination, since otherwise a rerun would copy it all again.
    """
    require_macos()
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    if only_missing and list_file is not None:
        raise typer.BadParameter("--only-missing works out what to copy itself, so it cannot be used with --list")
    match = parse_mode(diff_mode)
    if (explain or ignore_trash) and not only_missing:
        raise typer.BadParameter("--explain and --ignore-trash are for --only-missing: they are about the photos missing from the destination")
    if check_duplicates and list_file is None and not only_missing:
        raise typer.BadParameter("--check-duplicates is for --list or --only-missing: a date range is always compared against the destination")
    names, hidden, no_original = None, {}, {}
    ids = None
    if only_missing:
        rows = only_in_left(source, destination, since, until, media, match, explain, ignore_trash)
        names, hidden, no_original = split_records(rows)
        # copies are named by their Photos identifiers, one for each photo, so that a file name another photo shares, or a
        # second copy of the photo, is not carried over too
        listable = [row for row in rows if not is_true(row["hidden"]) and not is_true(row["missing"])]
        ids = [row["id"] for row in listable if row["id"]]
        for row in listable:
            if not row["id"]:
                typer.echo(f"  not copied: {row['date']} {row['name']} (the database of {source} gives it no identifier)", err=True)
        names = names if ids else []
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
    check = check_duplicates or names is None
    try:
        with pp.one_at_a_time():
            libraries = known_libraries()
            if ids is not None:  # copies by identifier leave out the photos on the blacklist
                known = read_library(libraries, source)
                key_of = {} if known is None else {uuids[0]: key for key, uuids in known.uuids.items()}
                kept = without_blacklisted(source, {listed: key_of[listed] for listed in ids if listed in key_of}, ignore_blacklist, say=True)
                ids = [listed for listed in ids if listed not in key_of or listed in kept]
                if not ids:
                    typer.echo(f"{destination}: no files copied (everything missing is left out, blacklisted)")
                    return
            if is_dry_run(dry_run):
                what = (
                    f"{len(ids)} photos of {source} that it lacks" if ids is not None
                    else f"the {len(names)} listed names of {source}" if names is not None
                    else f"the photos of {source}" + (f" from {since.date()}" if since else "") + (f" to {until.date()}" if until else "")
                    + " (PowerPhotos skips those it already has)"
                )
                typer.echo(f"{destination}: would copy {what} [dry run]")
                if names is not None and hidden:
                    report_hidden(hidden)
                if names is not None:
                    report_missing(no_original, source)
                return
            expected, uuid_of = expected_arrivals(libraries, source, destination, since, until, media, names, ids, match)
            before_log = powerphotos_reports.snapshot()
            result = copy_one(
                libraries, source, destination,
                since.date() if since else None,
                until.date() + timedelta(days=1) if until else None,
                media.value, on_upgrade, names, check, ids=ids,
            )
            typer.echo(f"{destination}: {describe_copy(result, check)}")
            if names is not None and hidden:
                report_hidden(hidden)
            if names is not None:
                report_missing(no_original, source)
            if ids is not None:
                names_of = {row["id"]: row["name"] for row in rows if row["id"]}
                report_unlisted([names_of.get(listed, listed) for listed in result.unlisted], source)
            else:
                report_unlisted(result.unlisted, source)
            if expected and result.sent is not None:
                listed = {name.lower() for name in result.unlisted}
                expected = Counter({key: 1 for key in expected if key[2].lower() not in listed})  # PowerPhotos could not copy these
                waited = wait_for_copy(libraries, destination, expected, match, timeout, before_log, uuid_of)
                settle_copy(destination, source, expected, waited, uuid_of, match, ignore_blacklist)
    except pp.Busy as error:
        fail(str(error))
    except CopyFailed:
        raise typer.Exit(1)


def expected_arrivals(
    libraries: dict, source: str, destination: str, since: Optional[datetime], until: Optional[datetime], media: Media,
    names: Optional[list], ids: Optional[list], match: matching.Mode,
) -> tuple:
    """The photos a copy is to add to the destination, worked out from the two databases before it starts: those the copy
    names (ids or file names) or, for a date range, those of the range that PowerPhotos can list, that the destination does
    not have, and each one's Photos identifier: (Counter, {key: identifier}). Empty when either database cannot be read, in
    which case the copy is not waited for."""
    wanted_from, held_in = read_library(libraries, source), read_library(libraries, destination)
    if wanted_from is None or held_in is None:
        return Counter(), {}
    if ids is not None:
        by_id = {uuids[0]: key for key, uuids in wanted_from.uuids.items()}
        wanted = Counter({by_id[listed]: 1 for listed in ids if listed in by_id})
    elif names is not None:
        listed = {pp.normalized_name(name) for name in names}
        wanted = Counter({key: 1 for key in wanted_from.visible if pp.normalized_name(key[2]) in listed and not wanted_from.original_missing(key)})
    else:
        wanted = Counter({key: 1 for key in pp.of_media(within(wanted_from.visible, since, until), media.value) if not wanted_from.original_missing(key)})
    expected = matching.absent(wanted, held_in.visible + held_in.hidden, match)
    return expected, {key: wanted_from.uuids[key][0] for key in expected if wanted_from.uuids.get(key)}


def read_library(libraries: dict, name: str) -> Optional[pp.Photos]:
    """The photos of a library in PowerPhotos, read from its database, or None when it is unknown or cannot be read."""
    if name not in libraries:
        return None
    try:
        return pp.library_photos(libraries[name])
    except pp.PowerPhotosError:
        return None


def read_photos(libraries: dict, name: str) -> Optional[Counter]:
    """The photos a library holds (see read_library), visible or hidden: a photo that is hidden in a library is there, or
    None."""
    found = read_library(libraries, name)
    return None if found is None else found.visible + found.hidden


def adjacent_libraries(plan_of, kind: str, year: int, part: split_plan.Part, source_photos: Optional[Counter]) -> list:
    """The names of the libraries beside a part, on each end the source has a photo within a day of (and none otherwise): the
    part before it when something is near its start, the part after it when something is near its end. plan_of(kind, year)
    gives the parts of a year in order."""
    if source_photos is None:
        return []
    near_start, near_end = split_plan.edge_ends(source_photos, part.start, part.end, kind)
    if not (near_start or near_end):
        return []
    ordered = [other for y in (year - 1, year, year + 1) for other in plan_of(kind, y)]
    at = next((index for index, other in enumerate(ordered) if other.name == part.name), None)
    if at is None:
        return []
    return ([ordered[at - 1].name] if near_start and at > 0 else []) + ([ordered[at + 1].name] if near_end and at + 1 < len(ordered) else [])


class Libraries:
    """The databases of the libraries a run looks at, each read once, and again only after the library was changed. Over a
    network share a read copies the database, so this is the slow part of a run: preload() reads the ones needed at the start
    several at a time, with a progress line. before_held() is a library as it was when first read, whatever was done to it
    since (the libraries either side of one that is being fixed, as split sees them)."""

    WORKERS = 8

    def __init__(self, libraries: dict):
        self.libraries = libraries
        self._now: dict = {}
        self._first: dict = {}

    def _read(self, name: str) -> tuple:
        found = read_library(self.libraries, name)
        return found, None if found is None else found.visible + found.hidden

    def preload(self, names: list) -> None:
        """Reads the libraries (those in PowerPhotos that were not read yet) in parallel."""
        todo = [name for name in dict.fromkeys(names) if name in self.libraries and name not in self._now]
        if not todo:
            return
        progress, started, done = Progress(), time.monotonic(), 0
        with ThreadPoolExecutor(max_workers=min(self.WORKERS, len(todo))) as pool:
            futures = {pool.submit(self._read, name): name for name in todo}
            for future in as_completed(futures):
                self._now[futures[future]] = self._first[futures[future]] = future.result()
                done += 1
                progress.update(f"Reading the libraries: {done} of {len(todo)} ...")
        if len(todo) > 1:
            progress.line(f"Read {len(todo)} library databases ({duration(time.monotonic() - started)})")

    def photos(self, name: str) -> Optional[pp.Photos]:
        if name not in self._now:
            self._now[name] = self._first[name] = self._read(name)
        return self._now[name][0]

    def held(self, name: str) -> Optional[Counter]:
        """What the library holds now, visible or hidden (None: not in PowerPhotos, or unreadable)."""
        self.photos(name)
        return self._now[name][1]

    def before_held(self, name: str) -> Optional[Counter]:
        self.photos(name)
        return self._first[name][1]

    def refresh(self, name: str) -> None:
        """The library was changed: read it again when it is next looked at."""
        self._now[name] = self._read(name)


def check_part(libs: Libraries, part: split_plan.Part, kind: str, year: int, source_data: Optional[pp.Photos], nearby: list, label: Optional[str] = None, match: matching.Mode = matching.TOLERANT, blacklisted=None) -> tuple:
    """Says whether the library of a part holds only photos of its year and, with a source, all its photos of the part's
    dates; without one, that it holds some photos of the year. Lists what is outside the year and what is missing (see
    split_command, --check). Also lists what is of the other kind (a video in a photos library) and what is of neither.
    Returns (whether it is in order, the items to move to another library, the library's photos or None, the source's photos
    it lacks, the source's photos within a day of the library's dates that no library holds): the items to move are those
    outside the year, and those of the other kind, which belong in the other kind's library. nearby names the libraries either
    side (as they were when first read), which matter only when the source has photos within a day of the dates' ends."""
    label = label or part.name
    if part.name not in libs.libraries:
        typer.echo(f"{label}: library missing")
        return False, Counter(), None, Counter(), Counter()
    own = libs.photos(part.name)
    if own is None:
        typer.echo(f"{label}: cannot be read from its database")
        return False, Counter(), None, Counter(), Counter()
    held = own.visible + own.hidden
    outside, near_ends = split_plan.outside_year(held, year, kind)
    other_kind = Counter({key: number for key, number in held.items() if pp.media_of(key[2]) not in (None, kind)})
    unknown = Counter({key: number for key, number in held.items() if pp.media_of(key[2]) is None})
    if source_data is None:
        lacking = uncertain = Counter()
    else:
        found = split_plan.missing_in_range(source_data.visible, part.start, part.end, kind, libs.held(part.name), [libs.before_held(name) for name in nearby], match)
        lacking, uncertain = found if found is not None else (Counter(), Counter())
    problems = []
    if source_data is None and not split_plan.count_in_range(held, date(year, 1, 1), date(year + 1, 1, 1), kind):
        problems.append(f"holds no {kind} from {year}")
    if outside:
        by_year = Counter()
        for key, number in outside.items():
            by_year[key[0][:4]] += number
        problems.append(f"{sum(outside.values())} outside {year} ({', '.join(f'{when} {number}' for when, number in sorted(by_year.items()))})")
    if other_kind:
        problems.append(f"{sum(other_kind.values())} {'video' if kind == 'photos' else 'photo'}{'' if sum(other_kind.values()) == 1 else 's'} in a {kind} library")
    if unknown:
        problems.append(f"{sum(unknown.values())} of a type that is neither photo nor video")
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
    for key in sorted(other_kind.elements()):
        typer.echo(f"    wrong kind: {key[0]} {key[2]}")
    for key in sorted(unknown.elements()):
        typer.echo(f"    unknown type: {key[0]} {key[2]}")
    for key in sorted(lacking.elements()):
        since = blacklisted(key) if blacklisted else None  # the date it was blacklisted, or None
        typer.echo(f"    missing: {key[0]} {key[2]}" + lookalikes(key, own) + (f" [blacklisted on {since}]" if since else ""))
    return not problems, outside + other_kind, own, lacking, uncertain


def lookalikes(key: tuple, photos: pp.Photos) -> str:
    """For a photo a library is said to lack, what it holds under the same file name, so that a copy that differs in date or
    size shows: ' (it has this name: dated ..., N bytes)' or '' when nothing has the name."""
    name = pp.normalized_name(key[2])
    found = sorted((other for other in photos.visible + photos.hidden if pp.normalized_name(other[2]) == name), key=lambda other: other[0])
    if not found:
        return ""
    shown = [
        " ".join(part for part in (f"dated {other[0]}" if other[0] != key[0] else "", f"{other[1]} bytes" if other[1] != key[1] else "") if part) or "the same date and size"
        for other in found[:3]
    ]
    return f" (the library has this name: {'; '.join(shown)}{f'; and {len(found) - 3} more' if len(found) > 3 else ''})"


def wait_for_library(libraries: dict, library: str, done, what: str, timeout: float = DEFAULT_TIMEOUT, progress=None) -> Counter:
    """The items a library holds once done(held) is true of them, or when it stops changing without getting there.
    PowerPhotos can hand back control of a copy or a delete while it is still writing, so this looks at the library's
    database until the change has landed, for as long as the database keeps changing (its item count and the modification
    times of its files), and gives up when it has not changed for timeout minutes. what is said when it has to wait, and
    progress(held), if given, says how far it has got about every minute."""
    path = libraries.get(library)
    files = [path / "database" / "Photos.sqlite", path / "database" / "Photos.sqlite-wal"] if path else []
    every = max(1, round(SECONDS_PER_MINUTE / max(POLL_SECONDS, 1)))  # looks between progress lines
    patience = max(1, round(timeout * SECONDS_PER_MINUTE / max(POLL_SECONDS, 1)))  # looks at an unchanged library
    waited = False
    previous, idle, looks = None, 0, 0
    while True:
        found = read_library(libraries, library)
        held = Counter() if found is None else found.visible + found.hidden
        if done(held):
            return held
        signature = (sum(held.values()), [item.stat().st_mtime_ns if item.exists() else 0 for item in files])
        idle = idle + 1 if signature == previous else 0
        if idle >= patience:
            return held
        if not waited:
            typer.echo(f"    waiting for PowerPhotos to finish {what} ...", err=True)
            waited = True
        elif progress is not None and looks % every == 0:
            typer.echo(f"    still waiting: {progress(held)}", err=True)
        previous = signature
        looks += 1
        time.sleep(POLL_SECONDS)


def blacklist_entries() -> list:
    try:
        return blacklist.entries()
    except blacklist.BlacklistError as error:
        fail(str(error))


def without_blacklisted(source: str, key_of: dict, ignore: bool, say: bool = False) -> dict:
    """key_of (a photo's Photos identifier -> its key) without the photos on the blacklist for copies from source, unless ignore
    (--ignore-blacklist). With say, how many were left out is said (the check says which, on the `missing:` lines)."""
    if ignore:
        return key_of
    known = {(entry["library"], entry["uuid"]): entry for entry in blacklist_entries() if entry["kind"] == blacklist.UNCOPYABLE}
    kept, skipped = {}, []
    for uuid, key in key_of.items():
        entry = known.get((source, uuid))
        if entry:
            skipped.append((key, entry))
        else:
            kept[uuid] = key
    if skipped and say:
        typer.echo(f"    {count(len(skipped), 'photo')} left out, blacklisted (--ignore-blacklist tries them again)")
    return kept


class Waited(NamedTuple):
    """held: the library's items when the wait ended. failed: {key: why} of the photos PowerPhotos says it could not copy.
    error: what PowerPhotos says stopped the copy as a whole, or None."""
    held: Counter
    failed: dict
    error: Optional[str]


def wait_for_copy(libraries: dict, library: str, expected: Counter, match: matching.Mode, timeout: float, before: set, uuids: dict) -> Waited:
    """Waits for the photos of expected (a key each) to arrive in library, as wait_for_library does, and watches the report
    PowerPhotos writes for the copy (see powerphotos_reports; before is the reports there were before the request, uuids a
    photo's key -> its Photos identifier): a photo it records a failure for is not waited for, and an error that stopped the
    operation ends the wait."""
    by_uuid = {uuid.split("/")[0].upper(): key for key, uuid in uuids.items()}
    seen = {"report": None, "failed": {}, "error": None}

    def look() -> None:
        if seen["report"] is None:
            seen["report"] = powerphotos_reports.find(before, library)
        report = powerphotos_reports.read(seen["report"]) if seen["report"] else None
        if report is not None:
            seen["failed"] = {by_uuid[uuid]: why for uuid, why in report.failed.items() if uuid in by_uuid and by_uuid[uuid] in expected}
            seen["error"] = report.error

    def done(held: Counter) -> bool:
        look()
        if seen["error"]:
            return True
        return not matching.absent(Counter({key: 1 for key in expected if key not in seen["failed"]}), held, match)

    def progress(held: Counter) -> str:
        return f"{len(expected) - len(matching.absent(expected, held, match))} of {len(expected)} photos have arrived in {library}"

    held = wait_for_library(libraries, library, done, f"copying into {library}", timeout, progress)
    look()
    return Waited(held, seen["failed"], seen["error"])


def settle_copy(library: str, source: str, expected: Counter, waited: Waited, uuids: dict, match: matching.Mode, ignore: bool) -> None:
    """After a copy was waited for (wait_for_copy): says what stopped it if PowerPhotos reports an error for the operation
    and stops; otherwise says which photos PowerPhotos reports a failure for and blacklists them as uncopyable (they will be
    left out of later copies), forgets a retried photo that copied, and, if photos that were not reported failed did not
    arrive either, says so and stops (report_copy_outcome)."""
    if waited.error:
        typer.echo(f"    PowerPhotos could not copy into {library}: {waited.error}")
        raise CopyFailed(library)
    absent = matching.absent(expected, waited.held, match)
    for key, why in sorted(waited.failed.items()):
        uuid = uuids.get(key)
        reason = why if len(why) <= 300 else why[:297] + "..."
        typer.echo(
            f"    PowerPhotos could not copy {key[0]} {key[2]} ({reason})"
            + ("; it is blacklisted and left out from now on (--ignore-blacklist tries it again)" if uuid else "")
        )
        if uuid:
            try:
                blacklist.add(blacklist.UNCOPYABLE, source, uuid, key[2], key[0], reason)
            except blacklist.BlacklistError as error:
                fail(str(error))
    if ignore:
        for key in expected:
            if key not in absent and uuids.get(key):
                try:
                    blacklist.remove(blacklist.UNCOPYABLE, source, uuids[key])
                except blacklist.BlacklistError as error:
                    fail(str(error))
    report_copy_outcome(library, len(expected) - len(waited.failed), len(expected) - len(absent))


def report_not_listed(photos: pp.Photos, uuids: list, key_of: dict, library: str) -> None:
    """Says which photos PowerPhotos listed no photo for in a library, and why that is: it does not list hidden items or ones
    whose original is not stored in the library, so it cannot copy them. Items Photos salvaged from a damaged database (its
    Recovered album) are not listed either, and the database does not mark them, so they get the unknown reason, which says so."""
    if not uuids:
        return
    typer.echo(f"    {len(uuids)} not copied: the database of {library} has them, but PowerPhotos lists no photo with their identifiers, so it cannot copy them:")
    for uuid in uuids:
        key = key_of.get(uuid)
        if key is None:
            typer.echo(f"      {uuid}")
            continue
        reason = (
            "hidden in Photos" + (", a burst member" if key in photos.bursts else "") if key in photos.hidden
            else "its original is not stored in the library" if photos.original_missing(key) else "no reason found in the database; if Photos lists it under Recovered, restore it there"
        )
        typer.echo(f"      {key[0]} {key[2]} ({reason})")


class CopyFailed(Exception):
    """Photos PowerPhotos was asked to copy into a library did not arrive: the cause is likely to be the same for the next
    library, and what was confirmed so far should not be built on, so --fix stops."""

    def __init__(self, library: str):
        super().__init__(library)
        self.library = library


def report_copy_outcome(library: str, requested: int, arrived: int) -> None:
    """Says so when photos PowerPhotos was asked to copy into a library did not arrive. PowerPhotos does not report a failed
    copy to the script: it answers at once and shows the failure in its own window and Reports section, so all the script
    can see is that the library did not change. Raises CopyFailed when any did not arrive."""
    if arrived >= requested:
        return
    if not arrived:
        typer.echo(
            f"    none of the {count(requested, 'photo')} arrived in {library}: PowerPhotos probably failed to copy them. Its Reports section "
            "has the reason. If that says Photos returned an internal error (PHPhotosErrorDomain Code=-1) for photos that import "
            "fine by hand, quit Photos and PowerPhotos, run `killall photolibraryd` (or restart the Mac), and try again."
        )
    else:
        typer.echo(f"    {requested - arrived} of the {count(requested, 'photo')} did not arrive in {library}: see PowerPhotos' Reports section for why.")
    raise CopyFailed(library)


def copy_missing(libraries: dict, source: str, part: split_plan.Part, kind: str, lacking: Counter, source_data: pp.Photos, on_upgrade: OnUpgrade, dry: bool, match: matching.Mode = matching.TOLERANT, timeout: float = DEFAULT_TIMEOUT, ignore_blacklist: bool = False) -> bool:
    """--fix with a source: copies the source's photos of the library's dates that it lacks into it (without PowerPhotos'
    duplicate check, since the databases say what it lacks), named by their Photos identifiers, and waits for them to
    arrive. Photos with no stored original are left out and said, and so are items PowerPhotos lists no photo for (hidden);
    a copy that did not arrive is said (see report_copy_outcome). Returns whether anything was copied."""
    key_of = {}
    no_original = {}
    for key in sorted(lacking):
        if source_data.original_missing(key):  # nothing to copy: Photos keeps a placeholder (iCloud only, or shared and never saved)
            no_original[key[2]] = no_original.get(key[2], False) or key in source_data.shared
        elif source_data.uuids.get(key):
            key_of[source_data.uuids[key][0]] = key  # one copy of each: copies of a photo are not carried over
        else:
            typer.echo(f"    not copied: {key[0]} {key[2]} (the database of {source} gives it no identifier)")
    report_missing(no_original, source)
    key_of = without_blacklisted(source, key_of, ignore_blacklist)
    if not key_of:
        return False
    if dry:
        typer.echo(f"    would copy {count(len(key_of), 'item')} that {part.name} lacks from {source}")
        return False
    before = powerphotos_reports.snapshot()
    result = copy_one(libraries, source, part.name, None, None, kind, on_upgrade, check_duplicates=False, ids=list(key_of))
    if result.sent is None:
        typer.echo(f"    {part.name}: skipped (needs a Photos upgrade)")
        return False
    report_not_listed(source_data, result.unlisted, key_of, source)
    wanted = Counter(dict.fromkeys(key_of.values(), 1))
    uuid_by_key = {key: uuid for uuid, key in key_of.items()}
    waited = wait_for_copy(libraries, part.name, wanted, match, timeout, before, uuid_by_key)
    typer.echo(f"    {part.name}: asked PowerPhotos to copy {count(len(key_of), 'item')} from {source}")
    settle_copy(part.name, source, wanted, waited, uuid_by_key, match, ignore_blacklist)
    return True


def fix_strays(libraries: dict, part: split_plan.Part, kind: str, outside: Counter, own: pp.Photos, parts_of, on_upgrade: OnUpgrade, dry: bool, match: matching.Mode = matching.TOLERANT, timeout: float = DEFAULT_TIMEOUT, ignore_blacklist: bool = False) -> bool:
    """--fix: moves the items outside the year of a library into the library of their own year. The right library ends with
    one copy of each (a photo it lacks, judged as the check judges it, so a copy that differs by an hour or a little in size
    is there, is copied once, from its Photos identifier, without PowerPhotos' duplicate check; copies of a photo are not
    carried over), and this library with none: once the right library's database shows the photo is there,
    every copy of it is moved out of this one, all in one delete after every copy has been confirmed, to its Recently
    Deleted (PowerPhotos' delete), so that PowerPhotos asks once however many years the strays come from. An item is left
    alone, and said so, when its year has no library, when it cannot be told which part of a capped year it belongs to,
    or when the database gives it no identifier. Returns whether anything was removed from this library."""
    wanted: dict = {}
    left_alone = []
    for key in sorted(outside.elements()):
        item_date = date.fromisoformat(key[0][:10])
        target_kind = pp.media_of(key[2]) or kind  # a video in a photos library belongs in the videos library of its year
        candidates = parts_of(target_kind, item_date.year)
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
        names_of.update({uuid: key[2] for key in keys for uuid in identifiers[key]})
        # What the target library already holds is read from its database, so PowerPhotos' own duplicate check (slow in a
        # large library) is not needed: only the items it lacks are copied.
        before = read_library(libraries, target)
        held = Counter() if before is None else before.visible + before.hidden
        wanted_here = Counter(dict.fromkeys(keys, 1))
        lacking = [key for key in keys if key in matching.absent(wanted_here, held, match)]  # as the check judges: a near copy is there
        stuck = [key for key in lacking if own.original_missing(key)]  # no original is stored, so there is nothing to copy
        copyable = [key for key in lacking if key not in stuck]
        for key in stuck:
            typer.echo(f"      not copied to {target}, so it stays in {part.name}: {key[0]} {key[2]} (its original is not stored in {part.name})")
        copyable = list(without_blacklisted(part.name, {identifiers[key][0]: key for key in copyable}, ignore_blacklist).values())  # one copy each: duplicates are not carried over
        to_copy = [identifiers[key][0] for key in copyable]
        before_log = powerphotos_reports.snapshot()
        if to_copy:
            result = copy_one(libraries, part.name, target, None, None, kind, on_upgrade, check_duplicates=False, ids=to_copy)
            if result.sent is None:
                typer.echo(f"    {target}: skipped (needs a Photos upgrade)")
                continue
            report_not_listed(own, result.unlisted, {uuid: key for key in keys for uuid in identifiers[key]}, part.name)
        arriving = Counter(dict.fromkeys(copyable, 1))
        uuid_by_key = {key: identifiers[key][0] for key in copyable}
        waited = wait_for_copy(libraries, target, arriving, match, timeout, before_log, uuid_by_key) if copyable else Waited(held, {}, None)
        now = waited.held
        still_lacking = matching.absent(wanted_here, now, match)
        confirmed = [key for key in keys if key not in still_lacking]
        typer.echo(f"    {target}: {count(len(keys) - len(lacking), 'item')} already there, copied {count(len(copyable), 'item')}; {len(confirmed)} of {len(keys)} confirmed in its database")
        for key in keys:
            if key not in confirmed:
                typer.echo(f"      NOT confirmed in {target}, so it stays in {part.name}: {key[0]} {key[2]}")
        if copyable:
            settle_copy(target, part.name, arriving, waited, uuid_by_key, match, ignore_blacklist)
        confirmed_everywhere += [(key, identifiers[key]) for key in confirmed]
    if not confirmed_everywhere:
        return False
    # Removed once, after every copy is confirmed: PowerPhotos asks to confirm each delete, and a library with strays of
    # several years would otherwise ask once per year.
    deleted, missing = trash_ids(libraries, part.name, [uuid for _, uuids in confirmed_everywhere for uuid in uuids])
    moved = [key for key, _ in confirmed_everywhere]
    wait_for_library(libraries, part.name, lambda held: not any(held[key] for key in moved), f"deleting from {part.name}", timeout)
    typer.echo(f"    {part.name}: moved {deleted} to its Recently Deleted: " + ", ".join(key[2] for key, _ in confirmed_everywhere))
    for uuid in missing:
        typer.echo(f"      not moved out of {part.name} (PowerPhotos lists no photo with that identifier, as it does not list hidden items): {names_of.get(uuid, uuid)}")
    return deleted > 0


def check_years(libraries: dict, kinds: list, first_year: int, last_year: int, destination_name: str, source_data: Optional[pp.Photos], max_bytes: Optional[int], fix: Optional[dict] = None, match: matching.Mode = matching.TOLERANT, timeout: float = DEFAULT_TIMEOUT, ignore_blacklist: bool = False, source: Optional[str] = None) -> bool:
    """--check: for each year and kind, the libraries photos would be split into (those the source's photos need, or without
    a source those that exist: the name, or name-1, name-2 ...), each checked by check_part. A year with no library
    that needs none (the source has nothing in it) or, without a source, has none, is named at the end. True when every
    library is in order."""
    libs = Libraries(libraries)
    known = {} if ignore_blacklist or source is None else {
        (entry["library"], entry["uuid"]): entry for entry in blacklist_entries() if entry["kind"] == blacklist.UNCOPYABLE
    }

    def blacklisted_on(key: tuple) -> Optional[str]:
        """The date the source's photo was blacklisted for copies, or None (also when --ignore-blacklist is given)."""
        entry = known.get((source, (source_data.uuids.get(key) or [None])[0])) if source_data is not None else None
        return entry["recorded"][:10] if entry else None

    def parts_of(kind: str, year: int) -> list:
        name = destination_name.format(media=kind, year=year)
        if source_data is not None:
            return split_plan.plan_year(source_data.visible, year, kind, name, max_bytes)
        found = [existing for existing in sorted(libraries) if existing == name or re.fullmatch(re.escape(name) + r"-\d+", existing)]
        return [split_plan.Part(existing, date(year, 1, 1), date(year + 1, 1, 1), 0) for existing in found]

    def neighbors(kind: str, year: int, part: split_plan.Part) -> list:
        """The libraries beside a part, which matter only on an end the source has a photo within a day of."""
        return adjacent_libraries(parts_of, kind, year, part, None if source_data is None else source_data.visible)

    # Only the years asked for are read, and a library either side of one only when something near its ends needs it; all of
    # them at once, before the first is looked at, so that what the neighbors held is what they held before any was fixed.
    wanted = [
        name for kind in kinds for year in range(first_year, last_year + 1) for part in parts_of(kind, year)
        for name in [part.name, *neighbors(kind, year, part)]
    ]
    libs.preload(wanted)
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
                nearby = neighbors(kind, year, part)
                ok, outside, own, lacking, uncertain = check_part(libs, part, kind, year, source_data, nearby, match=match, blacklisted=blacklisted_on)
                if fix is not None and own is not None:
                    to_copy = lacking + uncertain  # as split copies them: what is lacking, and what is near the ends and in no library
                    changed = copy_missing(libraries, fix["source"], part, kind, to_copy, source_data, fix["on_upgrade"], fix["dry"], match, timeout, ignore_blacklist) if to_copy else False
                    changed = bool(outside) and fix_strays(libraries, part, kind, outside, own, parts_of, fix["on_upgrade"], fix["dry"], match, timeout, ignore_blacklist) or changed
                    if changed:
                        libs.refresh(part.name)  # it has changed
                        ok, _, _, _, _ = check_part(libs, part, kind, year, source_data, nearby, f"{part.name} (after the fix)", match, blacklisted_on)
                in_order = ok and in_order
    if nothing:
        typer.echo(f"No library, and none needed: {', '.join(sorted(set(nothing)))}")
    return in_order


def split_command(
    media: Annotated[Media, typer.Option(help="Split photos, videos or both. With all, photos and videos go to their own libraries (photos-2018 and videos-2018), in the same run.")],
    years: Annotated[str, typer.Option(help="One year or a range, such as 2018 or 2018-2020.")],
    source: Annotated[Optional[str], typer.Argument(metavar="[SOURCE]", help="Source library, as named by `./apple photos libraries`. Needed unless --check is given: `-` is no source, to give a DESTINATION to --check without one.")] = None,
    destination_name: Annotated[
        str, typer.Argument(metavar="[DESTINATION]", help="Destination library name for each year; {media} and {year} are filled in.")
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
    check_duplicates: CheckDuplicatesOption = False,
    diff_mode: DiffModeOption = "",
    timeout: TimeoutOption = DEFAULT_TIMEOUT,
    ignore_blacklist: IgnoreBlacklistOption = False,
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
            "there from that library's database, then move it out of the wrong library to that library's Recently Deleted; and, with "
            "a SOURCE, copy into each library the source's photos of its dates that it lacks. --dry-run shows what would be done.",
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

    Each library gets the photos it lacks: they are worked out from the databases and copied, one copy of each, named by
    their Photos identifiers and without PowerPhotos' duplicate check, so a file name another photo shares or a second
    copy of the photo is not carried over. A photo within a day of the start or end of a library's dates, whose library
    the databases cannot settle, is copied into every library it could belong to that lacks it, so it can end up in
    two. --check-duplicates copies the whole date range instead and lets PowerPhotos skip what the library has.

    --check copies nothing: it reads the databases and, for each library, lists the photos dated outside its year
    (strays) and the source's photos of its dates that it lacks, so it can be run to see that photos-2005 really holds
    the photos of 2005 and only those. A photo within a day of the year's ends is not a stray, since the databases keep
    UTC dates and PowerPhotos files by local date; one that close that the library lacks is reported as near the ends
    and does not fail the check. Exits 1 when any library has strays, lacks photos or is not in PowerPhotos.

    A photo of the other kind (a video in photos-2005) or of neither kind (a type not in the photo or video lists) is listed
    too, and fails the check.

    --fix (it implies --check) puts that right, as far as it safely can. With a SOURCE it copies into each library the
    source's photos of its dates that the library lacks. It moves the strays, and the items of the other kind, to the
    library of their own year and kind: each is copied there if that library lacks it (judged from its database, so
    without PowerPhotos' duplicate check), and once that library's database shows every copy has arrived, all of them are
    moved out of the wrong library in one delete, to its Recently Deleted (PowerPhotos' delete; they can be put back for
    30 days, and nothing is removed from disk). Photos are named by their Photos identifiers, so a shared file name does
    not matter. It waits for PowerPhotos to finish each copy and delete, and then checks the library again. An item is
    left alone, and said so, when its year has no library, when it is not clear which part of a capped year it belongs
    to, or when the database gives it no identifier; one PowerPhotos lists no photo for (hidden, or with no stored
    original) is said with its reason.
    """
    require_macos()
    check = check or fix
    match = parse_mode(diff_mode)
    if source == "-":
        source = None
    if source is None and not check:
        raise typer.BadParameter("SOURCE is needed (it is optional only with --check)")
    first, _, last = years.partition("-")
    if not (first.isdigit() and (last.isdigit() or not last)):
        raise typer.BadParameter("--years must be a year or a range such as 2018-2020")
    first_year, last_year = int(first), int(last or first)
    if last_year < first_year:
        raise typer.BadParameter("--years ends before it starts")
    if not check_duplicates and not skip_complete:
        raise typer.BadParameter("--no-skip-complete copies every year whole and lets PowerPhotos skip what is there, so it needs --check-duplicates")
    if check and (check_duplicates or not skip_complete):
        raise typer.BadParameter("--check works out what is missing and copies nothing, so it cannot be used with --check-duplicates or --no-skip-complete")
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
                in_order = check_years(libraries, kinds, first_year, last_year, destination_name, source_data, max_bytes,
                                       {"on_upgrade": on_upgrade, "dry": dry, "source": source} if fix else None, match, timeout, ignore_blacklist, source)
                if not in_order:
                    raise typer.Exit(1)
                return
            source_photos = None if source_data is None else source_data.visible
            if source_data is not None and sum(source_data.superseded.values()):
                typer.echo(
                    f"{source}: {count(sum(source_data.superseded.values()), 'shared placeholder')} with an imported copy in the same "
                    "library not counted.", err=True,
                )
            if (not check_duplicates or max_bytes) and source_photos is None:
                fail(f"The photos of {source} cannot be read from its database, so " + (
                    "what is missing cannot be worked out." if not check_duplicates else "its size cannot be worked out."))
            plans = {}

            def plan(kind: str, year: int) -> list:
                if (kind, year) not in plans:
                    name = destination_name.format(media=kind, year=year)
                    plans[(kind, year)] = split_plan.plan_year(source_photos, year, kind, name, max_bytes)
                return plans[(kind, year)]

            libs = Libraries(libraries)

            def nearby_names(kind: str, year: int, part: split_plan.Part) -> list:
                """The libraries beside a part, which matter only on an end the source has a photo within a day of."""
                return adjacent_libraries(plan, kind, year, part, source_photos)

            work = [part for kind in kinds for year in range(first_year, last_year + 1) for part in plan(kind, year)]
            absent = [part.name for part in work if part.name not in libraries]
            if absent and on_missing == OnMissing.stop and not dry:
                fail(f"Not in PowerPhotos: {', '.join(absent)}. Create them there, or use --on-missing skip.")
            if source_photos is not None:  # what each library holds is read once, all at the start, and only for the years asked for
                libs.preload([
                    name for kind in kinds for year in range(first_year, last_year + 1) for part in plan(kind, year)
                    for name in [part.name, *nearby_names(kind, year, part)]
                ])
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
                            near = [libs.before_held(name) for name in nearby_names(kind, year, part)]
                            found = split_plan.missing_in_range(source_photos, part.start, part.end, kind, libs.held(part.name), near, match)
                            if found is not None and not found[0] and not found[1]:
                                typer.echo(f"{label}: up to date{note}")
                                continue
                        if dry:
                            typer.echo(f"{label}: would copy{note}")
                            continue
                        no_original = {}
                        expected = Counter()  # the photos whose arrival in the library is waited for
                        uuid_of = {}  # a photo's key -> its Photos identifier, to record one that fails
                        before_log = powerphotos_reports.snapshot()
                        if not check_duplicates:
                            if found is None:
                                fail(f"The photos of {part.name} cannot be read from its database, so what is missing cannot be worked out.")
                            # One copy of each photo the library lacks, named by its Photos identifier (so a file name another photo
                            # shares, or a second copy of the photo, is not carried over); near the ends too: any library will do.
                            ids, names_of, keys_of = [], {}, {}
                            for key in sorted(found[0] + found[1]):
                                if source_data is not None and source_data.original_missing(key):
                                    no_original[key[2]] = no_original.get(key[2], False) or key in source_data.shared
                                elif source_data is not None and source_data.uuids.get(key):
                                    ids.append(source_data.uuids[key][0])
                                    names_of[ids[-1]] = key[2]
                                    keys_of[ids[-1]] = key
                                else:
                                    typer.echo(f"    not copied: {key[0]} {key[2]} (the database of {source} gives it no identifier)")
                            keys_of = without_blacklisted(source, keys_of, ignore_blacklist, say=True)
                            ids = [listed for listed in ids if listed in keys_of]
                            if not ids:
                                typer.echo(f"{label}: nothing to copy{note}")
                                report_missing(no_original, source)
                                continue
                            result = copy_one(
                                libraries, source, part.name, None, None, kind, on_upgrade, check_duplicates=False, ids=ids
                            )
                            expected = Counter(dict.fromkeys((key for uuid, key in keys_of.items() if uuid not in result.unlisted), 1))
                            uuid_of = {key: uuid for uuid, key in keys_of.items()}
                            result = CopyResult(result.sent, [names_of.get(listed, listed) for listed in result.unlisted])
                        else:
                            result = copy_one(libraries, source, part.name, part.start, part.end, kind, on_upgrade)
                            if found is not None and source_data is not None:  # what PowerPhotos is to add: the photos it can list that the library lacks
                                expected = Counter(dict.fromkeys((key for key in found[0] if not source_data.original_missing(key)), 1))
                                uuid_of = {key: source_data.uuids[key][0] for key in expected if source_data.uuids.get(key)}
                        typer.echo(f"{label}: {describe_copy(result, check_duplicates)}{note}")
                        report_missing(no_original, source)
                        report_unlisted(result.unlisted, source)
                        if expected and result.sent is not None:
                            waited = wait_for_copy(libraries, part.name, expected, match, timeout, before_log, uuid_of)
                            settle_copy(part.name, source, expected, waited, uuid_of, match, ignore_blacklist)
    except pp.Busy as error:
        fail(str(error))
    except CopyFailed as stopped:
        typer.echo(
            f"Stopped: the copy into {stopped.library} did not complete, so the libraries after it were not touched, and nothing more was "
            "removed from any library. Fix the cause, then run this again: what was already copied is kept and the rerun carries on from there."
        )
        raise typer.Exit(1)
