# NFC Readers — Project Notes

## Hardware

Three PN532 NFC readers connected via UART on a Raspberry Pi 5:

| Device | Board | Reset pin |
|---|---|---|
| `/dev/ttyAMA0` | Waveshare/Elechouse PN532 HAT (mounted on GPIO header) | GPIO 20 (wired) |
| `/dev/ttyUSB0` | Plain PN532 module via CH340 USB-serial adapter | None (not wired) |
| `/dev/ttyUSB1` | Plain PN532 module via CH340 USB-serial adapter | None (not wired) |

## Configuring which readers are connected (`NFC_READERS`)

The set of readers is **not** hardcoded — both `nfc_listener.py` and
`write_tag.py` read it from the `NFC_READERS` env var via `readers.py` (single
source of truth). Format: comma-separated `NAME=DEVICE[:RESET_PIN]`. The
`RESET_PIN` is a BCM GPIO pin; omit it for plain USB modules (no reset line).

Default (this Pi — HAT + two USB modules), used when `NFC_READERS` is unset:

```
ttyAMA0=/dev/ttyAMA0:20,ttyUSB0=/dev/ttyUSB0,ttyUSB1=/dev/ttyUSB1
```

A developer with **only the two USB readers and no HAT** sets:

```bash
export NFC_READERS=ttyUSB0=/dev/ttyUSB0,ttyUSB1=/dev/ttyUSB1
```

This stops the listener from spamming connection errors for an absent ttyAMA0,
and makes the writer default to the first reader (`ttyUSB0`). The listener also
accepts repeatable `--reader NAME=DEVICE[:RESET]` flags to add/override a single
reader on top of the env set.

You don't have to write the var by hand: `one_time_setup.py` **probes** the
serial ports (constructing a `PN532_UART` is the firmware handshake, so only a
reader that actually answers is counted — a bare `/dev/ttyAMA0` UART node with no
HAT is correctly skipped) and writes the detected `NFC_READERS` line into
`~/.bashrc`. That script also bootstraps `./venv` and re-execs under it, so on a
fresh clone just run `python3 one_time_setup.py`.

## Important: ttyAMA0 is correct on this Pi 5

`/dev/ttyAMA0` is the correct device for the GPIO-header UART on this board.
Do NOT change it to `ttyAMA10` or `serial0` — that analysis was wrong. The user
confirmed their pre-existing script used `ttyAMA0` successfully.

## Driver

The **Waveshare/Elechouse pn532 driver** is vendored into `./pn532/` (`pn532.py`
and `uart.py`). Only the UART submodule is included — I2C and SPI are not needed.

Do NOT use `nfcpy`. It fails to complete the PN532 UART handshake reliably on
this hardware (the init wakeup sequence is incompatible / timing-sensitive).

```python
from pn532.uart import PN532_UART
```

The venv at `./venv` uses `--system-site-packages` to inherit `RPi.GPIO`,
`lgpio`, `spidev` etc. from the system Raspberry Pi OS packages.

## RPi.GPIO thread safety

`RPi.GPIO` is not thread-safe. When constructing `PN532_UART(...)` from
multiple threads, serialise with a lock — otherwise concurrent `GPIO.setmode`/
`GPIO.setup` calls corrupt each other's pin state:

```python
gpio_lock = threading.Lock()
with gpio_lock:
    pn532 = PN532_UART(dev=device_path, reset=reset_pin)
```

## Reset pins

Only the ttyAMA0 HAT has its RSTPDN pin physically wired to GPIO 20. Pass
`reset=20` for that reader and `reset=None` for the USB ones. Toggling an
unwired GPIO pin is harmless but meaningless.

## USB reader recovery

If the USB readers stop responding (e.g. after malformed raw serial bytes are
sent to them), they have no software reset path. Unplug and replug the USB
cables to power-cycle the CH340+PN532 assembly.

## Signal strength / RSSI

The PN532 chip does not report RSSI or signal strength. `InListPassiveTarget`
only returns UID, SAK, ATQA — no proximity metric. This is a hardware
limitation, not a library gap.

## Tags store typed content (video or playlist)

A tag holds a small **newline-delimited ASCII record**, NUL-padded, describing
what to play. The layout is the single source of truth in `tag_payload.py`,
imported by the writer and both readers so they can't drift. Two content types:

