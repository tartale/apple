# apple

Shell scripts (in `photos/`) that wrap rclone to back up iCloud Photos on a Synology NAS. README.md has the layout, identity rule and configuration; this file holds what the code doesn't say.

## Rules for working here

- **The scripts run on the NAS, often while I'm editing.** Keep each script's body inside `main() { ... }` ending with `main "$@"; exit`, so bash has parsed it all before running. Replace files atomically (write `<file>.new`, then `mv`), never edit in place. The user says when a run is in progress; if unsure, ask before touching `photos/*.sh`.
- **A backup, not a mirror.** Never add deletion or pruning of `originals/`, `by-date/` or `albums/`.
- **Configuration is environment variables only**, with no system-specific defaults (no remote names, paths or accounts in code). Booleans are written `true`/`false`.
- **Missing prerequisites fail with a copy/paste install command** (`require_tools` in `photos/lib.sh`), not a README note.
- Shared setup (env check, lock, logging, `DRY_RUN`) lives in `photos/lib.sh`, sourced inside `main`.

## Identity

A photo is its file name + size + modification time. `backup.sh` builds `albums.csv` (`path,size,mtime,stored`) from one sorted `rclone lsf -R` listing; an album entry is matched to the All Photos listing, then to what `originals/` already holds, and only otherwise downloaded once to the first album path in C-locale order. `organize.sh` only reads `albums.csv` and `originals/`. See README for the user-facing rule.

## Testing

- `photos/lint.sh` (shellcheck) and `photos/test.sh` (bats, `test/`). Run both before pushing.
- Tests use `test/fake-rclone`, a local-directory stand-in (`FAKE_CLOUD/<remote>/<path>`; `FAKE_FAIL=lsf|copy-album` injects failures). There is no real iCloud account in the sandbox, so behavior against the real remote is validated by the user on the NAS: `DRY_RUN=true photos/backup.sh`, then a real run.
- Mutation-check a test written for a bug: reintroduce the bug, confirm the test fails, restore.
- bats is baked into the sandbox image (`.claude/sandbox/plugin.sh`). Until the image is rebuilt it can be installed from bats-core into `.tmp/` (gitignored) and put on `PATH`.

## Issues

Fixed issues get the `validate` and `needs attention` labels and stay open for the user to close; PRs say `Refs #N`, never `Closes #N`.
