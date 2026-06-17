#!/usr/bin/env python3
"""Static server + queue API for the kiosk player.

Serves player assets from WEB_ROOT (only that directory is web-exposed) and owns
a small FIFO queue of YouTube video ids. The queue is the single source of truth
for what plays; it is mutated ONLY here (one process, lock-guarded) so the
browser (which advances on video end) and play-video.sh (which enqueues) can
never race. The queue is persisted to STATE_DIR/queue.json so it survives a
browser reload.

HTTP API (all JSON, no-store):
  GET  /queue     -> {"queue": ["id", ...]}
  POST /enqueue   body = raw video id  -> {"queue": [...], "added": bool}
                  (rejected with 409 if the queue already holds MAX_QUEUE items)
  POST /replace   body = JSON array OR comma-separated ids; atomically replaces
                  the whole queue (capped to MAX_QUEUE) -> {"queue": [...]}
  POST /advance   removes the head (the just-finished video) -> {"queue": [...]}

Static:
  GET  /<path>    served from WEB_ROOT

Config via env: KIOSK_PORT, KIOSK_WEB_ROOT, KIOSK_STATE_DIR
"""
import json
import os
import threading
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
QUEUE_FILE = os.path.join(STATE_DIR, "queue.json")
MAX_QUEUE = 5

_lock = threading.Lock()  # serialises all queue reads/writes


def _load_queue():
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data][:MAX_QUEUE]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return []


def _save_queue(q):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f)
    os.replace(tmp, QUEUE_FILE)  # atomic


class Handler(SimpleHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n).decode().strip() if n else ""

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/queue":
            with _lock:
                return self._send_json({"queue": _load_queue()})
        return super().do_GET()  # static file from WEB_ROOT

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/enqueue":
            vid = self._read_body()
            if vid.startswith("{"):  # also accept {"video_id": "..."}
                try:
                    vid = str(json.loads(vid).get("video_id", "")).strip()
                except ValueError:
                    vid = ""
            if not vid:
                return self._send_json({"error": "missing video id"}, 400)
            with _lock:
                q = _load_queue()
                added = len(q) < MAX_QUEUE
                if added:
                    q.append(vid)
                    _save_queue(q)
                return self._send_json({"queue": q, "added": added},
                                       200 if added else 409)

        if path == "/replace":
            body = self._read_body()
            if body.startswith("["):
                try:
                    ids = [str(x).strip() for x in json.loads(body)]
                except ValueError:
                    return self._send_json({"error": "invalid JSON array"}, 400)
            else:
                ids = [x.strip() for x in body.split(",")]
            ids = [x for x in ids if x][:MAX_QUEUE]  # drop blanks, cap length
            with _lock:
                _save_queue(ids)
                return self._send_json({"queue": ids})

        if path == "/advance":
            with _lock:
                q = _load_queue()
                if q:
                    q.pop(0)
                    _save_queue(q)
                return self._send_json({"queue": q})

        return self._send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    handler = partial(Handler, directory=WEB_ROOT)
    with ThreadingHTTPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"serving {WEB_ROOT} on 127.0.0.1:{PORT} "
              f"(queue -> {QUEUE_FILE}, max {MAX_QUEUE})")
        httpd.serve_forever()
