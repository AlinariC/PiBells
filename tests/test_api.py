import importlib
import sys
import threading
from datetime import datetime, timedelta, timezone
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


def save_threadhall_connection(module, overrides):
    config = {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "test-device-token",
        "device_uuid": "local-box",
        "authority": {
            "district_id": "district-1",
            "school_id": "school-1",
            "device_id": "device-1",
        },
    }
    config.update(overrides)
    saved = module.save_threadhall_config(config)
    if not module.THREADHALL_COMMAND_HISTORY_FILE.exists():
        module.save_threadhall_command_history({})
    return saved


def verify_managed_schedule_clock(module):
    instant = datetime.now(timezone.utc)
    tick = module.time.monotonic()
    module.record_threadhall_clock_sample(
        module.load_threadhall_config(), instant.isoformat(), instant, instant, tick, tick
    )


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


def test_threadhall_pairing_saves_device_token(module, authed_client, monkeypatch):
    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        assert payload["pairing_code"] == "ABCD-EFGH"
        assert payload["protocol_version"] == 1
        assert payload["capabilities"]["calendar_exceptions"] is True
        assert payload["capabilities"]["targeted_outputs"] is True
        assert payload["capabilities"]["effective_schedule_evidence"] is True
        assert payload["local_url"] == "http://10.80.2.42"
        return {
            "token": "pbv1.device-1.test-token-value",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            "protocol_version": 1,
            "poll_seconds": 15,
            "device": {"id": "device-1", "status": "online"},
        }

    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "10.80.2.42")

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
    assert module.load_threadhall_config()["token"] == "pbv1.device-1.test-token-value"
    assert (module.BASE_DIR / "threadhall-pairing.json").stat().st_mode & 0o777 == 0o600


def test_environment_managed_token_blocks_interactive_pair_before_network(
    module,
    monkeypatch,
):
    save_threadhall_connection(module, {"token": "existing-file-token"})
    monkeypatch.setenv("THREADHALL_PIBELLS_TOKEN", "environment-token")
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: pytest.fail("pairing request must not start"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test",
            pairing_code="PAIR-ENV-MANAGED",
        ))

    assert exc_info.value.status_code == 409
    assert module.load_threadhall_config()["token"] == "environment-token"
    assert module.load_threadhall_config()["authority"]["device_id"] == "device-1"


def test_pairing_without_authority_fails_closed_and_removes_managed_state(
    module,
    monkeypatch,
):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
        },
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {"token": "unscoped-token"},
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test",
            pairing_code="PAIR-NO-AUTHORITY",
        ))

    assert exc_info.value.status_code == 502
    assert module.load_threadhall_config()["token"] == ""
    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }


def test_ambiguous_repair_failure_clears_existing_managed_authority(
    module,
    monkeypatch,
):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
        },
    })
    save_threadhall_connection(module, {
        "schedule_revision": "revision-1",
        "managed_schedule_hash": "managed-sha256:prior",
        "calendar": [{"date": "2026-09-07", "instructional": False}],
        "last_sync_at": "2026-09-05T12:00:00",
    })
    module.save_threadhall_command_history({
        "command-1": {
            "status": "acknowledged",
            "summary": "Prior outcome",
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": True,
        }
    })

    def fail_pair_request(*args, **kwargs):
        raise module.HTTPException(
            status_code=502,
            detail="connection outcome unknown",
        )

    monkeypatch.setattr(module, "threadhall_request", fail_pair_request)

    with pytest.raises(module.HTTPException) as exc_info:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test",
            pairing_code="PAIR-UNKNOWN-OUTCOME",
        ))

    assert exc_info.value.status_code == 502
    config = module.load_threadhall_config()
    assert config["token"] == ""
    assert config["authority"] == {}
    assert config["schedule_revision"] == ""
    assert config["calendar"] == []
    assert config["last_sync_at"] is None
    assert module.load_threadhall_command_history() == {}
    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }


