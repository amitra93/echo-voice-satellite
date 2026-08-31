"""Remote mute toggle (HA button entity) — reuses the existing
`POST /api/devices/{id}/media` endpoint, the same precedent `volume` already
set on this route, rather than a new one.

Source-shape assertions: em_api.py needs aiohttp/websockets, neither of
which are in this test environment (see CLAUDE.md's "Run controller tests"
section) — reading and parsing the source is how the rest of this suite
pins behaviour in this module.
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _media_command_body() -> str:
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _post_media_command\(.*?\n(?=\nasync def |\n@|\Z)", src, re.S)
    assert fn, "could not find _post_media_command in em_api.py"
    return fn.group(0)


def test_mute_toggle_sends_the_control_message_the_device_expects():
    """
    Must match device/internal/client/control.go's `case "mute_toggle":`
    exactly: a bare {"type": "mute_toggle"} with no other fields — the
    device routes it straight to the same MuteToggle() the hardware button
    calls, so there is nothing else for the controller to compute or send.
    """
    body = _media_command_body()
    assert 'command == "mute_toggle"' in body, (
        "_post_media_command no longer recognises mute_toggle"
    )
    branch = body[body.index('command == "mute_toggle"'):]
    branch = branch[:branch.index("elif")]
    assert '{"type": "mute_toggle"}' in branch, (
        "the mute_toggle branch must send exactly {\"type\": \"mute_toggle\"} "
        "down the control plane — device/internal/client/control.go matches "
        "on this literal shape"
    )


def test_mute_toggle_does_not_guess_the_new_muted_state():
    """
    Unlike volume (which sets live.volume optimistically because the value
    IS the command), mute is device-sovereign: the controller must not set
    live.muted here. The device's own mute_state report — sent by
    MuteToggle() through the same path a physical press uses — is the only
    source of truth, exactly like a hardware button press.
    """
    body = _media_command_body()
    branch = body[body.index('command == "mute_toggle"'):]
    branch = branch[:branch.index("elif")]
    # An assignment specifically, not a bare substring check — this
    # function's own explanatory comment mentions "live.muted" in prose,
    # which a substring check would trip over.
    assert not re.search(r"live\.muted\s*=", branch), (
        "the mute_toggle branch must not assign live.muted — that would "
        "race the device's own mute_state report and could show the wrong "
        "state if the toggle is refused or delayed on the device"
    )


def test_mute_toggle_is_gated_the_same_way_every_other_media_command_is():
    """
    Reuses the endpoint's existing device-offline guard and exception
    handling rather than adding a parallel code path with its own auth/
    error shape.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _post_media_command\(.*?\n(?=\nasync def |\n@|\Z)", src, re.S)
    body = fn.group(0)
    offline_check = body.index("device_offline")
    mute_branch = body.index('command == "mute_toggle"')
    assert offline_check < mute_branch, (
        "mute_toggle must be reached only after the existing "
        "device-not-connected guard, like every other command on this route"
    )
