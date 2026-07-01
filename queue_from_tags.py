#!/usr/bin/env python3
"""Drive the kiosk queue from the NFC tags physically on the readers.

Reads each present tag's content (a video or a playlist tile — see
tag_payload.py) and assembles a queue in the order the readers are listed in
NFC_READERS (see readers.py). A reader with no tag, no payload, or no response
within the poll timeout is simply omitted. Whenever the assembled queue changes,
play-video.sh is called once (with a JSON array) to replace the kiosk queue.
Removing every tag empties the queue (via serve.py's /replace), which stops
playback and blacks out the screen — play-video.sh can't express an empty queue,
so we hit the server's API directly.

Rotate-to-skip:
    A playlist tile is a square with an NFC tag on each edge, all four holding
    the same playlist id but sequence 1-4. Rotating the tile a quarter-turn
    swaps which tag the reader sees. A sequence transition of +1 (mod 4) means
    skip forward, -1 means skip back; a 180° flip (+2) is ambiguous and ignored.
    A skip is forwarded to the player (serve.py /skip) ONLY for the reader that
    currently owns the queue head, so rotating a queued-but-not-playing tile
    never hijacks whatever is playing. The queue item itself carries only
    type+id (not the sequence), so rotation never churns the outer queue.

Multiplexed polling:
    Each reader is polled by its OWN thread, so the reads happen in parallel
    rather than one after another. An all-empty round therefore costs
    ~POLL_TIMEOUT (~0.5s) of wall-clock, not N times that, and the main loop never
    blocks on a serial read at all — it just snapshots each thread's latest
    debounced result every EVAL_PAUSE and acts on changes. (PN532 construction
    touches RPi.GPIO, which isn't thread-safe, so it's serialised with a lock;
    the reads themselves are independent serial I/O and run concurrently.)

Flicker handling:
    A poorly-aligned tag can be read on one poll and missed on the next, then
    read again. If we treated every miss as a removal the queue would flap and
    restart playback. So presence is debounced ASYMMETRICALLY: a successful read
    is trusted immediately, but a tag is only treated as removed after
    MISS_THRESHOLD consecutive misses. A read is positive evidence (you can't
    decode a payload off an empty reader); a miss is weak evidence.

Note: this owns the serial devices while running — don't run nfc_listener.py at
the same time (they can't both open the same UART).
"""

import argparse
import datetime
import json
import os
import signal
import subprocess
import threading
import urllib.error
import urllib.request

import RPi.GPIO as GPIO

from pn532.uart import PN532_UART
from readers import get_readers
from tag_payload import decode_content, read_payload

# play-video.sh sits alongside this script in the repo checkout.
PLAY_VIDEO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play-video.sh")
# serve.py listens here (play-video.sh hardcodes the same port). /replace with an
# empty body atomically empties the queue; /skip records a rotate-to-skip command.
SERVER = "http://127.0.0.1:8000"
REPLACE_URL = SERVER + "/replace"
SKIP_URL = SERVER + "/skip"

POLL_TIMEOUT = 0.5      # seconds a single read_passive_target waits for a tag
POLL_PAUSE = 0.05       # breather per reader poll (caps re-read rate when a tag is present)
MISS_THRESHOLD = 3      # consecutive misses before a tag counts as removed
EVAL_PAUSE = 0.2        # how often the main loop snapshots state and acts
RECONNECT_PAUSE = 2.0   # wait before reopening a reader after a device error


def log(message):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def queue_item(content):
    """The outer-queue identity of a tile: type + id, WITHOUT the sequence, so
    rotating a playlist tile (seq changes) never churns the queue."""
    return {"type": content["type"], "id": content["id"]}


class ReaderThread(threading.Thread):
    """Polls one PN532 reader continuously and publishes its debounced tile.

    Owns its own connection, debounce counters, and rotation state. Shared state
    written under `lock`: the published tile in `results[name]`, and any detected
    skip events appended to `skip_events`.
    """

    def __init__(self, name, device, reset_pin, results, skip_events,
                 lock, gpio_lock, stop_event):
        super().__init__(name=name, daemon=True)
        self.device = device
        self.reset_pin = reset_pin
        self.results = results
        self.skip_events = skip_events
        self.lock = lock
        self.gpio_lock = gpio_lock
        self.stop_event = stop_event
        self.pn532 = None
        self.confirmed = None             # debounced, currently-accepted tile dict
        self.miss_count = MISS_THRESHOLD  # start in the "absent" state
        self.last_error = None            # dedupe repeated identical error logs
        # Rotation tracking for the tile currently on this reader.
        self.rot_playlist_id = None       # playlist id we're tracking rotation for
        self.rot_seq = None               # last sequence value seen for it

    def _read_once(self):
        """One poll. Returns a content dict, or None if no tag/payload is present.
        Raises on device/communication errors."""
        if self.pn532 is None:
            # GPIO setup/reset during construction isn't thread-safe; serialise it.
            with self.gpio_lock:
                self.pn532 = PN532_UART(dev=self.device, reset=self.reset_pin, debug=False)
            self.pn532.SAM_configuration()
        uid = self.pn532.read_passive_target(timeout=POLL_TIMEOUT)
        if uid is None:
            return None
        return decode_content(read_payload(self.pn532, bytes(uid)))

    def _detect_rotation(self, content):
        """Update rotation state from a freshly read tile and, on a quarter-turn
        of the same playlist, append a skip event for the main loop to forward."""
        if content["type"] != "playlist":
            self.rot_playlist_id = None
            self.rot_seq = None
            return
        seq = content.get("seq")
        if self.rot_playlist_id == content["id"] and self.rot_seq is not None \
                and seq is not None:
            delta = (seq - self.rot_seq) % 4
            action = "next" if delta == 1 else "prev" if delta == 3 else None
            if action:
                with self.lock:
                    self.skip_events.append((self.name, action, content["id"]))
        self.rot_playlist_id = content["id"]
        self.rot_seq = seq

    def _reset_rotation(self):
        self.rot_playlist_id = None
        self.rot_seq = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                content = self._read_once()
                self.last_error = None
            except Exception as exc:
                if str(exc) != self.last_error:
                    log(f"reader={self.name} read_error={exc}")
                    self.last_error = str(exc)
                self.pn532 = None  # force a reopen next time
                self.stop_event.wait(RECONNECT_PAUSE)
                content = None

            if content is not None:
                self._detect_rotation(content)  # before overwriting confirmed
                self.confirmed = queue_item(content)
                self.miss_count = 0
            else:
                self.miss_count += 1
                if self.miss_count >= MISS_THRESHOLD:
                    self.confirmed = None
                    self._reset_rotation()  # a real removal — next placement rebaselines

            with self.lock:
                self.results[self.name] = self.confirmed
            self.stop_event.wait(POLL_PAUSE)


