# icloud-tools

Scripts that wrap [rclone](https://rclone.org) to back up iCloud Photos to a local disk (developed on a Synology NAS).

## Scripts

The scripts live in `photos/`.

| Script | What it does |
| --- | --- |
| `photos/backup.sh` | For each account, `rclone copy`s `PrimarySync/All Photos` into `$ICLOUD_ROOT/<name>/originals/`, then copies photos that exist only in albums to `originals/<Album>/`, once per photo. Writes `albums.csv`. Safe to run from cron; skips if a run is already active. |
| `photos/organize.sh` | Builds `by-date/YYYY/MM/` (by file mtime) and `albums/<Album>/` hardlink trees beside `originals/`. Rerunnable; never modifies `originals/`. |
| `photos/lint.sh` | Runs `shellcheck` over every script in the repo. |
| `photos/test.sh` | Runs the [bats](https://github.com/bats-core/bats-core) tests in `test/`. They use `test/fake-rclone`, a local-directory stand-in for rclone, so they need no iCloud account. |

`photos/backup.sh` and `photos/organize.sh` share one lock, so they never run at the same time.

## Layout

Every photo and video is stored once, under `originals/`. `by-date/` and `albums/` are hardlinks into it, so they cost no extra space.

```
$ICLOUD_ROOT/<name>/
  originals/        # one copy of everything
  by-date/YYYY/MM/  # hardlinks
  albums/<Album>/   # hardlinks
  albums.csv        # path,size,mtime,stored: each album entry and the originals/ file it links to
```

A photo is the same photo when its file name, size and modification time all match. A photo in several albums is downloaded once; a different photo that merely shares a name (and even a size) is kept separately. If two different photos share a name in the same month, `by-date/` links the second as `<name>-<size>.<ext>`.

## Configuration

The scripts are configured only through environment variables; there is no config file. A script exits with an error naming any required variable that is not set.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `ICLOUD_ROOT` | yes | | Destination root. Each account gets `$ICLOUD_ROOT/<name>/`; logs are written here too |
| `ICLOUD_ACCOUNTS` | yes | | Space-separated `<name>=<rclone remote>` pairs, e.g. `alice=alice.icloud bob=bob.icloud` |
| `ICLOUD_RCLONE_CONF` | no | rclone's own default config location | Path to the rclone config that defines the iCloud remotes (passed as `--config`) |
| `ICLOUD_RCLONE` | no | `rclone` (from `PATH`) | rclone binary |
| `ICLOUD_LOCK_FILE` | no | `/tmp/rclone-icloudphotos.lock` | Lock shared by both scripts |
| `DRY_RUN` | no | `false` | `true`/`1`: `backup.sh` passes `--dry-run` to rclone; it changes nothing under `$ICLOUD_ROOT/<name>/`; `organize.sh` counts only and creates nothing |
| `PROGRESS_EVERY` | no | `1000` | `organize.sh` progress-log interval, in files |

Example:

```
export ICLOUD_ROOT=/data/icloud
export ICLOUD_ACCOUNTS="alice=alice.icloud bob=bob.icloud"
export ICLOUD_RCLONE_CONF=/etc/rclone/rclone.conf
photos/backup.sh && photos/organize.sh
```

Logs: `$ICLOUD_ROOT/backup.log`, `$ICLOUD_ROOT/organize.log`. Run from a terminal, both scripts also print their status lines there, and `backup.sh` shows rclone's live transfer progress; from cron they only write the log.
