# apple

Apple-related tools, run through one command, `./apple`. It backs up iCloud Photos to a local disk (developed on a Synology NAS) with [rclone](https://rclone.org).

## Running it

Run `./apple` from this checkout; nothing is installed globally.

```
./apple doctor
./apple --help
```

The first run (and any run after `pyproject.toml` changes) sets up a virtual environment in `~/.cache/apple/venv-<os>-<arch>-<hash>` (`$XDG_CACHE_HOME/apple/` if set), using Python 3.9 or later. It is on the local disk, not in the checkout, because the checkout may be a network share (which often cannot hold a venv's symlinks) or shared with a container; the name identifies the environment (the hash is of the checkout's path). `APPLE_VENV` overrides the location. If Python or its venv module is missing, it prints the command that installs it. Then `./apple doctor` checks what the commands need and prints a copy/paste fix for anything missing.

## Commands

`./apple --help` and `./apple photos --help` list them.

| Command | What it does |
| --- | --- |
| `./apple doctor` | Checks what the commands need (rclone and the iCloud variables, and on macOS osascript and PowerPhotos) and prints a copy/paste fix for anything missing. Exits 1 if something is missing. |
| `./apple photos backup` | For each account, `rclone copy`s `PrimarySync/All Photos` into `$ICLOUD_ROOT/<name>/originals/`, then copies photos that exist only in albums to `originals/<Album>/`, once per photo. `albums.csv` lists only entries whose file is in `originals/`; a download that fails leaves out its own rows, makes the run exit 1, and is retried next run. Safe to run from cron; skips if a run is already active. |
| `./apple photos organize` | Builds `by-date/YYYY/MM/` (by file mtime) and `albums/<Album>/` hardlink trees beside `originals/`. Rerunnable; never modifies `originals/`. Exits 1 if a link fails or an `albums.csv` row names a file that is not in `originals/`. |
| `./apple photos libraries` | macOS only. Lists the libraries PowerPhotos knows, where each is on disk and its Photos database version, read as it is on disk (instant, but it can miss a version Photos wrote moments ago). `--outdated` reads the write-ahead log too (exact, but copies each database, slow over a network share) and lists only those older than the newest (or than `--against NAME`), which Photos must upgrade before they accept photos. Two libraries with the same name are listed with a warning, and every other command refuses to guess between them. |
| `./apple photos diff` | `diff LEFT RIGHT` (names from PowerPhotos, macOS only, or paths of .photoslibrary folders, anywhere) reads the libraries' databases directly, without PowerPhotos, and lists the photos and videos in only one of two libraries (a photo is its date, size and file name; the trash is ignored), then the counts on stderr. Exits 1 when they differ. Text output is a header and aligned columns with `<` (only in LEFT) or `>` (only in RIGHT). Items Photos keeps out of view (mostly burst members) are compared too and marked in true/false `hidden` and `burst` columns: PowerPhotos does not list them and cannot copy them. `--exclude-hidden` leaves them out and says on stderr how many; exit code is 1 when the compared items differ either way. `--only left\|right` shows one side without the marks, `--format text\|csv\|json` (csv and json add a `side` field), `--fields date,size,name,hidden,burst` chooses and orders the columns, `--ignore-date` treats the same size and file name as the same photo (a clip imported twice with a shifted time), `--sort date\|name\|size`, `--since` / `--until` (`YYYY-MM-DD`, last day included, UTC as the databases keep it) and `--media photos\|videos\|all` limit what is compared, `--summary` prints only the counts. Changes nothing. |
| `./apple photos copy` | macOS only. Copies the photos and videos in a date range from one Photos library to another with PowerPhotos: `--from`, `--to`, `--since`, `--until`, `--media photos\|videos\|all`, `--on-upgrade skip\|ask\|stop`. Skips photos already in the destination; removes nothing. `--list FILE` (`-` for stdin) copies instead the photos with the file names in a list, the csv or json output of `diff` or one name per line: `diff A B --only left --format csv \| copy --from A --to B --list -`. Every source photo with a listed name is copied (a name more than one photo has copies all of them); names PowerPhotos does not list in the source are reported, and so are hidden items in the list (from `diff`'s `hidden` column), which are left out. `--no-duplicate-check` (needs `--list`) asks PowerPhotos not to compare against the destination, so listed photos the destination already has are copied again. |
| `./apple photos split` | macOS only. Runs `copy` once per year into `photos-<year>` or `videos-<year>` libraries (`--media photos\|videos\|all`; `all` does both in one run, each kind into its own libraries, checking that all of them exist before copying anything). A year whose library already holds every photo of that year (judged from the databases, with photos near New Year allowed in the neighbouring year's library) is reported as up to date and not copied; `--no-skip-complete` copies every year. `--no-duplicate-check` copies only the photos each year's library lacks, matched by file name and without PowerPhotos' duplicate check (a missing photo whose name another photo shares copies that one too; photos near New Year go into every year in range that lacks them). `--max-size 200GB` caps each library (decimal units, the sum of the original files' sizes from the database): a year over the cap goes into `<name>-1`, `<name>-2` ... each a run of whole days from January 1 on, filled up to the cap in turn; photos near a boundary may be filed in either part, and a rerun after new photos arrive can move a boundary. The libraries must already exist in PowerPhotos: `--on-missing stop` (default) lists those that do not before copying anything, `skip` copies the rest. `--dry-run` (or `DRY_RUN=true`) shows each library, its dates and size and whether it needs copying. `split` says how many hidden items of the chosen media it cannot copy, in total and on each library's line (`photos-2018: up to date, 12 hidden not copied`). Database copies are read from `~/.cache/apple/scratch` (local disk). |
| `./apple schedule add\|remove\|list` | macOS only. Runs an `apple` command every day through a launchd agent: `./apple schedule add nightly --at 03:00 -- photos split --from ...`. |

`backup` and `organize` take `--dry-run` (the same as `DRY_RUN=true`). They share one lock, so they never run at the same time. The macOS-only commands exit with status 2 anywhere else; they need PowerPhotos and see [macos/README.md](macos/README.md). They are Python (`src/apple/photos/backup.py`, `organize.py`); `common.py` holds the shared setup (environment check, rclone check, lock, log). The only outside program is `rclone`, so they run the same on the NAS and on a Mac.



## Development

| Script | What it does |
| --- | --- |
| `scripts/lint.sh` | Runs `shellcheck` over the shell scripts (the `./apple` launcher and the sandbox scripts). |
| `scripts/test.sh` | Runs the [bats](https://github.com/bats-core/bats-core) tests in `test/` (the launcher) and the pytest tests in `tests/` (the CLI). They use `tests/fakes/rclone`, a local-directory stand-in for rclone, so they need no iCloud account. |

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

The commands are configured only through environment variables; there is no config file. A command exits with an error naming any required variable that is not set.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `ICLOUD_ROOT` | yes | | Destination root. Each account gets `$ICLOUD_ROOT/<name>/`; logs are written here too |
| `ICLOUD_ACCOUNTS` | yes | | Space-separated `<name>=<rclone remote>` pairs, e.g. `alice=alice.icloud bob=bob.icloud` |
| `ICLOUD_RCLONE_CONF` | no | rclone's own default config location | Path to the rclone config that defines the iCloud remotes (passed as `--config`) |
| `ICLOUD_RCLONE` | no | `rclone` (from `PATH`) | rclone binary |
| `ICLOUD_LOCK_FILE` | no | `/tmp/rclone-icloudphotos.lock` | Lock shared by both commands |
| `DRY_RUN` | no | `false` | `true` (or `1`, `yes`, `on`, in any case; `--dry-run` does the same): `backup` passes `--dry-run` to rclone and changes nothing under `$ICLOUD_ROOT/<name>/`; `organize` counts only and creates nothing |
| `PROGRESS_EVERY` | no | `1000` | `organize` progress-log interval, in files |

Example:

```
export ICLOUD_ROOT=/data/icloud
export ICLOUD_ACCOUNTS="alice=alice.icloud bob=bob.icloud"
export ICLOUD_RCLONE_CONF=/etc/rclone/rclone.conf
./apple photos backup && ./apple photos organize
```

Logs: `$ICLOUD_ROOT/backup-YYYY-MM-DD.log` and `$ICLOUD_ROOT/organize-YYYY-MM-DD.log`; all of a day's runs append to that day's file (the date is the one the run started on). Run from a terminal, both commands also print their status lines there, and `backup` shows rclone's live transfer progress; from cron they only write the log.
