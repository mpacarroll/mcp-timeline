#!/usr/bin/env bash
#
# Install the mcp-timeline services as macOS launchd user agents, so they
# start at login, restart if they crash, and survive a reboot.
#
# Why this exists: run from a terminal, these processes die when the window
# closes or the machine restarts. For the MCP server that means an outage
# you notice quickly. For the OwnTracks receiver it is worse and quieter:
# location fixes that arrive while nothing is listening are gone for good,
# and the database just silently stops growing.
#
# Usage:
#   ./deploy/install-macos.sh              install or reinstall
#   ./deploy/install-macos.sh --uninstall  remove all three services
#   ./deploy/install-macos.sh --dry-run    write plists to ./deploy/dry-run
#                                          and load nothing (works anywhere)
#
# Configuration lives in deploy/mcp-timeline.env, created on first run from
# the sample with a freshly generated token. Secrets stay in that file and
# in the generated plists, all written owner-only.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/deploy/mcp-timeline.env"
SAMPLE_ENV="$REPO_DIR/deploy/mcp-timeline.env.sample"
LOG_DIR="$HOME/Library/Logs/mcp-timeline"

SERVER_LABEL="com.mpacarroll.mcp-timeline.server"
OWNTRACKS_LABEL="com.mpacarroll.mcp-timeline.owntracks"
HEALTH_LABEL="com.mpacarroll.mcp-timeline.healthcapture"

MODE="install"
case "${1:-}" in
  --uninstall) MODE="uninstall" ;;
  --dry-run)   MODE="dry-run" ;;
  "")          ;;
  *)           echo "unknown option: $1" >&2; exit 2 ;;
esac

if [[ "$MODE" == "dry-run" ]]; then
  AGENTS_DIR="$REPO_DIR/deploy/dry-run"
else
  AGENTS_DIR="$HOME/Library/LaunchAgents"
fi

die() { echo "error: $*" >&2; exit 1; }

unload_agent() {
  [[ "$MODE" == "dry-run" ]] && return 0
  local label="$1"
  # bootout is the modern verb; fall back to unload on older systems.
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
    || launchctl unload "$AGENTS_DIR/$label.plist" 2>/dev/null \
    || true
}

if [[ "$MODE" == "uninstall" ]]; then
  for label in "$SERVER_LABEL" "$OWNTRACKS_LABEL" "$HEALTH_LABEL"; do
    unload_agent "$label"
    rm -f "$AGENTS_DIR/$label.plist"
    echo "removed $label"
  done
  echo
  echo "Services removed. The database and $ENV_FILE were left alone."
  exit 0
fi

if [[ "$MODE" == "install" && "$(uname)" != "Darwin" ]]; then
  die "installing needs macOS (launchd). Use --dry-run to inspect the plists here."
fi

if [[ ! -f "$ENV_FILE" ]]; then
  [[ -f "$SAMPLE_ENV" ]] || die "missing $SAMPLE_ENV"
  cp "$SAMPLE_ENV" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  # Generate a real token up front rather than shipping a placeholder that
  # looks configured but protects nothing.
  # Each endpoint gets its own secret, so rotating or leaking one does not
  # affect the other.
  tmp="$(mktemp)"
  cp "$ENV_FILE" "$tmp"
  for var in OWNTRACKS_TOKEN WEBHOOK_TOKEN; do
    tmp2="$(mktemp)"
    sed "s|^$var=.*|$var=$(openssl rand -hex 20)|" "$tmp" > "$tmp2"
    mv "$tmp2" "$tmp"
  done
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "created $ENV_FILE with freshly generated tokens"
  echo
fi

# shellcheck source=/dev/null
set -a; source "$ENV_FILE"; set +a

