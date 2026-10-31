# PowerPhotos Shortcuts (macOS)

[PowerPhotos](https://www.fatcatsoftware.com/powerphotos/) is Mac-only, so none of this runs on iOS or iPadOS. The work is done by `./apple photos` commands, which drive PowerPhotos through AppleScript files bundled with the CLI; you never call AppleScript yourself. `./apple schedule` runs a command on a schedule. A Shortcut is only needed if you want a button; it is a thin wrapper around a command, built in the Shortcuts app.

The actions and filters named here come from the [PowerPhotos Shortcuts docs](https://www.fatcatsoftware.com/powerphotos/VersionedDocs/v9/automation_shortcuts.html). Items marked *check* were not confirmed there.

## Split Photo Library

```
./apple photos split --from "Default Library" --media photos --years 2018-2020
```

Copies one media type of a library into one library per year (`photos-2018`, `photos-2019`, ...), using PowerPhotos' `duplicate` command with `skip duplicates`, so reruns add only what is new and nothing is removed from the source. `./apple photos copy` does one date range into any destination:

```
./apple photos copy --from "Default Library" --to photos-2018 --since 2018-01-01 --until 2018-12-31 --media videos
```

`./apple photos libraries` lists the names PowerPhotos knows, with each library's path and Photos database version (a destination with a lower version than the source needs a Photos upgrade). The destination libraries must already exist in PowerPhotos (its scripting has no command to create one). A photo is a video by its file extension, because PowerPhotos' `photo` class has no media-type property.

A destination made by an older Photos must be upgraded by Photos before it accepts photos. The command compares each destination's Photos database version (`PLModelVersion`) with the source's before copying, and again if PowerPhotos refuses a copy. `--on-upgrade` chooses what happens: `skip` (the default, for unattended runs) leaves that destination out and says so, `ask` prompts in the terminal (skip, quit, or open the library in Photos to upgrade it and retry; it skips when there is no terminal), `stop` ends with an error.

Only one copy or split runs at a time; a second one stops at once. It cannot see a copy started from the PowerPhotos window.

The Shortcut is one **Run Shell Script** action, `/path/to/this/checkout/apple photos split --from "Default Library" --media photos --years 2018-2020`, followed by **Show Notification** with its output. The first run asks you to allow Shortcuts to control PowerPhotos.

Before the first run, in PowerPhotos settings keep both edit versions, keep Live Photo videos and do not reduce bursts, so edits and Live Photos survive the copy. Hidden photos are copied only from the Hidden album.

## Find duplicates

The AppleScript `duplicate` and `merge` commands skip photos already in the destination (`skip duplicates`), which handles duplicates between libraries when copying. PowerPhotos' duplicate search has no Shortcuts or AppleScript action, so it cannot be started from a script. **Find Duplicates** is a reminder: **Show Notification** ("Run Find Duplicates in PowerPhotos") then **Open Library in PowerPhotos** on the library to search. The search itself is then run by hand.

## Schedule

`./apple schedule` runs any `apple` command every day through a launchd agent, so no Shortcut is needed:

```
./apple schedule add nightly-split --at 03:00 -- photos split --from "Default Library" --media photos --years 2018-2020
./apple schedule list
./apple schedule remove nightly-split
```

`--at` is 24-hour `HH:MM` (default `03:00`). Adding a name again replaces it. `--dry-run` shows the launchd agent without installing it. The agent is `~/Library/LaunchAgents/local.apple.<name>.plist`, and output goes to `~/Library/Logs/apple/<name>.log`. launchd gives the command a minimal environment, so a scheduled command that needs variables (such as the `ICLOUD_*` ones) cannot see your shell's. The first run may ask you to allow access to PowerPhotos and Photos.
