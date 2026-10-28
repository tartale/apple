# apple

Apple-related tools, run through one command, `apple`. It backs up iCloud Photos to a local disk (developed on a Synology NAS) with [rclone](https://rclone.org).

## Install

From a clone of this repo:

```
./install
apple doctor
```

`./install` finds Python 3.9 or later (or prints the command that installs it), installs `apple` with `uv` or `pipx` if you have either, and otherwise into its own virtual environment linked from `~/.local/bin` (set `APPLE_HOME` and `APPLE_BIN_DIR` to change those). Rerun it to upgrade; `DRY_RUN=true ./install` shows the plan. `apple doctor` then checks what the commands need and prints a copy/paste fix for anything missing.

Without installing, `PYTHONPATH=src python3 -m apple` runs the same command.

## Commands

`apple --help` and `apple photos --help` list them.

| Command | What it does |
| --- | --- |
| `apple doctor` | Checks what the commands need (programs, the iCloud variables, and on macOS osascript and PowerPhotos) and prints a copy/paste fix for anything missing. Exits 1 if something is missing. |
| `apple photos backup` | Runs `backup.sh`. For each account, `rclone copy`s `PrimarySync/All Photos` into `$ICLOUD_ROOT/<name>/originals/`, then copies photos that exist only in albums to `originals/<Album>/`, once per photo. `albums.csv` lists only entries whose file is in `originals/`; a download that fails leaves out its own rows, makes the run exit 1, and is retried next run. Safe to run from cron; skips if a run is already active. |
| `apple photos organize` | Runs `organize.sh`. Builds `by-date/YYYY/MM/` (by file mtime) and `albums/<Album>/` hardlink trees beside `originals/`. Rerunnable; never modifies `originals/`. Exits 1 if a link fails or an `albums.csv` row names a file that is not in `originals/`. |
| `apple photos libraries` | macOS only. Lists the libraries PowerPhotos knows and where each is on disk. |
| `apple photos copy` | macOS only. Copies the photos and videos in a date range from one Photos library to another with PowerPhotos: `--from`, `--to`, `--since`, `--until`, `--media photos\|videos\|all`, `--on-upgrade skip\|ask\|stop`. Skips photos already in the destination; removes nothing. |
| `apple photos split` | macOS only. Runs `copy` once per year into `photos-<year>` or `videos-<year>` libraries. |

`backup` and `organize` take `--dry-run` (the same as `DRY_RUN=true`). They share one lock, so they never run at the same time. The macOS-only commands exit with status 2 anywhere else; they need PowerPhotos and see [macos/README.md](macos/README.md). The shell scripts behind them are in `src/apple/photos/scripts/`; `lib.sh` there holds the shared setup (environment check, required-program check, lock, logging).

`macos/schedule.sh` (macOS only) runs a PowerPhotos Shortcut on a daily schedule through a launchd agent. See [macos/README.md](macos/README.md).

## Development

| Script | What it does |
| --- | --- |
| `scripts/lint.sh` | Runs `shellcheck` over every shell script in the repo. |
| `scripts/test.sh` | Runs the [bats](https://github.com/bats-core/bats-core) tests in `test/` (the shell scripts) and the pytest tests in `tests/` (the CLI). They use `test/fake-rclone`, a local-directory stand-in for rclone, so they need no iCloud account. |

## Layout

Every photo and video is stored once, under `originals/`. `by-date/` and `albums/` are hardlinks into it, so they cost no extra space.

```
$ICLOUD_ROOT/<name>/
  originals/        # one copy of everything
  by-date/YYYY/MM/  # hardlinks
  albums/<Album>/   # hardlinks
  albums.csv        # path,size,mtime,stored: each album entry and the originals/ file it links to
```

A photo is the same photo when its file name, size and modification time all match. A photo in several albums is downloaded once, and a photo already in `originals/` (for example one since deleted from iCloud, which stays in Recently Deleted) is never downloaded again; a different photo that merely shares a name (and even a size) is kept separately. If two different photos share a name in the same month, `by-date/` links the second as `<name>-<size>.<ext>`.

## Backup, not mirror

Nothing is ever deleted or pruned. A photo removed from iCloud stays in `originals/`, and its links stay in `by-date/` and `albums/`. Album changes only add links; a photo moved between albums keeps its old link too.

## Configuration

The scripts are configured only through environment variables; there is no config file. A script exits with an error naming any required variable that is not set.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `ICLOUD_ROOT` | yes | | Destination root. Each account gets `$ICLOUD_ROOT/<name>/`; logs are written here too |
| `ICLOUD_ACCOUNTS` | yes | | Space-separated `<name>=<rclone remote>` pairs, e.g. `alice=alice.icloud bob=bob.icloud` |
| `ICLOUD_RCLONE_CONF` | no | rclone's own default config location | Path to the rclone config that defines the iCloud remotes (passed as `--config`) |
| `ICLOUD_RCLONE` | no | `rclone` (from `PATH`) | rclone binary |
| `ICLOUD_LOCK_FILE` | no | `/tmp/rclone-icloudphotos.lock` | Lock shared by both scripts |
| `DRY_RUN` | no | `false` | `true`: `backup.sh` passes `--dry-run` to rclone; it changes nothing under `$ICLOUD_ROOT/<name>/`; `organize.sh` counts only and creates nothing |
| `PROGRESS_EVERY` | no | `1000` | `organize.sh` progress-log interval, in files |

Example:

```
export ICLOUD_ROOT=/data/icloud
export ICLOUD_ACCOUNTS="alice=alice.icloud bob=bob.icloud"
export ICLOUD_RCLONE_CONF=/etc/rclone/rclone.conf
apple photos backup && apple photos organize
```

Logs: `$ICLOUD_ROOT/backup-YYYY-MM-DD.log` and `$ICLOUD_ROOT/organize-YYYY-MM-DD.log`; all of a day's runs append to that day's file (the date is the one the run started on). Run from a terminal, both commands also print their status lines there, and `backup.sh` shows rclone's live transfer progress; from cron they only write the log.
