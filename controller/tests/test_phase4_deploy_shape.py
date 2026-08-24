"""
Phase 4 cutover (docs/design/full-duplex-plan.md) — deployment-shape and dead-code-removal
guards that the other test_phase4_*.py files don't already cover: the parts
of the cutover that live in `main()`'s startup/shutdown, the connect/disconnect
handlers, `em_db.py`'s port allocators, the Dockerfile, and the dashboard.

Source-shape assertions throughout, matching the rest of this suite
(test_deploy.py, test_phase4_ble_proxy_cutover.py): em_controller.py and
em_api.py need aiohttp/websockets/zeroconf, none of which are in this test
environment (see CLAUDE.md's "Run controller tests" section), so behaviour in
those two modules is pinned by reading and parsing the source rather than
importing and calling it. em_db.py needs only sqlite3 (stdlib), so those
checks import it directly instead.
"""

import re
from pathlib import Path

import em_db

CONTROLLER = Path(__file__).resolve().parents[1]


def test_local_deployment_uses_the_canonical_controller_api_port():
    """The local deployment must match the documented/add-on API port."""
    compose = (CONTROLLER / "docker-compose.new-impl.yaml").read_text()
    config = (CONTROLLER / "config.yaml").read_text()

    assert "API_PORT=8768" in compose
    assert "API_PORT=8771" not in compose
    assert "ingress_port: 8768" in config


def test_test_audio_is_transferred_before_the_device_query_is_started():
    """The E2E test must use a temporary device WAV, not local injection."""
    api = (CONTROLLER / "em_api.py").read_text()
    start = api.index("async def _post_test_turn")
    body = api[start:api.index("async def _delete_test_audio", start)]

    transfer = body.index("_stream_file_to_device(")
    trigger = body.index("start_device_test_turn(live)")
    assert transfer < trigger
    assert '"test_audio" not in' in body
    assert "TEST_AUDIO_DEVICE_PATH" in body
    assert 'mode="600"' in body
    assert "require_verify=True" in body
    assert '"type": "test_audio_cleanup"' in (CONTROLLER / "em_turn_engine.py").read_text()
    control = (CONTROLLER.parent / "device" / "internal" / "client" / "control.go").read_text()
    assert 'case "test_audio":' in control
    assert 'case "test_audio_cleanup":' in control


def test_controller_and_device_agree_on_the_test_audio_path():
    api = (CONTROLLER / "em_api.py").read_text()
    device = (CONTROLLER.parent / "device" / "internal" / "client" / "test_audio.go").read_text()
    path = "/data/local/tmp/echomuse-test-query.wav"
    assert f'TEST_AUDIO_DEVICE_PATH = "{path}"' in api
    assert f'TestAudioPath     = "{path}"' in device


def test_test_audio_ui_is_capability_gated_and_targets_the_selected_device():
    dashboard = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    logic = (CONTROLLER / "static" / "dashboard_logic.js").read_text()
    dashboard = dashboard + "\n" + logic
    assert "includes('test_audio')" in dashboard
    assert "`/api/devices/${device.device_id}/test_audio`" in dashboard
    assert "`/api/devices/${device.device_id}/test_turn`" in dashboard


def test_test_audio_tab_has_its_font_scope_and_render_fallback():
    """A Test-tab-only ReferenceError once blanked the entire device modal.

    `mono` was declared in other components but not Detail, so opening Test
    evaluated an unbound identifier before the upload input could render. Keep
    the tab in its own component with its own scope, behind a boundary that
    leaves the modal usable if another render error is introduced later.
    """
    dashboard = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    start = dashboard.index("function TestAudioTab(")
    body = dashboard[start:dashboard.index("function Detail(", start)]

    assert "const mono = \"'DM Mono',monospace\";" in body
    assert "fontFamily:mono" in body
    assert "class DetailTabErrorBoundary extends React.Component" in dashboard
    assert "This tab failed to render. Choose another tab or retry." in dashboard

    test_tab = dashboard[dashboard.index("{tab === 'test' && ("):dashboard.index("{/* CONFIG */")]
    assert '<DetailTabErrorBoundary key="test" label="Audio query test">' in test_tab
    assert "<TestAudioTab" in test_tab


