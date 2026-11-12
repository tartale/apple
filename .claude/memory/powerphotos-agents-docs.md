---
name: powerphotos-agents-docs
description: PowerPhotos documents its reports and logs for tools in two AGENTS.md files; read them before touching copy-failure detection
metadata:
  type: reference
---

PowerPhotos 3.5 writes two documents meant for AI agents, rewritten at every launch, on the Mac:

- `~/Library/Application Support/com.fatcatsoftware.PowerPhotos/Reports/AGENTS.md`: the `.ppreport` bundles (one per copy, merge, duplicate search or export, named `yyyy-MM-dd-HHmmss <title>.ppreport`), the `Assets.db` schema (`assets.result`: 6 imported, 7 partial, 8 import failed, 9 analysis failed, 2 skipped, 1 already in destination, 12 hidden in burst; `assets.resultDescription` is the error text; `reportMedatata.endDate`, `.wasCancelled`, `.error` a JSON blob with `errorDescription` and `recoverySuggestion`; the `errors` table).
- `~/Library/Logs/PowerPhotos/AGENTS.md`: the `.log` files (plain text; `Session “…” started/completed/cancelled/failed (…)`, `Error shown: …`), the binary `Diagnostics/*.pplogz` and `PowerPhotos.buffer` (not worth reading), `log show` recipes.

Copies are in `.tmp/powerphotos-reports-AGENTS.md` and `.tmp/powerphotos-logs-AGENTS.md` (gitignored; the user copies them over with `cp`, since the sandbox cannot see the Mac). Re-read them when PowerPhotos is updated.

**Why:** the user asked that development use these as guidance. Copy failures cannot be seen from the AppleScript call (`duplicate` answers at once), so `copy` and `split` read the report PowerPhotos wrote for the copy (`powerphotos_reports.py`) instead of guessing from the library not changing.

**How to apply:** match a copy to its report by the new bundle named with the destination library in curly quotes; match photos by Photos UUID (the part before `/L0/001`); an operation-level `error` or an `Error shown:` / `Session … failed` line in the bundle's `operation.log` means the operation itself failed.
