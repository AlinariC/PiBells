import importlib
import io
import json
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
        "protocol_version": 1,
        "authority": {
            "district_id": "district-1",
            "school_id": "school-1",
            "device_id": "device-1",
        },
    }
    config.update(overrides)
    saved = module.save_threadhall_config(config)
    if not module.THREADHALL_COMMAND_HISTORY_FILE.exists():
        module.initialize_threadhall_command_history(saved, fresh_install=True)
    return saved


def save_claim_threadhall_connection(module, overrides):
    return save_threadhall_connection(module, {
        "protocol_version": 2,
        "claim_url_template": module.THREADHALL_CLAIM_URL_TEMPLATE,
        **overrides,
    })


def claim_pair_fields(module):
    return {
        "protocol_version": 2,
        "claim_url_template": module.THREADHALL_CLAIM_URL_TEMPLATE,
    }


def claim_sync_offer(command_id, command_type="PLAY_SOUND", *, expires_at=None):
    return {
        "id": command_id,
        "type": command_type,
        "claim_required": True,
        "expires_at": expires_at,
        "idempotency_key": f"offer-{command_id}",
    }


def claim_grant(command_id, command_type="PLAY_SOUND", payload=None, *, expires_at=None):
    return {
        "ok": True,
        "execute": True,
        "claim_id": "00000000-0000-4000-8000-000000000001",
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "command": {
            "id": command_id,
            "type": command_type,
            "payload": payload or {},
            "expires_at": expires_at,
        },
    }


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
        assert payload["protocol_version"] == 2
        assert payload["capabilities"]["execution_claim_v1"] is True
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
            **claim_pair_fields(module),
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


def test_first_failed_pair_preserves_bootstrap_receipt_and_can_retry(module, monkeypatch):
    seen_devices = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        seen_devices.append(payload["device_uuid"])
        if len(seen_devices) == 1:
            raise OSError("pair response was lost")
        return {
            "token": "new-device-token",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            **claim_pair_fields(module),
            "device": {"id": "device-1"},
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "127.0.0.1")
    request = module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test", pairing_code="PAIR-RETRY",
    )
    with pytest.raises(module.HTTPException):
        module.pair_threadhall(request)
    bootstrap = module.read_threadhall_history_envelope(
        module.THREADHALL_COMMAND_HISTORY_FILE, allow_bootstrap=True,
    )
    assert bootstrap["partitions"] == {}
    assert module.load_threadhall_config()["history_bootstrap_epoch"] == bootstrap["bootstrap"]["epoch"]
    with pytest.raises(module.HTTPException):
        module.read_threadhall_history_envelope(module.THREADHALL_COMMAND_HISTORY_FILE)

    status = module.pair_threadhall(request)
    assert status["paired"] is True
    assert seen_devices == [bootstrap["bootstrap"]["device_uuid"]] * 2
    history = module.read_threadhall_history_envelope(module.THREADHALL_COMMAND_HISTORY_FILE)
    assert len(history["partitions"]) == 1
    assert "bootstrap" not in history
    assert module.load_threadhall_config()["history_bootstrap_epoch"] == ""


def test_empty_bootstrap_without_matching_pending_pair_is_rejected(module, monkeypatch):
    module.save_threadhall_history_bootstrap("00000000-0000-4000-8000-000000000001")
    module.save_threadhall_config({"enabled": False, "device_uuid": "different-controller"})
    monkeypatch.setattr(module, "threadhall_request", lambda *a, **kw: pytest.fail("invalid bootstrap must block pairing"))
    with pytest.raises(module.HTTPException) as error:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test", pairing_code="PAIR-MISMATCH",
        ))
    assert error.value.status_code == 503


def test_first_pair_can_resume_bootstrap_saved_before_pending_config(module, monkeypatch):
    device_uuid = "00000000-0000-4000-8000-000000000002"
    bootstrap = module.save_threadhall_history_bootstrap(device_uuid)
    assert not module.THREADHALL_CONFIG_FILE.exists()
    seen = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        seen.append(payload["device_uuid"])
        return {
            "token": "new-device-token",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            **claim_pair_fields(module),
            "device": {"id": "device-1"},
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "127.0.0.1")
    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test", pairing_code="PAIR-BOOTSTRAP",
    ))
    assert seen == [device_uuid]
    assert module.load_threadhall_config()["history_bootstrap_epoch"] == ""


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
    retained = json.loads(module.THREADHALL_COMMAND_HISTORY_FILE.read_text())
    assert any(
        partition["entries"].get("command-1", {}).get("pending_ack") is True
        for partition in retained["partitions"].values()
    )
    assert module.load_all_schedules() == {
        "active": "Local Safety",
        "schedules": {"Local Safety": []},
    }


def test_empty_unbound_history_allows_only_authenticated_stop_poll_and_blocks_repair(module, monkeypatch):
    save_threadhall_connection(module, {})
    module.THREADHALL_COMMAND_HISTORY_FILE.write_text("{}")
    requests = []
    stopped = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append(path)
        assert path == "api/pibells/v1/sync"
        assert payload["telemetry"]["stop_only_poll"] is True
        return {
            "protocol_version": 1,
            "device": {"id": "device-1"},
            "commands": [
                {"id": "urgent-stop", "type": "STOP_AUDIO", "payload": {}},
                {"id": "untrusted-play", "type": "PLAY_SOUND", "payload": {"sound_key": "passing"}},
            ],
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: pytest.fail("No untrusted command may run"))

    with pytest.raises(module.HTTPException) as sync_error:
        module.sync_threadhall_once()
    with pytest.raises(module.HTTPException) as pair_error:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test",
            pairing_code="PAIR-WITHOUT-HISTORY",
        ))

    assert sync_error.value.status_code == 503
    assert pair_error.value.status_code == 503
    assert requests == ["api/pibells/v1/sync"]
    assert stopped == [True]
    assert module.THREADHALL_COMMAND_HISTORY_FILE.read_text() == "{}"


