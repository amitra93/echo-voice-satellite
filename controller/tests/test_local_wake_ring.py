"""
#263: the device lights the listening ring locally at its own wake crossing.

The crossing used to travel to the controller and wait for leds_listening
to come back — measured at +522ms before the ring moved, on a link whose
control-plane tail reaches 2s (#139). The fix has two halves that must both
exist: the controller hands the device its current listening animation in
the config push, and the device draws it before reporting the crossing.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def test_the_controller_pushes_the_listening_anim_at_registration():
    src = (CONTROLLER / "em_controller.py").read_text()
    block = src[src.index("device.led_scene     = em_scenes.resolve(config)"):]
    block = block[:2000]
    assert '"listeningAnim"' in block, \
        "registration must hand the device its listening animation"
    assert "led_anim_capable" in block, \
        "firmware without local animation must keep the old behaviour"


def test_the_controller_updates_it_on_a_live_scene_change():
    """
    Without this, a scene changed on the dashboard lights the ring locally
    in the OLD colours until the device happens to reconnect — the same
    'mirrored at registration but not live' shape test_config_mirrors
    exists for.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    body = src[src.index("live.led_scene = em_scenes.resolve(effective)"):]
    body = body[:1200]
    assert '"listeningAnim"' in body, \
        "a live scene change must refresh the device's cached animation"
