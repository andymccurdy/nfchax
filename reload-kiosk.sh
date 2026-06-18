#!/usr/bin/env bash
#
# reload-kiosk.sh
#
#   Restarts the whole kiosk stack: kills Firefox AND serve.py, then relaunches
#   both. Use this to pick up changed code/assets (player/youtube-fullscreen.html
#   or serve.py). The queue is persisted in ~/kiosk-state/queue.json and reloaded
#   by serve.py on startup, so the current queue survives the restart.
#
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # repo root (this script lives here)
WEB_ROOT="$APP_DIR/player"               # ONLY this dir is exposed by the web server
STATE_DIR="$HOME/kiosk-state"            # queue.json lives here, OUT of the source tree
PROFILE="$HOME/.kiosk-firefox"
PORT=8000
URL="http://127.0.0.1:${PORT}/youtube-fullscreen.html"

wait_gone() {  # wait_gone <pgrep-pattern> ; up to ~5s
  local pat="$1"
  for _ in $(seq 1 50); do
    pgrep -f "$pat" >/dev/null || return 0
    sleep 0.1
  done
  return 1
}

# 1) Stop browser then server (browser depends on the server).
pkill firefox 2>/dev/null || true
wait_gone 'firefox' || pkill -9 firefox 2>/dev/null || true

pkill -f "$APP_DIR/serve.py" 2>/dev/null || true
wait_gone "$APP_DIR/serve.py" || pkill -9 -f "$APP_DIR/serve.py" 2>/dev/null || true

# 2) Start the queue server (owns + persists the queue).
mkdir -p "$STATE_DIR"
KIOSK_PORT="$PORT" KIOSK_WEB_ROOT="$WEB_ROOT" KIOSK_STATE_DIR="$STATE_DIR" \
  setsid python3 "$APP_DIR/serve.py" >/tmp/httpd.log 2>&1 < /dev/null &
disown
# Wait for it to accept connections before launching the page.
for _ in $(seq 1 50); do
  ss -ltn 2>/dev/null | grep -q ":${PORT} " && break
  sleep 0.1
done

# 3) Launch Firefox in kiosk mode (same Wayland env as play-video.sh).
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY=wayland-0
export DISPLAY=:0
export MOZ_ENABLE_WAYLAND=1
rm -f "$PROFILE/lock" "$PROFILE/.parentlock" 2>/dev/null || true

setsid firefox --kiosk --profile "$PROFILE" "$URL" \
  >/tmp/firefox-kiosk.log 2>&1 < /dev/null &
disown
echo "Reloaded kiosk stack: serve.py on :${PORT} + Firefox ($URL)."
