#!/usr/bin/env python3
"""Write a YouTube video_id to a single NFC tag using the PN532 HAT on /dev/ttyAMA0.

Hold one tag on the HAT reader and run:

    ./venv/bin/python write_tag.py dQw4w9WgXcQ

The id is stored as NUL-padded ASCII. Tag type is auto-detected from the UID
length: 4-byte UIDs are treated as MIFARE Classic (authenticated with the
factory default key), 7-byte UIDs as NTAG2xx. After writing, the payload is
read back and compared so a silent half-write is reported as a failure.

Only ttyAMA0 (the Waveshare HAT, reset wired to GPIO 20) is used for writing.
"""

import argparse
import sys
import time

import RPi.GPIO as GPIO

from pn532.uart import PN532_UART
from tag_payload import PAYLOAD_LEN, decode_payload, encode_payload, write_payload

DEVICE = "/dev/ttyAMA0"
RESET_PIN = 20

POLL_TIMEOUT = 0.5  # seconds per read_passive_target call


def wait_for_tag(pn532, timeout=30.0):
    """Poll until a tag is present; return its UID bytes."""
    print("Hold a tag on the HAT reader...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        uid = pn532.read_passive_target(timeout=POLL_TIMEOUT)
        if uid is not None:
            return bytes(uid)
    raise TimeoutError(f"no tag detected within {timeout:.0f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video_id", help="YouTube video id to store (<=16 ASCII chars)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for a tag (default: 30)")
    args = parser.parse_args()

    try:
        payload = encode_payload(args.video_id)
    except (ValueError, UnicodeEncodeError) as exc:
        parser.error(str(exc))

    pn532 = PN532_UART(dev=DEVICE, reset=RESET_PIN, debug=False)
    try:
        pn532.SAM_configuration()
        uid = wait_for_tag(pn532, timeout=args.timeout)
        print(f"Tag detected: uid={uid.hex()} ({len(uid)} bytes)", flush=True)

        readback = write_payload(pn532, uid, payload)

        if readback[:PAYLOAD_LEN] != payload:
            raise RuntimeError(
                f"verification failed: wrote {payload!r} but read back {readback!r}"
            )
        print(f"OK: wrote video_id={decode_payload(readback)!r} and verified.", flush=True)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        GPIO.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