VENV_PYTHON="${VENV_PYTHON:-$REPO_DIR/.venv/bin/python}"
TIMELINE_DB="${TIMELINE_DB:-$REPO_DIR/timeline.db}"
MCP_PORT="${MCP_PORT:-8000}"
OWNTRACKS_PORT="${OWNTRACKS_PORT:-8002}"
HEALTH_PORT="${HEALTH_PORT:-8001}"
CAPTURE_DIR="${WEBHOOK_CAPTURE_DIR:-$REPO_DIR/captures}"
OWNTRACKS_MAX_ACCURACY_M="${OWNTRACKS_MAX_ACCURACY_M:-500}"
MCP_PUBLIC_HOST="${MCP_PUBLIC_HOST:-}"

if [[ "$MODE" == "install" ]]; then
  [[ -x "$VENV_PYTHON" ]] || die "no Python at $VENV_PYTHON. Create the venv first:
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  [[ -f "$TIMELINE_DB" ]] || echo "note: no database at $TIMELINE_DB yet; the receiver creates one"
fi
[[ -n "${OWNTRACKS_TOKEN:-}" ]] || die "OWNTRACKS_TOKEN is empty in $ENV_FILE"
[[ -n "${WEBHOOK_TOKEN:-}" ]] || die "WEBHOOK_TOKEN is empty in $ENV_FILE"
# The sample ships a placeholder so the file documents itself. Treat it as
# unset: installing with it would succeed here and then fail much later,
# and much more confusingly, as a 421 from the server.
if [[ -z "$MCP_PUBLIC_HOST" || "$MCP_PUBLIC_HOST" == *example.com ]]; then
  die "set MCP_PUBLIC_HOST in $ENV_FILE to your own tunnel hostname
(currently ${MCP_PUBLIC_HOST:-empty}, which is the sample placeholder).
Without a real value the MCP server rejects tunneled requests with
421 Invalid Host header."
fi

mkdir -p "$AGENTS_DIR"
[[ "$MODE" == "dry-run" ]] || mkdir -p "$LOG_DIR"

LOAD_FAILURES=()

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

# write_plist <label> <script> <env-pairs...> -- <script-args...>
# env pairs are KEY=VALUE strings; everything after -- is passed to the script.
write_plist() {
  local label="$1" script="$2"; shift 2
  local env_pairs=() script_args=() seen_sep=0
  for item in "$@"; do
    if [[ "$item" == "--" ]]; then seen_sep=1; continue; fi
    if [[ $seen_sep -eq 0 ]]; then env_pairs+=("$item"); else script_args+=("$item"); fi
  done

  local plist="$AGENTS_DIR/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
    echo '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    printf '  <key>Label</key><string>%s</string>\n' "$(xml_escape "$label")"
    echo '  <key>ProgramArguments</key>'
    echo '  <array>'
    printf '    <string>%s</string>\n' "$(xml_escape "$VENV_PYTHON")"
    printf '    <string>%s</string>\n' "$(xml_escape "$REPO_DIR/$script")"
    for arg in ${script_args+"${script_args[@]}"}; do
      printf '    <string>%s</string>\n' "$(xml_escape "$arg")"
    done
    echo '  </array>'
    printf '  <key>WorkingDirectory</key><string>%s</string>\n' "$(xml_escape "$REPO_DIR")"
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    printf '  <key>StandardOutPath</key><string>%s</string>\n' "$(xml_escape "$LOG_DIR/$label.log")"
    printf '  <key>StandardErrorPath</key><string>%s</string>\n' "$(xml_escape "$LOG_DIR/$label.err")"
    echo '  <key>EnvironmentVariables</key>'
    echo '  <dict>'
    # Python block-buffers stdout when it is a file rather than a terminal,
    # so a long-running service's log stays empty until the buffer fills.
    # That made a working capture endpoint look broken.
    echo '    <key>PYTHONUNBUFFERED</key><string>1</string>'
    for pair in ${env_pairs+"${env_pairs[@]}"}; do
      printf '    <key>%s</key><string>%s</string>\n' \
        "$(xml_escape "${pair%%=*}")" "$(xml_escape "${pair#*=}")"
    done
    echo '  </dict>'
    echo '</dict>'
    echo '</plist>'
  } > "$plist"

  # Plists carry bearer tokens, so keep them owner-only.
  chmod 600 "$plist"
  unload_agent "$label"
  if [[ "$MODE" != "dry-run" ]]; then
    # launchctl load returns 0 even when it prints "Load failed", so its exit
    # status cannot be trusted. Ask launchctl afterwards whether the job is
    # actually registered, rather than reporting a success we never checked.
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null \
      || launchctl load "$plist" 2>/dev/null || true
    if launchctl list 2>/dev/null | grep -q "[[:space:]]$label\$"; then
      echo "loaded  $label"
    else
      echo "FAILED to load $label (plist written to $plist)"
      LOAD_FAILURES+=("$label")
    fi
  else
    echo "wrote $plist"
  fi
}

