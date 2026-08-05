import importlib
import sys
from datetime import datetime
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
    monkeypatch.setenv("PIBELLS_DISABLE_PIXELSIS_SYNC", "1")
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


def test_barix_binary_discovery_packet_is_parsed(module):
    payload = (
        module.BARIX_DISCOVERY_HEADER
        + bytes((module.BARIX_DISCOVERY_GET_RESPONSE,))
        + bytes.fromhex("0008e100b177")
        + bytes((10, 80, 2, 44))
        + bytes((10,))
        + b"Exstreamer"
        + bytes((2,))
        + b"P5"
        + bytes((4,))
        + b"1.23"
        + bytes((11,))
        + b"rccs-zone-a"
    )

    meta = module.parse_barix_discovery_payload(payload)
    candidate = module.candidate_payload("10.80.3.250", "UDP discovery", meta)

    assert module.BARIX_DISCOVERY_GET_REQUEST == bytes.fromhex("8188538101")
    assert meta == {
        "mac": "00:08:E1:00:B1:77",
        "ip": "10.80.2.44",
        "name": "rccs-zone-a",
        "model": "Exstreamer",
    }
    assert candidate["ip"] == "10.80.2.44"
    assert candidate["key"] == "10.80.2.44:3030"


def test_barix_binary_discovery_ignores_request_echo(module):
    assert module.parse_barix_discovery_payload(module.BARIX_DISCOVERY_GET_REQUEST) == {}


def test_pixelsis_pairing_saves_device_token(module, authed_client, monkeypatch):
    def fake_pixelsis_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        assert payload["pairing_code"] == "ABCD-EFGH"
        assert payload["protocol_version"] == 1
        assert payload["capabilities"]["calendar_exceptions"] is True
        assert payload["capabilities"]["targeted_outputs"] is True
        assert payload["local_url"] == "http://10.80.2.42"
        return {
            "token": "pbv1.device-1.test-token-value",
            "protocol_version": 1,
            "poll_seconds": 15,
            "device": {"status": "online"},
        }

    monkeypatch.setattr(module, "pixelsis_request", fake_pixelsis_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "10.80.2.42")

    response = authed_client.post(
        "/api/pixelsis/pair",
        json={
            "base_url": "https://pixelsis.example.test",
            "pairing_code": "ABCD-EFGH",
            "name": "High School PiBells",
        },
    )

    assert response.status_code == 200
    assert response.json()["paired"] is True
    assert module.load_pixelsis_config()["token"] == "pbv1.device-1.test-token-value"
    assert (module.BASE_DIR / "pixelsis-pairing.json").stat().st_mode & 0o777 == 0o600


def test_successful_pairing_clears_history_so_reused_command_id_executes(
    module,
    authed_client,
    monkeypatch,
):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_pixelsis_config({
        "schedule_revision": "prior-server-revision",
        "managed_schedule_hash": "managed-sha256:prior-state",
        "calendar": [
            {
                "date": "2026-08-04",
                "instructional": False,
                "label": "Prior server closure",
            }
        ],
        "last_sync_at": "2026-08-04T08:00:00",
    })
    module.save_pixelsis_command_history({
        "7": {
            "status": "acknowledged",
            "summary": "Command from prior pairing",
            "occurred_at": "2026-01-01T00:00:00+00:00",
        }
    })
    played = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/pair":
            return {"token": "new-device-token", "poll_seconds": 15}
        if path == "api/pibells/v1/sync":
            return {
                "schedules": [],
                "calendar": [],
                "commands": [
                    {
                        "id": 7,
                        "type": "PLAY_SOUND",
                        "payload": {"sound_key": "passing"},
                    }
                ],
            }
        if path == "api/pibells/v1/commands/7/ack":
            acknowledgements.append(payload)
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "pixelsis_request", fake_request)
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False: played.append((sound_file, loop)),
    )

    paired = authed_client.post(
        "/api/pixelsis/pair",
        json={
            "base_url": "https://pixelsis.example.test",
            "pairing_code": "NEW-PAIR",
            "name": "Re-paired controller",
        },
    )

    assert paired.status_code == 200
    assert module.load_pixelsis_command_history() == {}
    paired_config = module.load_pixelsis_config()
    assert paired_config["schedule_revision"] == ""
    assert paired_config["managed_schedule_hash"] == ""
    assert paired_config["calendar"] == []
    assert paired_config["last_sync_at"] is None

    module.sync_pixelsis_once()

    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgements[0]["status"] == "acknowledged"
    assert module.load_pixelsis_command_history()["7"]["status"] == "acknowledged"

    module.unpair_pixelsis()

    assert module.load_pixelsis_command_history() == {}


