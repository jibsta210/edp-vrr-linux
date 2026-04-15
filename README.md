# eDP VRR on Linux: Fixing Variable Refresh Rate for OLED Laptop Panels

Kernel patches and workarounds to enable **VRR (Variable Refresh Rate) 20-120Hz** on Intel eDP OLED panels where the OEM firmware doesn't set the VBT VRR flag.

Tested on **Dell XPS 2026** (Intel Panther Lake, LG Display OLED 3200x2000) running **CachyOS** with the **COSMIC desktop** (cosmic-comp compositor). Should apply to any laptop with a VRR-capable eDP panel that works on Windows but not Linux.

## The Problem

Your laptop's OLED panel supports VRR 20-120Hz. Windows uses it fine. Linux says `vrr_capable: 0`. Why?

**Three bugs in the Linux display stack block eDP VRR:**

| Layer | Bug | Impact |
|-------|-----|--------|
| **DRM core** (`drm_edid.c`) | Doesn't parse DisplayID v2.0 Adaptive Sync block (tag 0x2B) into `monitor_range` | Kernel doesn't know the VRR range |
| **Intel driver** (`intel_vrr.c`) | Gates eDP VRR on VBT firmware flag that OEMs don't set | `vrr_capable=0` even with valid EDID |
| **Compositor** (smithay/cosmic-comp) | Returns `NotSupported` when `vrr_capable=0` | Compositor never tries VRR |

### Why it works on Windows

The Intel Windows driver reads the DisplayID Adaptive Sync block directly and doesn't require the VBT flag. AMD's Linux driver (`amdgpu_dm.c`) has its own workaround (`parse_edid_displayid_vrr()`). Intel's Linux driver just hasn't caught up.

## The Fix

### Part 1: Kernel Patches (for upstream)

Two patches in `patches/`:

**`0001-drm-edid-parse-displayid-adaptive-sync.patch`** -- Adds `drm_get_monitor_range_displayid()` to the DRM EDID parser. Reads the DisplayID v2.0 Adaptive Sync Data Block (tag 0x2B) and Dynamic Video Timing block (tag 0x25) as a fallback when the base EDID has no Range Limits descriptor. This is the generic fix that benefits all drivers.

**`0002-drm-i915-allow-edp-vrr-without-vbt.patch`** -- Relaxes the eDP VRR check in `intel_vrr_is_capable()`. Instead of requiring the VBT VRR flag, it also accepts a valid EDID monitor range (delta > 10Hz) as proof of VRR capability. The existing DPCD and range checks still apply.

### Part 2: EDID Override (interim workaround)

Until the kernel patches land upstream, use an EDID override to inject a standard Range Limits descriptor that the existing kernel can parse:

```bash
# Generate the modified EDID from your panel
sudo python3 edid/generate-edid.py
sudo cp vrr.bin /lib/firmware/edid/

# Add to initramfs
# Edit /etc/mkinitcpio.conf:
#   FILES=(/lib/firmware/edid/vrr.bin)
sudo mkinitcpio -P

# Add kernel command line parameter
# (method depends on bootloader -- see configs/)
drm.edid_firmware=eDP-1:edid/vrr.bin
```

### Part 3: Compositor Configuration

For COSMIC desktop (cosmic-comp), set VRR to force mode:

Edit `~/.local/state/cosmic-comp/outputs.ron` and change `vrr: r#false` to `vrr: force` for your eDP output. Or use:

```bash
cosmic-randr mode eDP-1 <width> <height> --refresh <hz> --adaptive-sync true
```

## Quick Start (CachyOS/Arch)

```bash
# 1. Generate and install EDID override
sudo python3 edid/generate-edid.py -o /lib/firmware/edid/vrr.bin

# 2. Add to initramfs
sudo sed -i 's/^FILES=.*/FILES=(\/lib\/firmware\/edid\/vrr.bin)/' /etc/mkinitcpio.conf
sudo mkinitcpio -P

# 3. Add kernel cmdline (Limine example)
# Edit /etc/default/limine, append to KERNEL_CMDLINE:
#   drm.edid_firmware=eDP-1:edid/vrr.bin xe.enable_dpcd_backlight=3

# 4. Build patched kernel (optional, for full fix)
cd cachyos/
makepkg -s --skippgpcheck

# 5. Enable VRR in compositor
# Edit ~/.local/state/cosmic-comp/outputs.ron: vrr: force

# 6. Reboot and verify
cosmic-randr list | grep Adaptive
# Should show: Adaptive Sync Support: true / Adaptive Sync: true
```

