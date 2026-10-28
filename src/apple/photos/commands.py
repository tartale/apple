"""`apple photos`: back up iCloud Photos to disk and organize the backup."""
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from apple.photos import library_commands

SCRIPTS = Path(__file__).parent / "scripts"

app = typer.Typer(
    help="Back up iCloud Photos to disk and organize the backup; copy between Photos libraries with PowerPhotos (macOS only).",
    no_args_is_help=True,
)

DryRun = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help="Change nothing; report what would happen. Same as DRY_RUN=true.",
    ),
]


def run_script(name: str, dry_run: bool) -> None:
    """Runs a bundled script with this process's environment and exits with its status."""
    env = dict(os.environ)
    if dry_run:
        env["DRY_RUN"] = "true"
    result = subprocess.run(["bash", str(SCRIPTS / name)], env=env)
    raise typer.Exit(result.returncode)


@app.command()
def backup(dry_run: DryRun = False) -> None:
    """Copy every photo and video from each iCloud account into ICLOUD_ROOT/<name>/originals/.

    Configured by the environment: ICLOUD_ROOT and ICLOUD_ACCOUNTS are required;
    ICLOUD_RCLONE_CONF, ICLOUD_RCLONE and ICLOUD_LOCK_FILE are optional.
    """
    run_script("backup.sh", dry_run)


@app.command()
def organize(dry_run: DryRun = False) -> None:
    """Build the by-date/ and albums/ hardlink trees beside originals/. Never changes originals/."""
    run_script("organize.sh", dry_run)


app.command("libraries")(library_commands.libraries_command)
app.command("copy")(library_commands.copy_command)
app.command("split")(library_commands.split_command)
