from typer.testing import CliRunner

from apple.cli import app

runner = CliRunner()


def test_help_lists_the_photos_group():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "photos" in result.output


def test_photos_help_lists_every_command():
    result = runner.invoke(app, ["photos", "--help"])
    assert result.exit_code == 0
    for command in ("backup", "organize", "libraries", "copy", "split"):
        assert command in result.output
