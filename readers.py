"""Shared NFC reader configuration for the listener and writer.

Readers are defined in the ``NFC_READERS`` environment variable as a
comma-separated list of ``NAME=DEVICE[:RESET_PIN]`` entries:

  - NAME      a label for log output (any string, e.g. ttyUSB0)
  - DEVICE    the serial device path (e.g. /dev/ttyUSB0)
  - RESET_PIN optional BCM GPIO pin wired to the PN532 RSTPDN line. Only the
              Waveshare HAT on the original Pi has one (GPIO 20). Omit it for
              plain USB PN532 modules, which have no reset line.

The default is the original Pi 5 setup (HAT + two USB modules). A developer
with only the two USB readers (no HAT) would instead set, e.g.:

    export NFC_READERS=ttyUSB0=/dev/ttyUSB0,ttyUSB1=/dev/ttyUSB1

Both nfc_listener.py and write_tag.py read this, so one variable configures
the whole project for the connected hardware.
"""

import os

# Original Pi 5 setup: Waveshare HAT on ttyAMA0 (reset wired to GPIO 20) plus
# two plain PN532 modules on USB-serial adapters (no reset line).
DEFAULT_READERS = "ttyAMA0=/dev/ttyAMA0:20,ttyUSB0=/dev/ttyUSB0,ttyUSB1=/dev/ttyUSB1"


def parse_reader(entry):
    """Parse one 'NAME=DEVICE[:RESET]' entry into (name, device, reset_pin)."""
    name, sep, rest = entry.strip().partition("=")
    if not sep or not rest:
        raise ValueError(f"bad reader spec {entry!r}; expected NAME=DEVICE[:RESET_PIN]")
    device, _, pin = rest.partition(":")
    return name, device, (int(pin) if pin else None)


def parse_readers(spec):
    """Parse a 'NAME=DEVICE[:RESET],...' spec into {name: (device, reset_pin)}."""
    readers = {}
    for entry in spec.split(","):
        if not entry.strip():
            continue
        name, device, pin = parse_reader(entry)
        readers[name] = (device, pin)
    return readers


def get_readers():
    """Return the configured readers from NFC_READERS, or the default Pi setup."""
    return parse_readers(os.environ.get("NFC_READERS") or DEFAULT_READERS)
