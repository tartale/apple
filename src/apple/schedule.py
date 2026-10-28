"""`apple schedule`: run an apple command every day through a launchd agent (macOS only)."""
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from apple.platform import require_macos

app = typer.Typer(help="Run an apple command every day (macOS only).", no_args_is_help=True)

PREFIX = "local.apple."
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def agents_dir() -> Path:
    return Path(os.environ.get("LAUNCH_AGENTS_DIR") or Path.home() / "Library" / "LaunchAgents")


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "apple"


def plist_path(name: str) -> Path:
    return agents_dir() / f"{PREFIX}{name}.plist"


def program_arguments() -> List[str]:
    """How to start this CLI from launchd: the installed `apple`, or this Python running the package."""
    installed = shutil.which("apple")
    return [installed] if installed else [sys.executable, "-m", "apple"]


def check_command(words: List[str]) -> None:
    """Fails unless the words start with an apple command, such as `photos split`."""
    from apple.cli import app as root

    command = typer.main.get_command(root)
    consumed = 0
    for word in words:
        subcommands = getattr(command, "commands", None)
        if subcommands is not None and word in subcommands:
            command = subcommands[word]
            consumed += 1
        else:
            break
    if consumed == 0 or getattr(command, "commands", None) is not None:
        raise typer.BadParameter(f"'{' '.join(words)}' is not an apple command (try `apple --help`)")


def launchctl(*args: str) -> int:
    return subprocess.run(["launchctl", *args], capture_output=True).returncode


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="A name for this schedule (letters, digits, dot, dash, underscore).")],
    command: Annotated[List[str], typer.Argument(help="The apple command to run, after `--`: for example photos split --from ...")],
    at: Annotated[str, typer.Option(help="Time of day, HH:MM (24-hour).")] = "03:00",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the launchd agent; install nothing.")] = False,
) -> None:
    """Run an apple command every day at a set time. Adding a name again replaces it."""
    require_macos()
    if not NAME.match(name):
        raise typer.BadParameter("the name may contain only letters, digits, dot, dash and underscore")
    match = TIME.match(at)
    if not match:
        raise typer.BadParameter("--at must be HH:MM, 00:00 to 23:59")
    check_command(command)
    log = log_dir() / f"{name}.log"
    plist = {
        "Label": f"{PREFIX}{name}",
        "ProgramArguments": [*program_arguments(), *command],
        "StartCalendarInterval": {"Hour": int(match.group(1)), "Minute": int(match.group(2))},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    if dry_run:
        typer.echo(plistlib.dumps(plist).decode())
        return
    path = plist_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".plist.new")
    temporary.write_bytes(plistlib.dumps(plist))
    temporary.replace(path)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{PREFIX}{name}")
    if launchctl("bootstrap", domain, str(path)) != 0:
        typer.echo(f"launchctl could not load {path}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Scheduled '{name}' daily at {int(match.group(1)):02d}:{match.group(2)}: apple {' '.join(command)}")
    typer.echo(f"Output goes to {log}. The first run may ask you to allow access to PowerPhotos and Photos.")


@app.command()
def remove(name: Annotated[str, typer.Argument(help="The schedule's name.")]) -> None:
    """Stop and delete a schedule."""
    require_macos()
    path = plist_path(name)
    launchctl("bootout", f"gui/{os.getuid()}/{PREFIX}{name}")
    existed = path.exists()
    path.unlink(missing_ok=True)
    typer.echo(f"Removed '{name}'" if existed else f"No schedule named '{name}'")


@app.command("list")
def list_schedules() -> None:
    """List the schedules: name, time and command."""
    require_macos()
    for path in sorted(agents_dir().glob(f"{PREFIX}*.plist")):
        plist = plistlib.loads(path.read_bytes())
        when = plist.get("StartCalendarInterval", {})
        command = " ".join(plist["ProgramArguments"][1:])
        typer.echo(f"{path.name[len(PREFIX):-len('.plist')]}\t{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}\t{command}")
