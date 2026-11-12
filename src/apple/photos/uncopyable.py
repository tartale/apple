"""Photos that PowerPhotos cannot copy, remembered so that they are skipped.

A photo whose copy fails with an error of its own in PowerPhotos' log (an edited photo whose edit data Photos rejects, say) is
recorded here with the library it was to be copied from, and left out of later copies, which would only fail again; the
output lists them. It is a file on the local disk, per machine, in ~/.cache/apple (APPLE_PHOTOS_UNCOPYABLE names another
place). A photo is forgotten when it copies after all (--retry-uncopyable tries the listed ones again).
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

PATH_VARIABLE = "APPLE_PHOTOS_UNCOPYABLE"


def path() -> Path:
    return Path(os.environ.get(PATH_VARIABLE) or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "apple" / "uncopyable.json")


def entries() -> Dict[str, dict]:
    """key (library, a tab, the photo's Photos identifier) -> what is known of it: name, date, reason and when it was recorded."""
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def key(library: str, uuid: str) -> str:
    return f"{library}\t{uuid}"


def listed(library: str, uuid: str) -> Optional[dict]:
    return entries().get(key(library, uuid))


def _write(found: Dict[str, dict]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".new")
    staging.write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    staging.replace(target)


def add(library: str, uuid: str, name: str, date: str, reason: str) -> None:
    found = entries()
    found[key(library, uuid)] = {"name": name, "date": date, "reason": reason, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write(found)


def remove(library: str, uuid: str) -> None:
    found = entries()
    if found.pop(key(library, uuid), None) is not None:
        _write(found)