def test_sync_without_complete_stored_authority_fails_closed_before_network(
    module,
    monkeypatch,
):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
        },
    })
    module.save_threadhall_config({
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "legacy-unscoped-token",
        "authority": {},
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: pytest.fail("network request must not start"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.sync_threadhall_once()

    assert exc_info.value.status_code == 409
    assert module.load_threadhall_config()["token"] == ""
    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }


def test_sync_response_for_another_device_clears_managed_authority_state(
    module,
    monkeypatch,
):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
        },
    })
    save_threadhall_connection(module, {
        "schedule_revision": "revision-1",
        "last_sync_at": "2026-09-05T12:00:00",
    })
    module.save_threadhall_command_history({
        "command-1": {
            "status": "acknowledged",
            "summary": "Prior authority outcome",
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": True,
        }
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {"device": {"id": "another-device"}},
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.sync_threadhall_once()

    assert exc_info.value.status_code == 409
    config = module.load_threadhall_config()
    assert config["token"] == ""
    assert config["last_sync_at"] is None
    assert module.load_threadhall_command_history() == {}
    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }


def test_successful_pairing_clears_history_so_reused_command_id_executes(
    module,
    authed_client,
    monkeypatch,
):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    save_threadhall_connection(module, {
        "authority": {
            "district_id": "district-1",
            "school_id": "school-before-transfer",
            "device_id": "device-1",
        },
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
    module.save_threadhall_command_history({
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
            return {
                "token": "new-device-token",
                "poll_seconds": 15,
                "authority": {
                    "district_id": "district-1",
                    "school_id": "school-1",
                    "device_id": "device-1",
                },
                "device": {"id": "device-1"},
            }
        if path == "api/pibells/v1/sync":
            return {
                "schedules": [],
                "calendar": [],
                "device": {"id": "device-1"},
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

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False: played.append((sound_file, loop)),
    )

    paired = authed_client.post(
        "/api/threadhall/pair",
        json={
            "base_url": "https://threadhall.example.test",
            "pairing_code": "NEW-PAIR",
            "name": "Re-paired controller",
        },
    )

    assert paired.status_code == 200
    assert module.load_threadhall_command_history() == {}
    paired_config = module.load_threadhall_config()
    assert paired_config["schedule_revision"] == ""
    assert paired_config["managed_schedule_hash"] == ""
    assert paired_config["calendar"] == []
    assert paired_config["last_sync_at"] is None

    module.sync_threadhall_once()

    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgements[0]["status"] == "acknowledged"
    assert module.load_threadhall_command_history()["7"]["status"] == "acknowledged"

    module.unpair_threadhall()

    assert module.load_threadhall_command_history() == {}


def test_same_authority_repair_preserves_managed_state_and_pending_outcomes(
    module,
    monkeypatch,
):
    authority = {
        "district_id": "district-1",
        "school_id": "school-1",
        "device_id": "device-1",
    }
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
        },
    })
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "old-token",
        "authority": authority,
        "device_uuid": "controller-1",
        "schedule_revision": "revision-1",
        "managed_schedule_hash": module.managed_schedule_state_hash(),
        "calendar": [{"date": "2026-09-07", "instructional": False}],
        "calendar_defaults": {"managed": True, "weekday_schedule_name": "Regular"},
    })
    module.save_threadhall_command_history({
        "command-1": {
            "status": "acknowledged",
            "summary": "Already executed",
            "detail": {},
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": True,
        }
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "token": "new-token",
            "authority": authority,
            "device": {"id": "device-1"},
        },
    )

    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test",
        pairing_code="NEW-PAIR",
    ))

    config = module.load_threadhall_config()
    assert config["schedule_revision"] == "revision-1"
    assert config["calendar"][0]["instructional"] is False
    assert config["last_sync_at"] is None
    assert "Threadhall - Regular" in module.load_all_schedules()["schedules"]
    assert module.load_threadhall_command_history()["command-1"]["pending_ack"] is True


def test_changed_authority_pair_and_unpair_remove_only_managed_state(module, monkeypatch):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Regular": [],
            module.LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX
            + "Retired Managed": [],
        },
    })
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "old-token",
        "authority": {
            "district_id": "district-1",
            "school_id": "school-1",
            "device_id": "device-1",
        },
        "schedule_revision": "revision-1",
        "calendar": [{"date": "2026-09-07", "instructional": False}],
    })
    module.save_threadhall_command_history({
        "command-1": {"status": "acknowledged", "occurred_at": "2026-09-05T12:00:00+00:00"}
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "token": "new-token",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-2",
                "device_id": "device-1",
            },
            "device": {"id": "device-1"},
        },
    )

    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test",
        pairing_code="MOVE-PAIR",
    ))

    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }
    config = module.load_threadhall_config()
    assert config["schedule_revision"] == ""
    assert config["calendar"] == []
    assert module.load_threadhall_command_history() == {}

    module.unpair_threadhall()
    assert module.load_all_schedules()["schedules"] == {"Local Safety": []}
    assert module.load_threadhall_config()["authority"] == {}


def test_threadhall_sync_applies_revisioned_schedules_and_commands(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []

    def fake_trigger(sound_file, loop=False):
        played.append((sound_file, loop))

    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
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
                "device": {"id": "device-1"},
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
    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)
    monkeypatch.setattr(module, "get_local_ip", lambda: "10.80.2.42")

    status = module.sync_threadhall_once()

    assert status["received_commands"] == 1
    assert module.load_all_schedules()["active"] == "Threadhall - Regular"
    assert module.load_schedule()[0].label == "Period 1"
    assert module.load_schedule()[0].output_keys == ["local"]
    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgements[0]["status"] == "acknowledged"
    assert module.load_threadhall_config()["schedule_revision"] == "revision-1"