```
v                 <- video tile          p                 <- playlist tile
<video_id>                                <playlist_id>
                                          <sequence 1-4>
```

- **48 bytes**, ASCII, NUL-padded. (The old 16-byte area held only a video id; a
  YouTube playlist id is 34–41 chars, so the area was grown.)
- Tag family auto-detected by **UID length**: 4 bytes → MIFARE Classic (payload
  in **blocks 4,5,6**, one auth on block 4 with the factory default key `FF…FF`);
  7 bytes → NTAG2xx (payload in **pages 4–15**).
- Only data blocks/pages are touched — never sector trailers (Classic, block 7)
  or lock/config pages (NTAG) — so a tag can't be bricked or locked read-only,
  and is safe to rewrite (~100k-cycle flash endurance).
- **Legacy tags** (bare video id, no `v`/`p` line) decode as a video, so tags
  written under the old 16-byte format keep working.

### Playlist tiles and rotate-to-skip

A playlist tile is a **square with an NFC tag on each of its four edges** — all
four hold the same playlist id but sequence `1,2,3,4`. Rotating the tile a
quarter-turn on the reader changes which edge is read; the reader turns that
sequence transition into a skip:

- `+1` mod 4 (`1→2→3→4→1`) → **skip forward** one video.
- `-1` mod 4 (`1→4→3→2→1`) → **skip back** one video.
- `+2` (180° flip) or `0` (same edge) → ambiguous → ignored.

Skips **wrap** within the playlist (forward off the last video → first; back off
the first → last). This is intra-playlist navigation only; the outer queue is
untouched (the queue item carries type+id, never the sequence). See
`queue_from_tags.py` for how a skip is gated to the head reader, and the player
section for how the wrap/skip is executed.

### Writing a tag — `write_tag.py`

Writes one tag at a time on a single reader. By default it uses the **first
reader in `NFC_READERS`** (the ttyAMA0 HAT with `reset=20` on this Pi); override
with `--device`/`--reset` to write on any reader:

```bash
cd ~/nfchax
./venv/bin/python write_tag.py dQw4w9WgXcQ                               # a video tile
./venv/bin/python write_tag.py --type playlist PLxxxx --sequence 1       # one playlist edge
./venv/bin/python write_tag.py --type playlist PLxxxx --edges            # all 4 edges in turn
./venv/bin/python write_tag.py --device /dev/ttyUSB0 --reset none ID     # a USB reader, no HAT
```

`--edges` prompts you to rotate the tile a quarter-turn between each of the four
writes. Each write waits up to 30s (`--timeout`) for a tag, then reads it back
and verifies — a half-write fails loudly instead of leaving a corrupt tag. Calls
`GPIO.cleanup()` on exit.

### Reading — `nfc_listener.py`

```bash
cd ~/nfchax
./venv/bin/python nfc_listener.py
```

Listens on every reader in `NFC_READERS`; on a scan it reads the payload back and
prints the decoded content. The payload is read **once per tag placement** (UID
debounce), not on every poll. A read/auth failure or a blank tag is reported but
keeps the reader alive. Output format:

```
[2026-06-16T23:09:51] reader=ttyAMA0 video_id=dQw4w9WgXcQ
[2026-06-16T23:10:04] reader=ttyUSB0 playlist_id=PLxxxx seq=1
[2026-06-16T23:10:12] reader=ttyUSB1 uid=047e2e… payload_error=...
```

`nfc_listener.py` is a **diagnostic** tool; `queue_from_tags.py` is what actually
drives playback from the tags on the readers (don't run both — they can't share
the same UART).

## Fullscreen YouTube player (kiosk)

A fullscreen YouTube player shown on the Pi's HDMI-connected monitor, driven by a
small FIFO **queue** of tiles (videos and playlists). `queue_from_tags.py` drives
the queue from the tags physically on the readers; `play-video.sh` is the manual
seam, and `serve.py` owns the queue behind an HTTP API.

### Components

| Path | Role |
|---|---|
| `play-video.sh ID[,ID,...]` | Control script. Replaces the queue + launches things. |
| `serve.py` | Static server **and** queue owner (stdlib only). NOT web-exposed. |
| `player/youtube-fullscreen.html` | The page. Only files under `player/` are web-exposed. |
| `~/kiosk-state/queue.json` | The queue, persisted. Kept OUTSIDE the repo. |
| `kiosk-firefox/user.js` | Canonical profile prefs, in the repo. Source of truth. |
| `~/.kiosk-firefox/` | Live Firefox profile (autoplay-with-sound enabled). Seeded from `kiosk-firefox/user.js`; rest is disposable runtime state. |