def test_nonempty_flat_history_requires_reconciliation_without_rewriting(module, monkeypatch):
    save_threadhall_connection(module, {})
    old_receipts = {
        "already-played": {
            "status": "acknowledged",
            "summary": "Prior physical action",
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": True,
        }
    }
    original = json.dumps(old_receipts).encode()
    module.THREADHALL_COMMAND_HISTORY_FILE.write_bytes(original)
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: pytest.fail("Incomplete older ledger must block network"),
    )

    with pytest.raises(module.HTTPException) as read_error:
        module.load_threadhall_command_history()
    with pytest.raises(module.HTTPException) as pair_error:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://threadhall.example.test",
            pairing_code="PAIR-INCOMPLETE-HISTORY",
        ))
    assert read_error.value.status_code == 503
    assert pair_error.value.status_code == 503
    assert module.THREADHALL_COMMAND_HISTORY_FILE.read_bytes() == original


def test_url_alias_for_same_authority_cannot_create_empty_history(module, monkeypatch):
    saved = save_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "already-played": {
            "status": "acknowledged",
            "summary": "Prior physical action",
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": False,
        }
    })
    original = module.THREADHALL_COMMAND_HISTORY_FILE.read_bytes()
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "token": "alias-token",
            **claim_pair_fields(module),
            "authority": saved["authority"],
            "device": {"id": saved["authority"]["device_id"]},
        },
    )

    with pytest.raises(module.HTTPException) as pair_error:
        module.pair_threadhall(module.ThreadhallPairRequest(
            base_url="https://THREADHALL.example.test:443",
            pairing_code="PAIR-URL-ALIAS",
        ))
    assert pair_error.value.status_code == 503
    assert module.THREADHALL_COMMAND_HISTORY_FILE.read_bytes() == original
    assert module.load_threadhall_config()["token"] == ""


def test_receipts_remain_bound_to_authority_across_unpair_and_repair(module, monkeypatch):
    first = save_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "physical-command": {
            "status": "acknowledged",
            "summary": "Already executed",
            "occurred_at": "2026-09-05T12:00:00+00:00",
            "pending_ack": True,
        }
    })
    module.unpair_threadhall()
    assert module.load_threadhall_command_history() == {}

    def pair_response(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        return {
            "token": "replacement-token",
            **claim_pair_fields(module),
            "authority": first["authority"],
            "device": {"id": first["authority"]["device_id"]},
        }

    monkeypatch.setattr(module, "threadhall_request", pair_response)
    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test",
        pairing_code="PAIR-SAME-AUTHORITY",
    ))
    assert module.load_threadhall_command_history()["physical-command"]["pending_ack"] is True

    module.unpair_threadhall()
    second_authority = {**first["authority"], "school_id": "another-school"}
    monkeypatch.setattr(
        module,
        "threadhall_request",
        lambda *args, **kwargs: {
            "token": "different-authority-token",
            **claim_pair_fields(module),
            "authority": second_authority,
            "device": {"id": second_authority["device_id"]},
        },
    )
    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test",
        pairing_code="PAIR-OTHER-AUTHORITY",
    ))
    assert module.load_threadhall_command_history() == {}
    retained = json.loads(module.THREADHALL_COMMAND_HISTORY_FILE.read_text())
    assert len(retained["partitions"]) == 2
    assert any("physical-command" in partition["entries"] for partition in retained["partitions"].values())


