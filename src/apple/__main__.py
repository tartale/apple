import os

from apple.cli import app

app(prog_name=os.environ.get("APPLE_PROG", "apple"))
