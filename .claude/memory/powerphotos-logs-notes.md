---
name: powerphotos-logs-notes
description: Condensed notes from PowerPhotos' own Logs AGENTS.md: the .log files and their session lines, the unreadable Diagnostics buffer, log show recipes
metadata:
  type: reference
---

From `~/Library/Logs/PowerPhotos/AGENTS.md` (PowerPhotos 3.5; refresh it as in [[refreshing-powerphotos-agents-docs]]). The original is `.tmp/powerphotos-logs-AGENTS.md` when it has been copied over.

**User logs.** `yyyy-MM-dd-HHmmss-<LoggerName>.log`, plain text, one entry per line: `yyyy-MM-dd HH:mm:ss.SSS  <message>` in local time. `General` is the session log (header with versions, libraries, PhotoKit authorization, iCloud mode, whether Photos was open; ends with `PowerPhotos is quitting` on a clean exit). An operation's log is named from its title and is the same file as the bundle's `operation.log` in its report.

**Lines worth reading.**
- `Session “<name>” started — <scale>`, then exactly one of `Session “<name>” completed after <duration> — <counts>`, `… cancelled after …`, `… failed (<error description>) after …`. A start with no completion means it did not finish.
- `Error shown: <description>`: the text of an alert PowerPhotos showed the user. A log whose last line is this stopped on an error the user saw (often before any session line), not on a crash or hang.
- `An error occurred (<domain> <code>): <operation>: <description>` for any failed top-level operation, with underlying errors indented beneath (surfaces Photos error codes).
- `Preflight complete: …` (scale of the work), `Opened library “X” in <duration>`, `PhotoKit authorization status: …`, "Photos is open / needs to quit" warnings, `cancelled by user` (not an error), iCloud download failures ("asset data unavailable"), `Cleaning iCloud downloads…` (informational even when it says failed).

**`Diagnostics/`.** `*.pplogz` (binary, compressed; decoded by Fat Cat's `fcsarchive diag`) and `*.buffer` (memory-mapped, binary-framed): technical detail and a copy of every user line. Not worth parsing by hand: `PowerPhotos.buffer` is a ring that overwrites old entries and its entries carry no readable time. Use the report and the operation log instead.

**System context.** `log show --start "…" --end "…" --info --debug --predicate '(process == "Photos" OR process == "photolibraryd" OR process == "PowerPhotos" …)'` fetches Photos and `photolibraryd` messages for a window; file paths show as `<private>` unless a profile enabling private data is installed. Debug logging (Command-Option-L in PowerPhotos) also records them under `Diagnostics/System/`.

**Retention.** Logs older than 30 days are pruned unless the folder is under 250 MB.
