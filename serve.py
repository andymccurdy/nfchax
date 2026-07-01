#!/usr/bin/env python3
"""Static server + queue API for the kiosk player.

Serves player assets from WEB_ROOT (only that directory is web-exposed) and owns
a small FIFO queue of things to play. The queue is the single source of truth for
what plays; it is mutated ONLY here (one process, lock-guarded) so the browser
(which advances on video end) and the tag reader (which enqueues) can never race.
The queue is persisted to STATE_DIR/queue.json so it survives a browser reload.

Each queue item is a tile object:
  {"type": "video",    "id": "<video_id>"}
  {"type": "playlist", "id": "<playlist_id>"}
Bare-string ids are accepted and normalised to video items.

Playlists are NOT played via YouTube's IFrame playlist embed — that embed fails
("An error occurred / Playback ID") on this box while single-video embeds work.
Instead the server expands a playlist id into its video ids (scraped from the
public playlist page, no API key) and tracks a current index. The player only
ever plays SINGLE videos: GET /queue resolves each playlist tile to its current
video. Advancing/skipping just moves the server-side index, so the player reloads
the next single video.

HTTP API (all JSON, no-store). Queue items in RESPONSES are resolved:
  video    -> {"type":"video","id":id}
  playlist -> {"type":"playlist","id":plid,"video":cur,"index":i,"count":n}

  GET  /queue     -> {"queue": [resolved, ...]}
  POST /enqueue   body = id, JSON item, or {"video_id": id}; append one tile
                  (rejected with 409 if the queue already holds MAX_QUEUE items)
  POST /replace   body = JSON array (items or ids) OR comma-separated ids;
                  atomically replace the whole queue (capped) -> {"queue":[...]}
  POST /advance   the head just finished. If the head is a playlist with more
                  videos, step to its next video; otherwise drop the tile.
  POST /skip      body = {"action":"next"|"prev","playlist_id":id}; rotate-to-skip
                  within the head playlist (wraps around the ends).

Static:
  GET  /<path>    served from WEB_ROOT

Config via env: KIOSK_PORT, KIOSK_BIND, KIOSK_WEB_ROOT, KIOSK_STATE_DIR
"""
import json
import os
import re
import threading
import time
import urllib.request
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
# Bind address. Defaults to loopback (not LAN-exposed). Set KIOSK_BIND=0.0.0.0 to
# reach it from another host.
BIND = os.environ.get("KIOSK_BIND", "127.0.0.1")
QUEUE_FILE = os.path.join(STATE_DIR, "queue.json")
MAX_QUEUE = 5
MAX_PLAYLIST_VIDEOS = 200   # cap on how many playlist videos we track
PLAYLIST_TTL = 6 * 3600     # re-scrape a playlist after this many seconds

_lock = threading.Lock()  # serialises all queue/playlist reads/writes
# Expanded playlists: playlist_id -> {"videos": [...], "index": int, "ts": float}.
# Kept only for playlists currently in the queue (pruned on replace/advance), so
# a playlist restarts from its first video when re-placed. Not persisted.
_playlists = {}


# --- playlist expansion ------------------------------------------------------
def _scrape_playlist(plid):
    """Fetch the public playlist page and pull out its video ids, in order.
    Returns a list (empty on error). No API key required. Retries because a cold
    request can get YouTube's cookie-consent interstitial (no videoIds); the
    CONSENT/SOCS cookies suppress it on the retry."""
    url = "https://www.youtube.com/playlist?list=" + plid
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+; SOCS=CAI",
    }
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=15) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        seen, out = set(), []
        for vid in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
            if vid not in seen:
                seen.add(vid)
                out.append(vid)
                if len(out) >= MAX_PLAYLIST_VIDEOS:
                    break
        if out:
            return out
    return []


def _ensure_playlist(plid):
    """Return the cached expansion for a playlist, scraping it if needed. The
    network fetch happens OUTSIDE the lock so polls for other items don't block."""
    with _lock:
        e = _playlists.get(plid)
        if e and e["videos"] and (time.time() - e["ts"]) < PLAYLIST_TTL:
            return e
    videos = _scrape_playlist(plid)
    with _lock:
        e = _playlists.get(plid)
        if e and e["videos"] and not videos:
            return e  # keep a good cache if a re-scrape failed
        # Preserve the index if we're just refreshing an existing playlist.
        idx = e["index"] if (e and videos) else 0
        _playlists[plid] = {"videos": videos, "index": min(idx, max(0, len(videos) - 1)), "ts": time.time()}
        return _playlists[plid]


def _prune_playlists(queue):
    """Drop cached playlists no longer present in the queue (so a removed-then-
    replaced tile restarts from the top)."""
    live = {it["id"] for it in queue if it.get("type") == "playlist"}
    for plid in list(_playlists):
        if plid not in live:
            del _playlists[plid]


