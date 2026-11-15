"""`apple photos compare`: what two files of one photo differ in, and which is the better one to keep."""
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from apple.photos import file_compare, folder_photos
from apple.photos import powerphotos as pp
from apple.photos.library_commands import fail, find_library

SIDES = {"first": "left", "second": "right"}


def files_of(side: str, name: Optional[str], refresh: bool) -> List[Path]:
    """The files a side names: the file itself, or the files of that name in a folder (read as `diff` reads one) or the
    originals of the photos of that name in a library."""
    path = Path(side).expanduser()
    if path.is_file():
        return [path]
    if not name:
        raise typer.BadParameter(f"{side} is not a file: give NAME, the file name of the photo to compare, to look it up there")
    try:
        data = folder_photos.folder_photos(path, refresh=refresh) if folder_photos.is_folder(side) else pp.library_photos(find_library(side))
    except pp.PowerPhotosError as error:
        fail(str(error))
    wanted = pp.normalized_name(name)
    found = sorted({data.library / stored for key, paths in data.files.items() if pp.normalized_name(key[2]) == wanted for stored in paths})
    here = [file for file in found if file.is_file()]
    if not here:
        fail(f"No file named {name} in {side}" + (f" ({len(found)} photo(s) of that name, whose original is not stored there)" if found else ""))
    return here


def table(rows: List[tuple]) -> None:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def show(result: file_compare.Compared) -> None:
    first, second = result.first, result.second
    for side, described in (("left: ", first), ("right:", second)):
        pixels = f"  {described.dimensions[0]}x{described.dimensions[1]}" if described.dimensions else ""
        typer.echo(f"{side} {described.path}  {described.format or 'not read in parts'}  {described.size:,} bytes{pixels}")
    apart = first.size - second.size
    start, end = result.ends
    if first.size == second.size and start == first.size:
        typer.echo("bytes: the same")
    else:
        typer.echo("size: " + (f"left by {apart:+,} bytes ({apart / max(first.size, second.size):+.1%})" if apart else "the same"))
        typer.echo(f"bytes: the first {start:,} are the same, and the last {end:,}; between them left has {first.size - start - end:,} and right has {second.size - start - end:,}")
    if first.parts and second.parts:
        typer.echo("")
        sides = [{part.name: part for part in described.parts} for described in (first, second)]
        rows = [("part", "left", "right", "")]
        for name in dict.fromkeys(list(sides[0]) + list(sides[1])):
            one, other = sides[0].get(name), sides[1].get(name)
            if one and other:
                note = "same" if one == other else (f"differs by {one.size - other.size:+,} bytes" if one.size != other.size else "differs, in the same size")
            else:
                note = "only in left" if one else "only in right"
            rows.append((name, f"{one.size:,}" if one else "-", f"{other.size:,}" if other else "-", note))
        table(rows)
        facts = [fact for fact in file_compare.WORTH + file_compare.OTHER_FACTS if fact in first.facts or fact in second.facts]
        if facts:
            typer.echo("")
            table([("says", "left", "right", "")] + [(fact, first.facts.get(fact, "-"), second.facts.get(fact, "-"), "" if first.facts.get(fact) == second.facts.get(fact) else "differs") for fact in facts])
    typer.echo("")
    if result.same_picture and result.differing:
        typer.echo(f"The same picture; what differs: {', '.join(result.differing)}.")
    elif result.same_picture is False:
        typer.echo("Not the same picture data.")
    better = {"either": "either one", None: "no recommendation"}.get(result.better) or SIDES[result.better]
    typer.echo(f"Better to keep: {better} ({result.why}).")


def compare_command(
    left: Annotated[str, typer.Argument(help="A file; or, with NAME, a folder of photo files or a library (a name from `./apple photos libraries`, or the path of a .photoslibrary).")],
    right: Annotated[str, typer.Argument(help="The file to compare it with, or where to look NAME up, named the same way.")],
    name: Annotated[Optional[str], typer.Argument(help="The file name of the photo, as `diff`, `match` and --explain show it: needed when a side is a folder or a library.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="For a folder: read it again instead of reusing what an earlier run saved.")] = False,
) -> None:
    """Show what two files of one photo differ in, and which is the better one to keep.

    For a JPEG the file is read part by part: the picture itself, and what is written around it (Exif, XMP, the color profile,
    IPTC, a thumbnail), so a difference of a few hundred bytes is told as the same picture with other metadata, or as another
    picture. What each file says about the photo is listed (capture time, GPS, camera, description, title, keywords, rating,
    faces, color profile, maker note, orientation), and the better one to keep is named when the files settle it: the same
    picture where only one has something worth keeping, or the one with more pixels. A file of another type is compared by
    where its bytes differ. A + is the left file being the larger. With NAME, every file of that name on the left is compared
    with every one on the right; a library's file is the original it stores. Changes nothing.
    """
    lefts, rights = files_of(left, name, refresh), files_of(right, name, refresh)
    shown = 0
    for one in lefts:
        for other in rights:
            try:
                result = file_compare.compare(one, other)
            except OSError as error:
                fail(f"Could not read a file: {error}")
            if shown:
                typer.echo("")
            show(result)
            shown += 1