def test_pair_network_wait_allows_schedule_read_and_unpair_to_fence_response(
    module, monkeypatch,
):
    save_threadhall_connection(module, {})
    request_started = threading.Event()
    release_request = threading.Event()
    schedule_read_finished = threading.Event()
    results = []

    def blocked_pair(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        request_started.set()
        assert release_request.wait(3)
        return {
            "token": "late-token",
            **claim_pair_fields(module),
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            "device": {"id": "device-1"},
        }

    def run_pair():
        try:
            results.append(module.pair_threadhall(module.ThreadhallPairRequest(
                base_url="https://threadhall.example.test",
                pairing_code="PAIR-LATE-RESPONSE",
            )))
        except BaseException as exc:
            results.append(exc)

    monkeypatch.setattr(module, "threadhall_request", blocked_pair)
    pair_worker = threading.Thread(target=run_pair, daemon=True)
    pair_worker.start()
    try:
        assert request_started.wait(1)
        read_worker = threading.Thread(
            target=lambda: (module.resolve_effective_schedule(), schedule_read_finished.set()),
            daemon=True,
        )
        read_worker.start()
        assert schedule_read_finished.wait(1), "Pair network wait blocked local schedule read"
        module.unpair_threadhall()
    finally:
        release_request.set()
        pair_worker.join(timeout=2)
    assert not pair_worker.is_alive()
    assert len(results) == 1
    assert isinstance(results[0], module.HTTPException)
    assert results[0].status_code == 409
    assert module.load_threadhall_config()["token"] == ""


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
    retained = json.loads(module.THREADHALL_COMMAND_HISTORY_FILE.read_text())
    assert any(
        partition["entries"].get("command-1", {}).get("pending_ack") is True
        for partition in retained["partitions"].values()
    )
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
                **claim_pair_fields(module),
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
                "protocol_version": 2,
                "execution_claim_v1": True,
                "schedules": [],
                "calendar": [],
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("7")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "7"):
            assert payload == {"protocol_version": 2, "device_uuid": "local-box"}
            return claim_grant("7", payload={"sound_key": "passing"})
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
    assert acknowledgements[0]["claim_id"] == "00000000-0000-4000-8000-000000000001"
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
            **claim_pair_fields(module),
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
            **claim_pair_fields(module),
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
    save_claim_threadhall_connection(module, {
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
            assert payload["protocol_version"] == 2
            assert payload["capabilities"]["execution_claim_v1"] is True
            assert payload["telemetry"]["active_schedule"] == "Default"
            assert payload["telemetry"]["outputs"][0]["key"] == "local"
            assert payload["capabilities"]["targeted_outputs"] is True
            assert payload["local_url"] == "http://10.80.2.42"
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
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
                "commands": [claim_sync_offer("7", "TEST_OUTPUT")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "7"):
            return claim_grant("7", "TEST_OUTPUT", {"sound_key": "passing", "label": "Test Bell"})
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
    assert acknowledgements[0]["claim_id"] == "00000000-0000-4000-8000-000000000001"
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


def test_pre_upgrade_pairing_without_protocol_stays_v1_and_can_stop(module, monkeypatch):
    config = {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "older-device-token",
        "device_uuid": "older-controller",
        "authority": {
            "district_id": "district-1",
            "school_id": "school-1",
            "device_id": "device-1",
        },
    }
    module.THREADHALL_CONFIG_FILE.write_text(json.dumps(config))
    loaded = module.load_threadhall_config()
    module.initialize_threadhall_command_history(loaded, fresh_install=True)
    assert loaded["protocol_version"] == 1
    normal_payload = module.threadhall_sync_payload(loaded)
    assert normal_payload["protocol_version"] == 1
    assert "stop_only_poll" not in normal_payload["telemetry"]
    assert module.safe_threadhall_status()["execution_claim_v1"] is False

    requests = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append((path, payload))
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 1,
                "device": {"id": "device-1"},
                "commands": [{"id": "older-stop", "type": "STOP_AUDIO", "payload": {}}],
            }
        return {"ok": True}

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    module.sync_threadhall_once()
    assert any(path.endswith("older-stop/ack") for path, _ in requests)


def test_threadhall_transport_rejects_redirect_and_plaintext_remote_url(module, monkeypatch):
    assert module.normalize_threadhall_base_url("http://localhost:3000/") == "http://localhost:3000"
    assert module.normalize_threadhall_base_url("http://127.0.0.1:3000") == "http://127.0.0.1:3000"
    with pytest.raises(module.HTTPException, match="HTTPS"):
        module.normalize_threadhall_base_url("http://district.example.test")
    with pytest.raises(module.HTTPException):
        module.normalize_threadhall_base_url("https://user:password@threadhall.example.test")

    requests = []

    class RedirectingOpener:
        def open(self, request, timeout):
            requests.append(request)
            raise module.urllib.error.HTTPError(
                request.full_url, 302, "Found",
                {"Location": "https://untrusted.example.test/collect"}, io.BytesIO(b""),
            )

    def fake_build_opener(*handlers):
        assert any(isinstance(handler, module.ThreadhallRejectRedirect) for handler in handlers)
        return RedirectingOpener()

    monkeypatch.setattr(module.urllib.request, "build_opener", fake_build_opener)
    with pytest.raises(module.HTTPException, match="302"):
        module.threadhall_request(
            {"base_url": "https://threadhall.example.test", "token": "device-secret"},
            "api/pibells/v1/sync", method="POST", payload={"protocol_version": 1},
        )
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer device-secret"
    assert module.ThreadhallRejectRedirect().redirect_request(None, None, 302, "Found", {}, "https://untrusted.example.test") is None


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


def test_threadhall_history_retains_pending_and_completed_outcomes(module):
    save_threadhall_connection(module, {})
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
    assert len(history) == 502
    assert set(history) == set(pending) | {"completed"}


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
    save_claim_threadhall_connection(module, {
        "enabled": True,
        "base_url": "https://threadhall.example.test",
        "token": "pbv1.device-1.test-token-value",
        "device_uuid": "local-box",
    })
    played = []
    acknowledgements = []
    claims = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "schedule_revision": "",
                "schedules": [],
                "calendar": [],
                "commands": [claim_sync_offer("command-1")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "command-1"):
            claims.append(payload)
            return claim_grant("command-1", payload={"sound_key": "passing"})
        if path == "api/pibells/v1/commands/command-1/ack":
            acknowledgements.append(payload)
            if len(acknowledgements) == 1:
                raise module.HTTPException(status_code=502, detail="temporary callback failure")
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "trigger_bell", lambda sound_file, loop=False: played.append((sound_file, loop)))

    module.sync_threadhall_once()
    module.sync_threadhall_once()

    assert played == [("bell-passing-classic.mp3", False)]
    assert len(claims) == 1
    assert len(acknowledgements) == 2
    assert acknowledgements[1] == acknowledgements[0]
    assert module.load_threadhall_command_history()["command-1"]["status"] == "acknowledged"


def test_v2_lost_claim_response_never_executes_or_retries(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    claims = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("lost-response")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "lost-response"):
            claims.append(payload)
            raise module.HTTPException(status_code=502, detail="claim response was lost")
        pytest.fail("An unknown claim must not be acknowledged")

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: pytest.fail("No grant was received"))
    module.sync_threadhall_once()
    module.sync_threadhall_once()

    assert claims == [{"protocol_version": 2, "device_uuid": "local-box"}]
    receipt = module.load_threadhall_command_history()["lost-response"]
    assert receipt["claim_state"] == "requested"
    assert receipt["pending_ack"] is False
    assert "operator reconciliation" in module.safe_threadhall_status()["last_error"]


def test_directory_fsync_failure_prevents_claim_http(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    claims = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("fsync-before-claim")],
            }
        claims.append(path)
        return claim_grant("fsync-before-claim")

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    _, config, identity, _, offers = module._sync_threadhall_once_before_acks()
    original_fsync = module.os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(module.os.fstat(fd).st_mode):
            raise OSError("directory fsync unavailable")
        return original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync unavailable"):
        module.process_threadhall_claim_offers(config, identity, offers)
    assert claims == []


def test_directory_fsync_failure_after_grant_never_replays_command(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    claims = []
    fail_directory = [False]
    original_fsync = module.os.fsync

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("fsync-after-grant")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "fsync-after-grant"):
            claims.append(path)
            fail_directory[0] = True
            return claim_grant("fsync-after-grant", payload={"sound_key": "passing"})
        pytest.fail("An unknown grant outcome must not be acknowledged")

    def maybe_fail_directory_fsync(fd):
        if fail_directory[0] and stat.S_ISDIR(module.os.fstat(fd).st_mode):
            raise OSError("directory fsync unavailable")
        return original_fsync(fd)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: pytest.fail("No durable pre-action receipt"))
    monkeypatch.setattr(module.os, "fsync", maybe_fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync unavailable"):
        module.sync_threadhall_once()
    monkeypatch.setattr(module.os, "fsync", original_fsync)
    module.sync_threadhall_once()
    assert len(claims) == 1
    assert module.load_threadhall_command_history()["fsync-after-grant"]["pending_ack"] is False


def test_v2_mismatched_claim_response_never_executes(module, monkeypatch):
    save_claim_threadhall_connection(module, {})

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("offered-command")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "offered-command"):
            return claim_grant("another-command", payload={"sound_key": "passing"})
        pytest.fail("A mismatched grant must not be acknowledged")

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: pytest.fail("Mismatched grant"))
    module.sync_threadhall_once()

    receipt = module.load_threadhall_command_history()["offered-command"]
    assert receipt["claim_state"] == "requested"
    assert receipt["pending_ack"] is False