def test_pixelsis_sync_applies_revisioned_schedules_and_commands(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []

    def fake_trigger(sound_file, loop=False):
        played.append((sound_file, loop))

    def fake_pixelsis_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["schedule_options"] == ["Default"]
            assert payload["device_uuid"] == "local-box"
            assert payload["protocol_version"] == 1
            assert payload["telemetry"]["active_schedule"] == "Default"
            assert payload["telemetry"]["outputs"][0]["key"] == "local"
            assert payload["capabilities"]["targeted_outputs"] is True
            assert payload["local_url"] == "http://10.80.2.42"
            return {
                "protocol_version": 1,
                "poll_seconds": 15,
                "schedule_revision": "revision-1",
                "calendar": [],
                "schedules": [
                    {
                        "name": "Regular",
                        "active": True,
                        "entries": [
                            {
                                "id": "bell-1",
                                "day": 0,
                                "time": "08:10",
                                "sound_key": "passing",
                                "label": "Period 1",
                                "enabled": True,
                                "output_keys": ["local"],
                            }
                        ],
                    }
                ],
                "commands": [
                    {
                        "id": 7,
                        "type": "TEST_OUTPUT",
                        "payload": {"sound_key": "passing", "label": "Test Bell"},
                    }
                ],
            }
        if path == "api/pibells/v1/commands/7/ack":
            acknowledgements.append(payload)
            return {"data": {"status": "acknowledged"}}
        raise AssertionError(path)

    monkeypatch.setattr(module, "trigger_bell", fake_trigger)
    monkeypatch.setattr(module, "pixelsis_request", fake_pixelsis_request)
    monkeypatch.setattr(module, "get_local_ip", lambda: "10.80.2.42")

    status = module.sync_pixelsis_once()

    assert status["received_commands"] == 1
    assert module.load_all_schedules()["active"] == "PixelSIS - Regular"
    assert module.load_schedule()[0].label == "Period 1"
    assert module.load_schedule()[0].output_keys == ["local"]
    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgements[0]["status"] == "acknowledged"
    assert module.load_pixelsis_config()["schedule_revision"] == "revision-1"


def test_legacy_sync_hash_applies_only_when_unrevisioned_schedule_changes(module, monkeypatch):
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "legacy-device-token",
        "device_uuid": "legacy-controller",
    })
    first_schedule = {
        "name": "Regular",
        "active": True,
        "entries": [
            {
                "id": "bell-1",
                "day": 0,
                "time": "08:10",
                "sound_file": "bell.mp3",
                "label": "First bell",
                "enabled": True,
            }
        ],
    }
    changed_schedule = {
        **first_schedule,
        "entries": [
            {
                **first_schedule["entries"][0],
                "time": "08:15",
            }
        ],
    }
    responses = [
        {**first_schedule, "generated_at": "2026-08-04T10:00:00Z"},
        {**first_schedule, "generated_at": "2026-08-04T10:00:20Z"},
        {**changed_schedule, "generated_at": "2026-08-04T10:00:40Z"},
    ]
    applied = []
    real_apply = module.apply_pixelsis_schedules

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        return {
            "schedules": [responses.pop(0)],
            "calendar": [],
            "commands": [],
        }

    def recording_apply(schedules):
        applied.append(schedules)
        return real_apply(schedules)

    monkeypatch.setattr(module, "pixelsis_request", fake_request)
    monkeypatch.setattr(module, "apply_pixelsis_schedules", recording_apply)

    first = module.sync_pixelsis_once()
    first_revision = module.load_pixelsis_config()["schedule_revision"]
    second = module.sync_pixelsis_once()
    second_revision = module.load_pixelsis_config()["schedule_revision"]
    third = module.sync_pixelsis_once()
    changed_revision = module.load_pixelsis_config()["schedule_revision"]

    assert first["received_schedules"] == 1
    assert first_revision.startswith("legacy-sha256:")
    assert second["received_schedules"] == 0
    assert second_revision == first_revision
    assert third["received_schedules"] == 1
    assert changed_revision.startswith("legacy-sha256:")
    assert changed_revision != first_revision
    assert len(applied) == 2
    assert module.load_schedule()[0].time.strftime("%H:%M") == "08:15"


