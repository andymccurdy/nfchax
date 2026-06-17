#!/usr/bin/env bash
#
# play-video.sh VIDEO_ID[,VIDEO_ID,...]
#
#   Atomically REPLACES the kiosk player's queue with the given video ids
#   (comma-separated, max 5).
#   - Starts the local web server (serve.py) if it isn't already running.
#   - Replaces the queue via the server's /replace API.
#   - Launches Firefox in kiosk mode if it isn't already running.
#
#   The open page polls the queue and plays its head. Because the page only
#   reloads the player when the head id changes, replacing the queue with a list
#   whose first id matches the currently-playing video continues playback
#   uninterrupted; a different first id switches the video.
#
#   Examples:
#     play-video.sh dQw4w9WgXcQ
#     play-video.sh dQw4w9WgXcQ,Y1ujpoDlgRU,9bZkp7q19f0
#
set -euo pipefail

VIDEO_IDS="${1:-}"
if [[ -z "$VIDEO_IDS" ]]; then
  echo "Usage: $0 VIDEO_ID[,VIDEO_ID,...]   (comma-separated, max 5)" >&2
  exit 1
fi

HOME_DIR="/home/andy"
APP_DIR="$HOME_DIR/nfchax"               # repo root (serve.py lives here)
WEB_ROOT="$APP_DIR/player"               # ONLY this dir is exposed by the web server
STATE_DIR="$HOME_DIR/kiosk-state"        # queue.json lives here, OUT of the source tree
PROFILE="$HOME_DIR/.kiosk-firefox"
PORT=8000
URL="http://127.0.0.1:${PORT}/youtube-fullscreen.html"

# 1) Ensure the local web server (queue owner) is up.
if ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  mkdir -p "$STATE_DIR"
  KIOSK_PORT="$PORT" KIOSK_WEB_ROOT="$WEB_ROOT" KIOSK_STATE_DIR="$STATE_DIR" \
    setsid python3 "$APP_DIR/serve.py" \
    >/tmp/httpd.log 2>&1 < /dev/null &
  disown
  sleep 1
fi

# 2) Atomically replace the queue via the server's API. The server drops blanks
#    and caps the list to 5.
out="$(curl -s -o /tmp/replace.out -w '%{http_code}' \
        -X POST --data-binary "$VIDEO_IDS" \
        "http://127.0.0.1:${PORT}/replace")"
if [[ "$out" == "200" ]]; then
  echo "Queue replaced. Queue is now: $(cat /tmp/replace.out)"
else
  echo "Replace failed (HTTP $out): $(cat /tmp/replace.out)" >&2
  exit 1
fi

# 3) Launch Firefox only if it isn't already running.
if pgrep firefox >/dev/null; then
  echo "Firefox already running — the open page will pick up the new queue."
else
  export XDG_RUNTIME_DIR=/run/user/1000
  export WAYLAND_DISPLAY=wayland-0
  export DISPLAY=:0
  export MOZ_ENABLE_WAYLAND=1
  rm -f "$PROFILE/lock" "$PROFILE/.parentlock" 2>/dev/null || true
  setsid firefox --kiosk --profile "$PROFILE" "$URL" \
    >/tmp/firefox-kiosk.log 2>&1 < /dev/null &
  disown
  echo "Launched Firefox in kiosk mode."
fi