def test_legacy_sync_hash_applies_only_when_unrevisioned_schedule_changes(module, monkeypatch):
    save_threadhall_connection(module, {
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
    real_apply = module.apply_threadhall_schedules

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        return {
            "device": {"id": "device-1"},
            "schedules": [responses.pop(0)],
            "calendar": [],
            "commands": [],
        }

    def recording_apply(schedules):
        applied.append(schedules)
        return real_apply(schedules)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "apply_threadhall_schedules", recording_apply)

    first = module.sync_threadhall_once()
    first_revision = module.load_threadhall_config()["schedule_revision"]
    second = module.sync_threadhall_once()
    second_revision = module.load_threadhall_config()["schedule_revision"]
    third = module.sync_threadhall_once()
    changed_revision = module.load_threadhall_config()["schedule_revision"]

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
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "device-token",
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "device": {"id": "device-1"},
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

    module.sync_threadhall_once()

    assert module.load_threadhall_config()["schedule_revision"] == "server-revision-7"


def test_sync_repairs_managed_schedule_when_cached_state_loses_targets(module, monkeypatch):
    module.save_all_schedules({
        "active": "Safe Setup",
        "schedules": {"Safe Setup": []},
    })
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
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
    real_apply = module.apply_threadhall_schedules

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        return {
            "device": {"id": "device-1"},
            "schedule_revision": "revision-targeted-1",
            "schedules": [remote_schedule],
            "calendar": [],
            "commands": [],
        }

    def recording_apply(schedules):
        apply_calls.append(schedules)
        return real_apply(schedules)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "apply_threadhall_schedules", recording_apply)

    first = module.sync_threadhall_once()
    first_config = module.load_threadhall_config()
    unchanged = module.sync_threadhall_once()

    assert first["received_schedules"] == 1
    assert unchanged["received_schedules"] == 0
    assert len(apply_calls) == 1
    assert first_config["managed_schedule_hash"].startswith("managed-sha256:")
    assert module.load_all_schedules()["active"] == "Safe Setup"

    on_disk = module.read_json(module.SCHEDULE_FILE, {})
    managed_entry = on_disk["schedules"]["Threadhall - Regular"][0]
    managed_entry.pop("output_keys")
    module.write_json(module.SCHEDULE_FILE, on_disk)

    repaired = module.sync_threadhall_once()
    repaired_schedules = module.load_all_schedules()
    repaired_entry = repaired_schedules["schedules"]["Threadhall - Regular"][0]
    repaired_config = module.load_threadhall_config()

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
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })

    def fake_threadhall_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["active_schedule"] == "Assembly Schedule"
            assert payload["schedule_options"] == ["Assembly Schedule"]
            return {
                "poll_seconds": 15,
                "device": {"id": "device-1"},
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

    monkeypatch.setattr(module, "threadhall_request", fake_threadhall_request)

    module.sync_threadhall_once()

    schedules = module.load_all_schedules()
    assert schedules["active"] == "Assembly Schedule"
    assert "Threadhall - Regular" in schedules["schedules"]


def test_retired_config_migrates_to_threadhall_owner_only_file(module):
    module.THREADHALL_CONFIG_FILE.unlink(missing_ok=True)
    module.LEGACY_THREADHALL_CONFIG_FILE.write_text(
        '{"enabled": true, "base_url": "https://threadhall.example.test", "token": "legacy-token", "device_uuid": "legacy-device"}'
    )

    config = module.load_threadhall_config()

    assert config["device_uuid"] == "legacy-device"
    assert config["token"] == "legacy-token"
    assert module.THREADHALL_CONFIG_FILE.exists()
    assert module.THREADHALL_CONFIG_FILE.stat().st_mode & 0o777 == 0o600
    status = module.safe_threadhall_status()
    assert status["paired"] is False
    assert status["enabled"] is False
    assert "Re-pair with Threadhall" in status["last_error"]


def test_retired_runtime_aliases_remain_available_but_hidden_from_openapi(
    module,
    authed_client,
):
    retired_root = f"/api/{module.RETIRED_INTEGRATION_SLUG}"

    response = authed_client.get(f"{retired_root}/status")
    schema = authed_client.get("/openapi.json").json()

    assert response.status_code == 200
    assert retired_root + "/status" not in schema["paths"]
    assert (
        getattr(module, f"load_{module.RETIRED_INTEGRATION_SLUG}_config")
        is module.load_threadhall_config
    )
    assert (
        getattr(module, f"{module.RETIRED_INTEGRATION_NAME}PairRequest")
        is module.ThreadhallPairRequest
    )


def test_threadhall_calendar_exception_suppresses_noninstructional_day(module):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.save_all_schedules({
        "active": "Assembly Schedule",
        "schedules": {
            "Assembly Schedule": [],
            "Threadhall - Regular": [
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
    save_threadhall_connection(module, {
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "Regular",
            "weekend_schedule_name": None,
        },
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


@pytest.mark.parametrize("code_point", ["a", "🔔"])
def test_threadhall_managed_schedule_key_enforces_final_code_point_limit(module, code_point):
    accepted = module.managed_threadhall_schedule_name(code_point * 67)
    assert accepted == f"Threadhall - {code_point * 67}"
    assert len(accepted) == 80

    with pytest.raises(module.HTTPException) as exc_info:
        module.managed_threadhall_schedule_name(code_point * 68)
    assert exc_info.value.status_code == 400


def test_managed_calendar_default_enforces_final_controller_key_limit(module):
    with pytest.raises(module.HTTPException) as exc_info:
        module.normalize_calendar_defaults({
            "managed": True,
            "weekday_schedule_name": "a" * 68,
            "weekend_schedule_name": None,
        })

    assert exc_info.value.status_code == 502


def test_retired_managed_prefix_is_canonicalized(module):
    retired_name = module.LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX + "Regular"
    assert module.managed_threadhall_schedule_name(retired_name) == "Threadhall - Regular"


def test_managed_schedule_apply_is_atomic_when_any_entry_is_invalid(module):
    original = {
        "active": "Local Safety",
        "schedules": {
            "Local Safety": [],
            "Threadhall - Prior": [],
        },
    }
    module.save_all_schedules(original)

    with pytest.raises(module.HTTPException) as exc_info:
        module.apply_threadhall_schedules([
            {
                "name": "Regular",
                "active": True,
                "entries": [
                    {
                        "id": "valid-entry",
                        "day": 0,
                        "time": "08:00",
                        "sound_file": "bell.mp3",
                    }
                ],
            },
            {
                "name": "Invalid",
                "active": False,
                "entries": [
                    {
                        "id": "invalid-entry",
                        "day": 7,
                        "time": "09:00",
                        "sound_file": "bell.mp3",
                    }
                ],
            },
        ])

    assert exc_info.value.status_code == 502
    assert module.load_all_schedules() == original


@pytest.mark.parametrize(
    ("calendar", "calendar_defaults", "detail"),
    [
        (
            [
                {
                    "date": "2026-09-07",
                    "instructional": True,
                    "schedule_name": "Missing",
                    "label": None,
                }
            ],
            None,
            "calendar references an unpublished managed schedule",
        ),
        (
            [],
            {
                "managed": True,
                "weekday_schedule_name": "Missing",
                "weekend_schedule_name": None,
            },
            "calendar default references an unpublished managed schedule",
        ),
    ],
)
def test_threadhall_bundle_rejects_unpublished_calendar_schedule(
    module,
    calendar,
    calendar_defaults,
    detail,
):
    with pytest.raises(module.HTTPException) as exc_info:
        module.validate_threadhall_bundle_links(
            [{"name": "Regular", "entries": []}],
            calendar,
            calendar_defaults,
        )

    assert exc_info.value.status_code == 502
    assert detail in str(exc_info.value.detail)


def test_pending_threadhall_outcomes_are_never_evicted_from_local_history(module):
    pending = {
        f"pending-{index}": {
            "status": "acknowledged",
            "summary": f"Outcome {index}",
            "detail": {},
            "occurred_at": f"2026-09-05T12:{index % 60:02d}:00+00:00",
            "pending_ack": True,
        }
        for index in range(501)
    }
    module.save_threadhall_command_history({
        **pending,
        "completed": {
            "status": "acknowledged",
            "summary": "Delivered receipt",
            "detail": {},
            "occurred_at": "2026-09-05T13:00:00+00:00",
            "pending_ack": False,
        },
    })

    history = module.load_threadhall_command_history()
    assert len(history) == 501
    assert set(history) == set(pending)


def test_fixed_mode_ignores_stale_calendar_and_reports_daemon_effective_schedule(module):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    module.apply_threadhall_schedules([
        {
            "name": "Two Hour Delay",
            "active": True,
            "entries": [
                {
                    "id": "delay-1",
                    "day": 0,
                    "time": "10:00",
                    "sound_key": "passing",
                    "label": "Delayed start",
                    "enabled": True,
                }
            ],
        }
    ])
    save_threadhall_connection(module, {
        "calendar": [
            {
                "date": "2026-08-03",
                "instructional": False,
                "schedule_name": "Regular",
            }
        ],
        "calendar_defaults": {
            "managed": False,
            "weekday_schedule_name": None,
            "weekend_schedule_name": None,
        },
    })

    entries, evidence = module.resolve_effective_schedule(datetime(2026, 8, 3))

    assert [entry.label for entry in entries] == ["Delayed start"]
    assert evidence == {
        "effective_date": "2026-08-03",
        "managed": True,
        "name": "Threadhall - Two Hour Delay",
        "source": "active",
    }


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
            "Threadhall - District Weekday": [
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
            "Threadhall - District Weekend": [
                {
                    "id": "weekend-saturday",
                    "day": 5,
                    "time": "09:00",
                    "sound_file": "weekend.mp3",
                    "label": "Weekend Saturday",
                    "enabled": True,
                }
            ],
            "Threadhall - Special Day": [
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
    save_threadhall_connection(module, {
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
    save_threadhall_connection(module, {
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
    save_threadhall_connection(module, {
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
    save_threadhall_connection(module, {
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "Missing Managed Schedule",
            "weekend_schedule_name": None,
        }
    })

    assert module.load_schedule(datetime(2026, 8, 3)) == []

    save_threadhall_connection(module, {"calendar_defaults": {}})

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
            module.LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX
            + "Stale Managed": [
                {
                    "id": "stale",
                    "day": 0,
                    "time": "08:00",
                    "sound_file": "stale.mp3",
                }
            ],
        },
    })
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "device-token",
        "schedule_revision": "revision-old",
        "managed_schedule_hash": module.managed_schedule_state_hash(),
    })
    responses = [
        {
            "device": {"id": "device-1"},
            "schedule_revision": "revision-policy-2",
            "schedules": [],
            "calendar": [],
            "calendar_defaults": {
                "managed": True,
                "weekday_schedule_name": None,
                "weekend_schedule_name": None,
            },
            "commands": [],
        },
        {
            "device": {"id": "device-1"},
            "schedule_revision": "revision-policy-2",
            "schedules": [],
            "calendar": [],
            "commands": [],
        },
    ]
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: responses.pop(0),
    )

    first = module.sync_threadhall_once()
    first_config = module.load_threadhall_config()
    first_schedules = module.load_all_schedules()
    module.sync_threadhall_once()
    second_config = module.load_threadhall_config()

    assert first["received_schedules"] == 0
    assert first_schedules == {"active": "Safe Setup", "schedules": {"Safe Setup": []}}
    assert first_config["schedule_revision"] == "revision-policy-2"
    assert first_config["calendar_defaults"] == {
        "managed": True,
        "weekday_schedule_name": None,
        "weekend_schedule_name": None,
    }
    assert first_config["managed_schedule_hash"] == module.managed_schedule_state_hash()
    assert first_config["managed_schedule_hash"] == (
        "managed-sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert second_config["calendar_defaults"] == first_config["calendar_defaults"]


def test_sync_rejects_invalid_calendar_defaults(module, monkeypatch):
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "device-token",
    })
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "device": {"id": "device-1"},
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
        module.sync_threadhall_once()

    assert exc_info.value.status_code == 502
    assert "invalid calendar defaults" in str(exc_info.value.detail)


def test_next_event_rechecks_managed_calendar_for_each_future_date(module):
    save_managed_calendar_schedules(module)
    save_threadhall_connection(module, {
        "district_time_zone": "America/Phoenix",
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
    verify_managed_schedule_clock(module)
    now = datetime(2026, 8, 7, 16, 0)
    today_entries = module.load_schedule(now)

    next_event = module.next_event_payload(today_entries, now=now)

    assert next_event is not None
    assert next_event["id"] == "weekday-tuesday"
    assert next_event["day_name"] == "Tuesday"
    assert next_event["time"] == "08:10"


def prepare_managed_dispatch(module, *, zone="America/Phoenix", day=0, bell_time="08:10"):
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {
            "Threadhall - Regular": [{
                "id": "managed-bell",
                "day": day,
                "time": bell_time,
                "sound_file": "bell.mp3",
                "label": "Managed bell",
                "enabled": True,
            }],
        },
    })
    return save_threadhall_connection(module, {
        "district_time_zone": zone,
        "calendar_defaults": {
            "managed": True,
            "weekday_schedule_name": "Regular",
            "weekend_schedule_name": "Regular",
        },
    })


def test_managed_dispatch_requires_configured_zone_and_fresh_server_clock(module, monkeypatch):
    prepare_managed_dispatch(module, zone="")
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))
    monday_utc = datetime(2026, 8, 3, 15, 10, tzinfo=timezone.utc)

    module.dispatch_due_events(monday_utc)
    assert module.dashboard()["schedule_clock"]["reason"] == "district_time_zone_missing"
    assert played == []

    module.save_threadhall_config({"district_time_zone": "Bad/Zone"})
    module.dispatch_due_events(monday_utc)
    assert module.dashboard()["schedule_clock"]["reason"] == "district_time_zone_invalid"
    assert played == []

    module.save_threadhall_config({"district_time_zone": "America/Phoenix"})
    module.dispatch_due_events(monday_utc)
    assert module.dashboard()["schedule_clock"]["reason"] == "server_clock_not_verified"
    assert played == []


def test_invalid_environment_zone_overrides_file_and_blocks_managed_bells(module, monkeypatch):
    prepare_managed_dispatch(module)
    verify_managed_schedule_clock(module)
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))
    monkeypatch.setenv("PIBELLS_DISTRICT_TIME_ZONE", "Bad/Zone")

    module.dispatch_due_events(datetime(2026, 8, 3, 15, 10, tzinfo=timezone.utc))

    assert module.managed_schedule_clock_status()["reason"] == "district_time_zone_invalid"
    assert played == []


def test_managed_dispatch_converts_utc_to_district_wall_time_and_observes_closure(module, monkeypatch):
    prepare_managed_dispatch(module)
    verify_managed_schedule_clock(module)
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    module.dispatch_due_events(datetime(2026, 8, 3, 8, 10, tzinfo=timezone.utc))
    assert played == []  # 01:10 in Phoenix, despite matching the UTC wall hour.
    module.dispatch_due_events(datetime(2026, 8, 3, 15, 10, tzinfo=timezone.utc))
    assert played == [("bell.mp3",)]

    module.save_threadhall_config({"calendar": [{
        "date": "2026-08-03",
        "instructional": False,
        "schedule_name": "Regular",
    }]})
    module.dispatch_due_events(datetime(2026, 8, 3, 15, 10, tzinfo=timezone.utc))
    assert played == [("bell.mp3",)]


def test_managed_calendar_uses_district_date_when_utc_is_next_day(module, monkeypatch):
    prepare_managed_dispatch(module, bell_time="23:10")
    verify_managed_schedule_clock(module)
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))
    tuesday_utc = datetime(2026, 8, 4, 6, 10, tzinfo=timezone.utc)
    module.save_threadhall_config({"calendar": [{
        "date": "2026-08-04",
        "instructional": False,
        "schedule_name": "Regular",
    }]})

    module.dispatch_due_events(tuesday_utc)
    assert played == [("bell.mp3",)]  # Still Monday, August 3 in Phoenix.

    module.save_threadhall_config({"calendar": [{
        "date": "2026-08-03",
        "instructional": False,
        "schedule_name": "Regular",
    }]})
    module.dispatch_due_events(tuesday_utc)
    assert played == [("bell.mp3",)]