write_plist "$SERVER_LABEL" "server.py" \
  "TIMELINE_DB=$TIMELINE_DB" \
  "MCP_TRANSPORT=streamable-http" \
  "MCP_HOST=127.0.0.1" \
  "MCP_PORT=$MCP_PORT" \
  "MCP_PUBLIC_HOST=$MCP_PUBLIC_HOST"

write_plist "$OWNTRACKS_LABEL" "owntracks_receiver.py" \
  "TIMELINE_DB=$TIMELINE_DB" \
  "OWNTRACKS_TOKEN=$OWNTRACKS_TOKEN" \
  "OWNTRACKS_MAX_ACCURACY_M=$OWNTRACKS_MAX_ACCURACY_M" \
  -- "$OWNTRACKS_PORT"

# Capture-only, and deliberately not writing to the database: the Apple
# Health payload format is still unknown, so this stores raw bodies and
# prints their structure until there is a parser worth trusting.
write_plist "$HEALTH_LABEL" "webhook_receiver.py" \
  "WEBHOOK_TOKEN=$WEBHOOK_TOKEN" \
  "WEBHOOK_CAPTURE_DIR=$CAPTURE_DIR" \
  -- "$HEALTH_PORT"

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "Dry run: nothing was loaded. Inspect the plists above."
  exit 0
fi

echo
if [[ ${#LOAD_FAILURES[@]} -gt 0 ]]; then
  echo
  echo "WARNING: ${#LOAD_FAILURES[@]} service(s) did not load: ${LOAD_FAILURES[*]}"
  echo "Most often something else is already bound to the port, commonly a copy"
  echo "still running from a terminal. Check with:"
  echo "  lsof -i :$MCP_PORT -i :$HEALTH_PORT -i :$OWNTRACKS_PORT"
  echo "and read the matching .err file in $LOG_DIR"
  echo
else
  echo
  echo "All three services loaded and will start at login."
fi
echo
echo "  status:  launchctl list | grep mcp-timeline"
echo "  logs:    tail -f $LOG_DIR/*.log"
echo "  remove:  ./deploy/install-macos.sh --uninstall"
echo
# Deliberately not echoing the tokens. Printing a secret to the terminal
# puts it in scrollback, and in any transcript pasted somewhere for help,
# which is how both of these leaked once already. Print where to read them
# instead, so copying one is a decision rather than an accident.
echo "Endpoints:"
echo "  location:  http://localhost:$OWNTRACKS_PORT   (OwnTracks app posts here)"
echo "  health:    http://localhost:$HEALTH_PORT   (captures to $CAPTURE_DIR)"
echo
echo "Point one tunnel public hostname at each."
echo
echo "Bearer tokens are in $ENV_FILE and are not printed here."
echo "Read one when you need it, for example:"
echo "  grep '^OWNTRACKS_TOKEN=' $ENV_FILE | cut -d= -f2"
echo "  grep '^WEBHOOK_TOKEN=' $ENV_FILE | cut -d= -f2"
