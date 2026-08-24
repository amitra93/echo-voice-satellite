"""Constants shared across the Echo Voice Satellite integration.

Device identity is the Echo's own `ro.serialno` end to end — the same string
the controller, the device firmware, and the ESPHome-MAC derivation already
use. There is no UUID layer and no converter: docs/design/full-duplex-plan.md's identity
decision.
"""

DOMAIN = "echo_voice_satellite"
MIN_HA_VERSION = "2026.8.0"

CONF_URL = "url"
CONF_API_KEY = "api_key"

PLATFORMS = (
    "assist_satellite",
    "sensor",
    "binary_sensor",
    "select",
    "switch",
    "media_player",
)

# "number" is deliberately absent: the Volume number entity (a continuous
# slider) was replaced by a 9-level select (select.py, EchoVolumeSelect) —
# the slider let HA's frontend send drag deltas that never reliably reached
# the low end, even though the backend/device path itself has no floor. See
# __init__.py's _remove_stale_volume_number_entities.
#
# "button" is deliberately absent: the momentary Privacy mute button
# (button.py, EchoMuteButton) plus binary_sensor.py's read-only mute sensor
# were replaced by one switch.py entity (EchoMuteSwitch) once the wire
# protocol offered mute_toggle — see __init__.py's
# _remove_stale_mute_button_entities and _remove_stale_mute_sensor_entities.

# Capability strings as reported by the device's register message
# (device/internal/client/control.go) and forwarded verbatim by the
# controller — negotiate by capability, never by firmware version.
#
# CAP_BUTTON_HOLD is deliberately absent: the Action Button event entity it
# gated (event.py) was removed — the controller-side tap/hold timing this
# fleet actually sees is unreliable enough (CLAUDE.md: 26.4% of gaps over
# 200ms) that the entity read "unknown" in practice rather than firing
# usable events. Nothing else in this package reads the capability.
CAP_AMBIENT_LIGHT = "ambient_light"
CAP_LED_ANIM = "led_anim"
CAP_AUDIO_MIX = "audio_mix"
CAP_MUSIC_SYNC = "music_sync"

REPAIR_CONTROLLER_UNREACHABLE = "controller_unreachable"
REPAIR_TTS_INCOMPATIBLE = "tts_incompatible"