def test_managed_dispatch_refuses_skew_stale_samples_and_zone_changes(module, monkeypatch):
    prepare_managed_dispatch(module)
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))
    monday_utc = datetime(2026, 8, 3, 15, 10, tzinfo=timezone.utc)
    instant = datetime.now(timezone.utc)
    tick = module.time.monotonic()
    module.record_threadhall_clock_sample(
        module.load_threadhall_config(),
        (instant + timedelta(minutes=2)).isoformat(),
        instant, instant, tick, tick,
    )
    assert module.managed_schedule_clock_status()["reason"] == "controller_clock_skew"
    module.dispatch_due_events(monday_utc)
    assert played == []

    old_instant = instant - timedelta(seconds=121)
    old_tick = tick - 121
    module.record_threadhall_clock_sample(
        module.load_threadhall_config(), old_instant.isoformat(),
        old_instant, old_instant, old_tick, old_tick,
    )
    assert module.managed_schedule_clock_status()["reason"] == "server_clock_sample_stale"
    module.dispatch_due_events(monday_utc)
    assert played == []

    verify_managed_schedule_clock(module)
    assert module.managed_schedule_clock_status()["ready"] is True
    proof = module.threadhall_clock_proof
    jumped = module.managed_schedule_clock_status(
        now_utc=proof["verified_at_utc"] + timedelta(seconds=10),
        now_monotonic=proof["verified_at_monotonic"] + 1,
    )
    assert jumped["reason"] == "controller_clock_changed"
    assert jumped["ready"] is False
    module.save_threadhall_config({"district_time_zone": "America/Denver"})
    assert module.managed_schedule_clock_status()["ready"] is False
    module.dispatch_due_events(monday_utc)
    assert played == []


