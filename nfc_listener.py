#!/usr/bin/env python3
"""Listen on multiple PN532 UART readers and print scanned video_ids to stdout.

Each tag stores a YouTube video_id as a NUL-padded ASCII payload (written by
write_tag.py); on a scan we read that payload back and print the video_id. The
on-tag layout lives in tag_payload.py so the reader and writer stay in sync.

Uses the Waveshare/Elechouse pn532 driver (proven working on this hardware)
rather than nfcpy, which fails to complete the PN532 handshake reliably here.
"""

import argparse
import datetime
import sys
import threading
import time

from pn532.uart import PN532_UART
from tag_payload import decode_payload, read_payload

# reader name -> (device path, reset GPIO pin or None)
# Only the ttyAMA0 reader is the Waveshare HAT with a wired reset pin (GPIO20).
# The ttyUSB0/ttyUSB1 readers are plain PN532 modules with no reset line.
READERS = {
    "ttyAMA0": ("/dev/ttyAMA0", 20),
    "ttyUSB0": ("/dev/ttyUSB0", None),
    "ttyUSB1": ("/dev/ttyUSB1", None),
}

POLL_TIMEOUT = 0.5  # seconds per read_passive_target call
DEBOUNCE_SECONDS = 1.0

print_lock = threading.Lock()
gpio_lock = threading.Lock()  # RPi.GPIO setup/output calls are not thread-safe
last_seen = {}  # reader_name -> (uid_hex, last_seen_monotonic_time)


def is_new_sighting(reader_name, uid_hex):
    """Debounce on UID. Returns True the first time a tag is seen (and again
    once it has been gone for DEBOUNCE_SECONDS), so we only read the payload
    once per placement rather than on every poll."""
    now = time.monotonic()
    with print_lock:
        prev_uid, prev_time = last_seen.get(reader_name, (None, 0.0))
        last_seen[reader_name] = (uid_hex, now)
        return not (uid_hex == prev_uid and (now - prev_time) < DEBOUNCE_SECONDS)


def emit(reader_name, message):
    with print_lock:
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        print(f"[{timestamp}] reader={reader_name} {message}", flush=True)


def listen(reader_name, device_path, reset_pin, stop_event):
    while not stop_event.is_set():
        try:
            with gpio_lock:
                pn532 = PN532_UART(dev=device_path, debug=False, reset=reset_pin)
            pn532.SAM_configuration()
            while not stop_event.is_set():
                uid = pn532.read_passive_target(timeout=POLL_TIMEOUT)
                if uid is None:
                    continue
                uid = bytes(uid)
                if not is_new_sighting(reader_name, uid.hex()):
                    continue
                try:
                    video_id = decode_payload(read_payload(pn532, uid))
                except Exception as exc:
                    emit(reader_name, f"uid={uid.hex()} payload_error={exc}")
                    continue
                if video_id:
                    emit(reader_name, f"video_id={video_id}")
                else:
                    emit(reader_name, f"uid={uid.hex()} (no video_id on tag)")
        except Exception as exc:
            with print_lock:
                print(f"reader={reader_name} error={exc}", file=sys.stderr, flush=True)
            stop_event.wait(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reader",
        action="append",
        dest="readers",
        metavar="NAME=DEVICE[:RESET_PIN]",
        help="Override a reader, e.g. ttyUSB0=/dev/ttyUSB0 or ttyAMA0=/dev/ttyAMA0:20. Repeatable.",
    )
    args = parser.parse_args()

    readers = dict(READERS)
    for override in args.readers or []:
        name, _, rest = override.partition("=")
        device, _, pin = rest.partition(":")
        readers[name] = (device, int(pin) if pin else None)

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=listen, args=(name, device, pin, stop_event), daemon=True)
        for name, (device, pin) in readers.items()
    ]

    print(f"Listening on readers: {', '.join(readers)}", flush=True)
    for t in threads:
        t.start()

    try:
        while True:
            stop_event.wait(1)
    except KeyboardInterrupt:
        stop_event.set()
        print("Stopping...", flush=True)


if __name__ == "__main__":
    main()
