# nfchax

NFC-driven YouTube kiosk for a Raspberry Pi 5: PN532 readers turn tagged cards
into a fullscreen YouTube playlist. Tags store a `video_id`; the readers drive a
server-owned queue that a kiosk Firefox window plays.

See [CLAUDE.md](CLAUDE.md) for hardware wiring, the PN532 driver, the tag payload
format, and how the player/queue fit together.

## Installation

The hardware, Python venv, and PN532 driver are described in
[CLAUDE.md](CLAUDE.md).

### First-time Raspberry Pi setup

The first time you set up a Pi for this kiosk, run the one-time host setup script
with the **system** Python (a fresh clone has no venv yet):

```bash
cd ~/nfchax
python3 one_time_setup.py
```

It does three things:

1. Creates the project `./venv` (`--system-site-packages`, inheriting `RPi.GPIO`
   / `pyserial` from the OS) if it doesn't exist, then re-execs itself under it.
2. Installs a fully-transparent cursor theme and points the labwc session at it,
   so no mouse pointer is ever drawn on top of the video. (Page-level CSS
   `cursor: none` can't do this — labwc draws the cursor and only refreshes it on
   pointer motion, so with no mouse it just sits over the video.)
3. Probes the serial ports for PN532 readers and writes the detected set to
   `NFC_READERS` in `~/.bashrc`, so the listener and writer adapt to your
   hardware (HAT + USB, or USB-only — see [CLAUDE.md](CLAUDE.md)). Open a new
   shell (or `source ~/.bashrc`) afterwards so they pick it up.

The script is **idempotent** — safe to re-run.

labwc reads its environment only at startup, so **reboot** (or restart the labwc
session) to apply, then relaunch the kiosk:

```bash
./reload-kiosk.sh
```

To revert, remove the `XCURSOR_*` lines from `~/.config/labwc/environment` (or set
`XCURSOR_THEME=PiXtrix`) and reboot.
