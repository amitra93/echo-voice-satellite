import json

import em_start


def test_apply_options_maps_values_without_overwriting_environment(monkeypatch, capsys):
    monkeypatch.setenv("SERVER_IP", "explicit-ip")
    em_start.apply_options({
        "server_ip": "option-ip",
        "debug": True,
        "require_device_tls": False,
        "oww_threshold": 0.42,
        "server_host": "",
        "unknown": "ignored",
    })
    assert em_start.os.environ["SERVER_IP"] == "explicit-ip"
    assert em_start.os.environ["DEBUG"] == "1"
    assert em_start.os.environ["REQUIRE_DEVICE_TLS"] == "0"
    assert em_start.os.environ["OWW_THRESHOLD"] == "0.42"
    assert "SERVER_HOST" not in em_start.os.environ
    assert "no env var mapped" in capsys.readouterr().out


def test_main_reads_options_and_execs_controller(monkeypatch, tmp_path):
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps({"mdns_name": "Echo", "sendspin_enabled": True}))
    monkeypatch.setattr(em_start, "OPTIONS_PATH", options_path)
    calls = []
    monkeypatch.setattr(em_start.os, "execvp", lambda *args: calls.append(args))
    monkeypatch.delenv("MDNS_NAME", raising=False)
    monkeypatch.delenv("SENDSPIN_ENABLED", raising=False)

    em_start.main()

    assert em_start.os.environ["MDNS_NAME"] == "Echo"
    assert em_start.os.environ["SENDSPIN_ENABLED"] == "1"
    assert calls == [("python3", ["python3", "-u", "em_controller.py"])]
