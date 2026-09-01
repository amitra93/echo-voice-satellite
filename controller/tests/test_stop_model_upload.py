from pathlib import Path


def test_stop_upload_route_and_provisioning_include_stop_model():
    src = (Path(__file__).resolve().parents[1] / "em_api.py").read_text()
    assert '"/api/oww_models/stop/upload"' in src
    manifest = src[src.index("async def _get_provision_oww_manifest"):]
    assert 'fleet.get("stopModel")' in manifest
    assert "_post_stop_model_upload" in src


def test_stop_upload_is_atomic_and_selects_fleet_model():
    src = (Path(__file__).resolve().parents[1] / "em_api.py").read_text()
    body = src[src.index("async def _post_stop_model_upload"):]
    assert 'destination = directory / "stop.onnx"' in body
    assert "os.replace(tmp_path, destination)" in body
    assert 'config["stopModel"]' in body
