import os
import time
from datetime import date
from pathlib import Path

import pytest

from apple.photos import common

REPO = Path(__file__).resolve().parent.parent
JUN_2020 = 1592222400  # 2020-06-15 12:00 UTC, mid-month so the by-date folder does not depend on the time zone
JUL_2020 = 1594814400  # 2020-07-15 12:00 UTC


@pytest.fixture
def icloud(tmp_path, monkeypatch):
    """A fake iCloud (a directory tree), an empty backup root, the fake rclone and a fixed date for the logs."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    cloud = tmp_path / "cloud"
    (cloud / "tom" / "PrimarySync").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    for name, value in {
        "FAKE_CLOUD": cloud,
        "ICLOUD_ROOT": root,
        "ICLOUD_ACCOUNTS": "tom=tom",
        "ICLOUD_RCLONE": REPO / "tests" / "fakes" / "rclone",
        "ICLOUD_LOCK_FILE": tmp_path / "lock",
    }.items():
        monkeypatch.setenv(name, str(value))
    for name in ("DRY_RUN", "ICLOUD_RCLONE_CONF", "FAKE_FAIL", "PROGRESS_EVERY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(common, "today", lambda: date(2026, 1, 2))

    class ICloud:
        pass

    ICloud.cloud = cloud / "tom" / "PrimarySync"
    ICloud.root = root
    ICloud.account = root / "tom"
    ICloud.lock = tmp_path / "lock"

    def cloud_file(path, size, mtime):
        target = ICloud.cloud / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        os.utime(target, (mtime, mtime))

    def original(path, size, mtime):
        target = ICloud.account / "originals" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        os.utime(target, (mtime, mtime))

    ICloud.cloud_file = staticmethod(cloud_file)
    ICloud.original = staticmethod(original)
    ICloud.log = staticmethod(lambda name: (root / f"{name}-2026-01-02.log").read_text())
    yield ICloud
    monkeypatch.undo()
    time.tzset()