def test_server_schedule_revision_takes_precedence_over_legacy_hash(module, monkeypatch):
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "device-token",
    })
    monkeypatch.setattr(
        module,
        "pixelsis_request",
        lambda *args, **kwargs: {
            "schedule_revision": "server-revision-7",
            "schedules": [
                {
                    "name": "Regular",
                    "entries": [
                        {
                            "id": "bell-1",
                            "day": 0,
                            "time": "08:10",
                            "sound_file": "bell.mp3",
                        }
                    ],
                }
            ],
            "calendar": [],
            "commands": [],
        },
    )

    module.sync_pixelsis_once()

    assert module.load_pixelsis_config()["schedule_revision"] == "server-revision-7"


def test_sync_repairs_managed_schedule_when_cached_state_loses_targets(module, monkeypatch):
    module.save_all_schedules({
        "active": "Safe Setup",
        "schedules": {"Safe Setup": []},
    })
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "device-token",
        "schedule_revision": "",
    })
    remote_schedule = {
        "name": "Regular",
        "active": True,
        "entries": [
            {
                "id": "targeted-bell",
                "day": 0,
                "time": "08:10",
                "sound_file": "bell.mp3",
                "label": "Gym only",
                "enabled": True,
                "output_keys": ["192.168.1.10:3030"],
            }
        ],
    }
    apply_calls = []
    real_apply = module.apply_pixelsis_schedules

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        return {
            "schedule_revision": "revision-targeted-1",
            "schedules": [remote_schedule],
            "calendar": [],
            "commands": [],
        }

    def recording_apply(schedules):
        apply_calls.append(schedules)
        return real_apply(schedules)

    monkeypatch.setattr(module, "pixelsis_request", fake_request)
    monkeypatch.setattr(module, "apply_pixelsis_schedules", recording_apply)

    first = module.sync_pixelsis_once()
    first_config = module.load_pixelsis_config()
    unchanged = module.sync_pixelsis_once()

    assert first["received_schedules"] == 1
    assert unchanged["received_schedules"] == 0
    assert len(apply_calls) == 1
    assert first_config["managed_schedule_hash"].startswith("managed-sha256:")
    assert module.load_all_schedules()["active"] == "Safe Setup"

    on_disk = module.read_json(module.SCHEDULE_FILE, {})
    managed_entry = on_disk["schedules"]["PixelSIS - Regular"][0]
    managed_entry.pop("output_keys")
    module.write_json(module.SCHEDULE_FILE, on_disk)

    repaired = module.sync_pixelsis_once()
    repaired_schedules = module.load_all_schedules()
    repaired_entry = repaired_schedules["schedules"]["PixelSIS - Regular"][0]
    repaired_config = module.load_pixelsis_config()

    assert repaired["received_schedules"] == 1
    assert len(apply_calls) == 2
    assert repaired_entry["output_keys"] == ["192.168.1.10:3030"]
    assert repaired_schedules["active"] == "Safe Setup"
    assert repaired_config["schedule_revision"] == "revision-targeted-1"
    assert repaired_config["managed_schedule_hash"] == module.managed_schedule_state_hash()


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