def test_query_history_is_a_fleet_section_below_device_provisioning():
    """Activity must be visible without opening a per-device modal.

    Turn records, recordings, and near-miss counters remain per-device, so
    App owns the selected device and polling while Detail stays focused on
    device management.
    """
    dashboard = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    detail = dashboard[
        dashboard.index("function Detail("):dashboard.index("function ProvisionWizard(")
    ]

    assert "tab === 'activity'" not in detail
    assert "setTurns" not in detail
    assert "const [activityTurns, setActivityTurns]" in dashboard
    assert "Query history" in dashboard
    assert dashboard.index("{/* Device grid */}") < dashboard.index("{/* QUERY HISTORY")
    assert "<TurnObservability" in dashboard[dashboard.index("{/* QUERY HISTORY"):]
    assert "Promise.all(approved.map" in dashboard
    assert "device_id: device.device_id" in dashboard


def test_query_history_rows_are_filterable_and_paginated():
    """The central feed must identify each device and be usable past one page."""
    dashboard = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    dashboard += "\n" + (CONTROLLER / "static" / "dashboard_logic.js").read_text()

    assert "const HISTORY_PAGE_SIZE = 20;" in dashboard
    assert 'aria-label="Filter by device"' in dashboard
    assert 'aria-label="Filter by result"' in dashboard
    assert 'aria-label="Filter by date"' in dashboard
    assert "toLocaleDateString" in dashboard
    assert "device?.label || t.device_id" in dashboard
    assert "Previous</Pill>" in dashboard
    assert "Next</Pill>" in dashboard
    assert "HA response" in dashboard
    assert "stt_latency_ms" in dashboard
    assert "tts_latency_ms" in dashboard
    assert "Play ${kind.toUpperCase()}" in dashboard
    assert "Download {kind.toUpperCase()}" in dashboard
    assert "isExpanded ? '▾ Hide' : '▸ Details'" in dashboard
    assert "TTS + playback" in dashboard


# ─── em_db.py: the port allocators are gone, the append-only migration isn't ──

def test_esphome_and_ble_proxy_port_allocators_are_gone():
    """
    get_esphome_port/assign_esphome_port/free_esphome_port and the BLE-proxy
    equivalents were deleted along with their only callers (em_esphome.py,
    em_ble_proxy.py). Asserted with hasattr against the imported module,
    not a source regex, so a reintroduction under a different definition
    style (e.g. as a class method) is still caught.
    """
    retired = (
        "get_esphome_port", "assign_esphome_port", "free_esphome_port",
        "get_ble_proxy_port", "ensure_ble_proxy_port", "free_ble_proxy_port",
    )
    present = [name for name in retired if hasattr(em_db, name)]
    assert not present, (
        f"em_db.py still defines retired port-allocator function(s): {present} "
        f"— their only callers (em_esphome.py/em_ble_proxy.py) are deleted"
    )


def test_the_append_only_migration_still_seeds_next_esphome_port():
    """
    MIGRATIONS is append-only (CLAUDE.md's "Schema migrations" section): the
    stored schema_version is an INDEX into it, so a deployed entry can never
    be edited or removed, only stopped being read. The migration that seeded
    'next_esphome_port' and added the esphome_api_port/ble_proxy_port columns
    must still be exactly where it was, unread by anything now, but present —
    editing it would corrupt every database that already ran it.
    """
    src = (CONTROLLER / "em_db.py").read_text()
    assert "esphome_api_port INTEGER" in src, (
        "the esphome_api_port column migration must not be removed — "
        "MIGRATIONS is append-only"
    )
    assert "ble_proxy_port INTEGER" in src, (
        "the ble_proxy_port column migration must not be removed — "
        "MIGRATIONS is append-only"
    )
    assert "next_esphome_port" in src, (
        "the next_esphome_port system_config seed must not be removed — "
        "MIGRATIONS is append-only"
    )


# ─── em_controller.py: startup/shutdown, connect, disconnect ──────────────────

