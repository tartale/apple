---
name: powerphotos-agents-docs
description: PowerPhotos documents its reports and logs for tools in two AGENTS.md files; read the notes before touching copy-failure detection
metadata:
  type: reference
---

PowerPhotos 3.5 writes two documents meant for AI agents, on the user's Mac: one in its Reports folder (the `.ppreport` bundles and `Assets.db`) and one in `~/Library/Logs/PowerPhotos/` (the `.log` files). Their content is condensed in [[powerphotos-reports-notes]] and [[powerphotos-logs-notes]]; how they were obtained and how to refresh them is in [[refreshing-powerphotos-agents-docs]]. The originals are in `.tmp/powerphotos-reports-AGENTS.md` and `.tmp/powerphotos-logs-AGENTS.md` (gitignored) when the user has copied them over.

**Why:** the user asked that development use these as guidance. A copy failure cannot be seen from the AppleScript call (`duplicate` answers at once), so `copy` and `split` read the report PowerPhotos wrote for the copy (`src/apple/photos/powerphotos_reports.py`) instead of guessing from the library not changing.

**How to apply:** match a copy to its report by the new bundle named with the destination library in curly quotes; match photos by Photos UUID (the part before `/L0/001`); an operation-level `error` (JSON, stored as text) or an `Error shown:` / `Session … failed` line in the bundle's `operation.log` means the operation itself failed. Re-read the notes before changing any of this.
