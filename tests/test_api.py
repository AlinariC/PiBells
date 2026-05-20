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
    monkeypatch.setenv("PIBELLS_DISABLE_THREADHALL_SYNC", "1")
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


def test_threadhall_pairing_saves_device_token(module, authed_client, monkeypatch):
    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        assert payload["pairing_code"] == "ABCD-EFGH"
        return {
            "data": {
                "token": "pb_test_token",
                "poll_seconds": 15,
                "device": {"status": "online"},
            }
        }

    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})

    response = authed_client.post(
        "/api/threadhall/pair",
        json={
            "base_url": "https://threadhall.example.test",
            "pairing_code": "ABCD-EFGH",
            "name": "High School PiBells",
        },
    )

    assert response.status_code == 200
    assert response.json()["paired"] is True
    assert module.load_threadhall_config()["token"] == "pb_test_token"


def test_threadhall_sync_applies_schedules_and_commands(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_threadhall_config({
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pb_test_token",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []

    def fake_trigger(sound_file, loop=False):
        played.append((sound_file, loop))

    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["schedule_options"] == ["Default"]
            return {
                "data": {
                    "poll_seconds": 15,
                    "schedules": [
                        {
                            "name": "Threadhall - High School",
                            "active": True,
                            "entries": [
                                {
                                    "id": "bell-1",
                                    "day": 0,
                                    "time": "08:10",
                                    "sound_key": "passing",
                                    "label": "Period 1",
                                    "enabled": True,
                                }
                            ],
                        }
                    ],
                    "commands": [
                        {
                            "id": 7,
                            "type": "test_bell",
                            "payload": {"sound_key": "passing", "label": "Test Bell"},
                        }
                    ],
                }
            }
        if path == "api/pibells/v1/commands/7/ack":
            acknowledgements.append(payload)
            return {"data": {"status": "acknowledged"}}
        raise AssertionError(path)

    monkeypatch.setattr(module, "trigger_bell", fake_trigger)
    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)

    status = module.sync_threadhall_once()

    assert status["received_commands"] == 1
    assert module.load_all_schedules()["active"] == "Threadhall - High School"
    assert module.load_schedule()[0].label == "Period 1"
    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgements[0]["status"] == "acknowledged"


def test_dashboard_reports_schedule_options(module):
    module.save_all_schedules({
        "active": "Assembly Schedule",
        "schedules": {
            "Default": [],
            "Assembly Schedule": [],
        },
    })

    payload = module.dashboard()

    assert payload["active_schedule"] == "Assembly Schedule"
    assert payload["schedule_options"] == ["Assembly Schedule", "Default"]


def test_threadhall_sync_preserves_active_local_alternate_schedule(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_all_schedules({
        "active": "Assembly Schedule",
        "schedules": {
            "Assembly Schedule": [
                {
                    "id": "assembly-1",
                    "day": 0,
                    "time": "10:00",
                    "sound_file": "bell-passing-classic.mp3",
                    "label": "Assembly",
                    "enabled": True,
                }
            ],
        },
    })
    module.save_threadhall_config({
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pb_test_token",
        "device_uuid": "local-box",
    })

    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["active_schedule"] == "Assembly Schedule"
            assert payload["schedule_options"] == ["Assembly Schedule"]
            return {
                "data": {
                    "poll_seconds": 15,
                    "schedules": [
                        {
                            "name": "Threadhall - High School",
                            "active": True,
                            "entries": [
                                {
                                    "id": "bell-1",
                                    "day": 0,
                                    "time": "08:10",
                                    "sound_key": "passing",
                                    "label": "Period 1",
                                    "enabled": True,
                                }
                            ],
                        }
                    ],
                    "commands": [],
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)

    module.sync_threadhall_once()

    schedules = module.load_all_schedules()
    assert schedules["active"] == "Assembly Schedule"
    assert "Threadhall - High School" in schedules["schedules"]


def test_default_audio_names_are_applied(module):
    (module.AUDIO_DIR / "emergency-lockdown.mp3").write_bytes(b"fake mp3")
    (module.AUDIO_DIR / "bell-lunch-light-chime.mp3").write_bytes(b"fake mp3")

    audio = {item.file: item.name for item in module.list_audio()}
    default_keys = {item.file: item.default_key for item in module.list_audio()}

    assert audio["emergency-lockdown.mp3"] == "Emergency Lockdown"
    assert audio["bell-lunch-light-chime.mp3"] == "Lunch Light Chime"
    assert default_keys["emergency-lockdown.mp3"] == "lockdown"
    assert default_keys["bell-lunch-light-chime.mp3"] == "lunch"
    assert module.audio_file_for_key("lockdown") == "emergency-lockdown.mp3"


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
