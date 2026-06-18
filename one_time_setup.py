#!/usr/bin/env python3
"""One-time host setup for the nfchax kiosk on Raspberry Pi OS (labwc/Wayland).

Two things:

1. Installs a fully-transparent cursor theme and points the labwc session at it,
   so no mouse pointer is ever drawn over the kiosk video. (Page-level CSS
   `cursor: none` can't do this — labwc draws the cursor and only refreshes it on
   pointer motion, so with no mouse it just sits on top of the video.)
2. Probes the serial ports for PN532 readers and writes the detected set to
   `NFC_READERS` in ~/.bashrc, so the listener/writer adapt to whatever hardware
   is connected (HAT + USB, or USB-only). Probing — rather than just globbing
   device paths — is deliberate: a Pi with no HAT often still exposes a bare
   /dev/ttyAMA0 UART node, and only a real reader answers the firmware handshake.

Bootstraps the project venv too, so a fresh clone needs nothing but system
Python. Run it with the system interpreter:

    python3 one_time_setup.py

It creates ./venv (if missing) and re-execs itself under it, then imports the
PN532 driver for the probe. Idempotent: safe to re-run. labwc reads its
environment file only at startup, so a reboot (or labwc session restart) is
required afterwards.
"""

import glob
import os
import struct
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(REPO_DIR, "venv")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python")

ICON_BASE = os.path.expanduser("~/.local/share/icons/blank")
LABWC_ENV = os.path.expanduser("~/.config/labwc/environment")
ENV_MARKER = "# nfchax kiosk: transparent cursor (see README.md)"

BASHRC = os.path.expanduser("~/.bashrc")
BASHRC_MARKER = "# nfchax: NFC readers detected by one_time_setup.py"

# Every common cursor name is symlinked to the transparent image so no pointer
# shape can ever render, whatever the surface under the (non-existent) mouse.
CURSOR_NAMES = (
    "default arrow top_left_arrow pointer hand1 hand2 text xterm watch wait "
    "progress fleur move grab grabbing not-allowed help center_ptr right_ptr "
    "sb_h_double_arrow sb_v_double_arrow e-resize w-resize n-resize s-resize "
    "ew-resize ns-resize nesw-resize nwse-resize size_all"
).split()


def write_blank_cursor_theme():
    """Write a transparent Xcursor theme to ICON_BASE; return the cursors dir."""
    # Minimal valid Xcursor file: one 1x1 fully-transparent ARGB image.
    # Layout: file header(16) + TOC(12) + image chunk(36 hdr + 4 px); chunk @ 28.
    size = 24
    img = struct.pack("<9I", 36, 0xFFFD0002, size, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
    toc = struct.pack("<3I", 0xFFFD0002, size, 28)
    hdr = b"Xcur" + struct.pack("<3I", 16, 0x00010000, 1)
    data = hdr + toc + img

    cursors = os.path.join(ICON_BASE, "cursors")
    os.makedirs(cursors, exist_ok=True)
    with open(os.path.join(ICON_BASE, "index.theme"), "w") as f:
        f.write("[Icon Theme]\nName=blank\nComment=Fully transparent cursor (kiosk)\n")
    with open(os.path.join(cursors, "left_ptr"), "wb") as f:
        f.write(data)
    for name in CURSOR_NAMES:
        link = os.path.join(cursors, name)
        if not os.path.lexists(link):
            os.symlink("left_ptr", link)
    return cursors


def _ensure_env_var(lines, key, value):
    """Set `key=value` in lines, replacing any existing assignment. Returns
    (lines, changed)."""
    out, found, changed = [], False, False
    for line in lines:
        if line.lstrip().startswith(f"{key}="):
            found = True
            if line != f"{key}={value}":
                changed = True
            out.append(f"{key}={value}")
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
        changed = True
    return out, changed


def update_labwc_environment():
    """Point labwc at the blank cursor theme. Returns True if the file changed."""
    os.makedirs(os.path.dirname(LABWC_ENV), exist_ok=True)
    lines = []
    if os.path.exists(LABWC_ENV):
        with open(LABWC_ENV) as f:
            lines = f.read().splitlines()

    changed = False
    already_set = any(l.lstrip().startswith("XCURSOR_THEME=") for l in lines)
    if ENV_MARKER not in lines and not already_set:
        lines.append(ENV_MARKER)
        changed = True
    lines, c1 = _ensure_env_var(lines, "XCURSOR_THEME", "blank")
    lines, c2 = _ensure_env_var(lines, "XCURSOR_SIZE", "24")
    changed = changed or c1 or c2

    if changed:
        with open(LABWC_ENV, "w") as f:
            f.write("\n".join(lines) + "\n")
    return changed


def running_under_venv():
    """True if the current interpreter is the project venv (not system python)."""
    return os.path.abspath(sys.prefix) == VENV_DIR


def ensure_venv():
    """Create ./venv with --system-site-packages if it doesn't exist yet.

    The venv inherits system packages (RPi.GPIO, pyserial, lgpio) rather than
    pip-installing them — see CLAUDE.md. Returns True if it created the venv."""
    if os.path.exists(VENV_PYTHON):
        return False
    print(f"Creating venv at {VENV_DIR} (inheriting system site-packages)...")
    import venv
    venv.EnvBuilder(system_site_packages=True, with_pip=True).create(VENV_DIR)
    return True


def bootstrap_venv():
    """Ensure the venv exists, then re-exec under it if we're not already there,
    so the PN532 driver (and anything else venv-only) is importable below."""
    try:
        ensure_venv()
    except Exception as exc:
        # Not fatal: the probe falls back to a graceful skip if deps are missing.
        print(f"Warning: could not create venv ({exc}); continuing with {sys.executable}.")
        return
    if not running_under_venv() and os.path.exists(VENV_PYTHON):
        print(f"Re-executing under the venv: {VENV_PYTHON}")
        os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])


