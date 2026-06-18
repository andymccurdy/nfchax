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

## Tags store a YouTube video_id

Each tag holds a YouTube `video_id` as a **NUL-padded ASCII payload** (not just a
UID lookup). The on-tag layout is the single source of truth in `tag_payload.py`,
imported by both the writer and the listener so they can't drift:

- 16 bytes, ASCII, NUL-padded (an 11-char id leaves 5 NUL bytes).
- Tag family auto-detected by **UID length**: 4 bytes → MIFARE Classic (payload
  in **block 4**, authenticated with the factory default key `FF…FF`); 7 bytes →
  NTAG2xx (payload in **pages 4–7**).
- Only data blocks/pages are touched — never sector trailers (Classic) or
  lock/config pages (NTAG) — so a tag can't be bricked or locked read-only, and
  is safe to rewrite (~100k-cycle flash endurance).

### Writing a tag — `write_tag.py`

Writes one tag at a time on a single reader. By default it uses the **first
reader in `NFC_READERS`** (the ttyAMA0 HAT with `reset=20` on this Pi); override
with `--device`/`--reset` to write on any reader:

```bash
cd ~/nfchax
./venv/bin/python write_tag.py dQw4w9WgXcQ                              # default reader
./venv/bin/python write_tag.py --device /dev/ttyUSB0 --reset none ID    # a USB reader, no HAT
```

Waits up to 30s (`--timeout`) for a tag, writes the id, then reads it back and
verifies — a half-write fails loudly instead of leaving a corrupt tag. Calls
`GPIO.cleanup()` on exit.

### Reading — `nfc_listener.py`

```bash
cd ~/nfchax
./venv/bin/python nfc_listener.py
```

Listens on every reader in `NFC_READERS`; on a scan it reads the payload back and prints the
`video_id`. The payload is read **once per tag placement** (UID debounce), not on
every poll. A read/auth failure or a blank tag is reported but keeps the reader
alive. Output format:

```
[2026-06-16T23:09:51] reader=ttyAMA0 video_id=dQw4w9WgXcQ
[2026-06-16T23:10:04] reader=ttyUSB0 uid=ca32b1d5 (no video_id on tag)
[2026-06-16T23:10:12] reader=ttyUSB1 uid=047e2e… payload_error=...
```

Reading needs no reset pin, so all readers can read; *writing* defaults to the
first reader but works on any. The listener does **not** yet drive playback — wiring a scan to
`play-video.sh` / the `/enqueue` API is the remaining seam.

## Fullscreen YouTube player (kiosk)

A fullscreen YouTube player shown on the Pi's HDMI-connected monitor, driven by a
small FIFO **queue** of video ids. The intent is that scanning an NFC tag changes
what plays — `play-video.sh` is the seam the listener can call, and `serve.py`
also exposes an HTTP API the listener could hit directly.

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
browser removes the finished video, while `play-video.sh` / the NFC listener add
videos — different actors mutating the same queue, funnelled through one process.
Max queue length is **5** (`MAX_QUEUE`).

HTTP API (JSON, `Cache-Control: no-store`):

| Method + path | Effect |
|---|---|
| `GET /queue` | `{"queue": [...]}` |
| `POST /enqueue` | body = raw id; append one. `409` if already at 5. |
| `POST /replace` | body = JSON array **or** comma-separated ids; atomically replace whole queue (blanks dropped, capped to 5). |
| `POST /advance` | remove the head (the just-finished video). |

`play-video.sh` uses **`/replace`** (atomic swap). `/enqueue` exists for an
append-on-scan style NFC flow.

### How playback tracks the queue (`player/youtube-fullscreen.html`)

The page polls `GET /queue` every 2s and reconciles in `applyQueue()`:

- It plays the **head** of the queue. Crucially it only reloads the player when
  `head !== playingId`. So **replacing the queue with a list whose first id is
  the currently-playing video continues playback uninterrupted**; a different
  first id switches the video. (This is intentional — don't "optimise" it into an
  unconditional `loadVideoById`.)
- When a video **ends** (`ENDED` event) the page calls `POST /advance` to drop the
  head, then plays the new head. An `advancing` flag prevents double-advance.
- When the queue is **empty**, an opaque black `#end-cover` div covers the screen
  (also hides YouTube's end-screen recommendations). It clears when a video loads.
- A bottom **overlay** (`#queue-bar`) shows one thumbnail tile per queued video
  (`img.youtube.com/vi/<id>/mqdefault.jpg`), head highlighted. It flashes in
  whenever the queue contents change and auto-hides after 5s (`OVERLAY_MS`).

So: first `play-video.sh` call launches Firefox; later calls just replace the
queue and the already-open page picks it up via polling. No browser remote-control.

### Why it's built this way (non-obvious constraints)

- **Must be served over HTTP, not `file://`.** The YouTube IFrame embed throws
  "error 153 / configuration error" from a `file://` origin, and browsers block
  `fetch()` of local files — both reasons the local web server exists. The
  `origin` playerVar is set to `location.origin` to match.
- **serve.py serves only `player/`** (via `KIOSK_WEB_ROOT`, default `./player`),
  so the rest of the repo (this file, `nfc_listener.py`, the venv) is not
  reachable over HTTP. The queue lives in `~/kiosk-state/` (`KIOSK_STATE_DIR`),
  outside the source tree, so nothing runtime-mutable is in git. Config env:
  `KIOSK_PORT`, `KIOSK_WEB_ROOT`, `KIOSK_STATE_DIR`.
- **Autoplay-with-sound + true fullscreen** normally require a user gesture.
  That's bypassed here because we control the browser launch: `--kiosk` gives
  real fullscreen, and the `~/.kiosk-firefox` profile's `user.js` sets
  `media.autoplay.default=0`. (On a normal web page neither would be allowed.)
- **The kiosk profile is repo-provisioned.** `~/.kiosk-firefox` is otherwise
  unmanaged (Firefox fills it with caches/history on first run); the only part
  we own is `user.js`. `play-video.sh` runs `mkdir -p` on the profile and copies
  `kiosk-firefox/user.js` in **when the live profile lacks it**, so the profile
  is reproducible after a wipe or on a fresh Pi. Because it seeds only when
  missing, editing the repo `user.js` won't update an existing profile until its
  `user.js` is deleted. Firefox re-applies `user.js` on every startup, so the
  prefs can't drift once seeded.
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