def test_main_no_longer_starts_or_stops_esphome_or_ble_proxy_servers():
    """
    main() used to bring up/tear down a per-device ESPHome listener pool and
    a separate BLE-proxy server pool. Both are gone: there is no per-device
server under the turn-engine/HACS architecture at all (docs/design/full-duplex-plan.md
    Phase 4), so nothing needs starting at boot or cancelling at shutdown.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    fn = re.search(r"async def main\(\):.*", src, re.S)
    assert fn, "could not find main() in em_controller.py"
    body = fn.group(0)
    for stale in (
        "start_esphome_servers", "stop_esphome_servers",
        "start_ble_proxy_servers", "stop_ble_proxy_servers",
    ):
        assert stale not in body, (
            f"main() still references {stale!r} — this mechanism was deleted "
            f"in the Phase 4 cutover"
        )


def test_disconnect_handler_no_longer_tears_down_a_per_device_esphome_server():
    """
    The live-connection disconnect path used to call esphome.device_disconnected
    and em_ble_proxy.device_disconnected to tear down that device's ESPHome
    TCP listener(s). There is no such listener anymore — HA reaches the
    controller through the shared em_api.py app, not a per-device socket —
    so the disconnect handler has nothing protocol-specific left to tear down
    beyond the existing device-registry/notify cleanup.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # The disconnect branch: the "else" arm of the stale-connection check in
    # the /control handler's `finally` block (see the "Device disconnected"
    # log line as an anchor).
    m = re.search(
        r'log\.info\(f"\[control\] Device disconnected.*?\n(?=\n|async def )',
        src, re.S,
    )
    assert m, "could not find the device-disconnected branch in em_controller.py"
    body = m.group(0)
    assert "esphome" not in body.lower(), (
        "the disconnect handler still mentions esphome — its per-device "
        "server teardown call must be gone"
    )
    assert "em_ble_proxy" not in body, (
        "the disconnect handler still calls into em_ble_proxy — deleted module"
    )


def test_connected_device_stores_standalone_play_instead_of_registering_with_esphome():
    """
    A freshly connected device used to be registered with
    esphome.device_connected(...), which built (or reused) that device's
    ESPHome server. Now the same "play PCM on this device's speaker outside
    a turn" capability is just stashed as a plain attribute on the Device
    object — nothing to build, because there is no per-device server.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "device.standalone_play = _standalone_play" in src, (
        "the connect handler must stash _standalone_play directly on the "
        "Device object"
    )
    assert "esphome.device_connected" not in src, (
        "no call site may still register a device with the deleted ESPHome "
        "server-pool mechanism"
    )


# ─── em_api.py: config-push and delete no longer reconcile a BLE proxy pool ───

def test_delete_device_no_longer_reconciles_a_ble_proxy_pool():
    """
    Deleting a device used to also call em_ble_proxy.reconcile(...) to tear
    down that device's BT-proxy ESPHome listener. There is no such listener
    to tear down anymore — bleProxyEnabled is now a plain config flag with
    nothing else to reconcile on delete.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _delete_device\(.*?\n(?=\n@|\nasync def )", src, re.S)
    assert fn, "could not find _delete_device in em_api.py"
    assert "em_ble_proxy" not in fn.group(0), (
        "_delete_device still references em_ble_proxy — deleted module"
    )


def test_global_config_push_no_longer_loops_offline_devices_for_ble_reconcile():
    """
    A fleet-wide config push used to walk every device in the DB (connected
    or not) specifically to reconcile each one's BLE-proxy listener, because
    an offline device still needed its ESPHome BT-proxy server's desired
    state updated. bleProxyEnabled now only matters to a connected device (it
    gates a live control-message handler), so an offline device simply picks
    up the new value on its next connect — the separate offline loop is gone.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _post_global_config\(.*?\n(?=\n@|\nasync def )", src, re.S)
    assert fn, "could not find _post_global_config in em_api.py"
    body = fn.group(0)
    assert "em_ble_proxy" not in body, (
        "_post_global_config still references em_ble_proxy — deleted module"
    )
    assert "db.get_all_devices()" not in body, (
        "_post_global_config should no longer need every device from the DB "
        "(connected or not) now that BLE-proxy reconcile is gone — only the "
        "connected-devices loop (_devices.items()) should remain"
    )


def test_merge_device_no_longer_reports_esphome_or_ble_proxy_ports():
    """
    /api/devices used to surface esphome_port and ble_proxy_port — allocated
    TCP ports for the now-deleted per-device ESPHome listeners. Nothing
    listens on a per-device port anymore, so there is nothing meaningful to
    report; the fields must be gone rather than reporting a stale or NULL
    port number a client might mistake for something live.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"def _merge_device\(.*?\n(?=\ndef |\Z)", src, re.S)
    assert fn, "could not find _merge_device in em_api.py"
    body = fn.group(0)
    assert '"esphome_port"' not in body, (
        "_merge_device must not report esphome_port — no per-device ESPHome "
        "listener exists anymore"
    )
    assert '"ble_proxy_port"' not in body, (
        "_merge_device must not report ble_proxy_port — no per-device "
        "ESPHome BT-proxy listener exists anymore"
    )