def test_pixelsis_sync_preserves_active_local_alternate_schedule(module, monkeypatch):
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
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })

    def fake_pixelsis_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["active_schedule"] == "Assembly Schedule"
            assert payload["schedule_options"] == ["Assembly Schedule"]
            return {
                "poll_seconds": 15,
                "schedule_revision": "revision-2",
                "calendar": [],
                "schedules": [
                    {
                        "name": "Regular",
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
        raise AssertionError(path)

    monkeypatch.setattr(module, "pixelsis_request", fake_pixelsis_request)

    module.sync_pixelsis_once()

    schedules = module.load_all_schedules()
    assert schedules["active"] == "Assembly Schedule"
    assert "PixelSIS - Regular" in schedules["schedules"]


def test_legacy_threadhall_config_migrates_to_pixelsis_owner_only_file(module):
    module.PIXELSIS_CONFIG_FILE.unlink(missing_ok=True)
    module.LEGACY_THREADHALL_CONFIG_FILE.write_text(
        '{"enabled": true, "base_url": "https://pixelsis.example.test", "token": "legacy-token", "device_uuid": "legacy-device"}'
    )

    config = module.load_pixelsis_config()

    assert config["device_uuid"] == "legacy-device"
    assert config["token"] == "legacy-token"
    assert module.PIXELSIS_CONFIG_FILE.exists()
    assert module.PIXELSIS_CONFIG_FILE.stat().st_mode & 0o777 == 0o600


def test_pixelsis_calendar_exception_suppresses_noninstructional_day(module):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_all_schedules({
        "active": "Assembly Schedule",
        "schedules": {
            "Assembly Schedule": [],
            "PixelSIS - Regular": [
                {
                    "id": "regular-1",
                    "day": 2,
                    "time": "08:10",
                    "sound_file": "bell-passing-classic.mp3",
                    "label": "Period 1",
                    "enabled": True,
                }
            ],
        },
    })
    module.save_pixelsis_config({
        "calendar": [
            {
                "date": "2026-07-15",
                "instructional": False,
                "schedule_name": "Regular",
                "label": "Summer closure",
            },
            {
                "date": "2026-07-16",
                "instructional": True,
                "schedule_name": "Regular",
                "label": "Regular day",
            },
        ]
    })

    assert module.load_schedule(datetime(2026, 7, 15)) == []
    assert module.load_schedule(datetime(2026, 7, 16))[0].label == "Period 1"
    assert module.load_schedule(datetime(2026, 7, 15), honor_calendar=False) == []


def save_managed_calendar_schedules(module):
    module.save_all_schedules({
        "active": "Local Alternate",
        "schedules": {
            "Local Alternate": [
                {
                    "id": "local-saturday",
                    "day": 5,
                    "time": "07:45",
                    "sound_file": "local.mp3",
                    "label": "Local Saturday",
                    "enabled": True,
                }
            ],
            "PixelSIS - District Weekday": [
                {
                    "id": "weekday-monday",
                    "day": 0,
                    "time": "08:05",
                    "sound_file": "weekday.mp3",
                    "label": "Weekday Monday",
                    "enabled": True,
                },
                {
                    "id": "weekday-tuesday",
                    "day": 1,
                    "time": "08:10",
                    "sound_file": "weekday.mp3",
                    "label": "Weekday Tuesday",
                    "enabled": True,
                },
                {
                    "id": "weekday-friday",
                    "day": 4,
                    "time": "08:15",
                    "sound_file": "weekday.mp3",
                    "label": "Weekday Friday",
                    "enabled": True,
                },
            ],
            "PixelSIS - District Weekend": [
                {
                    "id": "weekend-saturday",
                    "day": 5,
                    "time": "09:00",
                    "sound_file": "weekend.mp3",
                    "label": "Weekend Saturday",
                    "enabled": True,
                }
            ],
            "PixelSIS - Special Day": [
                {
                    "id": "special-saturday",
                    "day": 5,
                    "time": "10:00",
                    "sound_file": "special.mp3",
                    "label": "Special Saturday",
                    "enabled": True,
                }
            ],
        },
    })


def test_managed_calendar_defaults_select_weekdays_and_keep_weekends_off(module):
    save_managed_calendar_schedules(module)
    module.save_pixelsis_config({
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "District Weekday",
            "weekend_schedule_name": None,
        }
    })

    monday = module.load_schedule(datetime(2026, 8, 3))
    saturday = module.load_schedule(datetime(2026, 8, 8))

    assert [entry.label for entry in monday] == [
        "Weekday Monday",
        "Weekday Tuesday",
        "Weekday Friday",
    ]
    assert saturday == []


def test_managed_calendar_defaults_can_select_a_weekend_schedule(module):
    save_managed_calendar_schedules(module)
    module.save_pixelsis_config({
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "District Weekday",
            "weekend_schedule_name": "District Weekend",
        }
    })

    saturday = module.load_schedule(datetime(2026, 8, 8))

    assert [entry.label for entry in saturday] == ["Weekend Saturday"]


def test_exact_calendar_days_override_defaults_and_fail_closed(module):
    save_managed_calendar_schedules(module)
    module.save_pixelsis_config({
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "District Weekday",
            "weekend_schedule_name": None,
        },
        "calendar": [
            {
                "date": "2026-08-08",
                "instructional": True,
                "schedule_name": "Special Day",
            },
            {
                "date": "2026-08-10",
                "instructional": False,
                "schedule_name": "District Weekday",
            },
            {
                "date": "2026-08-11",
                "instructional": True,
                "schedule_name": "Missing Managed Schedule",
            },
        ],
    })

    assert [
        entry.label for entry in module.load_schedule(datetime(2026, 8, 8))
    ] == ["Special Saturday"]
    assert module.load_schedule(datetime(2026, 8, 10)) == []
    assert module.load_schedule(datetime(2026, 8, 11)) == []


