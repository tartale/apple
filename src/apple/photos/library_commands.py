"""`./apple photos libraries|copy|split`: move photos between Photos libraries with PowerPhotos (macOS only)."""
import sys
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


def copy_one(
    libraries: dict, source: str, destination: str, since: Optional[date], before: Optional[date],
    media: str, mode: OnUpgrade,
) -> Optional[int]:
    """Copies one date range; returns how many photos matched, or None when the destination was skipped."""
    source_path = need(libraries, source)
    destination_path = need(libraries, destination)
    source_version = pp.model_version(source_path)
    destination_version = pp.model_version(destination_path)
    if source_version is not None and destination_version is not None and destination_version < source_version:
        reason = f"It is in an older format than {source}."
        if not resolve_upgrade(destination, destination_path, source_version, mode, reason):
            return None
    args = [source, destination, since.isoformat() if since else "-", before.isoformat() if before else "-", media]
    while True:
        try:
            output = pp.run_applescript("copy-photos.applescript", *args)
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
    libraries = known_libraries()
    with ThreadPoolExecutor(max_workers=8) as pool:  # an exact read can mean copying a database over the network; overlap them
        versions = dict(zip(libraries, pool.map(lambda path: pp.model_version(path, exact=outdated), libraries.values())))
    if not outdated:
        for name, path in libraries.items():
            version = versions[name]
            typer.echo(f"{name}\t{path}\t{version if version is not None else 'unknown'}")
        return

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


def copy_command(
    source: SourceOption,
    destination: Annotated[str, typer.Option("--to", help="Destination library; it must already exist in PowerPhotos.")],
    since: Annotated[Optional[datetime], typer.Option(help="First date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    until: Annotated[Optional[datetime], typer.Option(help="Last date to copy (YYYY-MM-DD).", formats=DateFormat)] = None,
    media: Annotated[Media, typer.Option(help="Copy photos, videos or both.")] = Media.all,
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
) -> None:
    """[macOS only] Copy the photos and videos in a date range from one library to another with PowerPhotos.

    Photos already in the destination are skipped, so reruns add only what is new. Nothing is removed from the source.
    """
    require_macos()
    if since and until and until < since:
        raise typer.BadParameter("--until is before --since")
    try:
        with pp.one_at_a_time():
            found = copy_one(
                known_libraries(), source, destination,
                since.date() if since else None,
                until.date() + timedelta(days=1) if until else None,
                media.value, on_upgrade,
            )
    except pp.Busy as error:
        fail(str(error))
    typer.echo(f"{destination}: skipped (needs a Photos upgrade)" if found is None else f"{destination}: found {found}")


def split_command(
    source: SourceOption,
    media: Annotated[SplitMedia, typer.Option(help="Split photos or videos.")],
    years: Annotated[str, typer.Option(help="One year or a range, such as 2018 or 2018-2020.")],
    destination_name: Annotated[
        str, typer.Option("--to", help="Destination library name for each year; {media} and {year} are filled in.")
    ] = "{media}-{year}",
    on_upgrade: OnUpgradeOption = OnUpgrade.skip,
) -> None:
    """[macOS only] Copy a library into one library per year (photos-2018, photos-2019 ...) with PowerPhotos.

    The destination libraries must already exist in PowerPhotos.
    """
    require_macos()
    first, _, last = years.partition("-")
    if not (first.isdigit() and (last.isdigit() or not last)):
        raise typer.BadParameter("--years must be a year or a range such as 2018-2020")
    first_year, last_year = int(first), int(last or first)
    if last_year < first_year:
        raise typer.BadParameter("--years ends before it starts")
    try:
        with pp.one_at_a_time():
            libraries = known_libraries()
            for year in range(first_year, last_year + 1):
                name = destination_name.format(media=media.value, year=year)
                found = copy_one(
                    libraries, source, name, date(year, 1, 1), date(year + 1, 1, 1), media.value, on_upgrade
                )
                typer.echo(f"{name}: skipped (needs a Photos upgrade)" if found is None else f"{name}: found {found}")
    except pp.Busy as error:
        fail(str(error))