def test_managed_dispatch_plays_once_in_repeated_fall_back_hour(module, monkeypatch):
    prepare_managed_dispatch(module, zone="America/Denver", day=6, bell_time="01:30")
    verify_managed_schedule_clock(module)
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    module.dispatch_due_events(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc))
    module.dispatch_due_events(datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc))

    assert played == [("bell.mp3",)]


def test_threadhall_sync_server_time_grants_and_revokes_managed_dispatch(module, monkeypatch):
    prepare_managed_dispatch(module)
    response_numbers = iter((1, 2))

    def fake_request(*args, **kwargs):
        response = {"device": {"id": "device-1"}, "commands": []}
        if next(response_numbers) == 1:
            response["server_time"] = datetime.now(timezone.utc).isoformat()
        return response

    monkeypatch.setattr(module, "threadhall_request", fake_request)

    first = module.sync_threadhall_once()
    assert first["schedule_clock"]["ready"] is True
    assert first["schedule_clock"]["district_time_zone"] == "America/Phoenix"
    assert first["schedule_clock"]["district_utc_offset_minutes"] == -420
    assert first["schedule_clock"]["clock_skew_bound_seconds"] is not None

    second = module.sync_threadhall_once()
    assert second["schedule_clock"]["ready"] is False
    assert second["schedule_clock"]["reason"] == "server_time_missing"


