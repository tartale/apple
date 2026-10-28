"""The `apple` command: every tool in this repo runs through it."""
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Optional

import typer

from apple.doctor import doctor_command
from apple.photos.commands import app as photos_app
from apple.schedule import app as schedule_app

app = typer.Typer(
    name="apple",
    help="Tools for Apple Photos.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(photos_app, name="photos")
app.add_typer(schedule_app, name="schedule")
app.command("doctor")(doctor_command)


def show_version(show: bool) -> None:
    if show:
        try:
            typer.echo(version("apple"))
        except PackageNotFoundError:
            typer.echo("unknown (not installed)")
        raise typer.Exit()


@app.callback()
def main(
    version_: Annotated[
        Optional[bool], typer.Option("--version", callback=show_version, is_eager=True, help="Show the version and exit.")
    ] = None,
) -> None:
    pass