def test_v2_unpair_during_claim_discards_late_execution_grant(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    claim_started = threading.Event()
    release_claim = threading.Event()
    executed = []
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("late-grant")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "late-grant"):
            claim_started.set()
            assert release_claim.wait(3)
            return claim_grant("late-grant", payload={"sound_key": "passing"})
        pytest.fail("A stale grant must not be acknowledged")

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: executed.append(command))
    worker = threading.Thread(target=run_sync, daemon=True)
    worker.start()
    try:
        assert claim_started.wait(1)
        module.unpair_threadhall()
    finally:
        release_claim.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert executed == []
    retained = json.loads(module.THREADHALL_COMMAND_HISTORY_FILE.read_text())
    assert any(
        partition["entries"].get("late-grant", {}).get("claim_state") == "claimed_after_authority_change"
        for partition in retained["partitions"].values()
    )


@pytest.mark.parametrize("stop_source", ["remote", "local"])
def test_valid_claim_grant_after_stop_never_starts_playback(module, monkeypatch, stop_source):
    save_claim_threadhall_connection(module, {})
    claim_started = threading.Event()
    release_claim = threading.Event()
    sync_count = 0
    acknowledgements = []
    executed = []
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        nonlocal sync_count
        if path == "api/pibells/v1/sync":
            sync_count += 1
            commands = [claim_sync_offer("delayed-play")] if sync_count == 1 else (
                [{"id": "ordered-stop", "type": "STOP_AUDIO", "payload": {}}]
                if stop_source == "remote" and sync_count == 2 else []
            )
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": commands,
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "delayed-play"):
            claim_started.set()
            assert release_claim.wait(3)
            return claim_grant("delayed-play", payload={"sound_key": "passing"})
        if path == "api/pibells/v1/commands/ordered-stop/ack":
            acknowledgements.append(payload)
            return {"ok": True}
        pytest.fail(f"Unexpected acknowledgement or request: {path}")

    original_run = module.run_threadhall_command

    def run_command(command):
        if command.get("type") == "STOP_AUDIO":
            return original_run(command)
        executed.append(command)
        return "played"

    def run_first_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", run_command)
    worker = threading.Thread(target=run_first_sync, daemon=True)
    worker.start()
    try:
        assert claim_started.wait(1)
        if stop_source == "remote":
            module.sync_threadhall_once()
            assert [ack["status"] for ack in acknowledgements] == ["acknowledged"]
        else:
            assert module.stop_sound() == {"status": "stopped"}
        assert module.load_threadhall_config()["stop_epoch"] == 1
    finally:
        release_claim.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert executed == []
    receipt = module.load_threadhall_command_history()["delayed-play"]
    assert receipt["claim_state"] == "claimed_after_stop"
    assert receipt["claim_id"] == "00000000-0000-4000-8000-000000000001"
    assert receipt["pending_ack"] is False
    module.sync_threadhall_once()
    assert executed == []


@pytest.mark.parametrize("disable_source", ["unpair", "authority_clear"])
def test_failed_audio_stop_cannot_keep_old_claim_authority(module, monkeypatch, disable_source):
    original_config = save_claim_threadhall_connection(module, {})
    claim_started = threading.Event()
    release_claim = threading.Event()
    executed = []
    errors = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("claim-before-disable")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "claim-before-disable"):
            claim_started.set()
            assert release_claim.wait(3)
            return claim_grant("claim-before-disable", payload={"sound_key": "passing"})
        pytest.fail("A disabled authority must not ACK the old claim")

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: (_ for _ in ()).throw(RuntimeError("player resisted STOP")))
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: executed.append(command))
    worker = threading.Thread(target=run_sync, daemon=True)
    worker.start()
    try:
        assert claim_started.wait(1)
        with pytest.raises(module.HTTPException, match="reconciliation"):
            if disable_source == "unpair":
                module.unpair_threadhall()
            else:
                with module.threadhall_integration_lock:
                    module.clear_threadhall_authority_state(
                        module.load_threadhall_config(), "Authority mismatch",
                    )
        disabled = module.load_threadhall_config()
        assert disabled["enabled"] is False
        assert disabled["token"] == ""
    finally:
        release_claim.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert executed == []
    receipt = module.load_threadhall_command_history(original_config)["claim-before-disable"]
    assert receipt["claim_state"] == "claimed_after_authority_change"
    assert receipt["pending_ack"] is False


def test_failed_unpair_write_fences_managed_dispatch_and_sync(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    module.save_all_schedules({
        "active": "Threadhall - Regular",
        "schedules": {"Threadhall - Regular": []},
    })
    original_save = module.save_threadhall_config

    def fail_disabled_save(updates):
        if updates.get("enabled") is False:
            raise OSError("directory fsync failed")
        return original_save(updates)

    monkeypatch.setattr(module, "save_threadhall_config", fail_disabled_save)
    with pytest.raises(module.HTTPException, match="reconciliation"):
        module.unpair_threadhall()
    assert module.threadhall_authority_fence_failed is True
    assert module.load_threadhall_config()["enabled"] is True
    assert module.managed_schedule_clock_status()["reason"] == "threadhall_authority_reconciliation_required"

    calls = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        calls.append(path)
        assert path == "api/pibells/v1/sync"
        assert set(payload) == {"protocol_version", "device_uuid", "sent_at", "capabilities", "telemetry"}
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [claim_sync_offer("must-not-claim")],
        }

    monkeypatch.setattr(module, "threadhall_sync_payload", lambda config: pytest.fail("fenced sync must use STOP-only payload"))
    monkeypatch.setattr(module, "threadhall_request", fake_request)
    status = module.sync_threadhall_once()
    assert calls == ["api/pibells/v1/sync"]
    assert status["enabled"] is False
    assert module.threadhall_authority_fence_failed is True
    monkeypatch.setattr(module, "load_all_schedules", lambda: pytest.fail("managed dispatch must stop before schedule mutation"))
    module.dispatch_due_events()
    assert module.next_event_payload() is None


