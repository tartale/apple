"""The `apple` command: every tool in this repo runs through it."""
import typer

from apple.photos.commands import app as photos_app

app = typer.Typer(
    name="apple",
    help="Tools for Apple Photos.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(photos_app, name="photos")
