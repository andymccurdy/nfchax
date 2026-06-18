#!/usr/bin/env python3
"""Write a YouTube video_id to a single NFC tag using one PN532 reader.

Hold one tag on the reader and run:

    ./venv/bin/python write_tag.py dQw4w9WgXcQ

By default the writer uses the first reader in NFC_READERS (the Waveshare HAT
on /dev/ttyAMA0 in the original setup, or whatever you configure — see
readers.py). Pick a specific reader with --device/--reset, e.g. on a USB-only
setup with no HAT:

    ./venv/bin/python write_tag.py --device /dev/ttyUSB0 --reset none dQw4w9WgXcQ

The id is stored as NUL-padded ASCII. Tag type is auto-detected from the UID
length: 4-byte UIDs are treated as MIFARE Classic (authenticated with the
factory default key), 7-byte UIDs as NTAG2xx. After writing, the payload is
read back and compared so a silent half-write is reported as a failure.
"""

import argparse
import sys
import time

import RPi.GPIO as GPIO

from pn532.uart import PN532_UART
from readers import get_readers
from tag_payload import PAYLOAD_LEN, decode_payload, encode_payload, write_payload

POLL_TIMEOUT = 0.5  # seconds per read_passive_target call


def reset_arg(value):
    """Parse a --reset value: an int GPIO pin, or 'none'/'' for no reset line."""
    if value.strip().lower() in ("", "none"):
        return None
    return int(value)


def wait_for_tag(pn532, timeout=30.0):
    """Poll until a tag is present; return its UID bytes."""
    print("Hold a tag on the reader...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        uid = pn532.read_passive_target(timeout=POLL_TIMEOUT)
        if uid is not None:
            return bytes(uid)
    raise TimeoutError(f"no tag detected within {timeout:.0f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # Default to the first reader in NFC_READERS (or the default Pi setup).
    _, (default_device, default_reset) = next(iter(get_readers().items()))
    parser.add_argument("video_id", help="YouTube video id to store (<=16 ASCII chars)")
    parser.add_argument("--device", default=default_device,
                        help=f"serial device of the reader to write on (default: {default_device})")
    parser.add_argument("--reset", default=default_reset, type=reset_arg,
                        help="BCM GPIO reset pin, or 'none' for USB readers "
                             f"(default: {default_reset})")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for a tag (default: 30)")
    args = parser.parse_args()

    try:
        payload = encode_payload(args.video_id)
    except (ValueError, UnicodeEncodeError) as exc:
        parser.error(str(exc))

    print(f"Using reader device={args.device} reset={args.reset}", flush=True)
    pn532 = PN532_UART(dev=args.device, reset=args.reset, debug=False)
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
