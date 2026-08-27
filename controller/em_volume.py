"""Volume scale conversion between the device's native level and HA's float.

Split out as pure logic for the reason em_linkauth.py was: the three call
sites (em_controller, em_esphome, em_api) each had their OWN copy of
`level / 175`, so the scale lived in three places and none of them were
covered by a test. Changing the ceiling meant finding all three.

The device level is the raw tinymix ctl 61 index — the tlv320aic32x4 DAC
digital volume, 0.5dB per step with 0dB (unity) at index 127. The scale is
therefore dB-linear, which is roughly perceptually linear, so the mapping to
HA's 0.0–1.0 is a plain proportion and needs no extra taper.

DEVICE_VOLUME_MAX is 127 and NOT the control's own maximum of 175. Indexes
above 127 apply positive digital gain to near-full-scale PCM and saturate
inside the DAC — measured at 65% THD by index 153 (see
device/internal/server/volume.go for the full measurement and why stock
FireOS never touches this control).
"""

# Codec unity gain. The mixer control accepts up to 175; everything above
# this clips. See the module docstring.
DEVICE_VOLUME_MAX = 127

# The device's physical buttons use ten 3dB steps. Keep this mapping here so
# dashboard state derives from the same scale that the firmware and HACS use.
DEVICE_VOLUME_BUTTON_FLOOR = 73
DEVICE_VOLUME_BUTTON_STEP = 6
DEVICE_VOLUME_BUTTON_LEVELS = 10

# 0.5dB per index step, 0dB at DEVICE_VOLUME_MAX.
DB_PER_STEP = 0.5


def level_to_db(level: int) -> float:
    """Device level as dB relative to unity. Index 127 -> 0.0, index 0 -> -63.5."""
    return (level - DEVICE_VOLUME_MAX) * DB_PER_STEP


def device_level_to_ha(level: int) -> float:
    """Convert a device volume level to an HA float (0.0–1.0)."""
    try:
        return max(0.0, min(1.0, float(level) / DEVICE_VOLUME_MAX))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def ha_volume_to_device(volume: float) -> int:
    """Convert an HA volume float (0.0–1.0) to a device volume level.

    Clamped to the codec's unity gain, so HA asking for full volume can never
    put the DAC into positive digital gain.
    """
    return max(0, min(DEVICE_VOLUME_MAX, round(float(volume) * DEVICE_VOLUME_MAX)))


def device_level_to_button_level(level: int | float | None) -> int | None:
    """Return the nearest physical volume level (1-10), or None when unknown."""
    try:
        raw = float(level)
    except (TypeError, ValueError):
        return None
    return max(1, min(DEVICE_VOLUME_BUTTON_LEVELS,
                      round((raw - DEVICE_VOLUME_BUTTON_FLOOR) / DEVICE_VOLUME_BUTTON_STEP) + 1))