def call_play_video(queue):
    """Replace the kiosk queue with a list of tile dicts, bootstrapping the
    server + browser via play-video.sh. The queue is passed as a JSON array,
    which serve.py's /replace accepts. Returns True on success."""
    payload = json.dumps(queue)
    try:
        result = subprocess.run([PLAY_VIDEO, payload],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"play-video.sh failed to run: {exc}")
        return False
    if result.stdout.strip():
        log(f"play-video.sh: {result.stdout.strip()}")
    if result.returncode != 0:
        log(f"play-video.sh exit={result.returncode} stderr={result.stderr.strip()}")
        return False
    return True


def post_skip(action, playlist_id):
    """Record a rotate-to-skip command for the player. Best-effort."""
    body = json.dumps({"action": action, "playlist_id": playlist_id}).encode()
    req = urllib.request.Request(SKIP_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.URLError as exc:
        log(f"skip {action}: server not reachable ({exc.reason})")
        return False


def clear_queue():
    """Empty the kiosk queue, which stops playback and blacks out the screen.
    Returns True on success."""
    req = urllib.request.Request(REPLACE_URL, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.URLError as exc:
        # No server running means nothing is playing, so the empty state is
        # already effectively true; don't keep retrying.
        log(f"clear queue: server not reachable ({exc.reason}); nothing to clear")
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="log the assembled queue string but never call play-video.sh")
    args = parser.parse_args()

    # Readers (and their concatenation order) come from NFC_READERS; see readers.py.
    readers = get_readers()
    if not readers:
        parser.error("no readers configured — set NFC_READERS (see readers.py)")
    reader_order = list(readers)

    stop_event = threading.Event()
    lock = threading.Lock()
    gpio_lock = threading.Lock()
    results = {name: None for name in reader_order}
    skip_events = []  # (reader_name, action, playlist_id) appended by threads

    # Stop cleanly (run GPIO.cleanup) on Ctrl-C and SIGTERM so service managers /
    # `timeout` can shut it down. A wedged serial read can still block a reader
    # thread, but the threads are daemons so the process still exits.
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    threads = [ReaderThread(name, *readers[name], results, skip_events,
                            lock, gpio_lock, stop_event)
               for name in reader_order]
    for t in threads:
        t.start()

    last_sent = None  # sentinel: distinct from [] so the first cycle always syncs
    prev = dict(results)
    log(f"Driving queue from readers (order: {', '.join(reader_order)})"
        + (" [dry-run]" if args.dry_run else ""))
    try:
        while not stop_event.is_set():
            with lock:
                snapshot = dict(results)
                pending_skips = skip_events[:]
                skip_events.clear()

            # Log per-reader presence transitions — handy for watching the
            # debounce cope with a fidgety tag.
            for name in reader_order:
                if snapshot[name] != prev[name]:
                    log(f"reader={name} tile: {prev[name]!r} -> {snapshot[name]!r}")
            prev = snapshot

            # The queue is the present tiles in reader order. The head is the
            # first present reader — the tile that's actually playing.
            queue = [snapshot[n] for n in reader_order if snapshot[n]]
            head_reader = next((n for n in reader_order if snapshot[n]), None)

            # Forward rotate-to-skip only for the reader that owns the head, and
            # only if that head is the same playlist the rotation happened on.
            for name, action, playlist_id in pending_skips:
                head = snapshot.get(head_reader) if head_reader else None
                if name == head_reader and head and head.get("id") == playlist_id:
                    if args.dry_run:
                        log(f"skip {action} on {name} (playlist {playlist_id}) (dry-run)")
                    else:
                        log(f"skip {action} on head reader {name} (playlist {playlist_id})")
                        post_skip(action, playlist_id)
                else:
                    log(f"ignoring skip {action} on non-head reader {name}")

            if queue != last_sent:
                if args.dry_run:
                    log(f"queue -> {queue!r} (dry-run, no action)")
                    last_sent = queue
                elif queue:
                    log(f"queue -> {queue!r}; calling play-video.sh")
                    if call_play_video(queue):
                        last_sent = queue
                    # On failure, leave last_sent so the next cycle retries.
                else:
                    log("all tags removed; clearing queue and stopping playback")
                    if clear_queue():
                        last_sent = queue

            stop_event.wait(EVAL_PAUSE)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=POLL_TIMEOUT + 1)
        log("Stopping...")
        GPIO.cleanup()


if __name__ == "__main__":
    main()