def test_sync_explicit_empty_calendar_clears_but_absence_preserves(module, monkeypatch):
    original_calendar = [
        {
            "date": "2026-08-04",
            "instructional": False,
            "label": "Closure",
        }
    ]
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "device-token",
        "calendar": original_calendar,
    })
    responses = [
        {
            "device": {"id": "device-1"},
            "schedules": [],
            "calendar": [],
            "commands": [],
        },
        {"device": {"id": "device-1"}, "schedules": [], "commands": []},
    ]

    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: responses.pop(0),
    )

    module.sync_threadhall_once()

    assert module.load_threadhall_config()["calendar"] == []

    save_threadhall_connection(module, {"calendar": original_calendar})
    module.sync_threadhall_once()

    assert module.load_threadhall_config()["calendar"] == original_calendar


def test_threadhall_command_delivery_is_idempotent_across_ack_retries(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 1,
                "device": {"id": "device-1"},
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

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "trigger_bell", lambda sound_file, loop=False: played.append((sound_file, loop)))

    module.sync_threadhall_once()
    module.sync_threadhall_once()

    assert played == [("bell-passing-classic.mp3", False)]
    assert len(acknowledgements) == 2
    assert acknowledgements[1] == acknowledgements[0]
    assert module.load_threadhall_command_history()["command-1"]["status"] == "acknowledged"


def test_threadhall_records_unknown_outcome_before_physical_execution(
    module,
    monkeypatch,
):
    save_threadhall_connection(module, {})
    sync_count = 0
    execution_attempts = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        nonlocal sync_count
        if path == "api/pibells/v1/sync":
            sync_count += 1
            return {
                "device": {"id": "device-1"},
                "commands": [
                    {
                        "id": "interrupted-command",
                        "type": "PLAY_SOUND",
                        "payload": {"sound_key": "passing"},
                    }
                ] if sync_count == 1 else [],
            }
        if path == "api/pibells/v1/commands/interrupted-command/ack":
            acknowledgements.append(payload)
            return {"ok": True}
        raise AssertionError(path)

    def interrupt_execution(command):
        execution_attempts.append(command["id"])
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", interrupt_execution)

    with pytest.raises(KeyboardInterrupt):
        module.sync_threadhall_once()

    pending = module.load_threadhall_command_history()["interrupted-command"]
    assert pending["status"] == "failed"
    assert pending["detail"] == {"execution_outcome_unknown": True}
    assert pending["pending_ack"] is True

    module.sync_threadhall_once()

    assert execution_attempts == ["interrupted-command"]
    assert acknowledgements == [{
        "status": pending["status"],
        "summary": pending["summary"],
        "detail": pending["detail"],
        "occurred_at": pending["occurred_at"],
    }]
    assert module.load_threadhall_command_history()["interrupted-command"]["pending_ack"] is False


@pytest.mark.parametrize(
    "damage",
    ["missing", "missing_with_stale_legacy", "invalid_json", "empty_outcome", "unreadable"],
)
def test_threadhall_restart_refuses_commands_when_outcome_history_is_unavailable(
    module,
    monkeypatch,
    damage,
):
    save_threadhall_connection(module, {})
    attempts = []
    command = {
        "id": "possibly-played-command",
        "type": "PLAY_SOUND",
        "payload": {"sound_key": "passing"},
    }

    def first_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {"device": {"id": "device-1"}, "commands": [command]}
        pytest.fail("The interrupted command must not be acknowledged yet")

    def interrupted_execution(offered):
        attempts.append(offered["id"])
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, "threadhall_request", first_request)
    monkeypatch.setattr(module, "run_threadhall_command", interrupted_execution)
    with pytest.raises(KeyboardInterrupt):
        module.sync_threadhall_once()

    history_path = module.THREADHALL_COMMAND_HISTORY_FILE
    assert module.load_threadhall_command_history()[command["id"]]["detail"] == {
        "execution_outcome_unknown": True
    }
    if damage == "missing_with_stale_legacy":
        module.LEGACY_THREADHALL_COMMAND_HISTORY_FILE.write_text("{}")
    if damage in {"missing", "missing_with_stale_legacy"}:
        history_path.unlink()
    elif damage == "invalid_json":
        history_path.write_text("{")
    elif damage == "empty_outcome":
        history_path.write_text('{"possibly-played-command": {}}')
    damaged_bytes = history_path.read_bytes() if history_path.exists() else None

    restarted = importlib.reload(module)
    monkeypatch.setattr(
        restarted,
        "threadhall_request",
        lambda *args, **kwargs: pytest.fail("Unsafe history must stop sync before network"),
    )
    monkeypatch.setattr(
        restarted,
        "run_threadhall_command",
        lambda *args, **kwargs: pytest.fail("Physical command must not replay"),
    )

    if damage == "unreadable":
        original_open = Path.open

        def deny_history_read(path, *args, **kwargs):
            if path == history_path:
                raise PermissionError("simulated unreadable history")
            return original_open(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", deny_history_read)
            with pytest.raises(restarted.HTTPException) as exc_info:
                restarted.sync_threadhall_once()
    else:
        with pytest.raises(restarted.HTTPException) as exc_info:
            restarted.sync_threadhall_once()

    assert exc_info.value.status_code == 503
    assert "physical commands are paused" in str(exc_info.value.detail)
    assert attempts == [command["id"]]
    assert (history_path.read_bytes() if history_path.exists() else None) == damaged_bytes


def test_threadhall_retries_a_durable_outcome_after_the_command_is_withdrawn(
    module,
    monkeypatch,
):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    save_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    sync_count = 0
    acknowledgement_attempts = []
    played = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        nonlocal sync_count
        if path == "api/pibells/v1/sync":
            sync_count += 1
            return {
                "device": {"id": "device-1"},
                "commands": [
                    {
                        "id": "command-withdrawn",
                        "type": "PLAY_SOUND",
                        "payload": {"sound_key": "passing"},
                    }
                ] if sync_count == 1 else [],
            }
        if path == "api/pibells/v1/commands/command-withdrawn/ack":
            acknowledgement_attempts.append(payload)
            if len(acknowledgement_attempts) == 1:
                raise module.HTTPException(status_code=502, detail="temporary callback failure")
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False: played.append((sound_file, loop)),
    )

    first = module.sync_threadhall_once()
    second = module.sync_threadhall_once()

    assert first["pending_acknowledgements"] == 1
    assert second["pending_acknowledgements"] == 0
    assert played == [("bell-passing-classic.mp3", False)]
    assert acknowledgement_attempts[1] == acknowledgement_attempts[0]