def test_unavailable_managed_default_is_silent_but_absent_defaults_remain_legacy(module):
    save_managed_calendar_schedules(module)
    module.save_pixelsis_config({
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "Missing Managed Schedule",
            "weekend_schedule_name": None,
        }
    })

    assert module.load_schedule(datetime(2026, 8, 3)) == []

    module.save_pixelsis_config({"calendar_defaults": {}})

    assert [
        entry.label for entry in module.load_schedule(datetime(2026, 8, 8))
    ] == ["Local Saturday"]


def test_sync_persists_managed_calendar_defaults_with_revision_and_cleans_empty_remote_state(
    module,
    monkeypatch,
):
    module.save_all_schedules({
        "active": "Safe Setup",
        "schedules": {
            "Safe Setup": [],
            "Threadhall - Legacy Managed": [],
            "PixelSIS - Stale Managed": [
                {
                    "id": "stale",
                    "day": 0,
                    "time": "08:00",
                    "sound_file": "stale.mp3",
                }
            ],
        },
    })
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "device-token",
        "schedule_revision": "revision-old",
        "managed_schedule_hash": module.managed_schedule_state_hash(),
    })
    responses = [
        {
            "schedule_revision": "revision-policy-2",
            "schedules": [],
            "calendar": [],
            "calendar_defaults": {
                "managed": True,
                "weekday_schedule_name": "  District   Weekday  ",
                "weekend_schedule_name": None,
            },
            "commands": [],
        },
        {
            "schedule_revision": "revision-policy-2",
            "schedules": [],
            "calendar": [],
            "commands": [],
        },
    ]
    monkeypatch.setattr(
        module,
        "pixelsis_request",
        lambda *args, **kwargs: responses.pop(0),
    )

    first = module.sync_pixelsis_once()
    first_config = module.load_pixelsis_config()
    first_schedules = module.load_all_schedules()
    module.sync_pixelsis_once()
    second_config = module.load_pixelsis_config()

    assert first["received_schedules"] == 0
    assert first_schedules == {"active": "Safe Setup", "schedules": {"Safe Setup": []}}
    assert first_config["schedule_revision"] == "revision-policy-2"
    assert first_config["calendar_defaults"] == {
        "managed": True,
        "weekday_schedule_name": "District Weekday",
        "weekend_schedule_name": None,
    }
    assert first_config["managed_schedule_hash"] == module.managed_schedule_state_hash()
    assert first_config["managed_schedule_hash"] == (
        "managed-sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert second_config["calendar_defaults"] == first_config["calendar_defaults"]


def test_sync_rejects_invalid_calendar_defaults(module, monkeypatch):
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "device-token",
    })
    monkeypatch.setattr(
        module,
        "pixelsis_request",
        lambda *args, **kwargs: {
            "schedule_revision": "revision-invalid-policy",
            "schedules": [],
            "calendar_defaults": {
                "managed": "true",
                "weekday_schedule_name": "District Weekday",
            },
            "commands": [],
        },
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.sync_pixelsis_once()

    assert exc_info.value.status_code == 502
    assert "invalid calendar defaults" in str(exc_info.value.detail)


def test_next_event_rechecks_managed_calendar_for_each_future_date(module):
    save_managed_calendar_schedules(module)
    module.save_pixelsis_config({
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "District Weekday",
            "weekend_schedule_name": None,
        },
        "calendar": [
            {
                "date": "2026-08-10",
                "instructional": False,
                "schedule_name": "District Weekday",
            }
        ],
    })
    now = datetime(2026, 8, 7, 16, 0)
    today_entries = module.load_schedule(now)

    next_event = module.next_event_payload(today_entries, now=now)

    assert next_event is not None
    assert next_event["id"] == "weekday-tuesday"
    assert next_event["day_name"] == "Tuesday"
    assert next_event["time"] == "08:10"


