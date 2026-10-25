# PowerPhotos Shortcuts (macOS)

[PowerPhotos](https://www.fatcatsoftware.com/powerphotos/) is Mac-only, so none of this runs on iOS or iPadOS. The work is done by Shortcuts that use PowerPhotos' own Shortcuts actions; `macos/schedule.sh` runs one on a schedule. A Shortcut is a binary file that cannot be reviewed in git, so the recipes below are the source of truth. Build them in the Shortcuts app and name them as shown.

The actions and filters named here come from the [PowerPhotos Shortcuts docs](https://www.fatcatsoftware.com/powerphotos/VersionedDocs/v9/automation_shortcuts.html). Items marked *check* were not confirmed there; open the action in Shortcuts to see what it offers.

## Split Photo Library

Copies one year of the System Photo Library into its own library. Repeat with each year in a list.

1. **List** of years (for example 2019, 2020 ...), then **Repeat with Each**.
2. **Create New Library**, in the folder that holds your split libraries, named `photos-<year>`. Photos for a year and videos for a year go in separate libraries (`photos-<year>`, `videos-<year>`) the way the archive is organized.
3. **Get System Library**.
4. **Find Photos** in the system library where **Date** is between 1 January and 31 December of the year. To split by media type, add a media-type filter (*check*; if there is none, make a Photos smart album with a Media Type rule and use **Get Photos** on that album).
5. **Copy Photos** to the new library. Choose **copy**, not move, so the system library is left alone.
6. **Show Notification** when the loop ends.

Before the first run, in PowerPhotos settings keep both edit versions, keep Live Photo videos and do not reduce bursts, so edits and Live Photos survive the copy. Hidden photos are copied only from the Hidden album.

## Find duplicates

PowerPhotos' duplicate search has no Shortcuts or AppleScript action, so it cannot be started from a script. **Find Duplicates** is a reminder: **Show Notification** ("Run Find Duplicates in PowerPhotos") then **Open Library in PowerPhotos** on the library to search. The search itself is then run by hand.

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

Output goes to `/tmp/local.icloud-tools.shortcut.<name>.log`. The first scheduled run may ask you to allow PowerPhotos and Photos access.
