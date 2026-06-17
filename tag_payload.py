"""Shared on-tag payload format for the NFC kiosk.

A short ASCII string (a YouTube video id) is stored NUL-padded in a fixed
location on the tag. The tag family is auto-detected from the UID length:
4-byte UIDs are MIFARE Classic, 7-byte UIDs are NTAG2xx.

Both write_tag.py (the writer) and nfc_listener.py (the reader) import these
helpers so the two always agree on the layout. Don't change the constants
without rewriting every tag already in the field.
"""

from pn532 import pn532 as pn532_consts

# Where the payload lives on each tag family.
# MIFARE Classic: block 4 is the first data block of sector 1 (sector 0 holds
# the manufacturer block, so we avoid it). One 16-byte block.
# NTAG2xx: user memory starts at page 4; 4 pages give 16 bytes. We never touch
# the lock/config pages, so tags can't be bricked or locked read-only.
CLASSIC_BLOCK = 4
NTAG_START_PAGE = 4
NTAG_PAGES = 4
PAYLOAD_LEN = 16  # both families expose 16 bytes here

# Factory default MIFARE Classic key A.
DEFAULT_KEY = b"\xFF\xFF\xFF\xFF\xFF\xFF"


def encode_payload(text: str) -> bytes:
    """ASCII string -> the exact 16 bytes to store on the tag."""
    raw = text.encode("ascii")  # raises UnicodeEncodeError on non-ASCII
    if len(raw) > PAYLOAD_LEN:
        raise ValueError(
            f"value is {len(raw)} bytes; max {PAYLOAD_LEN} bytes fit on the tag"
        )
    return raw.ljust(PAYLOAD_LEN, b"\x00")


def decode_payload(raw: bytes) -> str:
    """Stored bytes -> ASCII string (NUL padding stripped). May be empty."""
    return raw.rstrip(b"\x00").decode("ascii", errors="replace")


def _classic_auth(pn532, uid: bytes):
    if not pn532.mifare_classic_authenticate_block(
        uid, CLASSIC_BLOCK, pn532_consts.MIFARE_CMD_AUTH_A, DEFAULT_KEY
    ):
        raise RuntimeError(f"authentication failed for block {CLASSIC_BLOCK}")


def write_payload(pn532, uid: bytes, payload: bytes) -> bytes:
    """Write the 16-byte payload to the tag; return the bytes read back."""
    if len(uid) == 4:
        _classic_auth(pn532, uid)
        pn532.mifare_classic_write_block(CLASSIC_BLOCK, payload)
        return bytes(pn532.mifare_classic_read_block(CLASSIC_BLOCK))[:PAYLOAD_LEN]
    if len(uid) == 7:
        for i in range(NTAG_PAGES):
            pn532.ntag2xx_write_block(NTAG_START_PAGE + i, payload[i * 4:(i + 1) * 4])
        return _read_ntag(pn532)
    raise RuntimeError(
        f"unsupported UID length {len(uid)}; expected 4 (MIFARE Classic) or 7 (NTAG2xx)"
    )


def read_payload(pn532, uid: bytes) -> bytes:
    """Read the raw 16-byte payload area from the tag."""
    if len(uid) == 4:
        _classic_auth(pn532, uid)
        return bytes(pn532.mifare_classic_read_block(CLASSIC_BLOCK))[:PAYLOAD_LEN]
    if len(uid) == 7:
        return _read_ntag(pn532)
    raise RuntimeError(
        f"unsupported UID length {len(uid)}; expected 4 (MIFARE Classic) or 7 (NTAG2xx)"
    )


def _read_ntag(pn532) -> bytes:
    readback = bytearray()
    for i in range(NTAG_PAGES):
        readback += pn532.ntag2xx_read_block(NTAG_START_PAGE + i)
    return bytes(readback[:PAYLOAD_LEN])
