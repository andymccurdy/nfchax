#!/usr/bin/env bash
#
# play-video.sh VIDEO_ID
#
#   Sets the YouTube video shown on the HDMI-connected kiosk display.
#   - If Firefox isn't running, starts the local web server (if needed)
#     and launches Firefox in kiosk mode showing the video.
#   - If Firefox is already running, the open page detects the change
#     (it polls the state file) and switches video in place — no relaunch.
#
set -euo pipefail

VIDEO_ID="${1:-}"
if [[ -z "$VIDEO_ID" ]]; then
  echo "Usage: $0 VIDEO_ID" >&2
  exit 1
fi

HOME_DIR="/home/andy"
APP_DIR="$HOME_DIR/nfchax"               # repo root (serve.py lives here)
WEB_ROOT="$APP_DIR/player"               # ONLY this dir is exposed by the web server
STATE_DIR="$HOME_DIR/kiosk-state"        # mutable state, kept OUT of the source tree
STATE_FILE="$STATE_DIR/current_video.txt"
PROFILE="$HOME_DIR/.kiosk-firefox"
PORT=8000
URL="http://127.0.0.1:${PORT}/youtube-fullscreen.html"

# 1) Publish the desired video id (the page polls this file).
mkdir -p "$STATE_DIR"
printf '%s\n' "$VIDEO_ID" > "$STATE_FILE"

# 2) Ensure the local web server is up. serve.py serves the repo but routes
#    the state file to $STATE_DIR (outside the source tree).
if ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  KIOSK_PORT="$PORT" KIOSK_WEB_ROOT="$WEB_ROOT" KIOSK_STATE_DIR="$STATE_DIR" \
    setsid python3 "$APP_DIR/serve.py" \
    >/tmp/httpd.log 2>&1 < /dev/null &
  disown
  sleep 1
fi

# 3) Launch Firefox only if it isn't already running.
if pgrep firefox >/dev/null; then
  echo "Firefox already running — switching to video $VIDEO_ID (page will pick it up within a couple seconds)."
else
  export XDG_RUNTIME_DIR=/run/user/1000
  export WAYLAND_DISPLAY=wayland-0
  export DISPLAY=:0
  export MOZ_ENABLE_WAYLAND=1
  rm -f "$PROFILE/lock" "$PROFILE/.parentlock" 2>/dev/null || true
  setsid firefox --kiosk --profile "$PROFILE" "$URL" \
    >/tmp/firefox-kiosk.log 2>&1 < /dev/null &
  disown
  echo "Launched Firefox in kiosk mode with video $VIDEO_ID."
fi
