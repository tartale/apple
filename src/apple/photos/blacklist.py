"""Photos that are left out of copies, and why.

A photo is blacklisted for a reason of some kind. The only kind so far is "uncopyable": a copy of the photo failed with an error
of its own in PowerPhotos' report (an edited photo whose edit data Photos rejects, say), so later copies would only fail
again. The file records for each entry the library it is blacklisted from (the source of the copy), the photo's Photos
identifier, its name and date, the kind, the reason (PowerPhotos' own text) and when it was recorded. It is JSON on the
local disk, per machine, at ~/.cache/apple/photos/blacklist.json (APPLE_PHOTOS_BLACKLIST_FILE names another); other kinds can
be added without changing it, as a photo can be blacklisted once for each kind. --ignore-blacklist tries the listed photos
again, and one that copies is removed.

    {"version": 1, "entries": [{"kind": "uncopyable", "library": "Default Library", "uuid": "...", "name": "IMG_1012.JPG",
                                "date": "2023-03-27 20:14:00", "reason": "...", "recorded": "2026-11-11T17:21:32"}]}
"""
import json
import os
import time
from pathlib import Path
from typing import List, Optional

FILE_VARIABLE = "APPLE_PHOTOS_BLACKLIST_FILE"
VERSION = 1
UNCOPYABLE = "uncopyable"


class BlacklistError(Exception):
    """The file exists but cannot be used (not JSON, or written by a newer version): it is left alone, not overwritten."""


def path() -> Path:
    return Path(os.environ.get(FILE_VARIABLE) or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "apple" / "photos" / "blacklist.json").expanduser()


def entries() -> List[dict]:
    """Every entry, in the order recorded. A file that is not there has none; one that cannot be used raises BlacklistError."""
    try:
        text = path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        raise BlacklistError(f"{path()} cannot be read: {error.strerror or error}")
    try:
        found = json.loads(text)
        if found.get("version") != VERSION or not isinstance(found.get("entries"), list):
            raise ValueError("another format")
        return found["entries"]
    except (ValueError, AttributeError):
        raise BlacklistError(f"{path()} is not a blacklist this version understands (version {VERSION}); move it aside, or set {FILE_VARIABLE} to another file")


def listed(library: str, uuid: str, kind: Optional[str] = None) -> Optional[dict]:
    """The entry for the photo of that library, of that kind (any kind when None), or None."""
    return next((entry for entry in entries() if entry["library"] == library and entry["uuid"] == uuid and kind in (None, entry["kind"])), None)


def _write(found: List[dict]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".new")
    staging.write_text(json.dumps({"version": VERSION, "entries": found}, indent=1), encoding="utf-8")
    staging.replace(target)


def add(kind: str, library: str, uuid: str, name: str, date: str, reason: str) -> None:
    """Blacklists the photo for the kind, replacing what was recorded for that kind before."""
    found = [entry for entry in entries() if not (entry["library"] == library and entry["uuid"] == uuid and entry["kind"] == kind)]
    found.append({"kind": kind, "library": library, "uuid": uuid, "name": name, "date": date, "reason": reason, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _write(found)


def remove(kind: str, library: str, uuid: str) -> None:
    found = entries()
    kept = [entry for entry in found if not (entry["library"] == library and entry["uuid"] == uuid and entry["kind"] == kind)]
    if len(kept) != len(found):
        _write(kept)