# --- queue persistence + resolution ------------------------------------------
def normalise_item(item):
    """Coerce a queue entry into a {"type","id"} tile, or None if unusable."""
    if isinstance(item, dict):
        if item.get("type") in ("video", "playlist") and str(item.get("id", "")).strip():
            return {"type": item["type"], "id": str(item["id"]).strip()}
        vid = str(item.get("video_id", "")).strip()
        return {"type": "video", "id": vid} if vid else None
    text = str(item).strip()
    return {"type": "video", "id": text} if text else None


def _load_queue():
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            items = [normalise_item(x) for x in data]
            return [x for x in items if x][:MAX_QUEUE]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return []


def _save_queue(q):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f)
    os.replace(tmp, QUEUE_FILE)  # atomic


def _resolve(item):
    """Turn a stored tile into what the player should see. A playlist resolves to
    its current video plus position; a video passes through."""
    if item["type"] != "playlist":
        return {"type": "video", "id": item["id"]}
    e = _ensure_playlist(item["id"])
    vids = e["videos"]
    idx = e["index"] if vids else 0
    return {
        "type": "playlist",
        "id": item["id"],
        "video": vids[idx] if vids else "",
        "index": idx,
        "count": len(vids),
    }


def _resolved_queue():
    return [_resolve(it) for it in _load_queue()]


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Kiosk assets (and the JSON API) must never be cached, or a reload
        # after editing player/youtube-fullscreen.html serves the stale page.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n).decode().strip() if n else ""

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/queue":
            return self._send_json({"queue": _resolved_queue()})
        return super().do_GET()  # static file from WEB_ROOT

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/enqueue":
            body = self._read_body()
            item = None
            if body.startswith("{") or body.startswith("["):
                try:
                    item = normalise_item(json.loads(body))
                except ValueError:
                    item = None
            else:
                item = normalise_item(body)
            if not item:
                return self._send_json({"error": "missing/invalid item"}, 400)
            with _lock:
                q = _load_queue()
                added = len(q) < MAX_QUEUE
                if added:
                    q.append(item)
                    _save_queue(q)
            return self._send_json({"queue": _resolved_queue()},
                                   200 if added else 409)

        if path == "/replace":
            body = self._read_body()
            if body.startswith("["):
                try:
                    raw = json.loads(body)
                except ValueError:
                    return self._send_json({"error": "invalid JSON array"}, 400)
            else:
                raw = body.split(",")
            items = [normalise_item(x) for x in raw]
            items = [x for x in items if x][:MAX_QUEUE]  # drop blanks, cap length
            with _lock:
                _save_queue(items)
                _prune_playlists(items)
            return self._send_json({"queue": _resolved_queue()})

        if path == "/advance":
            # The head just finished (video ended, or a bad item errored). If the
            # head is a playlist with more videos, step to the next one; otherwise
            # drop the tile.
            head = (_load_queue() or [None])[0]
            if head and head["type"] == "playlist":
                _ensure_playlist(head["id"])  # expand OUTSIDE the lock (may fetch)
            with _lock:
                q = _load_queue()
                if q:
                    head = q[0]
                    stepped = False
                    if head["type"] == "playlist":
                        e = _playlists.get(head["id"])
                        if e and e["videos"] and e["index"] < len(e["videos"]) - 1:
                            e["index"] += 1
                            stepped = True
                    if not stepped:
                        q.pop(0)
                        _save_queue(q)
                        _prune_playlists(q)
            return self._send_json({"queue": _resolved_queue()})

        if path == "/skip":
            # Rotate-to-skip within the HEAD playlist. Wraps around the ends.
            body = self._read_body()
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                data = {}
            action = data.get("action")
            plid = str(data.get("playlist_id", ""))
            if action not in ("next", "prev"):
                return self._send_json({"error": "action must be next|prev"}, 400)
            _ensure_playlist(plid)  # expand OUTSIDE the lock (may fetch)
            with _lock:
                q = _load_queue()
                head = q[0] if q else None
                if head and head["type"] == "playlist" and head["id"] == plid:
                    e = _playlists.get(plid)
                    n = len(e["videos"]) if e else 0
                    if n:
                        step = 1 if action == "next" else -1
                        e["index"] = (e["index"] + step) % n
            return self._send_json({"queue": _resolved_queue()})

        return self._send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    handler = partial(Handler, directory=WEB_ROOT)
    with ThreadingHTTPServer((BIND, PORT), handler) as httpd:
        print(f"serving {WEB_ROOT} on {BIND}:{PORT} "
              f"(queue -> {QUEUE_FILE}, max {MAX_QUEUE})")
        httpd.serve_forever()