def test_sync_http_wait_does_not_block_minute_dispatch(module, monkeypatch):
    save_threadhall_connection(module, {})
    event = module.ScheduleEntry(
        id="minute-bell",
        day=0,
        time="08:10",
        sound_file="bell.mp3",
    )
    module.save_schedule([event])
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda sound_file, **kwargs: played.append(sound_file))
    request_started = threading.Event()
    release_request = threading.Event()
    dispatch_finished = threading.Event()
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        request_started.set()
        assert release_request.wait(3)
        return {"device": {"id": "device-1"}, "commands": []}

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    sync_worker = threading.Thread(target=run_sync, daemon=True)
    sync_worker.start()
    try:
        assert request_started.wait(1)
        dispatch_worker = threading.Thread(
            target=lambda: (module.dispatch_due_events(datetime(2026, 8, 3, 8, 10)), dispatch_finished.set()),
            daemon=True,
        )
        dispatch_worker.start()
        assert dispatch_finished.wait(1), "minute dispatch waited for Threadhall HTTP"
        assert played == ["bell.mp3"]
    finally:
        release_request.set()
        sync_worker.join(timeout=2)
    assert not sync_worker.is_alive()
    assert errors == []


def test_pending_ack_retry_cannot_delay_remote_stop_or_schedule_read(module, monkeypatch):
    save_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "prior-command": {
            "status": "failed",
            "summary": "Prior result",
            "detail": {},
            "occurred_at": "2026-08-03T08:00:00+00:00",
            "pending_ack": True,
        },
    })
    stopped = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    ack_started = threading.Event()
    release_ack = threading.Event()
    schedule_read_finished = threading.Event()
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "device": {"id": "device-1"},
                "commands": [{"id": "stop-now", "type": "STOP_AUDIO", "payload": {}}],
            }
        if path == "api/pibells/v1/commands/prior-command/ack":
            ack_started.set()
            assert release_ack.wait(3)
            return {"ok": True}
        if path == "api/pibells/v1/commands/stop-now/ack":
            return {"ok": True}
        raise AssertionError(path)

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    sync_worker = threading.Thread(target=run_sync, daemon=True)
    sync_worker.start()
    try:
        assert ack_started.wait(1)
        assert stopped == [True], "STOP must execute before old acknowledgement retry"
        schedule_worker = threading.Thread(
            target=lambda: (module.resolve_effective_schedule(), schedule_read_finished.set()),
            daemon=True,
        )
        schedule_worker.start()
        assert schedule_read_finished.wait(1), "schedule read waited for acknowledgement HTTP"
    finally:
        release_ack.set()
        sync_worker.join(timeout=2)
    assert not sync_worker.is_alive()
    assert errors == []
    history = module.load_threadhall_command_history()
    assert history["prior-command"]["pending_ack"] is False
    assert history["stop-now"]["pending_ack"] is False


def test_daemon_poll_reaches_later_stop_while_ack_worker_is_waiting(module, monkeypatch):
    save_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "prior-command": {
            "status": "failed",
            "summary": "Prior result",
            "detail": {},
            "occurred_at": "2026-08-03T08:00:00+00:00",
            "pending_ack": True,
        },
    })
    stopped = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    ack_started = threading.Event()
    release_ack = threading.Event()
    sync_calls = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            sync_calls.append(True)
            commands = (
                [{"id": "later-stop", "type": "STOP_AUDIO", "payload": {}}]
                if len(sync_calls) == 2
                else []
            )
            return {"device": {"id": "device-1"}, "commands": commands}
        if path == "api/pibells/v1/commands/prior-command/ack":
            ack_started.set()
            assert release_ack.wait(3)
            return {"ok": True}
        if path == "api/pibells/v1/commands/later-stop/ack":
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    try:
        module.sync_threadhall_once(acknowledgements_in_background=True)
        assert ack_started.wait(1)
        module.sync_threadhall_once(acknowledgements_in_background=True)
        assert stopped == [True], "a previous ACK wait must not hold the poll thread"
    finally:
        release_ack.set()
        assert module.threadhall_ack_worker_lock.acquire(timeout=2)
        module.threadhall_ack_worker_lock.release()

    module.sync_threadhall_once()
    assert module.load_threadhall_command_history()["later-stop"]["pending_ack"] is False
    assert stopped == [True]


