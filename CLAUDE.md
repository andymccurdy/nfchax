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
