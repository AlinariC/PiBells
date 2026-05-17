import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_app(tmp_path, monkeypatch):
    (tmp_path / "audio").mkdir()
    (tmp_path / "static").mkdir()
    monkeypatch.setenv("PIBELLS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("PIBELLS_DISABLE_DAEMON", "1")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    return module


@pytest.fixture()
def module(tmp_path, monkeypatch):
    return load_app(tmp_path, monkeypatch)


@pytest.fixture()
def authed_client(module):
    module.save_auth_record("admin", "password123")
    with TestClient(module.app) as client:
        response = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


def test_first_run_setup_creates_admin(module):
    with TestClient(module.app) as client:
        status = client.get("/api/auth/status")
        assert status.json()["configured"] is False

        response = client.post(
            "/api/setup",
            json={"username": "admin", "password": "password123"},
        )
        assert response.status_code == 200

        login = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "session" in login.headers.get("set-cookie", "")


def test_test_sound_accepts_existing_audio(module, authed_client, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    calls = []

    def fake_trigger(sound_file, loop=False):
        calls.append((sound_file, loop))

    monkeypatch.setattr(module, "trigger_bell", fake_trigger)

    response = authed_client.post("/api/test", json={"sound_file": "bell.mp3", "loop": True})

    assert response.status_code == 200
    assert calls == [("bell.mp3", True)]


def test_audio_path_traversal_is_rejected(module, authed_client):
    response = authed_client.post(
        "/api/schedule",
        json={"day": 0, "time": "08:00", "sound_file": "../bell.mp3"},
    )

    assert response.status_code == 400


def test_devices_are_validated_and_deduplicated(authed_client):
    first = authed_client.post("/api/devices", json={"ip": "192.168.1.10", "name": "Gym"})
    second = authed_client.post("/api/devices", json={"ip": "192.168.1.10", "name": "Gym"})
    invalid = authed_client.post("/api/devices", json={"ip": "not-an-ip"})

    assert first.status_code == 200
    assert second.json() == [{"ip": "192.168.1.10", "name": "Gym", "port": 3030}]
    assert invalid.status_code == 400


def test_legacy_device_list_is_migrated(module):
    module.DEVICES_FILE.write_text('["192.168.1.10", "192.168.1.10:4040"]')

    devices = module.load_devices()

    assert [module.device_to_json(device) for device in devices] == [
        {"ip": "192.168.1.10", "name": "Barix 192.168.1.10", "port": 3030},
        {"ip": "192.168.1.10", "name": "Barix 192.168.1.10", "port": 4040},
    ]


def test_scan_ranges_can_be_saved(authed_client):
    response = authed_client.put(
        "/api/devices/scan_ranges",
        json={"ranges": ["10.80.2.0/24", "10.80.3.12"]},
    )

    assert response.status_code == 200
    assert response.json()["ranges"] == ["10.80.2.0/24", "10.80.3.12/32"]


def test_version_and_update_endpoints_are_removed(authed_client):
    assert authed_client.get("/api/version").status_code == 404
    assert authed_client.post("/api/update").status_code == 404
    assert authed_client.get("/api/update_stream").status_code == 404


def test_trigger_bell_starts_barix_and_local_outputs(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])
    commands = []

    class FakeProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, stdout=None, stderr=None):
        commands.append(cmd)
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    module.trigger_bell("bell.mp3")

    assert any("udp://192.168.1.10:3030?pkt_size=1200" in cmd for cmd in commands)
    assert any(cmd[0] in {"ffplay", "bash"} for cmd in commands)