def test_sync_explicit_empty_calendar_clears_but_absence_preserves(module, monkeypatch):
    original_calendar = [
        {
            "date": "2026-08-04",
            "instructional": False,
            "label": "Closure",
        }
    ]
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "device-token",
        "calendar": original_calendar,
    })
    responses = [
        {"schedules": [], "calendar": [], "commands": []},
        {"schedules": [], "commands": []},
    ]

    monkeypatch.setattr(
        module,
        "pixelsis_request",
        lambda *args, **kwargs: responses.pop(0),
    )

    module.sync_pixelsis_once()

    assert module.load_pixelsis_config()["calendar"] == []

    module.save_pixelsis_config({"calendar": original_calendar})
    module.sync_pixelsis_once()

    assert module.load_pixelsis_config()["calendar"] == original_calendar


def test_pixelsis_command_delivery_is_idempotent_across_ack_retries(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_pixelsis_config({
        "enabled": True,
        "base_url": "https://pixelsis.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 1,
                "schedule_revision": "",
                "schedules": [],
                "calendar": [],
                "commands": [
                    {
                        "id": "command-1",
                        "type": "PLAY_SOUND",
                        "payload": {"sound_key": "passing"},
                    }
                ],
            }
        if path == "api/pibells/v1/commands/command-1/ack":
            acknowledgements.append(payload)
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "pixelsis_request", fake_request)
    monkeypatch.setattr(module, "trigger_bell", lambda sound_file, loop=False: played.append((sound_file, loop)))

    module.sync_pixelsis_once()
    module.sync_pixelsis_once()

    assert played == [("bell-passing-classic.mp3", False)]
    assert len(acknowledgements) == 2
    assert acknowledgements[1]["detail"]["duplicate_delivery"] is True
    assert module.load_pixelsis_command_history()["command-1"]["status"] == "acknowledged"


@pytest.mark.parametrize(
    "action_key",
    ["hold", "secure", "lockdown", "evacuate", "shelter", "medical", "all_clear", "emergency"],
)
def test_pixelsis_command_accepts_legacy_emergency_action_keys(module, monkeypatch, action_key):
    played = []
    monkeypatch.setattr(module, "audio_file_for_key", lambda sound_key: f"{sound_key}.mp3")
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False, output_keys=None: played.append(
            (sound_file, loop, output_keys)
        ),
    )

    summary = module.run_pixelsis_command({
        "type": action_key,
        "payload": {"output_keys": ["local"]},
    })

    assert played == [(f"{action_key}.mp3", False, ["local"])]
    assert action_key in summary


def test_emergency_action_payload_sound_key_takes_precedence(module, monkeypatch):
    played = []
    monkeypatch.setattr(module, "audio_file_for_key", lambda sound_key: f"{sound_key}.mp3")
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False, output_keys=None: played.append(
            (sound_file, loop, output_keys)
        ),
    )

    module.run_pixelsis_command({
        "type": "lockdown",
        "payload": {
            "sound_key": "secure",
            "loop": True,
            "output_keys": ["10.80.2.44:3030"],
        },
    })

    assert played == [("secure.mp3", True, ["10.80.2.44:3030"])]


def test_pixelsis_command_rejects_unsupported_action_type(module, monkeypatch):
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    with pytest.raises(module.HTTPException) as exc_info:
        module.run_pixelsis_command({
            "type": "dismiss_school",
            "payload": {"sound_key": "hold", "output_keys": ["local"]},
        })

    assert exc_info.value.status_code == 422
    assert "Unsupported PixelSIS command" in str(exc_info.value.detail)
    assert played == []


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
        stderr = None

        def poll(self):
            return None

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

    stream_command = next(
        cmd for cmd in commands if "udp://192.168.1.10:3030?pkt_size=1200" in cmd
    )
    assert stream_command.index("-i") < stream_command.index("-flush_packets")
    assert stream_command.index("-i") < stream_command.index("-muxdelay")
    assert stream_command.index("-i") < stream_command.index("-muxpreload")
    assert "+nobuffer" not in stream_command
    assert any(cmd[0] in {"ffplay", "bash"} for cmd in commands)


