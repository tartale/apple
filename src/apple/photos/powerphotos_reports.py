"""What PowerPhotos says about a copy it was asked for, from the report it writes.

PowerPhotos answers a copy request at once and keeps working, and shows what went wrong only in its window and its Reports
section, so the script that asked cannot tell. Every copy writes a report, a bundle ~/Library/Application Support/
com.fatcatsoftware.PowerPhotos/Reports/<yyyy-MM-dd-HHmmss> Copy N photos to library “<name>”.ppreport, whose Assets.db
(documented in the AGENTS.md PowerPhotos keeps in that folder) records for each photo its outcome and, for a failure, the
error, and for the operation whether it ended and with what error. The bundle's operation.log is the operation's plain-text log:
an operation that stopped on an error the user was shown ends with `Error shown: <description>`, and one that failed has a
`Session “<name>” failed (<description>) after <time>` line. A copy is found by the reports there were not before it:
snapshot() before the request and find() after it.
"""
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Set

REPORTS_VARIABLE = "APPLE_PHOTOS_POWERPHOTOS_REPORTS_DIR"
DEFAULT_REPORTS = "~/Library/Application Support/com.fatcatsoftware.PowerPhotos/Reports"

SKIPPED = 2  # IPAssetResult: not processed (its resources are missing or it is unsupported)
FAILURES = {8, 9, 11}  # importFailure, analysisFailed, exportFailure


class Report(NamedTuple):
    """finished: the operation has ended (or was cancelled). error: what PowerPhotos says went wrong with the operation as a
    whole, or None. failed: photo identifier (the UUID without /L0/001) -> why that photo was not copied."""
    path: Path
    finished: bool
    error: Optional[str]
    failed: Dict[str, str]


def reports_dir() -> Path:
    return Path(os.environ.get(REPORTS_VARIABLE) or DEFAULT_REPORTS).expanduser()


def snapshot() -> Set[str]:
    """The names of the reports there are now."""
    try:
        return {entry.name for entry in reports_dir().iterdir() if entry.name.endswith(".ppreport")}
    except OSError:
        return set()


def find(before: Set[str], library: str) -> Optional[Path]:
    """The newest report not in before whose name says it copied to library, or None."""
    found = sorted(name for name in snapshot() - before if f"“{library}”" in name or f'"{library}"' in name)
    return reports_dir() / found[-1] if found else None


def error_text(blob) -> Optional[str]:
    """The operation's error, a JSON object (BWCodableError) in the report, stored as text or as bytes: its description and what
    to do about it."""
    if not blob:
        return None
    raw = blob if isinstance(blob, str) else bytes(blob).decode("utf-8", errors="replace")
    try:
        found = json.loads(raw)
        parts = [found.get("errorDescription"), found.get("recoverySuggestion")]
        return " ".join(" ".join(part.split()) for part in parts if part) or raw
    except (ValueError, AttributeError):
        return " ".join(re.findall(r"[ -~]{8,}", raw)) or "an error"


SESSION_FAILED = re.compile(r"Session “[^”]*” failed \((.*)\) after ")
ERROR_SHOWN = re.compile(r"Error shown: (.*)")


def log_error(report: Path) -> Optional[str]:
    """What the bundle's operation.log says stopped the operation: the last `Error shown:` or `Session … failed (…)` line,
    or None (no log, or it ended well)."""
    try:
        text = (report / "operation.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        match = SESSION_FAILED.search(line) or ERROR_SHOWN.search(line)
        if match:
            found = " ".join(match.group(1).split())
    return found


def read(report: Path) -> Optional[Report]:
    """The report as it is now, or None when its database cannot be read (not there yet, or being written)."""
    try:
        connection = sqlite3.connect(f"file:{report / 'Assets.db'}?mode=ro", uri=True)
        try:
            meta = connection.execute("select endDate, wasCancelled, error from reportMedatata limit 1").fetchone()
            rows = connection.execute(
                f"select uuid, result, resultDescription from assets where result in ({','.join(str(code) for code in FAILURES | {SKIPPED})})"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    try:
        return _report(report, meta, rows)
    except (TypeError, ValueError, AttributeError, KeyError):  # a report in a shape not expected is no report: it must never stop a copy
        return None


def _report(report: Path, meta, rows) -> Report:
    end, cancelled, error = meta if meta else (None, 0, None)
    failed = {}
    for uuid, result, description in rows:
        why = " ".join((description or "").split()) or ("PowerPhotos skipped it (its resources are missing, or it is unsupported)" if result == SKIPPED else "the copy failed")
        failed[str(uuid).split("/")[0].upper()] = why
    finished = bool(end) or bool(cancelled)
    return Report(report, finished, error_text(error) or (log_error(report) if finished else None), failed)