def test_minimal_sync_poll_can_stop_audio_after_dashboard_disk_failure(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    monkeypatch.setattr(
        module, "threadhall_sync_payload",
        lambda config: (_ for _ in ()).throw(OSError("schedule normalization failed")),
    )
    monkeypatch.setattr(module, "load_all_schedules", lambda: pytest.fail("degraded STOP poll must not load schedules"))
    monkeypatch.setattr(module, "apply_threadhall_schedules", lambda *a, **kw: pytest.fail("degraded poll must not publish"))
    stopped = []
    requests = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append((path, payload))
        assert path == "api/pibells/v1/sync"
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [
                {"id": "urgent-stop", "type": "STOP_AUDIO", "payload": {}},
                claim_sync_offer("must-not-claim"),
            ],
            "schedules": ["malformed schedule must be ignored in degraded mode"],
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    status = module.sync_threadhall_once()
    assert stopped == [True]
    assert len(requests) == 1
    assert set(requests[0][1]) == {"protocol_version", "device_uuid", "sent_at", "capabilities", "telemetry"}
    assert requests[0][1]["capabilities"]["execution_claim_v1"] is True
    assert requests[0][1]["telemetry"]["stop_only_poll"] is True
    assert status["enabled"] is False
    assert "only remote STOP" in status["last_error"]


def test_sync_poll_survives_secondary_error_persistence_failure(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    attempted = []
    slept = []

    class FinishedProbe(Exception):
        pass

    def fail_sync(*, acknowledgements_in_background=False):
        attempted.append(True)
        raise OSError("sync could not read disk")

    def fail_save(updates):
        raise OSError("error state fsync failed")

    def end_after_sleep(seconds):
        slept.append(seconds)
        raise FinishedProbe()

    monkeypatch.setattr(module, "sync_threadhall_once", fail_sync)
    monkeypatch.setattr(module, "save_threadhall_config", fail_save)
    monkeypatch.setattr(module.time, "sleep", end_after_sleep)
    with pytest.raises(FinishedProbe):
        module.threadhall_sync_loop()
    assert attempted == [True]
    assert len(slept) == 1


def test_local_stop_durably_holds_unseen_remote_play_until_repair(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    stopped = []
    requests = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    module.stop_sound()
    assert module.load_threadhall_config()["remote_command_hold"] is True
    assert module.safe_threadhall_status()["remote_commands_held"] is True

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append((path, payload))
        assert path == "api/pibells/v1/sync"
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [claim_sync_offer("unseen-old-play")],
        }

    monkeypatch.setattr(module, "threadhall_sync_payload", lambda config: pytest.fail("held sync must be STOP-only"))
    monkeypatch.setattr(module, "threadhall_request", fake_request)
    status = module.sync_threadhall_once()
    assert status["enabled"] is False
    assert len(requests) == 1
    assert requests[0][0] == "api/pibells/v1/sync"
    assert requests[0][1]["telemetry"]["stop_only_poll"] is True
    assert module.load_threadhall_command_history() == {}

    def pair_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        return {
            "token": "reconciled-token",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            **claim_pair_fields(module),
            "device": {"id": "device-1"},
        }

    monkeypatch.setattr(module, "threadhall_request", pair_request)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "127.0.0.1")
    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test", pairing_code="RECONCILE-STOP",
    ))
    assert module.load_threadhall_config()["remote_command_hold"] is False
    assert module.safe_threadhall_status()["remote_commands_held"] is False


@pytest.mark.parametrize("stop_source", ["local", "remote"])
def test_stop_during_schedule_planning_prevents_late_bell(module, monkeypatch, stop_source):
    if stop_source == "remote":
        save_threadhall_connection(module, {})
        monkeypatch.setattr(module, "threadhall_sync_payload", module.threadhall_stop_only_sync_payload)

        def fake_request(config, path, *, method="GET", payload=None, token=None):
            if path == "api/pibells/v1/sync":
                return {
                    "protocol_version": 1,
                    "device": {"id": "device-1"},
                    "commands": [{"id": "scheduled-stop", "type": "STOP_AUDIO", "payload": {}}],
                }
            assert path.endswith("scheduled-stop/ack")
            return {"ok": True}

        monkeypatch.setattr(module, "threadhall_request", fake_request)
    else:
        save_claim_threadhall_connection(module, {})
    planned = threading.Event()
    resume = threading.Event()
    errors = []
    played = []
    instant = datetime.now().replace(second=0, microsecond=0)
    event = SimpleNamespace(
        enabled=True,
        day=instant.weekday(),
        time=instant.time(),
        sound_file="passing.mp3",
        output_keys=None,
        id="due-now",
    )

    def delayed_schedule(now):
        planned.set()
        assert resume.wait(2)
        return [event]

    def run_dispatch():
        try:
            module.dispatch_due_events(instant)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "load_schedule", delayed_schedule)
    monkeypatch.setattr(module, "trigger_bell", lambda *a, **kw: played.append(True))
    monkeypatch.setattr(module, "stop_loops", lambda: None)
    worker = threading.Thread(target=run_dispatch, daemon=True)
    worker.start()
    try:
        assert planned.wait(1)
        if stop_source == "local":
            module.stop_sound()
        else:
            module.sync_threadhall_once()
    finally:
        resume.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert played == []


def test_corrupt_pairing_file_stops_audio_and_uses_cached_stop_only_identity(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    stopped = []
    requests = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    module.THREADHALL_CONFIG_FILE.write_text("{corrupt")
    loaded = module.load_threadhall_config()
    assert loaded["token"] == "test-device-token"
    assert stopped == [True]
    assert module.threadhall_config_integrity_failed is True
    assert module.safe_threadhall_status()["enabled"] is False
    with pytest.raises(module.HTTPException, match="pairing file"):
        module.trigger_bell("passing.mp3")

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append((path, payload))
        assert path == "api/pibells/v1/sync"
        assert config["token"] == "test-device-token"
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [
                {"id": "stop-after-corruption", "type": "STOP_AUDIO", "payload": {}},
                claim_sync_offer("blocked-play"),
            ],
        }

    monkeypatch.setattr(module, "threadhall_sync_payload", lambda config: pytest.fail("corrupt config must be STOP-only"))
    monkeypatch.setattr(module, "threadhall_request", fake_request)
    with pytest.raises(module.HTTPException) as error:
        module.sync_threadhall_once()
    assert error.value.status_code == 503
    assert len(stopped) >= 2
    assert len(requests) == 1
    assert requests[0][1]["telemetry"]["stop_only_poll"] is True


