"""BLE proxy retirement (Phase 4 cutover, docs/design/full-duplex-plan.md).

em_ble_proxy.py's ESPHome-hosted BT proxy (its own TCP listener, mDNS entry,
protobuf-encoded advertisements) is gone. What forwarding still needs — "only
forward while bleProxyEnabled is on" — is now a plain config flag cached on
the live Device object, the same idiom as ns_asr/save_utterances, checked per
ble_adverts batch in em_controller.handle_control.

Source-shape assertions throughout: em_api.py/em_controller.py need aiohttp,
websockets and zeroconf, none of which are in this test environment (see
CLAUDE.md's "Run controller tests" section) — reading the source and parsing
is how the rest of this suite pins behaviour in those two modules.
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def test_apply_live_config_caches_ble_proxy_enabled_like_ns_asr():
    """
    The one place both the per-device and fleet config-push endpoints go
    through (see _apply_live_config's own docstring) must mirror
    bleProxyEnabled onto the live Device the same way it already does for
    nsAsr/saveUtterances/bargeInEnabled — or a config toggle only takes
    effect on the device's next reconnect instead of immediately.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _apply_live_config\(.*?\n(?=\nasync def |\ndef )", src, re.S)
    assert fn, "_apply_live_config not found"
    body = fn.group(0)
    assert 'if "bleProxyEnabled" in effective:' in body, (
        "_apply_live_config no longer checks for bleProxyEnabled"
    )
    assert "live.ble_proxy_enabled = bool(effective[\"bleProxyEnabled\"])" in body, (
        "_apply_live_config must mirror bleProxyEnabled onto live.ble_proxy_enabled"
    )
    gate = body.index('if "bleProxyEnabled" in effective:')
    assign = body.index('live.ble_proxy_enabled = bool(effective["bleProxyEnabled"])')
    assert gate < assign


def test_handle_control_seeds_ble_proxy_enabled_on_connect():
    """
    The same caching must happen at connect time (em_controller.py), not
    only on a later config push — a device that connects and immediately
    sends ble_adverts before any config push must still be gated correctly.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert 'device.ble_proxy_enabled = bool(config.get("bleProxyEnabled"' in src, (
        "device.ble_proxy_enabled is not seeded from the connect-time config "
        "push — a freshly connected device would forward adverts regardless "
        "of the bleProxyEnabled toggle until its first config push"
    )


def test_ble_adverts_handler_gates_on_the_cached_flag_before_forwarding():
    """
    Pins the replacement for em_ble_proxy.forward_adverts's own gate (it
    silently no-opped when _proxies.get(device_id) was None, i.e. when the
    device's BT proxy was not enabled). Asserted on the handler body, not
    just that the flag exists somewhere in the file, so a future refactor
    that reads the flag but forgets to gate on it is still caught.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    handler = re.search(
        r'elif msg_type == "ble_adverts":(.*?)\n\s*elif msg_type ==', src, re.S
    )
    assert handler, "the ble_adverts control-message handler was not found"
    body = handler.group(1)
    assert "device.ble_proxy_enabled" in body, (
        "ble_adverts forwarding must be gated on device.ble_proxy_enabled"
    )
    assert "ha_sidechannels.ble_adverts" in body, (
        "adverts are no longer forwarded to the HACS integration at all"
    )
    gate = body.index("device.ble_proxy_enabled")
    forward = body.index("ha_sidechannels.ble_adverts")
    assert gate < forward, (
        "the flag must be checked BEFORE forwarding, not after — otherwise "
        "every device forwards regardless of the toggle"
    )


def test_merge_device_reports_ble_proxy_as_a_plain_toggle():
    """
    _merge_device's bleProxy field used to be em_ble_proxy.get_status()'s
    rich (port/listening/haConnected/haSubscribed/advertsForwarded) shape.
    There is nothing left to report but the toggle — the HACS integration's
    own BLE scanner state lives inside Home Assistant, with no reverse
    channel back to the controller.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    assert '"bleProxy":' in src
    assert '"enabled": getattr(live, "ble_proxy_enabled", False)' in src


def test_no_controller_module_still_imports_em_ble_proxy_or_em_esphome():
    """
    Broad sweep: both modules are deleted, so nothing in the controller
    package may still `import` them — a stale import is a
    ModuleNotFoundError at process start, not a runtime surprise on some
    rarely-hit code path.

    Parses with ast rather than matching text: several docstrings in this
    cutover explain the NEW code by naming what the OLD code used to do
    (e.g. em_announce.py's "the controller test suite does not\\nimport
    em_esphome (zeroconf, aiohttp, the database)"), and a line-wrapped
    docstring sentence can start with the exact words "import em_esphome"
    by pure accident of where the wrap fell. ast.parse only sees real
    syntax, so prose can never masquerade as an import statement.
    """
    import ast

    offenders = []
    for path in CONTROLLER.glob("em_*.py"):
        if path.name in ("em_esphome.py", "em_ble_proxy.py"):
            continue  # deleted; a glob match here would be its own bug
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module} if node.module else set()
            else:
                continue
            hit = names & {"em_esphome", "em_ble_proxy"}
            if hit:
                offenders.append(f"{path.name}:{node.lineno}: imports {sorted(hit)}")
    assert not offenders, f"stale imports of deleted modules: {offenders}"
