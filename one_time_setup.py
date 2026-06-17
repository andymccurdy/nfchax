#!/usr/bin/env python3
"""One-time host setup for the nfchax kiosk on Raspberry Pi OS (labwc/Wayland).

Installs a fully-transparent cursor theme and points the labwc session at it, so
no mouse pointer is ever drawn over the kiosk video. (Page-level CSS `cursor:
none` can't do this — labwc draws the cursor and only refreshes it on pointer
motion, so with no mouse it just sits on top of the video.)

Idempotent: safe to re-run. labwc reads its environment file only at startup, so
a reboot (or labwc session restart) is required afterwards.
"""

import os
import struct

ICON_BASE = os.path.expanduser("~/.local/share/icons/blank")
LABWC_ENV = os.path.expanduser("~/.config/labwc/environment")
ENV_MARKER = "# nfchax kiosk: transparent cursor (see README.md)"

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


def main():
    cursors = write_blank_cursor_theme()
    print(f"Transparent cursor theme installed: {ICON_BASE}")
    print(f"  ({len(os.listdir(cursors))} cursor names -> 1x1 transparent image)")

    if update_labwc_environment():
        print(f"Updated {LABWC_ENV}: XCURSOR_THEME=blank, XCURSOR_SIZE=24")
    else:
        print(f"{LABWC_ENV} already points at the blank cursor — no change")

    print()
    print("Done. labwc reads its environment only at startup, so REBOOT (or restart")
    print("the labwc session) to apply, then relaunch the kiosk: ./reload-kiosk.sh")


if __name__ == "__main__":
    main()
