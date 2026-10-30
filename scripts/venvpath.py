"""Where THIS checkout's venv lives, for the ./apple launcher.

The venv is on the local disk, under ~/.cache/apple (or $XDG_CACHE_HOME/apple), not in the checkout: the checkout may be a
network share (SMB and NFS mounts often cannot hold the symlinks a venv is made of, which fails with "Exec format error"),
and it may be shared between a host and a container.

A venv is not relocatable: pyvenv.cfg, the bin/python* symlinks and every console-script shebang bake in absolute paths. So
the directory name has to identify the ENVIRONMENT the venv was built for: <os>-<arch> plus a hash of the checkout's resolved
absolute path, so two checkouts (or two paths to one) get their own venv.

Standard library only: the launcher runs it before any dependency exists.
"""
import hashlib
import os
import platform
import sys
from pathlib import Path


def env_tag(repo: Path) -> str:
    """<os>-<arch>-<first 6 hex of a hash of the resolved checkout path>."""
    root = Path(repo).resolve()
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:6]
    return f"{platform.system().lower()}-{platform.machine()}-{digest}"


def venv_dir(repo: Path) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "apple" / f"venv-{env_tag(repo)}"


if __name__ == "__main__":
    print(venv_dir(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent))
