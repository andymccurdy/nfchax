#!/usr/bin/env python3
"""Static server for the kiosk page.

Serves the page and assets from this script's own directory (the repo), but
routes the mutable state file to a directory *outside* the source tree so it
never gets committed. This is the multi-directory trick the stdlib
``python3 -m http.server`` CLI can't do on its own.

Config via environment (with sensible defaults):
  KIOSK_PORT       TCP port to listen on             (default 8000)
  KIOSK_WEB_ROOT   directory of player assets to serve(default ./player)
  KIOSK_STATE_DIR  directory holding the state file  (default ~/kiosk-state)
"""
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Only the player assets are exposed — not the rest of the repo.
WEB_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("KIOSK_WEB_ROOT", os.path.join(SCRIPT_DIR, "player"))
))
STATE_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("KIOSK_STATE_DIR", "~/kiosk-state"))
)
PORT = int(os.environ.get("KIOSK_PORT", "8000"))
STATE_FILE = "current_video.txt"  # this URL path is served from STATE_DIR


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # Strip query/fragment, mirroring the base class, then check the path.
        clean = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if clean == STATE_FILE:
            return os.path.join(STATE_DIR, STATE_FILE)
        return super().translate_path(path)


if __name__ == "__main__":
    handler = partial(Handler, directory=WEB_ROOT)
    with ThreadingHTTPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"serving {WEB_ROOT} on 127.0.0.1:{PORT} "
              f"(/{STATE_FILE} -> {STATE_DIR})")
        httpd.serve_forever()
