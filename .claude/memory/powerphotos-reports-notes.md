---
name: powerphotos-reports-notes
description: Condensed notes from PowerPhotos' own Reports AGENTS.md: report bundle layout, Assets.db tables, result codes, error blob
metadata:
  type: reference
---

From `~/Library/Application Support/com.fatcatsoftware.PowerPhotos/Reports/AGENTS.md` (PowerPhotos 3.5; refresh it as in [[refreshing-powerphotos-agents-docs]]). The original is `.tmp/powerphotos-reports-AGENTS.md` when it has been copied over.

**Reports.** One bundle per copy/merge, duplicate search or export, named `yyyy-MM-dd-HHmmss <title>.ppreport` (a copy's title is `Copy N photos to library “<destination>”`, with curly quotes). It is an ordinary directory. Pruned after 30 days unless the folder is under 250 MB. Opened in PowerPhotos from the Reports sidebar or with `open`.

**Bundle files.** `Info.plist` (always), `Assets.db` (SQLite, always for transfers), `operation.log` (hard link to the operation's log) and `operation_link.log` (symlink to it), `DuplicateSearchInfo.plist` / `DuplicateMatches.db` / `DuplicateDebugData.db` (duplicate searches only), `ImportScan-<uuid>.db` (import sources), `Assets/` (exports only).

**`Assets.db`.**
- `assets`: `libraryIdentifier`, `uuid` (PhotoKit UUID, may carry `/L0/001`), `assetMediaType` (1 image, 2 video), `name`, `filename`, `fileSize`, `date`, `localFilePath`, `status`, `result`, `resultDescription` (the error text for a failure). Primary key (libraryIdentifier, uuid).
- `reportMedatata` (the typo is deliberate): `operationType` (1 transfer, 2 duplicate search, 3 export), `operationSubtype`, `lastCompletedStage`, `wasCancelled`, `startDate`, `endDate` (REAL, seconds since the 2001 reference date; null while running), `error` (JSON text such as `{"code":10,"domain":"BWFoundation.BWCodableError","errorDescription":"...","recoverySuggestion":"..."}`; read as text, not bytes), `settings`, `version`.
- `libraryInfo`: `identifier`, `path`, `name`, `storeType` (source or destination), `assetCount`. `albumsCreated`. `errors` (`errorMessage`, `context`, `timestamp`: non-fatal operation errors).

**`assets.status`** (state before the operation): 1 complete, 2 incomplete, 3 missing (iCloud, never downloaded), 4 unsupported.
**`assets.result`**: 0 undecided, 1 already in destination, 2 skipped (incomplete, missing or unsupported), 3/4/5 duplicate non-keeper/keeper/deleted, 6 import success, 7 partial import success, 8 import failure, 9 analysis failed (a transfer copies it anyway), 10/11 export success/failure, 12 imported but hidden in a burst. Failures: 8, 9, 11. Successes: 1, 4, 5, 6, 7, 10, 12.

**Sample queries.** Outcome breakdown: `select result, count(*) from assets group by result`. Failures: `select uuid, filename, result, resultDescription from assets where result in (8, 11, 9)`.

**Privacy.** Reports hold UUIDs, file names, library paths and error messages with file paths; not credentials or photos (except export bundles).