def test_trigger_bell_reports_ffmpeg_startup_failure(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])

    class FakeStderr:
        def read(self):
            return b"Invalid argument"

    class FailedProcess:
        stderr = FakeStderr()

        def poll(self):
            return 234

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda cmd, stdout=None, stderr=None: FailedProcess(),
    )

    with pytest.raises(RuntimeError, match="Audio stream failed to start: Invalid argument"):
        module.trigger_bell("bell.mp3")


@pytest.mark.parametrize("loop", [True, False])
def test_mixed_stream_startup_failure_cleans_started_processes(module, monkeypatch, loop):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([
        module.Device(ip="192.168.1.10", name="Working zone", port=3030),
        module.Device(ip="192.168.1.11", name="Failed zone", port=3030),
    ])

    class StartedProcess:
        stderr = None

        def __init__(self):
            self.terminated = False
            self.killed = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            self.killed = True

    started = StartedProcess()

    def fake_popen(cmd, stdout=None, stderr=None):
        if any("udp://192.168.1.11:3030" in part for part in cmd):
            raise OSError("simulated second output failure")
        return started

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="Unable to start every configured audio stream"):
        module.trigger_bell(
            "bell.mp3",
            loop=loop,
            output_keys=["192.168.1.10:3030", "192.168.1.11:3030"],
        )

    assert started.terminated is True
    assert started.waited is True
    assert started.killed is False
    assert module.loop_processes == []


def test_loop_local_start_failure_rolls_back_started_stream(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])

    class StartedProcess:
        stderr = None

        def __init__(self):
            self.terminated = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            pass

    stream = StartedProcess()

    def fake_popen(cmd, stdout=None, stderr=None):
        if any("udp://192.168.1.10:3030" in part for part in cmd):
            return stream
        raise OSError("simulated local player failure")

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="Unable to start local audio playback"):
        module.trigger_bell(
            "bell.mp3",
            loop=True,
            output_keys=["192.168.1.10:3030", "local"],
        )

    assert stream.terminated is True
    assert stream.waited is True
    assert module.loop_processes == []


def test_targeted_trigger_starts_only_selected_outputs(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([
        module.Device(ip="192.168.1.10", name="Gym", port=3030),
        module.Device(ip="192.168.1.11", name="Classrooms", port=4040),
    ])
    commands = []

    class FakeProcess:
        stderr = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda cmd, stdout=None, stderr=None: commands.append(cmd) or FakeProcess(),
    )

    module.trigger_bell(
        "bell.mp3",
        output_keys=["192.168.1.11:4040", module.LOCAL_OUTPUT_KEY],
    )

    flattened = [" ".join(command) for command in commands]
    assert any("udp://192.168.1.11:4040?pkt_size=1200" in command for command in flattened)
    assert not any("udp://192.168.1.10:3030?pkt_size=1200" in command for command in flattened)
    assert any(command[0] in {"ffplay", "bash"} for command in commands)


def test_targeted_barix_output_does_not_start_local_playback(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])
    commands = []

    class FakeProcess:
        stderr = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda cmd, stdout=None, stderr=None: commands.append(cmd) or FakeProcess(),
    )

    module.trigger_bell("bell.mp3", output_keys=["192.168.1.10:3030"])

    assert len(commands) == 1
    assert "udp://192.168.1.10:3030?pkt_size=1200" in commands[0]


@pytest.mark.parametrize(
    "output_keys",
    [[], ["192.168.1.99:3030"], ["local", "192.168.1.99:3030"]],
)
def test_empty_or_stale_targets_fail_before_playback(module, monkeypatch, output_keys):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    started = []
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *args, **kwargs: started.append(args[0]),
    )

    with pytest.raises(module.HTTPException):
        module.trigger_bell("bell.mp3", output_keys=output_keys)

    assert started == []


