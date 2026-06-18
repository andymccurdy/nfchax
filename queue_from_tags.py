#!/usr/bin/env python3
"""Drive the kiosk queue from the NFC tags physically on the readers.

Reads each present tag's video_id and assembles a comma-separated queue string
in the order the readers are listed in NFC_READERS (see readers.py), exactly as
play-video.sh expects. A reader with no tag, no payload, or no response within
the poll timeout is simply omitted. Whenever the assembled string changes,
play-video.sh is called once to replace the kiosk queue. Removing every tag
empties the queue (via serve.py's /replace), which stops playback and blacks out
the screen — play-video.sh can't express an empty queue, so we hit the server's
API directly.

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
import os
import signal
import subprocess
import threading
import urllib.error
import urllib.request

import RPi.GPIO as GPIO

from pn532.uart import PN532_UART
from readers import get_readers
from tag_payload import decode_payload, read_payload

# play-video.sh sits alongside this script in the repo checkout.
PLAY_VIDEO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play-video.sh")
# serve.py listens here (play-video.sh hardcodes the same port). /replace with an
# empty body atomically empties the queue.
REPLACE_URL = "http://127.0.0.1:8000/replace"

POLL_TIMEOUT = 0.5      # seconds a single read_passive_target waits for a tag
POLL_PAUSE = 0.05       # breather per reader poll (caps re-read rate when a tag is present)
MISS_THRESHOLD = 3      # consecutive misses before a tag counts as removed
EVAL_PAUSE = 0.2        # how often the main loop snapshots state and acts
RECONNECT_PAUSE = 2.0   # wait before reopening a reader after a device error


def log(message):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


class ReaderThread(threading.Thread):
    """Polls one PN532 reader continuously and publishes its debounced tag id.

    Owns its own connection and debounce counters; the only shared state is the
    published id, written under `lock` into `results[name]`.
    """

    def __init__(self, name, device, reset_pin, results, lock, gpio_lock, stop_event):
        super().__init__(name=name, daemon=True)
        self.device = device
        self.reset_pin = reset_pin
        self.results = results
        self.lock = lock
        self.gpio_lock = gpio_lock
        self.stop_event = stop_event
        self.pn532 = None
        self.confirmed_id = None          # debounced, currently-accepted id
        self.miss_count = MISS_THRESHOLD  # start in the "absent" state
        self.last_error = None            # dedupe repeated identical error logs

    def _read_once(self):
        """One poll. Returns a video_id, or None if no tag/payload is present.
        Raises on device/communication errors."""
        if self.pn532 is None:
            # GPIO setup/reset during construction isn't thread-safe; serialise it.
            with self.gpio_lock:
                self.pn532 = PN532_UART(dev=self.device, reset=self.reset_pin, debug=False)
            self.pn532.SAM_configuration()
        uid = self.pn532.read_passive_target(timeout=POLL_TIMEOUT)
        if uid is None:
            return None
        return decode_payload(read_payload(self.pn532, bytes(uid))) or None

    def run(self):
        while not self.stop_event.is_set():
            try:
                video_id = self._read_once()
                self.last_error = None
            except Exception as exc:
                if str(exc) != self.last_error:
                    log(f"reader={self.name} read_error={exc}")
                    self.last_error = str(exc)
                self.pn532 = None  # force a reopen next time
                self.stop_event.wait(RECONNECT_PAUSE)
                video_id = None

            if video_id is not None:
                self.confirmed_id = video_id
                self.miss_count = 0
            else:
                self.miss_count += 1
                if self.miss_count >= MISS_THRESHOLD:
                    self.confirmed_id = None

            with self.lock:
                self.results[self.name] = self.confirmed_id
            self.stop_event.wait(POLL_PAUSE)


def call_play_video(queue_str):
    """Replace the kiosk queue. Returns True on success."""
    try:
        result = subprocess.run([PLAY_VIDEO, queue_str],
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

    # Stop cleanly (run GPIO.cleanup) on Ctrl-C and SIGTERM so service managers /
    # `timeout` can shut it down. A wedged serial read can still block a reader
    # thread, but the threads are daemons so the process still exits.
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    threads = [ReaderThread(name, *readers[name], results, lock, gpio_lock, stop_event)
               for name in reader_order]
    for t in threads:
        t.start()

    last_sent = None  # sentinel: distinct from "" so the first cycle always syncs
    prev = dict(results)
    log(f"Driving queue from readers (order: {', '.join(reader_order)})"
        + (" [dry-run]" if args.dry_run else ""))
    try:
        while not stop_event.is_set():
            with lock:
                snapshot = dict(results)

            # Log per-reader presence transitions — handy for watching the
            # debounce cope with a fidgety tag.
            for name in reader_order:
                if snapshot[name] != prev[name]:
                    log(f"reader={name} tag: {prev[name]!r} -> {snapshot[name]!r}")
            prev = snapshot

            queue_str = ",".join(snapshot[n] for n in reader_order if snapshot[n])
            if queue_str != last_sent:
                if args.dry_run:
                    log(f"queue -> {queue_str!r} (dry-run, no action)")
                    last_sent = queue_str
                elif queue_str:
                    log(f"queue -> {queue_str!r}; calling play-video.sh")
                    if call_play_video(queue_str):
                        last_sent = queue_str
                    # On failure, leave last_sent so the next cycle retries.
                else:
                    log("all tags removed; clearing queue and stopping playback")
                    if clear_queue():
                        last_sent = queue_str

            stop_event.wait(EVAL_PAUSE)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=POLL_TIMEOUT + 1)
        log("Stopping...")
        GPIO.cleanup()


if __name__ == "__main__":
    main()