def test_unreadable_pairing_without_cached_token_fails_closed(module, monkeypatch):
    module.THREADHALL_CONFIG_FILE.mkdir()
    stopped = []
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    with pytest.raises(module.HTTPException) as error:
        module.load_threadhall_config()
    assert error.value.status_code == 503
    assert stopped == [True]
    assert module.threadhall_config_integrity_failed is True


def test_stop_between_offer_and_claim_dispatch_blocks_old_offer(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    requests = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append(path)
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("queued-before-stop")],
            }
        pytest.fail("STOP must prevent an older offer from seeking a claim")

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    _, config, identity, _, offers = module._sync_threadhall_once_before_acks()
    assert len(offers) == 1
    module.stop_sound()
    assert module.process_threadhall_claim_offers(config, identity, offers) == []
    assert requests == ["api/pibells/v1/sync"]
    receipt = module.load_threadhall_command_history()["queued-before-stop"]
    assert receipt["claim_state"] == "superseded_before_claim"
    assert receipt["pending_ack"] is False


def test_local_stop_during_sync_http_suppresses_late_play_offer(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    sync_started = threading.Event()
    release_sync = threading.Event()
    errors = []
    requests = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append(path)
        if path == "api/pibells/v1/sync":
            sync_started.set()
            assert release_sync.wait(3)
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("stale-http-offer")],
            }
        pytest.fail("A pre-STOP sync response must not seek a claim or ACK")

    def run_sync():
        try:
            module.sync_threadhall_once()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    worker = threading.Thread(target=run_sync, daemon=True)
    worker.start()
    try:
        assert sync_started.wait(1)
        assert module.stop_sound() == {"status": "stopped"}
    finally:
        release_sync.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert requests == ["api/pibells/v1/sync"]
    assert module.load_threadhall_command_history() == {}
    assert module.load_threadhall_config()["remote_command_hold"] is True


def test_remote_stop_runs_before_broken_receipt_disk_blocks_sync(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    stopped = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [{"id": "urgent-stop", "type": "STOP_AUDIO", "payload": {}}],
            }
        acknowledgements.append(path)
        return {"ok": True}

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(
        module, "load_threadhall_command_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt disk unavailable")),
    )

    with pytest.raises(module.HTTPException) as error:
        module.sync_threadhall_once()
    assert error.value.status_code == 503
    assert stopped == [True]
    assert acknowledgements == []


def test_remote_stop_precedes_invalid_schedule_bundle(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    stopped = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/sync"
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "schedules": [42],
            "commands": [{"id": "stop-before-invalid-bundle", "type": "STOP_AUDIO", "payload": {}}],
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    with pytest.raises(module.HTTPException, match="invalid schedules"):
        module.sync_threadhall_once()
    assert stopped == [True]


def test_remote_stop_fence_fsync_failure_still_stops_without_ack(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    stopped = []
    acknowledgements = []
    original_fsync = module.os.fsync

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [{"id": "durability-stop", "type": "STOP_AUDIO", "payload": {}}],
            }
        acknowledgements.append(path)
        return {"ok": True}

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(module.os.fstat(fd).st_mode):
            raise OSError("directory fsync unavailable")
        return original_fsync(fd)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(module.os, "fsync", fail_directory_fsync)
    with pytest.raises(module.HTTPException, match="ordering state could not be saved"):
        module.sync_threadhall_once()
    assert stopped == [True]
    assert acknowledgements == []
    assert module.threadhall_stop_runtime_generation == 1


def test_background_claim_wait_does_not_delay_later_remote_stop(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    claim_started = threading.Event()
    release_claim = threading.Event()
    stopped = []
    sync_count = 0

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        nonlocal sync_count
        if path == "api/pibells/v1/sync":
            sync_count += 1
            commands = (
                [claim_sync_offer("slow-claim")]
                if sync_count == 1
                else [{"id": "stop-during-claim", "type": "STOP_AUDIO", "payload": {}}]
            )
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": commands,
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "slow-claim"):
            claim_started.set()
            assert release_claim.wait(3)
            return {"ok": False, "execute": False}
        if path == "api/pibells/v1/commands/stop-during-claim/ack":
            assert "claim_id" not in payload
            return {"ok": True}
        pytest.fail(path)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: module.stop_loops() or "Stopped")
    try:
        module.sync_threadhall_once(acknowledgements_in_background=True)
        assert claim_started.wait(1)
        module.sync_threadhall_once()
        assert stopped == [True]
    finally:
        release_claim.set()
        assert module.threadhall_claim_worker_lock.acquire(timeout=2)
        module.threadhall_claim_worker_lock.release()


def test_v2_partial_action_error_keeps_physical_outcome_unknown(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("partial-output")],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "partial-output"):
            return claim_grant("partial-output", payload={"sound_key": "passing"})
        acknowledgements.append(payload)
        return {"ok": True}

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "run_threadhall_command", lambda command: (_ for _ in ()).throw(RuntimeError("one output failed")))
    module.sync_threadhall_once()
    module.sync_threadhall_once()

    receipt = module.load_threadhall_command_history()["partial-output"]
    assert receipt["claim_state"] == "execution_unknown"
    assert receipt["pending_ack"] is False
    assert receipt["claim_id"] == "00000000-0000-4000-8000-000000000001"
    assert acknowledgements == []


def test_v1_non_stop_is_quarantined_while_stop_remains_available(module, monkeypatch):
    save_threadhall_connection(module, {})
    stopped = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            assert payload["protocol_version"] == 1
            return {
                "protocol_version": 1,
                "device": {"id": "device-1"},
                "commands": [
                    {"id": "unsafe-play", "type": "PLAY_SOUND", "payload": {"sound_key": "passing"}},
                    {"id": "urgent-stop", "type": "STOP_AUDIO", "payload": {}},
                ],
            }
        acknowledgements.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(module, "trigger_bell", lambda *args, **kwargs: pytest.fail("v1 play must not run"))
    module.sync_threadhall_once()

    assert stopped == [True]
    assert module.load_threadhall_command_history()["unsafe-play"]["status"] == "failed"
    assert len(acknowledgements) == 2


