"""A cache of what `find` has indexed, so that a second run over the same places does not read them again.

What is cached is what cannot go stale without the cache noticing: a Photos library's database, which is looked up by its
size and modification time, and a group of Time Machine backup folders, whose finished snapshots never change and whose set
of snapshots is part of the key. Ordinary folders are not cached; they are read every time. The cache is one SQLite file on the
local disk, per machine, since the paths a machine sees differ from another's.
"""
import hashlib
import json
import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Optional

FILE_VARIABLE = "APPLE_PHOTOS_CACHE_FILE"
VERSION = 1  # raise it when what is indexed, or how, changes: an older entry is then never used


def cache_file() -> Path:
    """Where the cache is: ~/.cache/apple/photos/cache.sqlite, or the file APPLE_PHOTOS_CACHE_FILE names."""
    return Path(os.environ.get(FILE_VARIABLE) or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "apple" / "photos" / "cache.sqlite").expanduser()


def fingerprint(*parts: Any) -> str:
    """A short key for the parts (anything JSON can hold), starting with the version."""
    return hashlib.sha256(json.dumps([VERSION, *parts], sort_keys=True, default=str).encode("utf-8")).hexdigest()


class Cache:
    """get and put by key. A cache that cannot be opened or read behaves as an empty one: it is only ever an optimisation."""

    def __init__(self, path: Optional[Path] = None, read: bool = True):
        self.path = path or cache_file()
        self.read = read  # False: entries are not used, but new ones are saved (a refresh)
        self._db: Optional[sqlite3.Connection] = None

    def db(self) -> Optional[sqlite3.Connection]:
        if self._db is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self.path, timeout=30)
                self._db.execute("create table if not exists entries (key text primary key, saved real, payload blob)")
            except (OSError, sqlite3.Error):
                self._db = None
        return self._db

    def get(self, key: str) -> Optional[tuple]:
        """(value, when it was saved) or None."""
        db = self.db()
        if db is None or not self.read:
            return None
        try:
            row = db.execute("select saved, payload from entries where key = ?", (key,)).fetchone()
            return (json.loads(zlib.decompress(row[1]).decode("utf-8")), row[0]) if row else None
        except (sqlite3.Error, zlib.error, ValueError):
            return None

    def put(self, key: str, value: Any) -> None:
        db = self.db()
        if db is None:
            return
        try:
            db.execute("insert or replace into entries values (?, ?, ?)", (key, time.time(), zlib.compress(json.dumps(value).encode("utf-8"))))
            db.commit()
        except (sqlite3.Error, TypeError, ValueError):
            pass