def candidate_devices():
    """Serial devices that might host a PN532, paired with their reset GPIO pin.
    Only the GPIO-header HAT (ttyAMA0) has a wired reset line (GPIO 20); USB
    modules have none."""
    candidates = []
    if os.path.exists("/dev/ttyAMA0"):
        candidates.append(("/dev/ttyAMA0", 20))
    for device in sorted(glob.glob("/dev/ttyUSB*")):
        candidates.append((device, None))
    return candidates


def probe_readers():
    """Probe each candidate device for a PN532. Returns {name: (device, reset)}
    for the ports a reader actually answers on, or None if the driver can't be
    imported (e.g. not run under the venv)."""
    try:
        from pn532.uart import PN532_UART
        import RPi.GPIO as GPIO
    except ImportError as exc:
        print(f"Skipping NFC reader detection — driver import failed ({exc}).")
        print("Re-run under the venv to detect readers: ./venv/bin/python one_time_setup.py")
        return None

    found = {}
    try:
        for device, reset in candidate_devices():
            name = os.path.basename(device)
            try:
                # Constructing PN532_UART performs the firmware handshake, so a
                # successful construction means a reader really answered here.
                PN532_UART(dev=device, reset=reset, debug=False)
            except Exception as exc:
                print(f"  {device}: no PN532 ({exc})")
                continue
            found[name] = (device, reset)
            detail = f" (reset GPIO {reset})" if reset else ""
            print(f"  {device}: PN532 detected{detail}")
    finally:
        GPIO.cleanup()
    return found


def spec_from_readers(readers):
    """Render {name: (device, reset)} as an NFC_READERS spec string."""
    parts = []
    for name, (device, reset) in readers.items():
        parts.append(f"{name}={device}" + (f":{reset}" if reset else ""))
    return ",".join(parts)


def update_bashrc(spec):
    """Write/replace the NFC_READERS export in ~/.bashrc. Returns True if changed."""
    export_line = f"export NFC_READERS={spec}"
    lines = []
    if os.path.exists(BASHRC):
        with open(BASHRC) as f:
            lines = f.read().splitlines()

    # Drop any previous nfchax block so re-runs replace rather than stack up.
    kept = [l for l in lines
            if l != BASHRC_MARKER and not l.lstrip().startswith("export NFC_READERS=")]
    new_lines = kept + [BASHRC_MARKER, export_line]
    if new_lines == lines:
        return False
    with open(BASHRC, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    return True


def setup_nfc_readers():
    """Detect connected readers and persist them to ~/.bashrc."""
    print("Probing serial ports for PN532 readers...")
    readers = probe_readers()
    if readers is None:
        return
    if not readers:
        print("No PN532 readers detected — leaving NFC_READERS unset.")
        print("Check wiring/USB cables, then re-run this script.")
        return

    spec = spec_from_readers(readers)
    if update_bashrc(spec):
        print(f"Wrote NFC_READERS to {BASHRC}:")
        print(f"  export NFC_READERS={spec}")
        print("Open a new shell (or `source ~/.bashrc`) so the listener/writer see it.")
    else:
        print(f"{BASHRC} already has the right NFC_READERS — no change.")
        print(f"  export NFC_READERS={spec}")


def main():
    bootstrap_venv()  # may re-exec the process under ./venv before continuing

    cursors = write_blank_cursor_theme()
    print(f"Transparent cursor theme installed: {ICON_BASE}")
    print(f"  ({len(os.listdir(cursors))} cursor names -> 1x1 transparent image)")

    if update_labwc_environment():
        print(f"Updated {LABWC_ENV}: XCURSOR_THEME=blank, XCURSOR_SIZE=24")
    else:
        print(f"{LABWC_ENV} already points at the blank cursor — no change")

    print()
    setup_nfc_readers()

    print()
    print("Done. labwc reads its environment only at startup, so REBOOT (or restart")
    print("the labwc session) to apply, then relaunch the kiosk: ./reload-kiosk.sh")


if __name__ == "__main__":
    main()