def test_threadhall_records_unknown_outcome_before_physical_execution(
    module,
    monkeypatch,
):
    save_claim_threadhall_connection(module, {})
    sync_count = 0
    execution_attempts = []
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        nonlocal sync_count
        if path == "api/pibells/v1/sync":
            sync_count += 1
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("interrupted-command")] if sync_count == 1 else [],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "interrupted-command"):
            return claim_grant("interrupted-command", payload={"sound_key": "passing"})
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
    assert pending["pending_ack"] is False
    assert pending["claim_state"] == "execution_unknown"
    assert pending["claim_id"] == "00000000-0000-4000-8000-000000000001"

    module.sync_threadhall_once()

    assert execution_attempts == ["interrupted-command"]
    assert acknowledgements == []
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
    save_claim_threadhall_connection(module, {})
    attempts = []
    command = claim_sync_offer("possibly-played-command")

    def first_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [command],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", command["id"]):
            return claim_grant(command["id"], payload={"sound_key": "passing"})
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
    requests = []
    stopped = []

    def stop_only_request(config, path, *, method="GET", payload=None, token=None):
        requests.append(path)
        assert path == "api/pibells/v1/sync"
        assert config["token"] == "test-device-token"
        assert payload["telemetry"]["stop_only_poll"] is True
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [
                {"id": "stop-despite-history", "type": "STOP_AUDIO", "payload": {}},
                command,
            ],
        }

    monkeypatch.setattr(restarted, "threadhall_request", stop_only_request)
    monkeypatch.setattr(restarted, "stop_loops", lambda: stopped.append(True))
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
    assert "physical commands are paused" in str(exc_info.value.detail).lower()
    assert requests == ["api/pibells/v1/sync"]
    assert stopped == [True]
    assert attempts == [command["id"]]
    assert (history_path.read_bytes() if history_path.exists() else None) == damaged_bytes


