"""
Device/controller compatibility is negotiated by CAPABILITY, not version.

The two halves of this project ship on independent version schemes (device
`v*`, controller `controller-v*`), so any given moment can pair new firmware
with an old controller or the reverse. Version comparison would mean encoding
release history into the controller and getting it wrong the first time
someone runs a dev build; a capability is the device stating what it
implements.

That only works if both sides spell the capability identically. A typo makes
the feature permanently unavailable and looks exactly like a device that does
not support it — silent, and the sort of thing you debug from the wrong end.
So the strings are asserted to match across the two languages, the same way
CONFIG_SECTIONS is mirrored between Python and dashboard.jsx.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONTROL_GO = ROOT / "device" / "internal" / "client" / "control.go"
CONTROLLER = ROOT / "controller" / "em_controller.py"
API = ROOT / "controller" / "em_api.py"
HACS_CONST = ROOT / "hacs" / "custom_components" / "echo_voice_satellite" / "const.py"


def device_capabilities() -> list[str]:
    """
    Every capability the firmware can announce.

    Read from the whole capabilities() function rather than a single literal:
    the list is no longer fixed — "ambient_light" is appended only when the
    hardware actually has a readable sensor, because the controller advertises
    an HA entity off the back of it. A parser that only understood one literal
    would silently stop covering the conditional ones, which is the direction
    that hides a typo rather than surfacing it.
    """
    src = CONTROL_GO.read_text()
    m = re.search(r'func capabilities\(\) \[\]string \{(.*?)\n\}', src, re.S)
    assert m, "could not find func capabilities() in control.go"
    return re.findall(r'"([a-z0-9_]+)"', m.group(1))


def test_device_announces_expected_capabilities():
    caps = device_capabilities()
    for expected in ("mic", "speaker", "leds", "led_anim", "buttons", "wake_request_v1"):
        assert expected in caps, f"firmware no longer announces {expected!r}"


def test_every_capability_the_controller_checks_is_one_the_device_sends():
    """
    A controller checking for a capability string the device never sends is a
    feature that is silently off forever. This catches the typo direction that
    the device-side test cannot.
    """
    caps = set(device_capabilities())
    # `"<cap>" in (self.capabilities or [])` on Device in em_controller — the
    # only place left, post Phase-4 cutover, that gates behaviour off a raw
    # capability string. (The old second idiom, `_device_has("<cap>")` on the
    # ESPHome satellite, no longer exists — see
    # test_every_hacs_capability_constant_is_one_the_device_sends below for
    # its replacement.)
    checked = set(re.findall(r'"([a-z_]+)"\s+in\s+\(self\.capabilities',
                             CONTROLLER.read_text()))
    assert checked, "no capability checks found — has the idiom changed?"
    unknown = checked - caps
    assert not unknown, (
        f"controller checks capabilities the firmware never announces: {sorted(unknown)}. "
        f"Device sends: {sorted(caps)}"
    )


def test_every_hacs_capability_constant_is_one_the_device_sends():
    """
    The HACS side never re-types a capability string at a call site — every
    entity gates through a named `CAP_*` constant in const.py
    (`self.capability_available(CAP_AMBIENT_LIGHT)`, never the literal
    `"ambient_light"`). So const.py is the single place a typo could be
    introduced, and checking it once covers every entity that imports it —
    the same "spell it identically on both sides" contract the controller-side
    test above enforces, just against the new consumer.

    Not every capability the device announces needs a HACS constant (e.g.
    `mic`/`speaker` are structural, not gated per-entity), so this only checks
    the direction that silently breaks a feature: a HACS constant naming a
    capability the firmware never sends.
    """
    caps = set(device_capabilities())
    declared = set(re.findall(r'^CAP_\w+\s*=\s*"([a-z_]+)"',
                              HACS_CONST.read_text(), re.M))
    assert declared, "no CAP_* constants found in const.py — has the idiom changed?"
    unknown = declared - caps
    assert not unknown, (
        f"hacs const.py declares capabilities the firmware never announces: {sorted(unknown)}. "
        f"Device sends: {sorted(caps)}"
    )


def test_wake_request_capability_is_surfaced_to_the_dashboard():
    """
    Older firmware remains visible for recovery, but must be identified as
    incompatible rather than silently receiving controller-side wake fallback.
    """
    assert "wake_request_capable" in CONTROLLER.read_text(), \
        "em_controller must expose mandatory local wake capability"
    assert "wakeRequestCapable" in API.read_text(), \
        "/api/devices must surface local wake capability"


def test_the_toggle_control_actually_honours_disabled():
    """
    "Disabled WITH the reason, never a control that silently does nothing" is
    the rule every capability gate above is enforced by — and Toggle did not
    accept a `disabled` prop at all; only Slider did. So the rule could not be
    expressed on a switch, and the one call site that needed it ("Tap sends an
    event", gated on button_hold) had to fake it by neutering `value` and
    `onChange` by hand — which leaves the control looking live: full-contrast
    label, pointer cursor, a switch that animates when clicked and stores
    nothing. Any caller passing `disabled` in good faith got worse: a switch
    greyed with its reason that WROTE THE OPPOSITE VALUE on click, so the
    stored setting silently disagreed with what the control showed.

    Asserted against the component rather than the call sites, because the
    call sites already looked correct while the bug was live.
    """
    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    m = re.search(r'function Toggle\(\{(.*?)\}\)', jsx, re.S)
    assert m, "dashboard.jsx must still define a Toggle component"
    assert "disabled" in m.group(1), \
        "Toggle must accept a `disabled` prop — every caller that passes one " \
        "is relying on it to refuse the write, not merely to grey the switch"

    body = jsx[m.end():jsx.index("\n}", m.end())]
    assert re.search(r'if\s*\(!disabled\)|disabled\s*\?\s*undefined|disabled\s*\|\|', body), \
        "Toggle's click handler must check `disabled` before calling onChange — " \
        "styling it grey while still writing the value is the bug this pins"


### Retired: the `_pending_caps` race (Phase 4 cutover) ###
#
# Three tests used to live here pinning `em_esphome.set_device_capabilities`'s
# `_pending_caps` mechanism, which existed to solve two variants of one
# problem: a device registers before its ESPHome server exists (the listener
# only comes up once the device is present), and HA's `ListEntities` is a
# ONE-SHOT — a capability arriving after HA has already enumerated is lost
# for the life of that connection unless the server bounces the HA dial.
#
# Both halves of that problem were structural to the ESPHome-impersonation
# design specifically: one TCP listener per device, created lazily, feeding
# a protocol whose entity list HA caches at connect and never re-reads. The
# HACS/turn-engine architecture has neither property. There is no per-device
# server to race against registration — `Device.capabilities` is set directly
# on the `Device` object at register time, which already exists by then (it's
# what `handle_control` is registering). And there is no ListEntities cache to
# go stale — every REST/`_merge_device` read and every `/api/events` push
# takes `capabilities` live off `Device`, on demand, so a capability that
# changes mid-session is simply reflected on the next read. See
# test_a_capability_change_is_visible_on_the_very_next_snapshot below for the
# replacement property this architecture actually needs to hold.


def test_a_capability_change_is_visible_on_the_very_next_snapshot():
    """
    The property the old `_pending_caps`/bounce machinery existed to buy —
    "a capability that arrives late is not lost for the life of the
    connection" — now has to hold for a different reason: `_merge_device`
    must read `capabilities` straight off the live `Device`, never a value
    captured at some earlier point (a connect-time snapshot, a cached
    dict), or a capability gained after registration (e.g. `ambient_light`
    on a later `als.resolve()` scan, per CLAUDE.md) would be invisible to
    `/api/devices` and `/api/events` until the device reconnects — the exact
    bug this used to be, one layer up.
    """
    src = API.read_text()
    m = re.search(r"def _merge_device\(.*?\n(?=\ndef |\Z)", src, re.S)
    assert m, "could not find _merge_device in em_api.py"
    body = m.group(0)
    assert re.search(r"capabilities.*getattr\(live,\s*[\"']capabilities[\"']", body) or \
           re.search(r"getattr\(live,\s*[\"']capabilities[\"'].*capabilities", body), (
        "_merge_device must read capabilities live off the connected Device "
        "on every call, not from a snapshot taken at some earlier point — "
        "that is what makes a late-arriving capability visible without a "
        "reconnect"
    )


def test_the_api_can_tell_no_sensor_from_no_reading():
    """
    0 lux is a real reading from a covered sensor, so absence cannot be
    expressed as a value: whether the device HAS the sensor has to be a
    separate field from what it read.

    Asserted on the API rather than the dashboard deliberately. A first
    attempt put this on the Status tab, which pushed the panel past its
    height and gave the page a scrollbar — what the device panel should show
    is its own question, tracked separately. The API field stands on its own
    merits regardless of who renders it.
    """
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    assert "ambientLightCapable" in api, \
        "/api/devices must report whether the device found its ALS"
