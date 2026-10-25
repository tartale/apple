#!/bin/bash
# Run a Shortcut on a daily schedule through a launchd agent, on macOS.
# Usage: SHORTCUT_NAME="Split Photo Library" macos/schedule.sh install|uninstall
# The whole script lives in main so bash has parsed all of it before running any of it.
main() {
  local action=${1:-} label plist
  if [ "$(uname)" != Darwin ]; then echo "macos/schedule.sh runs on macOS only" >&2; return 2; fi
  case $action in install|uninstall) ;; *) echo "Usage: SHORTCUT_NAME=<name> $0 install|uninstall" >&2; return 2;; esac
  if [ -z "${SHORTCUT_NAME:-}" ]; then echo "SHORTCUT_NAME is not set (the name of the Shortcut to run)" >&2; return 2; fi

  # Shortcut names go into a file name and XML, so keep them to letters, digits, spaces and hyphens
  if [[ ! $SHORTCUT_NAME =~ ^[A-Za-z0-9\ -]+$ ]]; then echo "SHORTCUT_NAME may contain only letters, digits, spaces and hyphens" >&2; return 2; fi
  local hour=${SCHEDULE_HOUR:-3} minute=${SCHEDULE_MINUTE:-0}
  if [[ ! $hour =~ ^[0-9]+$ ]] || ((10#$hour > 23)) || [[ ! $minute =~ ^[0-9]+$ ]] || ((10#$minute > 59)); then
    echo "SCHEDULE_HOUR must be 0-23 and SCHEDULE_MINUTE 0-59" >&2; return 2
  fi

  label="local.icloud-tools.shortcut.${SHORTCUT_NAME// /-}"
  plist="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}/$label.plist"

  if [ "$action" = uninstall ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "Removed $label"
    return 0
  fi

  if ! command -v shortcuts >/dev/null; then echo "The 'shortcuts' command is missing; it ships with macOS 12 or later" >&2; return 2; fi
  mkdir -p "$(dirname "$plist")"
  cat > "$plist.new" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/shortcuts</string>
    <string>run</string>
    <string>$SHORTCUT_NAME</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$((10#$hour))</integer>
    <key>Minute</key><integer>$((10#$minute))</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/$label.log</string>
  <key>StandardErrorPath</key><string>/tmp/$label.log</string>
</dict>
</plist>
PLIST
  mv "$plist.new" "$plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "Scheduled '$SHORTCUT_NAME' daily at $(printf '%02d:%02d' "$((10#$hour))" "$((10#$minute))") ($plist)"
}

main "$@"; exit
