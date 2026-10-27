# PowerPhotos Shortcuts (macOS)

[PowerPhotos](https://www.fatcatsoftware.com/powerphotos/) is Mac-only, so none of this runs on iOS or iPadOS. The work is done by Shortcuts that use PowerPhotos' own Shortcuts actions; `macos/schedule.sh` runs one on a schedule. The logic is in an AppleScript file in git; a Shortcut is a thin wrapper around it, built in the Shortcuts app.

The actions and filters named here come from the [PowerPhotos Shortcuts docs](https://www.fatcatsoftware.com/powerphotos/VersionedDocs/v9/automation_shortcuts.html). Items marked *check* were not confirmed there.

## Split Photo Library

`macos/split-library.applescript` copies one media type of a library into one library per year (`photos-2019`, `videos-2019`, ...). It uses PowerPhotos' `duplicate` command with `skip duplicates`, so reruns add only what is new, and nothing is removed from the source. A photo is a video by its file extension (PowerPhotos' `photo` class has no media-type property).

```
osascript macos/split-library.applescript photos 2018 2020 "Photos Library" [skip|ask|stop]
```

The arguments are `photos` or `videos`, the first and last year, the name of the source library as shown in PowerPhotos, and optionally what to do when a target library is in an older format and Photos must upgrade it before it takes photos: `skip` (default, for unattended runs) leaves that year out and says so in the report, `ask` shows PowerPhotos' message in a dialog (Skip, Stop, or open the library in Photos to upgrade it and Retry), `stop` ends the run. The need for an upgrade is found before any copying by comparing each target's Photos database version (`PLModelVersion`, read with `sqlite3` and `plutil`) with the source's, and again if PowerPhotos refuses a copy. Only one run goes at a time: a second run stops at once with a message (a lock under `$TMPDIR` holds the running process id, and a lock left by a run that died is replaced). It cannot see a copy started from the PowerPhotos window. Create the empty target libraries (`photos-2018`, ...) in PowerPhotos first; the dictionary has no way to create one, so the script stops with a message naming the one it needs.

The Shortcut is one action:

1. **Run Shell Script**: `osascript /path/to/apple/macos/split-library.applescript photos 2018 2020 "Photos Library"`
2. **Show Notification** with the script's output.

Add a second **Run Shell Script** with `videos` in place of `photos`, or make two Shortcuts. The first run asks you to allow Shortcuts to control PowerPhotos.

Before the first run, in PowerPhotos settings keep both edit versions, keep Live Photo videos and do not reduce bursts, so edits and Live Photos survive the copy. Hidden photos are copied only from the Hidden album.

## Find duplicates

The AppleScript `duplicate` and `merge` commands skip photos already in the destination (`skip duplicates`), which handles duplicates between libraries when copying. PowerPhotos' duplicate search has no Shortcuts or AppleScript action, so it cannot be started from a script. **Find Duplicates** is a reminder: **Show Notification** ("Run Find Duplicates in PowerPhotos") then **Open Library in PowerPhotos** on the library to search. The search itself is then run by hand.

## Schedule

Use Shortcuts' own time-of-day automation if your macOS has one (*check*). Otherwise `macos/schedule.sh` installs a launchd agent that runs `shortcuts run`:

```
SHORTCUT_NAME="Split Photo Library" SCHEDULE_HOUR=3 macos/schedule.sh install
SHORTCUT_NAME="Split Photo Library" macos/schedule.sh uninstall
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `SHORTCUT_NAME` | yes | | Name of the Shortcut to run (letters, digits, spaces, hyphens) |
| `SCHEDULE_HOUR` | no | `3` | Hour of day, 0-23 |
| `SCHEDULE_MINUTE` | no | `0` | Minute, 0-59 |

Output goes to `/tmp/local.apple.shortcut.<name>.log`. The first scheduled run may ask you to allow PowerPhotos and Photos access.