# ─── Dockerfile: no COPY line for a deleted module ─────────────────────────────

def test_dockerfile_does_not_copy_deleted_modules():
    """
    Complements test_deploy.py's test_dockerfile_copies_every_controller_module
    (which only catches a *missing* COPY for a module that still exists on
    disk). A COPY line naming a deleted file fails the Docker build loudly,
    but is otherwise easy to leave behind by accident during a rename-heavy
    cutover like this one — pinned directly so it's caught by the fast
    Python suite rather than only by a full image build.
    """
    dockerfile = (CONTROLLER / "Dockerfile").read_text()
    for stale in ("em_esphome.py", "em_ble_proxy.py"):
        assert f"COPY {stale}" not in dockerfile, (
            f"Dockerfile still has a COPY line for deleted module {stale}"
        )
    assert "esphome/" not in dockerfile, (
        "Dockerfile must not reference the deleted esphome/ protocol package"
    )
    for new_module in (
        "em_turn_engine.py", "em_audio_frame.py",
        "em_ha_sidechannels.py", "em_test_audio.py",
    ):
        assert f"COPY {new_module}" in dockerfile, (
            f"Dockerfile is missing the COPY line for {new_module}"
        )


# ─── dashboard.jsx: no leftover ESPHome-port UI or stale copy ─────────────────

def test_dashboard_does_not_render_an_esphome_port_row():
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "ESPHome port" not in jsx, (
        "the dashboard must not render a device's (now nonexistent) "
        "ESPHome port"
    )


def test_dashboard_gates_ble_proxy_panel_on_the_enabled_flag_not_truthiness():
    """
    device.bleProxy is now always an object ({enabled: bool}) when present
    at all (see _merge_device), never the old rich em_ble_proxy.get_status()
    shape. Gating on `device.bleProxy` alone would show the panel for every
    device the instant `bleProxy` stopped being undefined, regardless of
    whether the toggle is actually on.
    """
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "device.bleProxy?.enabled &&" in jsx, (
        "the Bluetooth proxy panel must gate on device.bleProxy?.enabled, "
        "not on device.bleProxy by itself"
    )
    assert "haState" not in jsx, (
        "the dashboard must not reference the retired ESPHome-side "
        "haState/haConnected BLE-proxy fields"
    )
    assert "Forwarded to HA" not in jsx, (
        "the dashboard must not render the retired ESPHome-side advert "
        "counter column — the controller has no reverse channel to it"
    )


def test_dashboard_bluetooth_copy_does_not_claim_a_separate_esphome_device():
    """
    The Bluetooth section's description used to say BLE adverts are
    forwarded to HA 'as a separate ESPHome device' — true under ESPHome
    impersonation, false now: they ride the HACS integration's own remote
    scanner (Phase 2b), with no separate device registered in HA at all.
    """
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "separate ESPHome device" not in jsx, (
        "the Bluetooth proxy description still describes the retired "
        "ESPHome-impersonation forwarding mechanism"
    )
