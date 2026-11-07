"""Progress for long commands, written to stderr so that what a command prints on stdout stays clean."""
import sys
import time


class Progress:
    """update() shows a status that is replaced by the next one; line() keeps a line.

    On a terminal the status is one line rewritten in place (at most five times a second). Anywhere else, such as a log file
    or a pipe, a status is written as a line at most once every interval seconds, so a long run shows it is alive without
    filling the file."""

    def __init__(self, stream=None, interval: float = 15.0):
        self.stream = stream or sys.stderr
        self.interval = interval
        self.terminal = bool(getattr(self.stream, "isatty", lambda: False)())
        self.last = 0.0
        self.width = 0

    def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self.last < (0.2 if self.terminal else self.interval):
            return
        self.last = now
        if self.terminal:
            self.stream.write("\r" + text.ljust(self.width))
            self.width = len(text)
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def line(self, text: str) -> None:
        if self.terminal and self.width:
            self.stream.write("\r" + " " * self.width + "\r")
            self.width = 0
        self.stream.write(text + "\n")
        self.stream.flush()
        self.last = time.monotonic()


def duration(seconds: float) -> str:
    """12s, 3m 05s, 1h 02m."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
