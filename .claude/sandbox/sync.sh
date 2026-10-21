#!/usr/bin/env bash
# Handles the patch a sandbox session wrote with claude-user-sync (user-scope rules
# and memories). Usage: .claude/sandbox/sync.sh [apply|show|status]
#   apply   (default) apply the patch to your claude-user checkout and push it
#   show    page through the patch without applying it
#   status  say whether a patch is waiting and which files it changes
# CS_USER_CONFIG_CHECKOUT is that checkout (default: ~/.claude-user).
set -euo pipefail

THIS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="${THIS_SCRIPT_DIR}/../../.tmp/claude-user.patch"
CHECKOUT="${CS_USER_CONFIG_CHECKOUT:-$HOME/.claude-user}"

# Colors only on a terminal, and never when NO_COLOR is set.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'; RESET=$'\e[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; CYAN=""; RESET=""
fi

# The patch with additions in green, deletions in red and hunk headers in cyan.
show_patch() {
    awk -v b="$BOLD" -v r="$RED" -v g="$GREEN" -v c="$CYAN" -v d="$DIM" -v z="$RESET" '
        /^(diff |index |--- |\+\+\+ |Subject: )/ { print b $0 z; next }
        /^@@/                                   { print c $0 z; next }
        /^-/                                    { print r $0 z; next }
        /^\+/                                   { print g $0 z; next }
                                                { print d $0 z }' "$PATCH"
}
page_patch() {
    if [ -t 1 ]; then show_patch | LESS="${LESS:-R}" "${PAGER:-less}"; else show_patch; fi
}

[ -s "$PATCH" ] || { echo "no patch at $PATCH; nothing to do"; exit 0; }

case "${1:-apply}" in
    status)
        echo "a patch is waiting:"
        git apply --stat "$PATCH"
        ;;
    show)
        page_patch
        ;;
    apply)
        [ -d "$CHECKOUT/.git" ] || { echo "$CHECKOUT is not a git checkout; set CS_USER_CONFIG_CHECKOUT" >&2; exit 1; }
        echo "Patch from the sandbox:"
        git apply --stat "$PATCH"
        echo
        echo "It changes the rules and memories every future session starts from. Read it before applying."
        # Enter shows the diff first; after that, Enter declines.
        shown=0
        while :; do
            if [ "$shown" = 0 ]; then hint="[y/n/D]"; else hint="[y/N/d]"; fi
            read -r -p "Apply and push to $CHECKOUT? $hint " a
            case "$a" in
                [Dd]*) page_patch; shown=1 ;;
                [Yy]*) break ;;
                [Nn]*) echo "not applied"; exit 1 ;;
                "")    if [ "$shown" = 0 ]; then page_patch; shown=1; else echo "not applied"; exit 1; fi ;;
            esac
        done
        git -C "$CHECKOUT" pull --ff-only --quiet
        git -C "$CHECKOUT" am < "$PATCH" || { git -C "$CHECKOUT" am --abort; echo "the patch does not apply; nothing changed" >&2; exit 1; }
        git -C "$CHECKOUT" push
        rm -f "$PATCH"
        ;;
    *)
        echo "usage: sync.sh [apply|show|status]" >&2
        exit 1
        ;;
esac
