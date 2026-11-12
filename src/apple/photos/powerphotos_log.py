"""What PowerPhotos says in its own log about a copy that failed.

PowerPhotos answers a copy request at once and keeps working, and shows a failure only in its window and its Reports
section, so the script that asked cannot tell. It also writes the failure to ~/Library/Logs/PowerPhotos/Diagnostics/
PowerPhotos.buffer, a fixed-size file of binary-framed records with readable text in them. A failed import is an entry like

    Got PhotoKit error Error Domain=PHPhotosErrorDomain Code=3302 "Invalid resource (code 3302)" UserInfo={_PHResourceUrlsErrorKey=(
        "file:///.../Batch/<uuid>/IMG_1012.JPG",
        "file:///.../Batch/<uuid>/FullSizeRender.jpeg"

whose file URLs name the photo. The buffer is a ring that overwrites old entries, and its entries have no readable time, so a
copy is judged by what entries there are after it that there were not before: failures() before the copy and new_failures()
after it.
"""
import os
import re
from collections import Counter
from pathlib import Path
from typing import List, NamedTuple
from urllib.parse import unquote

LOG_VARIABLE = "APPLE_PHOTOS_POWERPHOTOS_LOG"
DEFAULT_LOG = "~/Library/Logs/PowerPhotos/Diagnostics/PowerPhotos.buffer"

ERROR = re.compile(r'Got PhotoKit error Error Domain=(\w+) Code=(-?\d+) "([^"]*)"')
URL = re.compile(r'file://[^"\s,]*')
NAME = re.compile(r'/([^/"]+?\.[A-Za-z0-9]{2,5})(?=[^A-Za-z0-9]|$)')
PARTS = ("fullsizerender", "adjustments")  # pieces of an edited photo, not its file


class Failure(NamedTuple):
    """One failed import: the error's code and text, the names of the photo files it names, and the URLs (which tell one
    entry from another)."""
    code: str
    message: str
    names: tuple
    urls: tuple


def log_path() -> Path:
    return Path(os.environ.get(LOG_VARIABLE) or DEFAULT_LOG).expanduser()


def failures() -> List[Failure]:
    """The failed imports in the log. Empty when there is no log (another machine, another PowerPhotos), in which case a copy
    that does not arrive cannot be explained."""
    try:
        data = log_path().read_bytes()
    except OSError:
        return []
    text = data.decode("latin-1")
    found = []
    for match in ERROR.finditer(text):
        window = text[match.end():match.end() + 2000]
        control = re.search(r"[\x00-\x09\x0b-\x1f]", window)  # the binary framing after the entry's text
        window = window[:control.start()] if control else window
        urls = tuple(URL.findall(window))
        names = tuple(
            name for url in urls for name in [unquote(NAME.findall(url)[-1])] if NAME.findall(url) and Path(name).stem.lower() not in PARTS
        )
        found.append(Failure(match.group(2), match.group(3), names, urls))
    return found


def new_failures(before: List[Failure]) -> List[Failure]:
    """The failed imports now in the log that were not there in before."""
    seen = Counter((failure.code, failure.urls) for failure in before)
    new = []
    for failure in failures():
        signature = (failure.code, failure.urls)
        if seen[signature]:
            seen[signature] -= 1
        else:
            new.append(failure)
    return new
