---
name: refreshing-powerphotos-agents-docs
description: How the PowerPhotos AGENTS.md notes were obtained and how to refresh them when PowerPhotos is updated
metadata:
  type: reference
---

The notes in [[powerphotos-reports-notes]] and [[powerphotos-logs-notes]] were read from two files PowerPhotos 3.5 writes on the user's Mac and rewrites at every launch (so they describe the installed version). The sandbox cannot see the Mac, so the user copies them into the repo's gitignored `.tmp/`; do not commit the originals (they are Fat Cat's documentation).

**How they were found (2026-11-12).** `find ~/Library -maxdepth 4 -iname '*powerphotos*' -o -maxdepth 4 -iname '*fatcat*'` listed `~/Library/Logs/PowerPhotos` and `~/Library/Application Support/com.fatcatsoftware.PowerPhotos`; `ls -lt …/Reports` showed a hidden-looking `AGENTS.md` beside the `.ppreport` bundles, and its headings (`grep -n '^#'`) showed it documents the report schema. The logs one is named in the reports one ("see the Logs AGENTS.md in ~/Library/Logs/PowerPhotos/").

**To refresh.** Ask the user to run, one command at a time (they paste output with `pbcopy`):

```
cp "/Users/Tom/Library/Application Support/com.fatcatsoftware.PowerPhotos/Reports/AGENTS.md" /Volumes/workspace/projects/apple/.tmp/powerphotos-reports-AGENTS.md && cp "/Users/Tom/Library/Logs/PowerPhotos/AGENTS.md" /Volumes/workspace/projects/apple/.tmp/powerphotos-logs-AGENTS.md
```

(`/Volumes/workspace/projects/apple` on the Mac is `/workspace` in the sandbox.) Then read both files in `/workspace/.tmp/`, diff against the two notes files, and update them and `powerphotos_reports.py` if a schema, a result code or a log line changed. The header says "Written by PowerPhotos 3.5": compare that version.

**Why:** copy failures cannot be seen from AppleScript (`duplicate` answers at once), and the user asked that development follow PowerPhotos' own documentation.