def test_threadhall_retries_a_durable_outcome_after_the_command_is_withdrawn(
    module,
    monkeypatch,
):
    (module.AUDIO_DIR / "bell-passing-classic.mp3").write_bytes(b"fake mp3")
    save_claim_threadhall_connection(module, {
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
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [claim_sync_offer("command-withdrawn")] if sync_count == 1 else [],
            }
        if path == module.THREADHALL_CLAIM_URL_TEMPLATE.replace("{command_id}", "command-withdrawn"):
            return claim_grant("command-withdrawn", payload={"sound_key": "passing"})
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

    def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
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


def test_stop_audio_terminates_ordinary_local_and_barix_playback(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    module.save_devices([module.Device(ip="192.168.1.10", name="Gym", port=3030)])
    processes = []

    class FakeProcess:
        stderr = None

        def __init__(self):
            self.done = threading.Event()
            self.terminated = False

        def poll(self):
            return 0 if self.done.is_set() else None

        def terminate(self):
            self.terminated = True
            self.done.set()

        def kill(self):
            self.done.set()

        def wait(self, timeout=None):
            if not self.done.wait(timeout):
                raise module.subprocess.TimeoutExpired("fake audio", timeout)
            return 0

    def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
        assert kwargs["start_new_session"] is True
        proc = FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    module.trigger_bell("bell.mp3")
    assert len(module.active_audio_processes) == 2

    summary = module.run_threadhall_command({"type": "STOP_AUDIO", "payload": {}})

    assert summary == "Stopped active audio"
    assert all(proc.terminated for proc in processes)
    assert module.active_audio_processes == []


def test_reaper_drops_group_handle_after_child_group_exits(module, monkeypatch):
    (module.AUDIO_DIR / "bell.mp3").write_bytes(b"fake mp3")
    reaper_waited = threading.Event()
    group_live = [True]

    class FakeProcess:
        stderr = None
        pid = 34567

        def poll(self):
            return 0

        def wait(self, timeout=None):
            reaper_waited.set()
            return 0

    proc = FakeProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(module, "audio_group_alive", lambda group_id: group_live[0])
    module.trigger_bell("bell.mp3", output_keys=["local"])
    assert reaper_waited.wait(1)
    assert proc in module.active_audio_processes
    assert module.audio_process_groups[id(proc)] == proc.pid

    group_live[0] = False
    deadline = module.time.monotonic() + 1
    while proc in module.active_audio_processes and module.time.monotonic() < deadline:
        module.time.sleep(0.02)
    assert proc not in module.active_audio_processes
    assert id(proc) not in module.audio_process_groups


def test_unverified_stop_has_no_ack_and_retries_on_server_reoffer(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    acknowledgements = []

    class ResistantProcess:
        def __init__(self):
            self.done = threading.Event()
            self.allow_stop = False
            self.attempts = 0

        def poll(self):
            return 0 if self.done.is_set() else None

        def terminate(self):
            self.attempts += 1
            if self.allow_stop:
                self.done.set()

        def kill(self):
            self.attempts += 1
            if self.allow_stop:
                self.done.set()

        def wait(self, timeout=None):
            if not self.done.wait(timeout):
                raise module.subprocess.TimeoutExpired("resistant audio", timeout)
            return 0

    proc = ResistantProcess()
    module.active_audio_processes.append(proc)

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [{"id": "stop-retry", "type": "STOP_AUDIO", "payload": {}}],
            }
        if path == "api/pibells/v1/commands/stop-retry/ack":
            acknowledgements.append(payload)
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    module.sync_threadhall_once()
    unresolved = module.load_threadhall_command_history()["stop-retry"]
    assert unresolved["stop_retry_pending"] is True
    assert unresolved["pending_ack"] is False
    assert module.load_threadhall_config()["unverified_stop_hold"] is True
    assert acknowledgements == []
    assert proc in module.active_audio_processes

    proc.allow_stop = True
    module.sync_threadhall_once()
    assert proc.attempts >= 2
    assert module.active_audio_processes == []
    assert [ack["status"] for ack in acknowledgements] == ["acknowledged"]
    assert module.load_threadhall_config()["unverified_stop_hold"] is False


def test_unverified_stop_blocks_later_play_without_reoffer_after_restart(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "unverified-stop": {
            "status": "failed",
            "summary": "Physical termination could not be verified",
            "detail": {"termination_unverified": True},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "pending_ack": False,
            "stop_retry_pending": True,
        },
    })
    # The receipt alone must retain the hold even if the separate config
    # write failed before a controller restart.
    module.save_threadhall_config({"unverified_stop_hold": False})
    restarted = importlib.reload(module)
    requests = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append(path)
        assert path == "api/pibells/v1/sync", "No claim or ACK is safe while STOP is unresolved"
        return {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "commands": [claim_sync_offer("later-play")],
        }

    monkeypatch.setattr(restarted, "threadhall_request", fake_request)
    monkeypatch.setattr(restarted, "run_threadhall_command", lambda command: pytest.fail("Unverified STOP must block playback"))
    restarted.sync_threadhall_once()
    assert requests == ["api/pibells/v1/sync"]
    assert restarted.load_threadhall_command_history()["later-play"]["claim_state"] == "held_after_unverified_stop"
    assert restarted.safe_threadhall_status()["enabled"] is False


def test_expired_stop_with_unverified_termination_remains_durable_hold(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    acknowledgements = []

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        if path == "api/pibells/v1/sync":
            return {
                "protocol_version": 2,
                "execution_claim_v1": True,
                "device": {"id": "device-1"},
                "commands": [{
                    "id": "expired-stop",
                    "type": "STOP_AUDIO",
                    "payload": {},
                    "expires_at": "2020-01-01T00:00:00+00:00",
                }],
            }
        acknowledgements.append(path)
        return {"ok": True}

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    monkeypatch.setattr(module, "stop_loops", lambda: (_ for _ in ()).throw(RuntimeError("speaker still active")))
    module.sync_threadhall_once()
    receipt = module.load_threadhall_command_history()["expired-stop"]
    assert receipt["stop_retry_pending"] is True
    assert receipt["pending_ack"] is False
    assert module.load_threadhall_config()["unverified_stop_hold"] is True
    assert acknowledgements == []


def test_verified_repair_pair_clears_unverified_stop_receipts(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    module.save_threadhall_command_history({
        "old-stop": {
            "status": "failed",
            "summary": "Termination unverified",
            "detail": {"termination_unverified": True},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "pending_ack": False,
            "stop_retry_pending": True,
        },
    })
    module.save_threadhall_config({"unverified_stop_hold": True})
    monkeypatch.setattr(module, "stop_loops", lambda: None)
    monkeypatch.setattr(module, "dashboard", lambda: {"active_schedule": "Default"})
    monkeypatch.setattr(module, "get_local_ip", lambda: "127.0.0.1")

    def fake_pair(config, path, *, method="GET", payload=None, token=None):
        assert path == "api/pibells/v1/pair"
        return {
            "token": "reconciled-token",
            "authority": {
                "district_id": "district-1",
                "school_id": "school-1",
                "device_id": "device-1",
            },
            **claim_pair_fields(module),
            "device": {"id": "device-1"},
        }

    monkeypatch.setattr(module, "threadhall_request", fake_pair)
    module.pair_threadhall(module.ThreadhallPairRequest(
        base_url="https://threadhall.example.test", pairing_code="RECONCILE-UNVERIFIED-STOP",
    ))
    assert module.load_threadhall_config()["unverified_stop_hold"] is False
    assert module.load_threadhall_command_history()["old-stop"]["stop_retry_pending"] is False
    assert module.safe_threadhall_status()["enabled"] is True


@pytest.mark.parametrize(
    ("requested_stop_only", "hold_code"),
    [
        (True, "PIBELLS_STOP_ONLY_POLL"),
        (False, "PIBELLS_CURRENT_SCHOOL_YEAR_INVALID"),
    ],
)
def test_server_held_v2_response_without_claim_capability_still_stops(
    module, monkeypatch, requested_stop_only, hold_code,
):
    save_claim_threadhall_connection(module, {})
    stopped = []
    requests = []
    if requested_stop_only:
        monkeypatch.setattr(
            module, "threadhall_sync_payload",
            lambda config: (_ for _ in ()).throw(OSError("dashboard unavailable")),
        )
    else:
        monkeypatch.setattr(module, "threadhall_sync_payload", lambda config: {"telemetry": {}})
    monkeypatch.setattr(module, "stop_loops", lambda: stopped.append(True))
    monkeypatch.setattr(
        module, "claim_and_execute_threadhall_offer",
        lambda *args, **kwargs: pytest.fail("A held response must not claim PLAY"),
    )

    def fake_request(config, path, *, method="GET", payload=None, token=None):
        requests.append((path, payload))
        assert path == "api/pibells/v1/sync"
        assert (payload["telemetry"].get("stop_only_poll") is True) is requested_stop_only
        return {
            "protocol_version": 2,
            "execution_claim_v1": False,
            "device": {"id": "device-1"},
            "schedule_publication": {"status": "HELD", "code": hold_code},
            "commands": [
                {"id": "safe-stop", "type": "STOP_AUDIO", "payload": {}},
                claim_sync_offer("must-not-claim"),
            ],
        }

    monkeypatch.setattr(module, "threadhall_request", fake_request)
    status = module.sync_threadhall_once()
    assert stopped == [True]
    assert status["enabled"] is False
    assert len(requests) == 1
    assert module.load_threadhall_command_history() == {}


def test_contradictory_server_stop_only_capability_never_claims_play(module, monkeypatch):
    save_claim_threadhall_connection(module, {})
    monkeypatch.setattr(module, "threadhall_sync_payload", lambda config: {"telemetry": {}})
    monkeypatch.setattr(
        module, "claim_and_execute_threadhall_offer",
        lambda *args, **kwargs: pytest.fail("Held publication cannot claim PLAY"),
    )
    monkeypatch.setattr(
        module, "threadhall_request",
        lambda *args, **kwargs: {
            "protocol_version": 2,
            "execution_claim_v1": True,
            "device": {"id": "device-1"},
            "schedule_publication": {
                "status": "HELD",
                "code": "PIBELLS_CURRENT_SCHOOL_YEAR_INVALID",
            },
            "commands": [claim_sync_offer("must-not-claim")],
        },
    )

    status = module.sync_threadhall_once()
    assert status["enabled"] is False
    assert status["execution_claim_v1"] is False
    assert module.load_threadhall_command_history() == {}


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
        lambda cmd, stdout=None, stderr=None, **kwargs: FailedProcess(),
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

    def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
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

    def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
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
        lambda cmd, stdout=None, stderr=None, **kwargs: commands.append(cmd) or FakeProcess(),
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
        lambda cmd, stdout=None, stderr=None, **kwargs: commands.append(cmd) or FakeProcess(),
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