### The queue is server-owned (single source of truth)

`serve.py` owns the queue and is the **only** writer (one process, guarded by a
`threading.Lock`, persisted atomically to `queue.json`). This avoids races: the
browser removes the finished tile, while `play-video.sh` / `queue_from_tags.py`
add tiles — different actors mutating the same queue, funnelled through one
process. Max queue length is **5** tiles (`MAX_QUEUE`; a playlist is one tile).

Each **stored** queue item is a tile object `{"type": "video"|"playlist", "id"}`.
Bare id strings are accepted and normalised to video items, so the CLI's
comma-separated form and any old `queue.json` still work.

**Playlists are expanded server-side** (see the constraints section for *why* —
YouTube's playlist *embed* fails on this box). `serve.py` scrapes a playlist id's
video ids from its public playlist page (no API key) and tracks a current
`index` per playlist. Responses therefore return **resolved** items — a playlist
tile resolves to the single video the player should show right now:

- video    → `{"type":"video","id":id}`
- playlist → `{"type":"playlist","id":plid,"video":cur,"index":i,"count":n}`

Expanded playlists live in an in-memory dict (not persisted), pruned when a tile
leaves the queue — so a removed-then-replaced playlist restarts from its first
video.

HTTP API (JSON, `Cache-Control: no-store`; queue items in responses are resolved):

| Method + path | Effect |
|---|---|
| `GET /queue` | `{"queue": [resolved, ...]}` |
| `POST /enqueue` | body = id, JSON item, or `{"video_id": id}`; append one tile. `409` if already at 5. |
| `POST /replace` | body = JSON array (items or ids) **or** comma-separated ids; atomically replace whole queue (blanks dropped, capped to 5). |
| `POST /advance` | the head finished. If the head is a playlist with more videos, step to its next video; otherwise drop the tile. |
| `POST /skip` | body = `{"action": "next"\|"prev", "playlist_id": id}`; rotate-to-skip **within the head playlist** (wraps around the ends). |

`play-video.sh` uses **`/replace`** (atomic swap). `queue_from_tags.py` uses
`/replace` (whole queue from the tags) and `/skip` (rotate-to-skip).

The server owns **all** playlist logic — the player never touches YouTube's
playlist API. `/skip` (from a tile rotation) and `/advance` (from a video
ending) both just move the server-side index; the player picks up the new
resolved video on its next poll and reloads it as a plain single video.

### How playback tracks the queue (`player/youtube-fullscreen.html`)

**The player only ever plays single videos.** It polls `GET /queue` every 500ms
and reconciles in `applyQueue()`:

- The head's video to play is `tileVideo(head)` — a video tile's `id`, or a
  playlist tile's resolved `video`. It only reloads (`loadVideoById`) when that
  **video id changes**. So a queue `/replace` whose head is the same playlist at
  the same index continues uninterrupted, while an advance/skip (which changes
  the resolved video) loads the next one. (Intentional — don't "optimise" away.)
- **`ENDED`** or **`onError`** → `POST /advance`. The *server* decides whether
  that means "next video in this playlist" or "drop the tile" — the player
  doesn't know or care. An `advancing` flag prevents double-advance.
- A head playlist tile whose `video` is empty (expansion failed / empty
  playlist) is skipped via `/advance`.
- When the queue is **empty**, an opaque black `#end-cover` div covers the screen
  (also hides YouTube's end-screen recommendations). It clears when a video loads.
- A bottom **overlay** (`#queue-bar`) shows one tile per queued item, using the
  item's current video thumbnail (`img.youtube.com/vi/<id>/mqdefault.jpg`);
  playlist tiles also show `index+1/count`. Head highlighted; flashes in on any
  queue change, auto-hides after 5s (`OVERLAY_MS`).

So: first `play-video.sh` call launches Firefox; later calls just replace the
queue and the already-open page picks it up via polling. No browser remote-control.

### Why it's built this way (non-obvious constraints)

- **YouTube's *playlist* embed does not work here; single-video embeds do.**
  Loading a playlist (via `loadPlaylist` or `listType=playlist`) reaches the
  PLAYING state but the video shows *"An error occurred / Playback ID …"* — the
  playback backend refuses the playlist-embed media path (localhost/non-public
  origin, unfixable from our side). Single-video embeds are fine. **So playlists
  are never embedded as playlists:** `serve.py` scrapes the playlist page for its
  video ids and plays them one at a time as single videos, tracking the position
  itself. This also means no YouTube Data API key is needed. (Scraping gets the
  first ~100 videos of a playlist — enough for a kiosk; deeper pages would need
  continuation-token handling, not implemented.) Don't "simplify" this back into
  a real playlist embed — it will break.
- **Must be served over HTTP, not `file://`.** The YouTube IFrame embed throws
  "error 153 / configuration error" from a `file://` origin, and browsers block
  `fetch()` of local files — both reasons the local web server exists. The
  `origin` playerVar is set to `location.origin` to match.
- **serve.py serves only `player/`** (via `KIOSK_WEB_ROOT`, default `./player`),
  so the rest of the repo (this file, `nfc_listener.py`, the venv) is not
  reachable over HTTP. The queue lives in `~/kiosk-state/` (`KIOSK_STATE_DIR`),
  outside the source tree, so nothing runtime-mutable is in git. Config env:
  `KIOSK_PORT`, `KIOSK_BIND` (default `127.0.0.1` — loopback only; not
  LAN-exposed), `KIOSK_WEB_ROOT`, `KIOSK_STATE_DIR`.
- **Autoplay-with-sound + true fullscreen** normally require a user gesture.
  That's bypassed here because we control the browser launch: `--kiosk` gives
  real fullscreen, and the `~/.kiosk-firefox` profile's `user.js` sets
  `media.autoplay.default=0`. (On a normal web page neither would be allowed.)
- **The kiosk profile is repo-provisioned.** `~/.kiosk-firefox` is otherwise
  unmanaged (Firefox fills it with caches/history on first run); the only part
  we own is `user.js`. `play-video.sh` runs `mkdir -p` on the profile and copies
  `kiosk-firefox/user.js` in **when the live profile lacks it**, so the profile
  is reproducible after a wipe or on a fresh Pi. Because it seeds only when
  missing, **editing the repo `user.js` does NOT update an existing live profile**
  — delete `~/.kiosk-firefox/user.js` (or the whole profile) to re-seed. Firefox
  re-applies `user.js` on every startup, so the prefs can't drift once seeded.
  Beyond autoplay, `user.js` disables the disk cache (so an edited
  `youtube-fullscreen.html` is never served stale after a reload) and disables
  crash/session-restore (so the kiosk always boots straight to the URL). If the
  live profile ever gets wedged (e.g. Firefox stops loading the YouTube IFrame
  API — symptom: the page loads but never polls `/queue`), just wipe and re-seed:
  `pkill firefox; rm -rf ~/.kiosk-firefox` then run `play-video.sh`/`reload-kiosk.sh`.
- **No mouse/keyboard on the Pi** — everything is driven from SSH. The page has
  no clickable UI; control is entirely via the queue API.
- **Embedding-disabled videos** show "video unavailable" — that's the video
  owner's setting and nothing here can override it. (Thumbnails still load, so a
  tile can appear for a video that won't actually play.)

### Launch environment

Firefox is launched from SSH against the local Wayland session (labwc), so the
script exports `XDG_RUNTIME_DIR=/run/user/$(id -u)`, `WAYLAND_DISPLAY=wayland-0`,
`DISPLAY=:0`, `MOZ_ENABLE_WAYLAND=1`. The runtime dir is derived from the current
user's UID rather than hardcoded, so it works regardless of which user runs it.

The server and browser do NOT survive a reboot (no systemd service, by choice).
After a reboot, just run `play-video.sh ...` again — it bootstraps everything
from scratch. `queue.json` persists across restarts, so a stale queue can carry
over; `rm ~/kiosk-state/queue.json` for a clean start. Logs: `/tmp/httpd.log`,
`/tmp/firefox-kiosk.log`.

```bash
~/nfchax/play-video.sh Y1ujpoDlgRU                 # replace queue with one video
~/nfchax/play-video.sh Y1ujpoDlgRU,FAyKDaXEAgc     # replace with a list (max 5)
pkill firefox                                       # stop the display
```
