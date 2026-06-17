# NFC Readers — Project Notes

## Hardware

Three PN532 NFC readers connected via UART on a Raspberry Pi 5:

| Device | Board | Reset pin |
|---|---|---|
| `/dev/ttyAMA0` | Waveshare/Elechouse PN532 HAT (mounted on GPIO header) | GPIO 20 (wired) |
| `/dev/ttyUSB0` | Plain PN532 module via CH340 USB-serial adapter | None (not wired) |
| `/dev/ttyUSB1` | Plain PN532 module via CH340 USB-serial adapter | None (not wired) |

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

## Running

```bash
cd /home/andy/nfchax
./venv/bin/python nfc_listener.py
```

Output format: `[2026-06-16T13:41:41] reader=ttyUSB0 uid=ca32b1d5`

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
| `~/.kiosk-firefox/` | Dedicated Firefox profile (autoplay-with-sound enabled). |

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
- **No mouse/keyboard on the Pi** — everything is driven from SSH. The page has
  no clickable UI; control is entirely via the queue API.
- **Embedding-disabled videos** show "video unavailable" — that's the video
  owner's setting and nothing here can override it. (Thumbnails still load, so a
  tile can appear for a video that won't actually play.)

### Launch environment

Firefox is launched from SSH against the local Wayland session (labwc), so the
script exports `XDG_RUNTIME_DIR=/run/user/1000`, `WAYLAND_DISPLAY=wayland-0`,
`DISPLAY=:0`, `MOZ_ENABLE_WAYLAND=1`. UID is 1000.

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
