#!/usr/bin/env python3
"""Write content to an NFC tag using one PN532 reader.

Two kinds of content (see tag_payload.py):

  video     — a single YouTube video id:
      ./venv/bin/python write_tag.py dQw4w9WgXcQ

  playlist  — a YouTube playlist id plus a sequence (1-4), one per edge of a
      square tile. Rotating the tile past each edge skips within the playlist:
      ./venv/bin/python write_tag.py --type playlist PLxxxx --sequence 1
      ./venv/bin/python write_tag.py --type playlist PLxxxx --edges   # writes all 4

By default the writer uses the first reader in NFC_READERS (the Waveshare HAT
on /dev/ttyAMA0 in the original setup, or whatever you configure — see
readers.py). Pick a specific reader with --device/--reset, e.g. on a USB-only
setup with no HAT:

    ./venv/bin/python write_tag.py --device /dev/ttyUSB0 --reset none dQw4w9WgXcQ

Content is stored NUL-padded in a 48-byte area. Tag type is auto-detected from
the UID length: 4-byte UIDs are MIFARE Classic (authenticated with the factory
default key), 7-byte UIDs are NTAG2xx. After writing, the payload is read back
and compared so a silent half-write is reported as a failure.
"""

import argparse
import sys
import time

import RPi.GPIO as GPIO

from pn532.uart import PN532_UART
from readers import get_readers
from tag_payload import decode_content, encode_content, read_payload, write_payload

POLL_TIMEOUT = 0.5  # seconds per read_passive_target call


def reset_arg(value):
    """Parse a --reset value: an int GPIO pin, or 'none'/'' for no reset line."""
    if isinstance(value, int) or value is None:
        return value
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


def write_one(pn532, content, timeout):
    """Wait for a tag, write `content`, read back and verify. Raises on failure."""
    payload = encode_content(content)
    uid = wait_for_tag(pn532, timeout=timeout)
    print(f"Tag detected: uid={uid.hex()} ({len(uid)} bytes)", flush=True)
    readback = write_payload(pn532, uid, payload)
    got = decode_content(readback)
    if got != content:
        raise RuntimeError(f"verification failed: wrote {content!r} but read back {got!r}")
    print(f"OK: wrote {got!r} and verified.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # Default to the first reader in NFC_READERS (or the default Pi setup).
    _, (default_device, default_reset) = next(iter(get_readers().items()))
    parser.add_argument("id", help="video id, or playlist id with --type playlist")
    parser.add_argument("--type", choices=("video", "playlist"), default="video",
                        help="content type to write (default: video)")
    parser.add_argument("--sequence", type=int, choices=(1, 2, 3, 4),
                        help="playlist edge sequence 1-4 (playlist only)")
    parser.add_argument("--edges", action="store_true",
                        help="playlist only: write all four edges 1-4 in turn, "
                             "prompting you to rotate the tile between each")
    parser.add_argument("--device", default=default_device,
                        help=f"serial device of the reader to write on (default: {default_device})")
    parser.add_argument("--reset", default=default_reset, type=reset_arg,
                        help="BCM GPIO reset pin, or 'none' for USB readers "
                             f"(default: {default_reset})")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for a tag (default: 30)")
    args = parser.parse_args()

    # Figure out which sequence(s) to write.
    if args.type == "playlist":
        if args.edges:
            sequences = [1, 2, 3, 4]
        elif args.sequence:
            sequences = [args.sequence]
        else:
            parser.error("playlist tags need --sequence N (1-4) or --edges")
    else:
        if args.sequence or args.edges:
            parser.error("--sequence/--edges only apply to --type playlist")
        sequences = [None]

    # Validate the payloads up front (size, charset) before touching hardware.
    contents = []
    for seq in sequences:
        content = {"type": args.type, "id": args.id}
        if seq is not None:
            content["seq"] = seq
        try:
            encode_content(content)
        except (ValueError, UnicodeEncodeError) as exc:
            parser.error(str(exc))
        contents.append(content)

    print(f"Using reader device={args.device} reset={args.reset}", flush=True)
    pn532 = PN532_UART(dev=args.device, reset=args.reset, debug=False)
    try:
        pn532.SAM_configuration()
        for i, content in enumerate(contents):
            if len(contents) > 1:
                print(f"\n--- Edge {content['seq']} of 4 ---", flush=True)
                if i > 0:
                    input("Rotate the tile a quarter-turn, then press Enter...")
            write_one(pn532, content, timeout=args.timeout)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        GPIO.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
