#!/usr/bin/env python3
"""
Generate a modified EDID with Display Range Limits for eDP VRR panels.

Many eDP OLED panels advertise their VRR range via a DisplayID v2.0
Adaptive Sync Data Block (tag 0x2B), but the Linux DRM core only reads
the base EDID Range Limits descriptor (tag 0xFD). This script reads
the panel's EDID, extracts the adaptive sync range from DisplayID,
and injects a standard Range Limits descriptor so the kernel can see it.

Usage:
    sudo python3 generate-edid.py                    # auto-detect eDP panel
    sudo python3 generate-edid.py /path/to/edid.bin  # from file
    sudo python3 generate-edid.py --output vrr.bin    # custom output path
"""

import argparse
import glob
import os
import struct
import sys


def find_edp_edid():
    """Find the eDP connector's EDID in sysfs."""
    for path in glob.glob("/sys/class/drm/card*-eDP-*/edid"):
        if os.path.getsize(path) > 0:
            return path
    return None


def parse_displayid_adaptive_sync(edid):
    """Extract min/max refresh from DisplayID Adaptive Sync block (0x2B)."""
    if len(edid) < 256:
        return None, None

    ext = edid[128:]
    if ext[0] != 0x70:  # DisplayID extension tag
        return None, None

    section_len = ext[2]
    offset = 5  # skip DisplayID header

    while offset < min(section_len + 4, len(ext) - 3):
        tag = ext[offset]
        rev = ext[offset + 1]
        length = ext[offset + 2]

        if tag == 0 and length == 0:
            break

        if tag == 0x2B and length >= 6:
            # Adaptive Sync Data Block
            data = ext[offset + 3:offset + 3 + length]
            min_vfreq = data[2]
            max_vfreq = data[3] | ((data[4] & 0x3) << 8)
            if min_vfreq and max_vfreq and max_vfreq > min_vfreq:
                return min_vfreq, max_vfreq

        if tag == 0x25 and length >= 6:
            # Dynamic Video Timing Range Limits
            data = ext[offset + 3:offset + 3 + length]
            has_duration = (data[0] >> 3) & 0x3
            off = 5 if has_duration else 3
            if length >= off + 3:
                min_vfreq = data[off]
                max_vfreq = data[off + 1] | ((data[off + 2] & 0x3) << 8)
                if min_vfreq and max_vfreq and max_vfreq > min_vfreq:
                    return min_vfreq, max_vfreq

        offset += 3 + length

    return None, None


def has_range_limits(edid):
    """Check if base EDID already has a Range Limits descriptor."""
    for slot in [54, 72, 90, 108]:
        if edid[slot + 3] == 0xFD:
            return True
    return False


def inject_range_limits(edid, min_hz, max_hz):
    """Inject a Display Range Limits descriptor into the first empty slot."""
    edid = bytearray(edid)

    for slot in [54, 72, 90, 108]:
        desc = edid[slot:slot + 18]
        if desc[0] == 0 and desc[1] == 0 and desc[2] == 0 and desc[3] in (0x00, 0x10):
            # Empty or manufacturer-specific descriptor -- replace it
            range_desc = bytearray(18)
            range_desc[0] = 0x00
            range_desc[1] = 0x00
            range_desc[2] = 0x00
            range_desc[3] = 0xFD  # Range Limits tag
            range_desc[4] = 0x00  # offsets
            range_desc[5] = min_hz & 0xFF
            range_desc[6] = max_hz & 0xFF
            range_desc[7] = 0xFF  # min hfreq
            range_desc[8] = 0xFF  # max hfreq
            range_desc[9] = 90    # max pixel clock / 10 MHz
            range_desc[10] = 0x01 # Range Limits Only
            range_desc[11:18] = b'\x0a' + b'\x20' * 6

            edid[slot:slot + 18] = range_desc

            # Fix base block checksum
            edid[127] = (256 - sum(edid[0:127]) % 256) % 256

            return edid

    print("ERROR: No empty descriptor slot found in base EDID", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="?", help="Input EDID file (default: auto-detect eDP)")
    parser.add_argument("-o", "--output", default="vrr.bin", help="Output file (default: vrr.bin)")
    args = parser.parse_args()

    if args.input:
        edid_path = args.input
    else:
        edid_path = find_edp_edid()
        if not edid_path:
            print("ERROR: No eDP panel found. Specify EDID path manually.", file=sys.stderr)
            sys.exit(1)
        print(f"Found eDP EDID: {edid_path}")

    with open(edid_path, "rb") as f:
        edid = f.read()

    print(f"EDID size: {len(edid)} bytes")

    if has_range_limits(edid):
        print("EDID already has Range Limits descriptor. No modification needed.")
        sys.exit(0)

    min_hz, max_hz = parse_displayid_adaptive_sync(edid)
    if min_hz is None:
        print("ERROR: No Adaptive Sync data found in DisplayID extension.", file=sys.stderr)
        sys.exit(1)

    print(f"DisplayID Adaptive Sync range: {min_hz}-{max_hz} Hz")

    modified = inject_range_limits(edid, min_hz, max_hz)

    # Verify checksum
    assert sum(modified[0:128]) % 256 == 0, "Base block checksum failed"

    with open(args.output, "wb") as f:
        f.write(modified)

    print(f"Written modified EDID to {args.output}")
    print(f"Install: sudo cp {args.output} /lib/firmware/edid/vrr.bin")
    print(f"Cmdline: drm.edid_firmware=eDP-1:edid/vrr.bin")


if __name__ == "__main__":
    main()
