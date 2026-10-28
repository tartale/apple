"""Where THIS checkout's venv lives, for the ./apple launcher.

A venv is not relocatable: pyvenv.cfg, the bin/python* symlinks and every console-script shebang bake in absolute paths. So
the directory name has to identify the ENVIRONMENT the venv was built for. <os>-<arch> alone does not: a checkout mounted
into a container is linux-x86_64 on both sides, and the host and the container would share one directory and break each
other's. The checkout's own resolved absolute path tells them apart, so each environment that reaches it by a different path
gets its own venv.

Standard library only: the launcher runs it before any dependency exists.
"""
import hashlib
import platform
import sys
from pathlib import Path


def env_tag(repo: Path) -> str:
    """<os>-<arch>-<first 6 hex of a hash of the resolved checkout path>."""
    root = Path(repo).resolve()
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:6]
    return f"{platform.system().lower()}-{platform.machine()}-{digest}"


def venv_dir(repo: Path) -> Path:
    return Path(repo).resolve() / f".venv-{env_tag(repo)}"


if __name__ == "__main__":
    print(venv_dir(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent))