## Verification

### Check VRR capability
```bash
# vrr_capable should be 1
sudo modetest -c | grep -A3 vrr_capable

# VRR range should show your panel's range
sudo cat /sys/kernel/debug/dri/0/eDP-1/vrr_range

# VRR_ENABLED should be 1 when compositor has adaptive sync on
sudo modetest -p | grep -A3 VRR_ENABLED
```

### Prove VRR is active (measure frame timing)
```bash
# Enable vblank tracing
sudo bash -c 'cd /sys/kernel/tracing && echo 1 > events/drm/drm_vblank_event_delivered/enable && echo 1 > tracing_on'

# Run something (move windows, play video, run glxgears)
sleep 3

# Analyze intervals
sudo bash -c 'echo 0 > /sys/kernel/tracing/tracing_on'
sudo python3 -c "
import re
with open('/sys/kernel/tracing/trace') as f:
    ts = [float(m.group(1)) for line in f if 'vblank_event_delivered' in line
          for m in [re.search(r'(\d+\.\d+):', line)] if m]
intervals = [(ts[i+1]-ts[i])*1000 for i in range(len(ts)-1) if 1 < (ts[i+1]-ts[i])*1000 < 60]
if intervals:
    print(f'Min: {min(intervals):.1f}ms ({1000/max(intervals):.0f}Hz)')
    print(f'Max: {max(intervals):.1f}ms ({1000/min(intervals):.0f}Hz)')
    print('VRR active!' if max(intervals)/min(intervals) > 1.3 else 'Fixed rate')
"
```

Example output showing VRR active (32-120Hz):
```
Vblank intervals (153 samples):
  Min: 8.324 ms (120 Hz)
  Max: 30.847 ms (32 Hz)
  *** VRR CONFIRMED ***
```

## Affected Hardware

This fix applies to any laptop with:
- Intel GPU (xe or i915 driver)
- eDP OLED panel with VRR support
- DisplayID v2.0 extension with Adaptive Sync Data Block (tag 0x2B)
- OEM firmware (VBT) that does NOT set the VRR flag for eDP

Known affected:
- **Dell XPS 2026** (Panther Lake, LG Display VNFT2 160WV1)
- **Framework Laptop 13** (BOE NE135A1M-NY1) -- similar VBT issue
- Likely many other 2025-2026 laptops with eDP OLED panels

## Technical Details

### How we found the bugs

1. `edid-decode` shows the panel's EDID has an Adaptive Sync Data Block declaring 20-120Hz VRR
2. But `vrr_capable=0` in the DRM connector properties
3. `intel_vrr_is_capable()` in `intel_vrr.c` returns false because:
   - Line 50: `if (!connector->panel.vbt.vrr) return false;` -- VBT flag not set by Dell
4. Even bypassing the VBT check, `monitor_range` is 0/0 because:
   - `drm_get_monitor_range()` in `drm_edid.c` only reads base EDID Range Limits (tag 0xFD)
   - The panel has no 0xFD descriptor -- range data is only in DisplayID tag 0x2B
   - AMD's driver has a private workaround; the generic DRM layer does not
5. The final check `info->monitor_range.max_vfreq - info->monitor_range.min_vfreq > 10` also fails

### DisplayID tag 0x2B vs 0x25

The kernel defines `DATA_BLOCK_2_DYNAMIC_VIDEO_TIMING` (0x25) but NOT the Adaptive Sync Data Block (0x2B). These are different DisplayID v2.0 block types:
- **0x25**: Dynamic Video Timing Range Limits (similar to EDID Monitor Range)
- **0x2B**: Adaptive Sync descriptor (specifically for VRR-capable panels)

Most modern eDP OLED panels use 0x2B. Our patch handles both.

### DSC interaction

At 3200x2000@120Hz, Display Stream Compression (DSC) is active with 2 slices. The kernel has a `joiner_pipes` check that disables VRR when pipe joiner is active, but 2 DSC slices on a single pipe does NOT trigger joiner. VRR works fine with DSC.

## Upstream Status

- No patches submitted yet
- Related: [Adriano Vero's LKML patch](https://lkml.org/lkml/2026/3/28/396) for DisplayID monitor range parsing (similar approach, not merged)
- Intel Xe driver VRR refactoring ongoing in Linux 7.x

## License

Kernel patches: GPL-2.0-only (matching Linux kernel)
Scripts and configs: MIT
