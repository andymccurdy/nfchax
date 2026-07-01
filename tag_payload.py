"""Shared on-tag payload format for the NFC kiosk.

A tag stores a small ASCII record describing what to play, NUL-padded into a
fixed 48-byte area. The record is newline-delimited with a one-character type
tag on the first line:

    video     ->  "v\n<video_id>"
    playlist  ->  "p\n<playlist_id>\n<sequence>"

The sequence (1-4) is the edge of a physical square tile. A playlist tile has
the SAME playlist id on all four edges but a different sequence per edge, so the
reader can tell which way the tile was rotated (see queue_from_tags.py).

Legacy tags written before the type tag existed hold a bare NUL-padded video id
with no "v"/"p" first line; decode_content() treats any unrecognised payload as
a video id, so old tags keep working.

The tag family is auto-detected from the UID length: 4-byte UIDs are MIFARE
Classic, 7-byte UIDs are NTAG2xx.

Both write_tag.py (the writer) and the readers (queue_from_tags.py,
nfc_listener.py) import these helpers so the two always agree on the layout.
Don't change the constants without rewriting every tag already in the field.
"""

from pn532 import pn532 as pn532_consts

# Where the payload lives on each tag family. We use 48 bytes so a full playlist
# id (up to ~41 chars) plus framing fits — the old 16-byte area only held a
# video id.
# MIFARE Classic: blocks 4,5,6 are the three DATA blocks of sector 1 (sector 0
# holds the manufacturer block; block 7 is the sector trailer, which we never
# touch, so a tag can't be bricked or locked). One auth on block 4 covers the
# whole sector.
# NTAG2xx: user memory starts at page 4; pages 4-15 give 48 bytes and stay well
# clear of the lock/config pages.
CLASSIC_BLOCKS = [4, 5, 6]
NTAG_START_PAGE = 4
NTAG_PAGES = 12
PAYLOAD_LEN = 48  # both families expose 48 bytes here

# Factory default MIFARE Classic key A.
DEFAULT_KEY = b"\xFF\xFF\xFF\xFF\xFF\xFF"

# One-character content type tags stored on line 1 of the record.
TYPE_VIDEO = "v"
TYPE_PLAYLIST = "p"


def encode_payload(text: str) -> bytes:
    """ASCII string -> the exact PAYLOAD_LEN bytes to store on the tag."""
    raw = text.encode("ascii")  # raises UnicodeEncodeError on non-ASCII
    if len(raw) > PAYLOAD_LEN:
        raise ValueError(
            f"value is {len(raw)} bytes; max {PAYLOAD_LEN} bytes fit on the tag"
        )
    return raw.ljust(PAYLOAD_LEN, b"\x00")


def decode_payload(raw: bytes) -> str:
    """Stored bytes -> ASCII string (NUL padding stripped). May be empty."""
    return raw.rstrip(b"\x00").decode("ascii", errors="replace")


def encode_content(content: dict) -> bytes:
    """A content dict -> the 48 bytes to store on the tag.

    content is {"type": "video", "id": str} or
               {"type": "playlist", "id": str, "seq": int}.
    """
    ctype = content["type"]
    cid = content["id"]
    if ctype == "video":
        text = f"{TYPE_VIDEO}\n{cid}"
    elif ctype == "playlist":
        seq = int(content["seq"])
        if not 1 <= seq <= 4:
            raise ValueError(f"sequence must be 1-4, got {seq}")
        text = f"{TYPE_PLAYLIST}\n{cid}\n{seq}"
    else:
        raise ValueError(f"unknown content type {ctype!r}")
    return encode_payload(text)


def decode_content(raw: bytes) -> dict:
    """Stored bytes -> content dict, or None if the tag is blank.

    Recognises the "v"/"p" records; anything else is treated as a legacy bare
    video id so tags written before this format keep working.
    """
    text = decode_payload(raw)
    if not text:
        return None
    lines = text.split("\n")
    if lines[0] == TYPE_VIDEO and len(lines) >= 2 and lines[1]:
        return {"type": "video", "id": lines[1]}
    if lines[0] == TYPE_PLAYLIST and len(lines) >= 3 and lines[1] and lines[2]:
        try:
            seq = int(lines[2])
        except ValueError:
            seq = None
        if seq is not None:
            return {"type": "playlist", "id": lines[1], "seq": seq}
    # Legacy: a bare video id with no type line.
    return {"type": "video", "id": text}


def _classic_auth(pn532, uid: bytes):
    # Authenticating any block authenticates its whole sector, so one auth on the
    # first data block covers blocks 4-6.
    if not pn532.mifare_classic_authenticate_block(
        uid, CLASSIC_BLOCKS[0], pn532_consts.MIFARE_CMD_AUTH_A, DEFAULT_KEY
    ):
        raise RuntimeError(f"authentication failed for block {CLASSIC_BLOCKS[0]}")


def write_payload(pn532, uid: bytes, payload: bytes) -> bytes:
    """Write the payload to the tag; return the bytes read back."""
    if len(payload) != PAYLOAD_LEN:
        raise ValueError(f"payload must be {PAYLOAD_LEN} bytes, got {len(payload)}")
    if len(uid) == 4:
        _classic_auth(pn532, uid)
        for i, block in enumerate(CLASSIC_BLOCKS):
            pn532.mifare_classic_write_block(block, payload[i * 16:(i + 1) * 16])
        return _read_classic(pn532)
    if len(uid) == 7:
        for i in range(NTAG_PAGES):
            pn532.ntag2xx_write_block(NTAG_START_PAGE + i, payload[i * 4:(i + 1) * 4])
        return _read_ntag(pn532)
    raise RuntimeError(
        f"unsupported UID length {len(uid)}; expected 4 (MIFARE Classic) or 7 (NTAG2xx)"
    )


def read_payload(pn532, uid: bytes) -> bytes:
    """Read the raw payload area from the tag."""
    if len(uid) == 4:
        _classic_auth(pn532, uid)
        return _read_classic(pn532)
    if len(uid) == 7:
        return _read_ntag(pn532)
    raise RuntimeError(
        f"unsupported UID length {len(uid)}; expected 4 (MIFARE Classic) or 7 (NTAG2xx)"
    )


def _read_classic(pn532) -> bytes:
    readback = bytearray()
    for block in CLASSIC_BLOCKS:
        readback += bytes(pn532.mifare_classic_read_block(block))[:16]
    return bytes(readback[:PAYLOAD_LEN])


def _read_ntag(pn532) -> bytes:
    readback = bytearray()
    for i in range(NTAG_PAGES):
        readback += pn532.ntag2xx_read_block(NTAG_START_PAGE + i)
    return bytes(readback[:PAYLOAD_LEN])