def test_test_api_forwards_explicit_output_targets(module, authed_client, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    calls = []

    def fake_trigger(sound_file, loop=False, output_keys=None):
        calls.append((sound_file, loop, output_keys))

    monkeypatch.setattr(module, "trigger_bell", fake_trigger)

    response = authed_client.post(
        "/api/test",
        json={"sound_file": "bell.mp3", "output_keys": ["local"]},
    )

    assert response.status_code == 200
    assert calls == [("bell.mp3", False, ["local"])]


def test_legacy_schedule_put_preserves_targets_but_explicit_null_resets_them(
    module,
    authed_client,
):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    created = authed_client.post(
        "/api/schedule",
        json={
            "day": 0,
            "time": "08:10",
            "sound_file": "bell.mp3",
            "label": "Gym bell",
            "output_keys": ["local"],
        },
    ).json()[0]
    legacy_payload = {
        "day": 0,
        "time": "08:11",
        "sound_file": "bell.mp3",
        "label": "Older client edit",
        "enabled": True,
    }

    preserved = authed_client.put(
        f"/api/schedule/{created['id']}",
        json=legacy_payload,
    )

    assert preserved.status_code == 200
    assert preserved.json()[0]["output_keys"] == ["local"]

    reset = authed_client.put(
        f"/api/schedule/{created['id']}",
        json={**legacy_payload, "output_keys": None},
    )

    assert reset.status_code == 200
    assert reset.json()[0]["output_keys"] is None


def test_scheduler_passes_targets_and_isolates_overlapping_event_errors(module, monkeypatch):
    events = [
        module.ScheduleEntry(
            id="broken-zone",
            day=0,
            time="08:10",
            sound_file="first.mp3",
            output_keys=["stale:3030"],
        ),
        module.ScheduleEntry(
            id="working-zone",
            day=0,
            time="08:10",
            sound_file="second.mp3",
            output_keys=["local"],
        ),
    ]
    calls = []

    monkeypatch.setattr(module, "load_schedule", lambda effective_date=None: events)

    def fake_trigger(sound_file, loop=False, output_keys=None):
        calls.append((sound_file, output_keys))
        if sound_file == "first.mp3":
            raise RuntimeError("stale output")

    monkeypatch.setattr(module, "trigger_bell", fake_trigger)

    module.dispatch_due_events(datetime(2026, 8, 3, 8, 10))

    assert calls == [
        ("first.mp3", ["stale:3030"]),
        ("second.mp3", ["local"]),
    ]


def test_output_manifest_is_reported_in_telemetry(module, monkeypatch):
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])
    module.save_pixelsis_config({"device_uuid": "controller-1"})
    monkeypatch.setattr(module, "check_device", lambda device, timeout=0.5: True)

    dashboard = module.dashboard()
    payload = module.pixelsis_sync_payload(module.load_pixelsis_config())

    assert dashboard["outputs"] == [
        {"key": "local", "type": "local", "name": "Local speaker", "online": True},
        {
            "key": "192.168.1.10:3030",
            "type": "barix",
            "name": "Gym",
            "ip": "192.168.1.10",
            "port": 3030,
            "online": True,
        },
    ]
    assert payload["telemetry"]["outputs"] == dashboard["outputs"]
    assert payload["capabilities"]["targeted_outputs"] is True


def test_schedule_round_trip_preserves_legacy_empty_and_targeted_outputs(module):
    module.save_all_schedules({
        "active": "Default",
        "schedules": {
            "Default": [
                {"id": "legacy", "day": 0, "time": "08:00", "sound_file": "bell.mp3"},
                {
                    "id": "silent",
                    "day": 0,
                    "time": "08:01",
                    "sound_file": "bell.mp3",
                    "output_keys": [],
                },
                {
                    "id": "gym",
                    "day": 0,
                    "time": "08:02",
                    "sound_file": "bell.mp3",
                    "output_keys": ["192.168.1.10:3030"],
                },
            ]
        },
    })

    entries = {entry.id: entry for entry in module.load_schedule(honor_calendar=False)}

    assert entries["legacy"].output_keys is None
    assert entries["silent"].output_keys == []
    assert entries["gym"].output_keys == ["192.168.1.10:3030"]


def test_remote_schedule_ingestion_preserves_each_output_mode(module):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")

    module.apply_pixelsis_schedules([
        {
            "name": "Regular",
            "active": True,
            "entries": [
                {"id": "legacy", "day": 0, "time": "08:00", "sound_key": "passing"},
                {
                    "id": "silent",
                    "day": 0,
                    "time": "08:01",
                    "sound_key": "passing",
                    "output_keys": [],
                },
                {
                    "id": "zone",
                    "day": 0,
                    "time": "08:02",
                    "sound_key": "passing",
                    "output_keys": ["192.168.1.10:3030"],
                },
            ],
        }
    ])

    entries = {entry.id: entry for entry in module.load_schedule(honor_calendar=False)}

    assert entries["legacy"].output_keys is None
    assert entries["silent"].output_keys == []
    assert entries["zone"].output_keys == ["192.168.1.10:3030"]
