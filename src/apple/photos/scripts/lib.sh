# shellcheck shell=bash disable=SC2034  # DRY is read by the scripts that source this file
# Shared by backup.sh and organize.sh; each sources it at the start of main.
# Not run directly.

# Exits 2 naming every required environment variable that is not set
require_env() {
  local v missing=()
  for v in ICLOUD_ROOT ICLOUD_ACCOUNTS; do [ -n "${!v:-}" ] || missing+=("$v"); done
  if ((${#missing[@]})); then echo "Missing required environment variables: ${missing[*]}" >&2; exit 2; fi
}

# Exits 2 when a program the scripts need is missing, printing a command that installs it.
# find and stat must be the GNU versions (find -printf, stat -c).
require_tools() {
  local t missing=() pkgs=() pm install
  for t in "$@"; do
    case $t in
      find) find / -maxdepth 0 -printf '' 2>/dev/null || { missing+=("GNU find"); pkgs+=(findutils); } ;;
      stat) stat -c %s / >/dev/null 2>&1 || { missing+=("GNU stat"); pkgs+=(coreutils); } ;;
      *) command -v "$t" >/dev/null && continue
         missing+=("$t")
         case $t in
           flock) pkgs+=(util-linux);;
           awk) pkgs+=(gawk);;
           *) pkgs+=("${t##*/}");;
         esac ;;
    esac
  done
  ((${#missing[@]})) || return 0

  pm=
  for t in brew apt-get dnf apk; do
    if command -v "$t" >/dev/null; then pm=$t; break; fi
  done
  case $pm in
    apt-get|dnf) install="$pm install -y ${pkgs[*]}";;
    apk) install="apk add ${pkgs[*]}";;
    brew) install="brew install ${pkgs[*]}";;
    *) install="";;
  esac
  [ -z "$install" ] || [ "$pm" = brew ] || [ "$(id -u)" = 0 ] || install="sudo $install"

  echo "Missing required programs: ${missing[*]}" >&2
  if [ -n "$install" ]; then
    echo "Install them with:" >&2
    echo "  $install" >&2
    if [ "$pm" = brew ] && [[ " ${pkgs[*]} " == *" findutils "* || " ${pkgs[*]} " == *" coreutils "* ]]; then
      echo "Homebrew installs the GNU tools as gfind and gstat; put them first on PATH:" >&2
      echo "  export PATH=\"\$(brew --prefix findutils)/libexec/gnubin:\$(brew --prefix coreutils)/libexec/gnubin:\$PATH\"" >&2
    fi
  else
    echo "Install the packages that provide: ${pkgs[*]}" >&2
  fi
  exit 2
}

# Takes the lock shared by both scripts on fd 9, or exits 0 with the given message when another run holds it
take_lock() {
  exec 9>"${ICLOUD_LOCK_FILE:-/tmp/rclone-icloudphotos.lock}"
  flock -n 9 || { echo "$1"; exit 0; }
}

# Sets DRY to true or false from DRY_RUN (true/1/yes/on, any case, is on)
parse_dry_run() {
  case ${DRY_RUN:-false} in
    [Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn]) DRY=true;;
    *) DRY=false;;
  esac
}

# Writes to the log file, and to the terminal too when run interactively
log() {
  local msg
  msg="$(date '+%F %T') $*"
  echo "$msg" >> "$LOG"
  if [ -t 1 ]; then echo "$msg"; fi
}