def test_unpair_during_sync_http_discards_stale_stop_command(module, monkeypatch):
    save_threadhall_connection(module, {})
    request_started = threading.Event()
    release_request = threading.Event()
    unpair_finished = threading.Event()
    executed = []
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        request_started.set()
        assert release_request.wait(3)
        return {
            "device": {"id": "device-1"},
            "commands": [{"id": "stale-stop", "type": "STOP_AUDIO", "payload": {}}],
        }

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: executed.append(command))
    sync_worker = threading.Thread(target=run_sync, daemon=True)
    sync_worker.start()
    try:
        assert request_started.wait(1)
        unpair_worker = threading.Thread(
            target=lambda: (module.unpair_threadhall(), unpair_finished.set()),
            daemon=True,
        )
        unpair_worker.start()
        assert unpair_finished.wait(1), "unpair waited for Threadhall HTTP"
    finally:
        release_request.set()
        sync_worker.join(timeout=2)
    assert not sync_worker.is_alive()
    assert errors == []
    assert executed == []
    assert module.load_threadhall_config()["enabled"] is False


@pytest.mark.parametrize(
    "action_key",
    ["hold", "secure", "lockdown", "evacuate", "shelter", "medical", "all_clear", "emergency"],
)
def test_threadhall_command_accepts_legacy_emergency_action_keys(module, monkeypatch, action_key):
    played = []
    monkeypatch.setattr(module, "audio_file_for_key", lambda sound_key: f"{sound_key}.mp3")
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, loop=False, output_keys=None: played.append(
            (sound_file, loop, output_keys)
        ),
    )

    summary = module.run_threadhall_command({
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

    module.run_threadhall_command({
        "type": "lockdown",
        "payload": {
            "sound_key": "secure",
            "loop": True,
            "output_keys": ["10.80.2.44:3030"],
        },
    })

    assert played == [("secure.mp3", True, ["10.80.2.44:3030"])]


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        ("lockdown", {}),
        ("EMERGENCY_ALERT", {}),
        ("PLAY_SOUND", {"sound_key": "lockdown"}),
    ],
)
def test_missing_emergency_audio_never_plays_another_file(
    module, monkeypatch, command_type, payload
):
    (module.AUDIO_DIR / "another-sound.mp3").write_bytes(b"unrelated audio")
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    with pytest.raises(module.HTTPException) as exc_info:
        module.run_threadhall_command({"type": command_type, "payload": payload})

    assert exc_info.value.status_code == 404
    assert played == []


@pytest.mark.parametrize("damage", ["unreadable", "empty"])
def test_unreadable_emergency_audio_never_plays_another_file(
    module, monkeypatch, damage
):
    selected = module.AUDIO_DIR / "emergency-lockdown.mp3"
    selected.write_bytes(b"selected audio" if damage == "unreadable" else b"")
    (module.AUDIO_DIR / "another-sound.mp3").write_bytes(b"unrelated audio")
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))
    if damage == "unreadable":
        original_open = Path.open

        def deny_selected_audio(path, *args, **kwargs):
            if path == selected:
                raise PermissionError("simulated unreadable emergency audio")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny_selected_audio)

    with pytest.raises(module.HTTPException) as exc_info:
        module.run_threadhall_command({"type": "lockdown", "payload": {}})

    assert exc_info.value.status_code == 503
    assert "emergency sound file is unreadable" in str(exc_info.value.detail)
    assert played == []


def test_emergency_command_rejects_non_emergency_sound_key(module, monkeypatch):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"routine audio")
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    with pytest.raises(module.HTTPException) as exc_info:
        module.run_threadhall_command({
            "type": "EMERGENCY_ALERT",
            "payload": {"sound_key": "passing"},
        })

    assert exc_info.value.status_code == 422
    assert played == []


def test_readable_emergency_audio_uses_the_exact_selected_file(module, monkeypatch):
    (module.AUDIO_DIR / "another-sound.mp3").write_bytes(b"unrelated audio")
    (module.AUDIO_DIR / "emergency-lockdown.mp3").write_bytes(b"selected audio")
    played = []
    monkeypatch.setattr(
        module,
        "trigger_bell",
        lambda sound_file, **kwargs: played.append(sound_file),
    )

    module.run_threadhall_command({"type": "lockdown", "payload": {}})

    assert played == ["emergency-lockdown.mp3"]


def test_threadhall_command_rejects_unsupported_action_type(module, monkeypatch):
    played = []
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: played.append(args))

    with pytest.raises(module.HTTPException) as exc_info:
        module.run_threadhall_command({
            "type": "dismiss_school",
            "payload": {"sound_key": "hold", "output_keys": ["local"]},
        })

    assert exc_info.value.status_code == 422
    assert "Unsupported Threadhall command" in str(exc_info.value.detail)
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
    save_threadhall_connection(module, {"device_uuid": "controller-1"})
    monkeypatch.setattr(module, "check_device", lambda device, timeout=0.5: True)

    dashboard = module.dashboard()
    payload = module.threadhall_sync_payload(module.load_threadhall_config())

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
    assert payload["capabilities"]["effective_schedule_evidence"] is True
    effective_schedule = payload["telemetry"]["effective_schedule"]
    assert set(effective_schedule) == {
        "effective_date",
        "managed",
        "name",
        "source",
    }
    assert datetime.fromisoformat(effective_schedule["effective_date"])
    assert effective_schedule["managed"] is False
    assert effective_schedule["name"] == "Default"
    assert effective_schedule["source"] == "active"


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

    module.apply_threadhall_schedules([
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
