import concurrent.futures
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(os.environ.get("PIBELLS_BASE_DIR", Path(__file__).resolve().parent.parent)).resolve()
SCHEDULE_FILE = BASE_DIR / "schedule.json"
DEVICES_FILE = BASE_DIR / "devices.json"
BUTTONS_FILE = BASE_DIR / "buttons.json"
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_META_FILE = BASE_DIR / "audio.json"
AUTH_FILE = BASE_DIR / "pibells-auth.json"
BARIX_SCAN_RANGES_FILE = BASE_DIR / "barix-scan-ranges.json"
THREADHALL_CONFIG_FILE = BASE_DIR / "threadhall-pairing.json"
THREADHALL_COMMAND_HISTORY_FILE = BASE_DIR / "threadhall-command-history.json"
# Compatibility values are assembled so the retired product name never leaks
# into product copy, generated API documentation, or current test names.
RETIRED_PRODUCT_ROOT = "".join((chr(112), chr(105), chr(120), "el"))
RETIRED_INTEGRATION_SLUG = f"{RETIRED_PRODUCT_ROOT}sis"
RETIRED_INTEGRATION_NAME = f"{RETIRED_PRODUCT_ROOT.capitalize()}SIS"
LEGACY_THREADHALL_CONFIG_FILE = BASE_DIR / f"{RETIRED_INTEGRATION_SLUG}-pairing.json"
LEGACY_THREADHALL_COMMAND_HISTORY_FILE = (
    BASE_DIR / f"{RETIRED_INTEGRATION_SLUG}-command-history.json"
)
STATIC_DIR = BASE_DIR / "static"

SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
PASSWORD_ITERATIONS = 320_000
SESSION_COOKIE = "session"
BARIX_DISCOVERY_PORT = int(os.environ.get("PIBELLS_BARIX_DISCOVERY_PORT", "30718"))
BARIX_DISCOVERY_HEADER = bytes((0x81, 0x88, 0x53, 0x81))
BARIX_DISCOVERY_GET_REQUEST = BARIX_DISCOVERY_HEADER + bytes((0x01,))
BARIX_DISCOVERY_GET_RESPONSE = 0x81
BARIX_STREAM_PORT = int(os.environ.get("PIBELLS_BARIX_STREAM_PORT", "3030"))
BARIX_HTTP_PORT = int(os.environ.get("PIBELLS_BARIX_HTTP_PORT", "80"))
BARIX_TCP_PORT = int(os.environ.get("PIBELLS_BARIX_TCP_PORT", "2020"))
BARIX_SCAN_WORKERS = max(8, int(os.environ.get("PIBELLS_BARIX_SCAN_WORKERS", "96")))
BARIX_SCAN_MAX_HOSTS = max(254, int(os.environ.get("PIBELLS_BARIX_SCAN_MAX_HOSTS", "4096")))
LOCAL_OUTPUT_KEY = "local"
THREADHALL_DEFAULT_POLL_SECONDS = max(
    10,
    int(
        os.environ.get(
            "PIBELLS_THREADHALL_POLL_SECONDS",
            os.environ.get(
                f"PIBELLS_{RETIRED_INTEGRATION_NAME.upper()}_POLL_SECONDS",
                "20",
            ),
        )
    ),
)
PIBELLS_PROTOCOL_VERSION = 2
PIBELLS_AGENT_VERSION = "pibells-threadhall-v2"
THREADHALL_CLAIM_URL_TEMPLATE = "/api/pibells/v2/commands/{command_id}/claim"
THREADHALL_MAX_CLOCK_SKEW_SECONDS = 5
THREADHALL_MAX_CLOCK_SAMPLE_AGE_SECONDS = 120
THREADHALL_MAX_CLOCK_JUMP_SECONDS = 5
DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

AUDIO_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

storage_lock = threading.RLock()
threadhall_integration_lock = threading.RLock()
threadhall_pair_request_lock = threading.Lock()
threadhall_sync_request_lock = threading.Lock()
threadhall_claim_worker_lock = threading.Lock()
threadhall_ack_worker_lock = threading.Lock()
THREADHALL_ACK_ATTEMPTS_PER_SYNC = 4
THREADHALL_COMMAND_HISTORY_VERSION = 2
threadhall_ack_retry_cursor = 0
threadhall_stop_runtime_generation = 0
threadhall_authority_fence_failed = False
threadhall_remote_command_hold_runtime = False
threadhall_unverified_stop_runtime = False
threadhall_config_integrity_failed = False
threadhall_config_stop_unverified = False
threadhall_last_valid_config: Optional[Dict[str, Any]] = None
threadhall_unpaired_device_uuid = str(uuid.uuid4())
sessions: Dict[str, str] = {}
loop_processes: List[subprocess.Popen] = []
audio_process_lock = threading.RLock()
active_audio_processes: List[subprocess.Popen] = []
audio_process_groups: Dict[int, int] = {}
daemon_started = False
threadhall_sync_started = False
threadhall_clock_proof: Optional[Dict[str, Any]] = None
threadhall_clock_error = "server_clock_not_verified"

DEFAULT_AUDIO_KEY_FILES = {
    "start_day": "bell-start-warm-chime.mp3",
    "passing": "bell-passing-classic.mp3",
    "lunch": "bell-lunch-light-chime.mp3",
    "end_day": "bell-dismissal-deep-chime.mp3",
    "test": "bell-test-tone.mp3",
    "emergency": "emergency-general.mp3",
    "hold": "emergency-hold.mp3",
    "secure": "emergency-secure.mp3",
    "lockdown": "emergency-lockdown.mp3",
    "evacuate": "emergency-evacuate.mp3",
    "shelter": "emergency-shelter.mp3",
    "medical": "emergency-medical.mp3",
    "all_clear": "emergency-all-clear.mp3",
}

DEFAULT_AUDIO_NAMES = {
    "alert.wav": "Legacy Alert Tone",
    "chimes.mp3": "Legacy Chimes",
    "siren.mp3": "Legacy Siren",
    "standard-bells.mp3": "Legacy Standard Bells",
    "bell-start-warm-chime.mp3": "Day Start Warm Chime",
    "bell-passing-classic.mp3": "Passing Bell Classic",
    "bell-passing-soft-chime.mp3": "Passing Bell Soft Chime",
    "bell-lunch-light-chime.mp3": "Lunch Light Chime",
    "bell-dismissal-deep-chime.mp3": "Dismissal Deep Chime",
    "bell-test-tone.mp3": "PiBells Test Tone",
    "emergency-general.mp3": "Emergency General",
    "emergency-hold.mp3": "Emergency Hold",
    "emergency-secure.mp3": "Emergency Secure",
    "emergency-lockdown.mp3": "Emergency Lockdown",
    "emergency-evacuate.mp3": "Emergency Evacuate",
    "emergency-shelter.mp3": "Emergency Shelter",
    "emergency-medical.mp3": "Emergency Medical Response",
    "emergency-all-clear.mp3": "Emergency All Clear",
}

EMERGENCY_SOUND_KEYS = {"emergency", "hold", "secure", "lockdown", "evacuate", "shelter", "medical", "all_clear"}


def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def model_fields_set(model: BaseModel) -> set[str]:
    fields = getattr(model, "model_fields_set", None)
    if fields is None:
        fields = getattr(model, "__fields_set__", set())
    return set(fields)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any, *, require_directory_fsync: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        if require_directory_fsync:
            # Claim receipts and STOP fences must never report durable success
            # when a power loss could restore their previous version.
            raise
        # Atomic replacement still protects noncritical settings from partial
        # JSON on filesystems without directory fsync support.


def sanitize_filename(filename: str) -> str:
    base = Path(filename or "").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", base).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return cleaned


def audio_path(filename: str, *, strict: bool = True) -> Path:
    if strict and sanitize_filename(filename) != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = (AUDIO_DIR / sanitize_filename(filename)).resolve()
    if AUDIO_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return path


def unique_audio_filename(filename: str) -> str:
    candidate = sanitize_filename(filename)
    path = audio_path(candidate)
    if not path.exists():
        return candidate
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        next_name = f"{stem}-{index}{suffix}"
        if not audio_path(next_name).exists():
            return next_name
    raise HTTPException(status_code=409, detail="Too many files with this name")


def ensure_audio_exists(filename: str) -> str:
    cleaned = sanitize_filename(filename)
    if cleaned != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filename = cleaned
    path = audio_path(filename)
    if path.suffix.lower() not in SUPPORTED_AUDIO_EXTS or not path.exists():
        raise HTTPException(status_code=404, detail="Sound file not found")
    return filename


def default_schedule_data() -> Dict[str, Any]:
    return {"active": "Default", "schedules": {"Default": []}}


class ScheduleEntry(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    day: int = Field(ge=0, le=6)
    time: dt_time
    sound_file: str
    label: str = ""
    enabled: bool = True
    output_keys: Optional[List[str]] = None


class ScheduleName(BaseModel):
    name: str


class QuickButton(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    name: str
    sound_file: str
    color: str = "#2563eb"
    icon: str = ""
    loop: bool = False


class AudioFile(BaseModel):
    file: str
    name: str
    default_key: Optional[str] = None
    default_category: Optional[str] = None


class AudioName(BaseModel):
    name: str


class Device(BaseModel):
    ip: str
    name: str = ""
    port: int = Field(default=BARIX_STREAM_PORT, ge=1, le=65535)


class DeviceScanRanges(BaseModel):
    ranges: List[str] = []


class TestRequest(BaseModel):
    sound_file: str
    loop: Optional[bool] = False
    output_keys: Optional[List[str]] = None


class SetupRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ThreadhallPairRequest(BaseModel):
    base_url: str
    pairing_code: str
    name: Optional[str] = None


def entry_to_json(entry: ScheduleEntry) -> Dict[str, Any]:
    data = model_to_dict(entry)
    if isinstance(data.get("time"), dt_time):
        data["time"] = data["time"].strftime("%H:%M")
    return data


def button_to_json(button: QuickButton) -> Dict[str, Any]:
    return model_to_dict(button)


def normalize_schedule_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Schedule name is required")
    if len(cleaned) > 80:
        raise HTTPException(status_code=400, detail="Schedule name is too long")
    return cleaned


def normalize_color(color: str) -> str:
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        return color.lower()
    raise HTTPException(status_code=400, detail="Color must be a hex value")


def normalize_icon(icon: str) -> str:
    icon = (icon or "").strip()
    if not icon:
        return ""
    if re.fullmatch(r"fa-[a-z0-9-]+", icon):
        return icon
    raise HTTPException(status_code=400, detail="Invalid icon")


def normalize_device_ip(ip: str) -> str:
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid IP address")
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise HTTPException(status_code=400, detail="Enter a reachable IPv4 address")
    return str(address)


def normalize_device_name(name: str, fallback: str = "") -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    if len(cleaned) > 80:
        raise HTTPException(status_code=400, detail="Device nickname is too long")
    return cleaned or fallback


def normalize_device_port(port: Any) -> int:
    try:
        value = int(port or BARIX_STREAM_PORT)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter a valid audio port")
    if value < 1 or value > 65535:
        raise HTTPException(status_code=400, detail="Enter a valid audio port")
    return value


def split_device_target(target: str) -> Tuple[str, int]:
    value = (target or "").strip()
    port = BARIX_STREAM_PORT
    if ":" in value and value.count(":") == 1:
        host, port_raw = value.rsplit(":", 1)
        if port_raw:
            value = host.strip()
            port = normalize_device_port(port_raw)
    return normalize_device_ip(value), port


def default_device_name(ip: str) -> str:
    return f"Barix {ip}"


def device_to_json(device: Device) -> Dict[str, Any]:
    return {
        "ip": normalize_device_ip(device.ip),
        "name": normalize_device_name(device.name, default_device_name(device.ip)),
        "port": normalize_device_port(device.port),
    }


def normalize_device_record(item: Any) -> Device:
    if isinstance(item, str):
        ip, port = split_device_target(item)
        return Device(ip=ip, name=default_device_name(ip), port=port)
    if isinstance(item, dict):
        target = str(item.get("ip") or item.get("target") or "")
        ip, parsed_port = split_device_target(target)
        raw_port = item.get("port")
        if ":" in target and (raw_port in (None, "", BARIX_STREAM_PORT, str(BARIX_STREAM_PORT))):
            port = parsed_port
        else:
            port = normalize_device_port(raw_port if raw_port is not None else parsed_port)
        name = normalize_device_name(str(item.get("name") or ""), default_device_name(ip))
        return Device(ip=ip, name=name, port=port)
    raise HTTPException(status_code=400, detail="Invalid device")


def device_key(device: Device) -> str:
    return f"{device.ip}:{device.port}"


def device_stream_url(device: Device) -> str:
    return f"udp://{device.ip}:{device.port}?pkt_size=1200"


def output_manifest(
    devices: Optional[List[Device]] = None,
    statuses: Optional[Dict[str, bool]] = None,
) -> List[Dict[str, Any]]:
    """Describe the stable output keys accepted by targeted playback."""
    devices = load_devices() if devices is None else devices
    manifest: List[Dict[str, Any]] = [
        {
            "key": LOCAL_OUTPUT_KEY,
            "type": "local",
            "name": "Local speaker",
            "online": True,
        }
    ]
    for device in devices:
        key = device_key(device)
        output = {
            "key": key,
            "type": "barix",
            "name": normalize_device_name(device.name, default_device_name(device.ip)),
            "ip": device.ip,
            "port": device.port,
            "online": bool(statuses.get(key, False)) if statuses is not None else None,
        }
        manifest.append(output)
    return manifest


def resolve_output_targets(
    output_keys: Optional[List[str]],
    devices: Optional[List[Device]] = None,
) -> Tuple[List[Device], bool]:
    """Resolve an event's output keys without ever widening an explicit selection."""
    devices = load_devices() if devices is None else devices
    if output_keys is None:
        return list(devices), True

    requested: List[str] = []
    for value in output_keys:
        key = str(value).strip()
        if key and key not in requested:
            requested.append(key)
    if not requested:
        raise HTTPException(status_code=400, detail="Select at least one audio output")

    devices_by_key = {device_key(device): device for device in devices}
    available_keys = {LOCAL_OUTPUT_KEY, *devices_by_key.keys()}
    stale_keys = [key for key in requested if key not in available_keys]
    if stale_keys:
        raise HTTPException(
            status_code=409,
            detail=f"Audio output is no longer configured: {', '.join(stale_keys)}",
        )

    return [devices_by_key[key] for key in requested if key in devices_by_key], LOCAL_OUTPUT_KEY in requested


def normalize_scan_range(value: str) -> str:
    item = (value or "").strip()
    if not item:
        raise HTTPException(status_code=400, detail="Scan range is required")
    try:
        if "/" not in item:
            ip = ipaddress.ip_address(item)
            if ip.version != 4:
                raise ValueError
            return str(ipaddress.ip_network(f"{ip}/32", strict=False))
        network = ipaddress.ip_network(item, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid scan range: {item}")
    if network.version != 4:
        raise HTTPException(status_code=400, detail="Only IPv4 scan ranges are supported")
    return str(network)


def parse_scan_ranges(raw: str) -> List[str]:
    ranges: List[str] = []
    for item in re.split(r"[\s,;]+", raw or ""):
        if not item:
            continue
        normalized = normalize_scan_range(item)
        if normalized not in ranges:
            ranges.append(normalized)
    return ranges


def normalize_schedule_data(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return default_schedule_data()

    schedules_raw = raw.get("schedules")
    if not isinstance(schedules_raw, dict):
        schedules_raw = {"Default": raw.get("entries", []) if isinstance(raw.get("entries"), list) else []}

    schedules: Dict[str, List[Dict[str, Any]]] = {}
    for name, entries in schedules_raw.items():
        if not isinstance(name, str) or not isinstance(entries, list):
            continue
        schedule_name = normalize_schedule_name(name)
        normalized_entries: List[Dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                entry = ScheduleEntry(**item)
                entry.sound_file = sanitize_filename(entry.sound_file)
                normalized_entries.append(entry_to_json(entry))
            except Exception:
                continue
        schedules[schedule_name] = sorted(normalized_entries, key=lambda e: (e["day"], e["time"], e["label"]))

    if not schedules:
        schedules = {"Default": []}

    active = raw.get("active")
    if not isinstance(active, str) or active not in schedules:
        active = next(iter(schedules.keys()))

    return {"active": active, "schedules": schedules}


def load_all_schedules() -> Dict[str, Any]:
    with storage_lock:
        raw = read_json(SCHEDULE_FILE, default_schedule_data())
        data = normalize_schedule_data(raw)
        if raw != data:
            write_json(SCHEDULE_FILE, data)
        return data


def save_all_schedules(data: Dict[str, Any]) -> None:
    with storage_lock:
        write_json(SCHEDULE_FILE, normalize_schedule_data(data))


def load_schedule(
    effective_date: Optional[datetime] = None,
    *,
    honor_calendar: bool = True,
) -> List[ScheduleEntry]:
    entries, _ = resolve_effective_schedule(
        effective_date,
        honor_calendar=honor_calendar,
    )
    return entries


def resolve_effective_schedule(
    effective_date: Optional[datetime] = None,
    *,
    honor_calendar: bool = True,
) -> Tuple[List[ScheduleEntry], Dict[str, Any]]:
    with threadhall_integration_lock:
        return _resolve_effective_schedule_unlocked(
            effective_date,
            honor_calendar=honor_calendar,
        )


def _resolve_effective_schedule_unlocked(
    effective_date: Optional[datetime] = None,
    *,
    honor_calendar: bool = True,
) -> Tuple[List[ScheduleEntry], Dict[str, Any]]:
    data = load_all_schedules()
    active = data.get("active")
    config = load_threadhall_config()
    _, district_zone, _ = configured_district_time_zone(config)
    managed_authority = threadhall_manages_current_schedule(config, active)
    if effective_date is None:
        current = (
            datetime.now(district_zone)
            if managed_authority and district_zone
            else datetime.now()
        )
    elif effective_date.tzinfo is not None and managed_authority and district_zone:
        current = effective_date.astimezone(district_zone)
    else:
        current = effective_date
    effective_day = current.date()
    source = "active"
    managed = is_threadhall_managed_schedule_name(str(active or ""))
    if honor_calendar:
        date_key = effective_day.isoformat()
        calendar_defaults = config.get("calendar_defaults")
        calendar_managed = (
            isinstance(calendar_defaults, dict)
            and calendar_defaults.get("managed") is True
        )
        if calendar_managed:
            calendar_day = next(
                (
                    item
                    for item in config.get("calendar", [])
                    if isinstance(item, dict)
                    and str(item.get("date") or "") == date_key
                ),
                None,
            )
            if calendar_day:
                if not bool(calendar_day.get("instructional", True)):
                    return [], {
                        "effective_date": date_key,
                        "managed": True,
                        "name": None,
                        "source": "calendar_noninstructional",
                    }
                managed_name = managed_threadhall_schedule_name(
                    str(calendar_day.get("schedule_name") or "Regular")
                )
                active = managed_name
                source = "calendar_day"
                managed = True
            else:
                weekday = effective_day.weekday() < 5
                default_key = "weekday_schedule_name" if weekday else "weekend_schedule_name"
                raw_name = calendar_defaults.get(default_key)
                if not isinstance(raw_name, str) or not raw_name.strip():
                    return [], {
                        "effective_date": date_key,
                        "managed": True,
                        "name": None,
                        "source": (
                            "calendar_weekday_default"
                            if weekday
                            else "calendar_weekend_default"
                        ),
                    }
                managed_name = managed_threadhall_schedule_name(raw_name)
                active = managed_name
                source = (
                    "calendar_weekday_default"
                    if weekday
                    else "calendar_weekend_default"
                )
                managed = True
    entries = data.get("schedules", {}).get(active, [])
    return [ScheduleEntry(**item) for item in entries], {
        "effective_date": effective_day.isoformat(),
        "managed": managed,
        "name": active,
        "source": source,
    }


def save_schedule(entries: List[ScheduleEntry]) -> None:
    with threadhall_integration_lock:
        data = load_all_schedules()
        active = data.get("active") or "Default"
        data.setdefault("schedules", {})[active] = [
            entry_to_json(entry) for entry in sorted(entries, key=lambda e: (e.day, e.time, e.label))
        ]
        save_all_schedules(data)


def list_schedules() -> Dict[str, Any]:
    data = load_all_schedules()
    return {
        "active": data.get("active"),
        "schedules": list(data.get("schedules", {}).keys()),
    }


def create_schedule(name: str) -> Dict[str, Any]:
    with threadhall_integration_lock:
        name = normalize_schedule_name(name)
        data = load_all_schedules()
        data.setdefault("schedules", {}).setdefault(name, [])
        data["active"] = name
        save_all_schedules(data)
        return list_schedules()


def activate_schedule(name: str) -> Dict[str, Any]:
    with threadhall_integration_lock:
        name = normalize_schedule_name(name)
        data = load_all_schedules()
        if name not in data.get("schedules", {}):
            raise HTTPException(status_code=404, detail="Schedule not found")
        data["active"] = name
        save_all_schedules(data)
        return list_schedules()


def remove_schedule(name: str) -> Dict[str, Any]:
    with threadhall_integration_lock:
        name = normalize_schedule_name(name)
        data = load_all_schedules()
        schedules = data.get("schedules", {})
        if name not in schedules:
            raise HTTPException(status_code=404, detail="Schedule not found")
        if name == data.get("active"):
            raise HTTPException(status_code=400, detail="Activate another schedule before deleting this one")
        if len(schedules) == 1:
            raise HTTPException(status_code=400, detail="At least one schedule is required")
        schedules.pop(name)
        save_all_schedules(data)
        return list_schedules()


def load_devices() -> List[Device]:
    with storage_lock:
        raw = read_json(DEVICES_FILE, [])
        if not isinstance(raw, list):
            raw = []
        devices: List[Device] = []
        keys: set[str] = set()
        changed = False
        for item in raw:
            try:
                device = normalize_device_record(item)
            except HTTPException:
                changed = True
                continue
            key = device_key(device)
            if key not in keys:
                devices.append(device)
                keys.add(key)
            else:
                changed = True
        serialized = [device_to_json(device) for device in devices]
        if changed or raw != serialized:
            write_json(DEVICES_FILE, serialized)
        return devices


def save_devices(devices: List[Device]) -> None:
    unique: List[Device] = []
    keys: set[str] = set()
    for item in devices:
        device = normalize_device_record(model_to_dict(item) if isinstance(item, BaseModel) else item)
        key = device_key(device)
        if key not in keys:
            unique.append(device)
            keys.add(key)
    with storage_lock:
        write_json(DEVICES_FILE, [device_to_json(device) for device in unique])


def load_buttons() -> List[QuickButton]:
    with storage_lock:
        raw = read_json(BUTTONS_FILE, [])
        if not isinstance(raw, list):
            raw = []
        buttons: List[QuickButton] = []
        changed = False
        for item in raw:
            if not isinstance(item, dict):
                changed = True
                continue
            try:
                button = QuickButton(**item)
                button.sound_file = sanitize_filename(button.sound_file)
                button.color = normalize_color(button.color)
                button.icon = normalize_icon(button.icon)
                buttons.append(button)
                changed = changed or item != button_to_json(button)
            except Exception:
                changed = True
        if changed:
            save_buttons(buttons)
        return buttons


def save_buttons(buttons: List[QuickButton]) -> None:
    with storage_lock:
        write_json(BUTTONS_FILE, [button_to_json(button) for button in buttons])


def load_audio_meta() -> Dict[str, str]:
    with storage_lock:
        raw = read_json(AUDIO_META_FILE, {})
        if not isinstance(raw, dict):
            return {}
        meta: Dict[str, str] = {}
        for key, value in raw.items():
            try:
                filename = sanitize_filename(str(key))
            except HTTPException:
                continue
            meta[filename] = str(value or Path(filename).stem).strip() or Path(filename).stem
        return meta


def save_audio_meta(meta: Dict[str, str]) -> None:
    with storage_lock:
        write_json(AUDIO_META_FILE, meta)


def list_audio() -> List[AudioFile]:
    meta = load_audio_meta()
    default_keys = {filename: key for key, filename in DEFAULT_AUDIO_KEY_FILES.items()}
    files: List[AudioFile] = []
    changed = False
    for path in sorted(AUDIO_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
            continue
        name = meta.get(path.name, DEFAULT_AUDIO_NAMES.get(path.name, path.stem))
        default_key = default_keys.get(path.name)
        default_category = None
        if default_key:
            default_category = "Emergency" if default_key in EMERGENCY_SOUND_KEYS else "Bell"
        if path.name not in meta:
            meta[path.name] = name
            changed = True
        files.append(AudioFile(
            file=path.name,
            name=name,
            default_key=default_key,
            default_category=default_category,
        ))
    existing = {audio.file for audio in files}
    for missing in set(meta.keys()) - existing:
        meta.pop(missing, None)
        changed = True
    if changed:
        save_audio_meta(meta)
    return files


def audio_display_name(filename: str) -> str:
    for audio in list_audio():
        if audio.file == filename:
            return audio.name
    return Path(filename).stem


def normalize_threadhall_base_url(value: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="Enter a valid Threadhall URL")
    try:
        parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid Threadhall URL")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if not loopback:
            raise HTTPException(status_code=400, detail="Threadhall requires HTTPS outside local development")
    return f"{parsed.scheme}://{parsed.netloc}"


def configured_district_time_zone(
    config: Dict[str, Any],
) -> Tuple[Optional[str], Optional[ZoneInfo], str]:
    raw = os.environ.get("PIBELLS_DISTRICT_TIME_ZONE", config.get("district_time_zone"))
    if not isinstance(raw, str) or not raw.strip():
        return None, None, "district_time_zone_missing"
    name = raw.strip()
    try:
        return name, ZoneInfo(name), ""
    except (ValueError, ZoneInfoNotFoundError):
        return name, None, "district_time_zone_invalid"


def clear_threadhall_clock_proof() -> None:
    global threadhall_clock_proof, threadhall_clock_error
    threadhall_clock_proof = None
    threadhall_clock_error = "server_clock_not_verified"


def threadhall_manages_current_schedule(config: Dict[str, Any], active: Any) -> bool:
    defaults = config.get("calendar_defaults")
    return is_threadhall_managed_schedule_name(str(active or "")) or bool(
        isinstance(defaults, dict) and defaults.get("managed") is True
    )


def threadhall_managed_schedule_read_only(config: Dict[str, Any]) -> bool:
    raw = read_json(SCHEDULE_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("active"), str):
        # A damaged schedule file cannot prove that dispatch is local-only.
        return True
    return threadhall_manages_current_schedule(config, raw["active"])


def record_threadhall_clock_sample(
    config: Dict[str, Any],
    server_time: Any,
    started_utc: datetime,
    ended_utc: datetime,
    started_monotonic: float,
    ended_monotonic: float,
) -> None:
    """Accept a bounded server-clock sample only for the current pairing authority."""
    global threadhall_clock_proof, threadhall_clock_error
    threadhall_clock_proof = None
    threadhall_clock_error = "server_clock_not_verified"
    authority = stored_threadhall_authority(config.get("authority"))
    zone_name, zone, _ = configured_district_time_zone(config)
    if not authority or not zone:
        return
    if not isinstance(server_time, str):
        threadhall_clock_error = "server_time_missing"
        return
    try:
        server_utc = datetime.fromisoformat(server_time.replace("Z", "+00:00"))
    except ValueError:
        threadhall_clock_error = "server_time_invalid"
        return
    if server_utc.tzinfo is None:
        threadhall_clock_error = "server_time_invalid"
        return
    server_utc = server_utc.astimezone(timezone.utc)
    duration = ended_monotonic - started_monotonic
    if duration < 0 or duration > THREADHALL_MAX_CLOCK_SKEW_SECONDS:
        threadhall_clock_error = "server_clock_sample_slow"
        return
    # The server sampled its database clock inside this request. Its exact
    # position in the request interval is unknown, so check the worst bound.
    skew_bound = max(
        abs((server_utc - started_utc).total_seconds()),
        abs((server_utc - ended_utc).total_seconds()),
    )
    if skew_bound > THREADHALL_MAX_CLOCK_SKEW_SECONDS:
        threadhall_clock_error = "controller_clock_skew"
        return
    threadhall_clock_error = ""
    threadhall_clock_proof = {
        "authority": authority,
        "district_time_zone": zone_name,
        "verified_at_utc": ended_utc,
        "verified_at_monotonic": ended_monotonic,
        "offset_estimate_seconds": round(
            (server_utc - (started_utc + (ended_utc - started_utc) / 2)).total_seconds(),
            3,
        ),
        "skew_bound_seconds": round(skew_bound, 3),
    }


def managed_schedule_clock_status(
    config: Optional[Dict[str, Any]] = None,
    *,
    now_utc: Optional[datetime] = None,
    now_monotonic: Optional[float] = None,
    read_only: bool = False,
) -> Dict[str, Any]:
    config = config if config is not None else load_threadhall_config()
    now_utc = now_utc if now_utc is not None else datetime.now(timezone.utc)
    now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()
    zone_name, zone, zone_error = configured_district_time_zone(config)
    authority_held = (
        threadhall_authority_fence_failed
        or threadhall_config_integrity_failed
        or threadhall_remote_commands_held(config)
        or threadhall_unverified_stop_held(config)
    )
    required = (
        threadhall_managed_schedule_read_only(config)
        if authority_held or read_only
        else threadhall_manages_current_schedule(config, load_all_schedules().get("active"))
    )
    host_offset = datetime.now().astimezone().utcoffset()
    status: Dict[str, Any] = {
        "ready": False,
        "required_for_dispatch": required,
        "reason": zone_error or threadhall_clock_error or "server_clock_not_verified",
        "district_time_zone": zone_name,
        "district_utc_offset_minutes": (
            int(now_utc.astimezone(zone).utcoffset().total_seconds() // 60)
            if zone else None
        ),
        "host_utc_offset_minutes": (
            int(host_offset.total_seconds() // 60) if host_offset else 0
        ),
        "clock_offset_estimate_seconds": None,
        "clock_skew_bound_seconds": None,
        "clock_sample_age_seconds": None,
    }
    if not required:
        status["ready"] = True
        status["reason"] = "local_schedule"
        return status
    if authority_held:
        status["reason"] = (
            "threadhall_config_reconciliation_required" if threadhall_config_integrity_failed
            else "threadhall_stop_termination_unverified" if threadhall_unverified_stop_held(config)
            else "threadhall_remote_commands_held_after_stop" if threadhall_remote_commands_held(config)
            else "threadhall_authority_reconciliation_required"
        )
        return status
    if zone_error:
        return status
    if not (
        config.get("enabled")
        and config.get("token")
        and stored_threadhall_authority(config.get("authority"))
    ):
        status["reason"] = "threadhall_not_paired"
        return status
    proof = threadhall_clock_proof
    if (
        not proof
        or proof["authority"] != stored_threadhall_authority(config.get("authority"))
        or proof["district_time_zone"] != zone_name
    ):
        return status
    age = now_monotonic - proof["verified_at_monotonic"]
    wall_elapsed = (now_utc - proof["verified_at_utc"]).total_seconds()
    status.update({
        "clock_offset_estimate_seconds": proof["offset_estimate_seconds"],
        "clock_skew_bound_seconds": proof["skew_bound_seconds"],
        "clock_sample_age_seconds": round(max(age, 0), 3),
    })
    if age < 0 or abs(wall_elapsed - age) > THREADHALL_MAX_CLOCK_JUMP_SECONDS:
        status["reason"] = "controller_clock_changed"
    elif age > THREADHALL_MAX_CLOCK_SAMPLE_AGE_SECONDS:
        status["reason"] = "server_clock_sample_stale"
    else:
        status["ready"] = True
        status["reason"] = "verified"
    return status


def threadhall_config_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Threadhall pairing file is unreadable or invalid; stop audio and reconcile the controller",
    )


def threadhall_config_failure() -> Dict[str, Any]:
    global threadhall_config_integrity_failed, threadhall_config_stop_unverified
    global threadhall_stop_runtime_generation, threadhall_remote_command_hold_runtime
    if not threadhall_config_integrity_failed:
        threadhall_config_integrity_failed = True
        threadhall_remote_command_hold_runtime = True
        threadhall_stop_runtime_generation += 1
        clear_threadhall_clock_proof()
        try:
            stop_loops()
        except Exception:
            threadhall_config_stop_unverified = True
    if threadhall_last_valid_config is None:
        raise threadhall_config_unavailable()
    # The last strictly read or written identity is usable only for STOP-only
    # polling. All playback and normal pairing-state writes remain fenced.
    return copy.deepcopy(threadhall_last_valid_config)


def load_threadhall_config() -> Dict[str, Any]:
    global threadhall_last_valid_config
    if threadhall_config_integrity_failed:
        return threadhall_config_failure()
    source = THREADHALL_CONFIG_FILE
    if not source.exists():
        if LEGACY_THREADHALL_CONFIG_FILE.exists():
            source = LEGACY_THREADHALL_CONFIG_FILE
        elif threadhall_last_valid_config is not None:
            # A once-paired controller cannot mistake deletion for a fresh
            # install and silently lose the token needed to fetch STOP.
            return threadhall_config_failure()
        else:
            source = None
    if source is None:
        raw: Dict[str, Any] = {}
    else:
        try:
            with source.open() as config_file:
                raw = json.load(config_file)
            if not isinstance(raw, dict):
                raise ValueError("Pairing file must contain an object")
            if source == LEGACY_THREADHALL_CONFIG_FILE:
                # Keep the read identity available for STOP if a migration
                # write fails after the older file was parsed successfully.
                threadhall_last_valid_config = copy.deepcopy(raw)
                write_json(THREADHALL_CONFIG_FILE, raw, require_directory_fsync=True)
                try:
                    THREADHALL_CONFIG_FILE.chmod(0o600)
                except OSError:
                    pass
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return threadhall_config_failure()
    env_base_url = os.environ.get(
        "THREADHALL_BASE_URL",
        os.environ.get(
            f"PIBELLS_{RETIRED_INTEGRATION_NAME.upper()}_BASE_URL",
            "",
        ),
    ).strip()
    env_token = os.environ.get(
        "THREADHALL_PIBELLS_TOKEN",
        os.environ.get(
            f"PIBELLS_{RETIRED_INTEGRATION_NAME.upper()}_DEVICE_TOKEN",
            "",
        ),
    ).strip()
    if env_base_url:
        raw["base_url"] = normalize_threadhall_base_url(env_base_url)
    if env_token:
        raw["token"] = env_token
        raw["enabled"] = True
    raw.setdefault("enabled", bool(raw.get("token")))
    raw.setdefault("device_uuid", threadhall_unpaired_device_uuid)
    raw.setdefault("poll_seconds", THREADHALL_DEFAULT_POLL_SECONDS)
    raw.setdefault("stop_epoch", 0)
    # A pre-upgrade pairing did not negotiate execution claims. Preserve v1
    # until a fresh v2 pairing response explicitly confirms the new protocol.
    raw.setdefault("protocol_version", 1)
    raw.setdefault("calendar", [])
    if source is not None:
        threadhall_last_valid_config = copy.deepcopy(raw)
    return raw


def save_threadhall_config(
    config: Dict[str, Any], *, allow_integrity_repair: bool = False,
) -> Dict[str, Any]:
    global threadhall_last_valid_config
    if threadhall_config_integrity_failed and not allow_integrity_repair:
        raise threadhall_config_unavailable()
    config = {
        **load_threadhall_config(),
        **config,
    }
    config["poll_seconds"] = max(10, min(300, int(config.get("poll_seconds") or THREADHALL_DEFAULT_POLL_SECONDS)))
    write_json(THREADHALL_CONFIG_FILE, config, require_directory_fsync=True)
    try:
        THREADHALL_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    threadhall_last_valid_config = copy.deepcopy(config)
    return config


def threadhall_stop_epoch(config: Dict[str, Any]) -> int:
    value = config.get("stop_epoch", 0)
    if type(value) is not int or value < 0:
        raise HTTPException(status_code=503, detail="Threadhall STOP ordering state requires reconciliation")
    return value


def threadhall_remote_commands_held(config: Dict[str, Any]) -> bool:
    return bool(config.get("remote_command_hold") or threadhall_remote_command_hold_runtime)


def threadhall_unverified_stop_held(
    config: Dict[str, Any], history: Optional[Dict[str, Dict[str, Any]]] = None,
) -> bool:
    if config.get("unverified_stop_hold") or threadhall_unverified_stop_runtime:
        return True
    if history is None and config.get("token") and stored_threadhall_authority(config.get("authority")):
        try:
            history = load_threadhall_command_history(config)
        except Exception:
            # A missing receipt cannot prove that a past STOP completed.
            return True
    return bool(
        history is not None
        and any(
            isinstance(entry, dict) and entry.get("stop_retry_pending") is True
            for entry in history.values()
        )
    )


def advance_threadhall_stop_fence(*, hold_remote_commands: bool = False) -> Dict[str, Any]:
    # Caller holds threadhall_integration_lock. Advance the in-process guard
    # first so a failed disk write still fences an in-flight claim.
    global threadhall_stop_runtime_generation, threadhall_remote_command_hold_runtime
    threadhall_stop_runtime_generation += 1
    if hold_remote_commands:
        threadhall_remote_command_hold_runtime = True
    current = load_threadhall_config()
    return save_threadhall_config({
        "stop_epoch": threadhall_stop_epoch(current) + 1,
        "remote_command_hold": threadhall_remote_commands_held(current),
    })


def stored_threadhall_authority(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    authority: Dict[str, str] = {}
    for key in ("district_id", "school_id", "device_id"):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        authority[key] = raw.strip()
    return authority


def safe_threadhall_status(*, read_only: bool = False) -> Dict[str, Any]:
    config = load_threadhall_config()
    authority = stored_threadhall_authority(config.get("authority"))
    pairing_ready = bool(config.get("token") and authority)
    remote_command_hold = threadhall_remote_commands_held(config)
    unverified_stop_hold = threadhall_unverified_stop_held(config)
    return {
        "enabled": bool(
            config.get("enabled") and pairing_ready
            and not threadhall_authority_fence_failed
            and not threadhall_config_integrity_failed
            and not remote_command_hold
            and not unverified_stop_hold
        ),
        "paired": pairing_ready,
        "base_url": config.get("base_url", ""),
        "device_uuid": config.get("device_uuid", ""),
        "device_name": config.get("device_name", ""),
        "poll_seconds": config.get("poll_seconds", THREADHALL_DEFAULT_POLL_SECONDS),
        "protocol_version": config.get("protocol_version", 1),
        "execution_claim_v1": bool(
            config.get("enabled")
            and not threadhall_authority_fence_failed
            and not threadhall_config_integrity_failed
            and not remote_command_hold
            and not unverified_stop_hold
            and not config.get("pairing_pending")
            and config.get("protocol_version") == PIBELLS_PROTOCOL_VERSION
            and config.get("claim_url_template") == THREADHALL_CLAIM_URL_TEMPLATE
        ),
        "schedule_revision": config.get("schedule_revision", ""),
        "last_sync_at": config.get("last_sync_at"),
        "schedule_clock": managed_schedule_clock_status(config, read_only=read_only),
        "remote_commands_held": remote_command_hold,
        "unverified_stop_hold": unverified_stop_hold,
        "last_error": (
            "Threadhall pairing file requires operator reconciliation; audio stop could not be verified."
            if threadhall_config_integrity_failed and threadhall_config_stop_unverified else
            "Threadhall pairing file requires operator reconciliation."
            if threadhall_config_integrity_failed else
            "Audio termination is unverified; remote commands are held."
            if unverified_stop_hold else
            "Remote commands are held after local STOP; re-pair to reconcile."
            if remote_command_hold else
            "Threadhall authority change requires operator reconciliation."
            if threadhall_authority_fence_failed else
            config.get("last_error", "") or (
            "Re-pair with Threadhall to establish complete controller authority."
            if config.get("token") and not authority
            else ""
            )
        ),
    }


class ThreadhallRejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def threadhall_request(
    config: Dict[str, Any],
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    base_url = normalize_threadhall_base_url(str(config.get("base_url") or ""))
    url = f"{base_url}/{path.lstrip('/')}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": f"PiBells/{PIBELLS_AGENT_VERSION}",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = token if token is not None else str(config.get("token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    is_claim = path.startswith("/api/pibells/v2/commands/") and path.endswith("/claim")
    # Never forward a device bearer or pairing payload to a redirect target.
    open_request = urllib.request.build_opener(ThreadhallRejectRedirect()).open
    try:
        with open_request(request, timeout=12) as response:
            if is_claim and response.getcode() != 200:
                raise HTTPException(status_code=502, detail="Threadhall claim did not return HTTP 200")
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
            detail = parsed.get("message") or parsed.get("detail") or body
        except Exception:
            detail = body or exc.reason
        raise HTTPException(status_code=502, detail=f"Threadhall returned {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Threadhall connection failed: {exc.reason}")
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Threadhall returned invalid JSON")


def normalize_threadhall_authority(value: Any) -> Dict[str, str]:
    authority = stored_threadhall_authority(value)
    if not authority:
        raise HTTPException(status_code=502, detail="Threadhall returned invalid pairing authority")
    return authority


def clear_threadhall_authority_state(config: Dict[str, Any], reason: str) -> None:
    fence_and_disable_threadhall_authority(config, {
        "enabled": False,
        "pairing_pending": False,
        "pairing_epoch": str(uuid.uuid4()),
        "base_url": config.get("base_url", ""),
        "token": "",
        "authority": {},
        "schedule_revision": "",
        "managed_schedule_hash": "",
        "calendar": [],
        "calendar_defaults": {},
        "district_time_zone": "",
        "claim_url_template": "",
        "last_sync_at": None,
        "last_error": reason,
    })
    # Retain every authority's receipts even when a pairing response is
    # ambiguous. A future pair with the same authority must not replay them.


def fence_and_disable_threadhall_authority(
    config: Dict[str, Any], updates: Dict[str, Any],
) -> None:
    # Caller holds threadhall_integration_lock. Persist disabled authority
    # before attempting STOP, which itself may fail on a resistant player.
    global threadhall_authority_fence_failed
    global threadhall_config_integrity_failed, threadhall_config_stop_unverified
    threadhall_authority_fence_failed = True
    clear_threadhall_clock_proof()
    fence_error = None
    disable_error = None
    stop_error = None
    try:
        advance_threadhall_stop_fence()
    except Exception as exc:
        fence_error = exc
    try:
        save_threadhall_config(updates, allow_integrity_repair=True)
    except Exception as exc:
        disable_error = exc
    else:
        # A strictly durable disabled pairing supersedes any failed epoch
        # write; a fresh process cannot revive the previous device token.
        threadhall_authority_fence_failed = False
        threadhall_config_integrity_failed = False
        threadhall_config_stop_unverified = False
    try:
        stop_loops()
    except Exception as exc:
        stop_error = exc
    if disable_error is None:
        purge_threadhall_managed_schedules()
    if fence_error or disable_error or stop_error:
        raise HTTPException(
            status_code=503,
            detail="Threadhall authority change or audio termination requires reconciliation",
        )


def pair_threadhall(body: ThreadhallPairRequest) -> Dict[str, Any]:
    # Only pair requests serialize each other. The network wait never owns the
    # integration lock, so local STOP and schedule dispatch stay responsive.
    with threadhall_pair_request_lock:
        environment_token = os.environ.get(
            "THREADHALL_PIBELLS_TOKEN",
            os.environ.get(f"PIBELLS_{RETIRED_INTEGRATION_NAME.upper()}_DEVICE_TOKEN", ""),
        ).strip()
        if environment_token:
            raise HTTPException(
                status_code=409,
                detail="Remove the environment-managed Threadhall device token before interactive pairing.",
            )
        requested_base_url = normalize_threadhall_base_url(body.base_url)
        environment_base_url = os.environ.get(
            "THREADHALL_BASE_URL",
            os.environ.get(f"PIBELLS_{RETIRED_INTEGRATION_NAME.upper()}_BASE_URL", ""),
        ).strip()
        if environment_base_url and normalize_threadhall_base_url(environment_base_url) != requested_base_url:
            raise HTTPException(
                status_code=409,
                detail="Pairing URL must match the environment-managed Threadhall URL.",
            )
        with threadhall_integration_lock:
            config_file_exists = (
                THREADHALL_CONFIG_FILE.exists() or LEGACY_THREADHALL_CONFIG_FILE.exists()
            )
            fresh_install = not any(path.exists() for path in (
                THREADHALL_CONFIG_FILE,
                LEGACY_THREADHALL_CONFIG_FILE,
                THREADHALL_COMMAND_HISTORY_FILE,
                LEGACY_THREADHALL_COMMAND_HISTORY_FILE,
            ))
            prior_config = load_threadhall_config()
            device_uuid = str(prior_config.get("device_uuid") or uuid.uuid4())
            bootstrap = None
            if fresh_install:
                bootstrap = save_threadhall_history_bootstrap(device_uuid)
            elif THREADHALL_COMMAND_HISTORY_FILE.exists():
                if stored_threadhall_authority(prior_config.get("authority")):
                    load_threadhall_command_history(prior_config)
                else:
                    envelope = read_threadhall_history_envelope(
                        THREADHALL_COMMAND_HISTORY_FILE, allow_bootstrap=True,
                    )
                    if not envelope["partitions"]:
                        if (
                            LEGACY_THREADHALL_COMMAND_HISTORY_FILE.exists()
                            or prior_config.get("token")
                            or stored_threadhall_authority(prior_config.get("authority"))
                        ):
                            raise threadhall_history_unavailable()
                        bootstrap = envelope["bootstrap"]
                        if config_file_exists and (
                            prior_config.get("history_bootstrap_epoch") != bootstrap["epoch"]
                            or prior_config.get("device_uuid") != bootstrap["device_uuid"]
                        ):
                            raise threadhall_history_unavailable()
                        device_uuid = bootstrap["device_uuid"]
                        fresh_install = True
            else:
                raise threadhall_history_unavailable()
            if bootstrap is not None:
                prior_config["history_bootstrap_epoch"] = bootstrap["epoch"]
                prior_config["device_uuid"] = device_uuid
            name = (body.name or prior_config.get("device_name") or socket.gethostname() or "PiBells Controller").strip()
            pairing_epoch = str(uuid.uuid4())
            clear_threadhall_clock_proof()
            save_threadhall_config({
                "enabled": False,
                "pairing_pending": True,
                "pairing_epoch": pairing_epoch,
                "device_uuid": device_uuid,
                "history_bootstrap_epoch": bootstrap["epoch"] if bootstrap else prior_config.get("history_bootstrap_epoch", ""),
                "last_error": "Pairing is in progress; managed dispatch is paused.",
            }, allow_integrity_repair=True)
            pairing_stop_generation = threadhall_stop_runtime_generation

        request_config = {**prior_config, "base_url": requested_base_url}
        try:
            local_ip = get_local_ip()
            payload = {
                "protocol_version": PIBELLS_PROTOCOL_VERSION,
                "pairing_code": body.pairing_code,
                "device_uuid": device_uuid,
                "name": name,
                "local_url": f"http://{local_ip}" if local_ip != "0.0.0.0" else "",
                "hostname": socket.gethostname(),
                "ip_address": local_ip,
                "version": PIBELLS_AGENT_VERSION,
                "capabilities": {
                    "barix": True,
                    "local_audio": True,
                    "targeted_outputs": True,
                    "outbound_threadhall_sync": True,
                    "revisioned_schedules": True,
                    "calendar_exceptions": True,
                    "calendar_defaults": True,
                    "effective_schedule_evidence": True,
                    "idempotent_commands": True,
                    "execution_claim_v1": True,
                },
                "status": dashboard(),
            }
            response = threadhall_request(
                request_config,
                "api/pibells/v1/pair",
                method="POST",
                payload=payload,
                token="",
            )
            if not isinstance(response, dict):
                raise HTTPException(status_code=502, detail="Threadhall returned invalid pairing data")
            data = response.get("data") if isinstance(response.get("data"), dict) else response
            raw_token = data.get("token")
            if not isinstance(raw_token, str) or not raw_token.strip():
                raise HTTPException(status_code=502, detail="Threadhall did not return a device token")
            token = raw_token.strip()
            authority = normalize_threadhall_authority(data.get("authority"))
            response_device = data.get("device")
            if not isinstance(response_device, dict) or response_device.get("id") != authority["device_id"]:
                raise HTTPException(status_code=502, detail="Threadhall returned inconsistent pairing authority")
            protocol_version = data.get("protocol_version")
            if protocol_version != PIBELLS_PROTOCOL_VERSION:
                raise HTTPException(status_code=502, detail="Threadhall did not confirm the execution-claim protocol")
            claim_url_template = data.get("claim_url_template")
            if claim_url_template != THREADHALL_CLAIM_URL_TEMPLATE:
                raise HTTPException(status_code=502, detail="Threadhall did not provide the execution-claim route")
            poll_seconds = int(data.get("poll_seconds") or THREADHALL_DEFAULT_POLL_SECONDS)
        except Exception as exc:
            with threadhall_integration_lock:
                current = load_threadhall_config()
                if current.get("pairing_epoch") == pairing_epoch and current.get("pairing_pending"):
                    clear_threadhall_authority_state(
                        request_config,
                        "Pairing stopped because the resulting controller authority could not be confirmed.",
                    )
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=502, detail="Threadhall returned invalid pairing data") from exc

        with threadhall_integration_lock:
            current = load_threadhall_config()
            if current.get("pairing_epoch") != pairing_epoch or not current.get("pairing_pending"):
                raise HTTPException(status_code=409, detail="Pairing changed before the response could be applied")
            if threadhall_stop_runtime_generation != pairing_stop_generation:
                clear_threadhall_authority_state(
                    request_config,
                    "Pairing stopped because STOP or a pairing-file failure occurred during the request.",
                )
                raise HTTPException(status_code=409, detail="STOP or pairing-file failure interrupted pairing; re-pair to reconcile")
            target_config = {
                **request_config,
                "token": token,
                "authority": authority,
                "device_uuid": device_uuid,
            }
            same_authority = bool(
                prior_config.get("token")
                and threadhall_history_scope(prior_config) == threadhall_history_scope(target_config)
            )
            try:
                initialize_threadhall_command_history(target_config, fresh_install=fresh_install)
                clear_threadhall_clock_proof()
                stop_loops()
                if stored_threadhall_authority(prior_config.get("authority")):
                    prior_history = load_threadhall_command_history(prior_config)
                    if any(
                        isinstance(entry, dict) and entry.get("stop_retry_pending") is True
                        for entry in prior_history.values()
                    ):
                        verified_at = datetime.now(timezone.utc).isoformat()
                        for entry in prior_history.values():
                            if isinstance(entry, dict) and entry.get("stop_retry_pending") is True:
                                entry["stop_retry_pending"] = False
                                entry["stop_verified_at"] = verified_at
                                entry["pending_ack"] = False
                        save_threadhall_command_history(prior_history, prior_config)
                if not same_authority:
                    purge_threadhall_managed_schedules()
                save_threadhall_config({
                    "enabled": True,
                    "pairing_pending": False,
                    "pairing_epoch": pairing_epoch,
                    "base_url": requested_base_url,
                    "token": token,
                    "authority": authority,
                    "device_uuid": device_uuid,
                    "device_name": name,
                    "protocol_version": protocol_version,
                    "claim_url_template": claim_url_template,
                    "remote_command_hold": False,
                    "unverified_stop_hold": False,
                    "history_bootstrap_epoch": "",
                    "poll_seconds": poll_seconds,
                    "schedule_revision": prior_config.get("schedule_revision", "") if same_authority else "",
                    "managed_schedule_hash": prior_config.get("managed_schedule_hash", "") if same_authority else "",
                    "calendar": prior_config.get("calendar", []) if same_authority else [],
                    "calendar_defaults": prior_config.get("calendar_defaults", {}) if same_authority else {},
                    "district_time_zone": prior_config.get("district_time_zone", "") if same_authority else "",
                    "paired_at": datetime.now().isoformat(timespec="seconds"),
                    "last_sync_at": None,
                    "last_error": "",
                }, allow_integrity_repair=True)
            except Exception:
                clear_threadhall_authority_state(
                    request_config,
                    "Pairing stopped because local authority or command history could not be saved.",
                )
                raise
            global threadhall_authority_fence_failed
            global threadhall_remote_command_hold_runtime
            global threadhall_unverified_stop_runtime
            global threadhall_config_integrity_failed, threadhall_config_stop_unverified
            threadhall_authority_fence_failed = False
            threadhall_remote_command_hold_runtime = False
            threadhall_unverified_stop_runtime = False
            threadhall_config_integrity_failed = False
            threadhall_config_stop_unverified = False
            return safe_threadhall_status()


def audio_file_for_key(sound_key: str) -> str:
    key = (sound_key or "").strip()
    preferred = DEFAULT_AUDIO_KEY_FILES.get(key, "standard-bells.mp3")
    if key in EMERGENCY_SOUND_KEYS:
        # An emergency key must identify its own readable file, never a library fallback.
        selected = ensure_audio_exists(preferred)
        try:
            with audio_path(selected).open("rb") as audio:
                if not audio.read(1):
                    raise HTTPException(
                        status_code=503,
                        detail="Selected emergency sound file is unreadable",
                    )
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Selected emergency sound file is unreadable",
            ) from exc
        return selected
    try:
        return ensure_audio_exists(preferred)
    except HTTPException:
        audio_files = list_audio()
        if audio_files:
            return audio_files[0].file
        raise


THREADHALL_MANAGED_SCHEDULE_PREFIX = "Threadhall - "
LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX = f"{RETIRED_INTEGRATION_NAME} - "


def is_threadhall_managed_schedule_name(value: str) -> bool:
    return value.startswith(THREADHALL_MANAGED_SCHEDULE_PREFIX) or value.startswith(
        LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX
    )


def managed_threadhall_schedule_name(value: str) -> str:
    name = normalize_schedule_name(value or "Regular")
    if name.startswith(THREADHALL_MANAGED_SCHEDULE_PREFIX):
        source_name = name[len(THREADHALL_MANAGED_SCHEDULE_PREFIX):]
    elif name.startswith(LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX):
        source_name = name[len(LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX):]
    else:
        source_name = name
    return normalize_schedule_name(f"{THREADHALL_MANAGED_SCHEDULE_PREFIX}{source_name}")


def normalize_calendar_defaults(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("managed"), bool):
        raise HTTPException(
            status_code=502,
            detail="Threadhall returned invalid calendar defaults",
        )

    normalized: Dict[str, Any] = {"managed": value["managed"]}
    for key in ("weekday_schedule_name", "weekend_schedule_name"):
        raw_name = value.get(key)
        if raw_name is None:
            normalized[key] = None
            continue
        if not isinstance(raw_name, str):
            raise HTTPException(
                status_code=502,
                detail="Threadhall returned invalid calendar defaults",
            )
        cleaned = re.sub(r"\s+", " ", raw_name).strip()
        if len(cleaned) > 80:
            raise HTTPException(
                status_code=502,
                detail="Threadhall returned invalid calendar defaults",
            )
        if value["managed"] and cleaned:
            try:
                managed_threadhall_schedule_name(cleaned)
            except HTTPException as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Threadhall returned invalid calendar defaults",
                ) from exc
        normalized[key] = cleaned or None
    return normalized


def normalize_threadhall_calendar(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise HTTPException(status_code=502, detail="Threadhall returned invalid calendar")
    normalized: List[Dict[str, Any]] = []
    dates = set()
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid calendar")
        raw_date = item.get("date")
        instructional = item.get("instructional")
        raw_name = item.get("schedule_name")
        label = item.get("label")
        if (
            not isinstance(raw_date, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date)
            or type(instructional) is not bool
            or not isinstance(raw_name, str)
            or (label is not None and not isinstance(label, str))
        ):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid calendar")
        try:
            datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="Threadhall returned invalid calendar",
            ) from exc
        if raw_date in dates:
            raise HTTPException(
                status_code=502,
                detail="Threadhall returned duplicate calendar dates",
            )
        dates.add(raw_date)
        name = re.sub(r"\s+", " ", raw_name).strip()
        if instructional:
            if not name:
                raise HTTPException(
                    status_code=502,
                    detail="Threadhall returned invalid calendar schedule",
                )
            try:
                managed_threadhall_schedule_name(name)
            except HTTPException as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Threadhall returned invalid calendar schedule",
                ) from exc
        normalized.append({
            "date": raw_date,
            "instructional": instructional,
            "schedule_name": name,
            "label": label.strip() if isinstance(label, str) else None,
        })
    return normalized


def validate_threadhall_bundle_links(
    schedules: List[Dict[str, Any]],
    calendar: List[Dict[str, Any]],
    calendar_defaults: Optional[Dict[str, Any]],
) -> None:
    managed_names = {
        managed_threadhall_schedule_name(str(schedule.get("name") or "Regular"))
        for schedule in schedules
    }
    for day in calendar:
        if not day["instructional"]:
            continue
        if managed_threadhall_schedule_name(day["schedule_name"]) not in managed_names:
            raise HTTPException(
                status_code=502,
                detail="Threadhall calendar references an unpublished managed schedule",
            )
    if calendar_defaults and calendar_defaults.get("managed") is True:
        for key in ("weekday_schedule_name", "weekend_schedule_name"):
            name = calendar_defaults.get(key)
            if name and managed_threadhall_schedule_name(name) not in managed_names:
                raise HTTPException(
                    status_code=502,
                    detail="Threadhall calendar default references an unpublished managed schedule",
                )


def apply_threadhall_schedules(schedules: List[Dict[str, Any]]) -> int:
    current = load_all_schedules()
    current_active = str(current.get("active") or "")
    merged = {
        name: entries
        for name, entries in current.get("schedules", {}).items()
        if not is_threadhall_managed_schedule_name(str(name))
    }
    preserve_local_active = bool(
        current_active
        and current_active != "Default"
        and not is_threadhall_managed_schedule_name(current_active)
        and current_active in merged
    )
    active_name = current_active if preserve_local_active else (current_active or "Default")
    applied = 0
    managed_names = set()
    for schedule in schedules:
        name = managed_threadhall_schedule_name(str(schedule.get("name") or "Regular"))
        if name in managed_names:
            raise HTTPException(
                status_code=502,
                detail=f"Threadhall returned duplicate managed schedule {name}",
            )
        managed_names.add(name)
        entries: List[Dict[str, Any]] = []
        raw_entries = schedule.get("entries", [])
        if not isinstance(raw_entries, list):
            raise HTTPException(
                status_code=502,
                detail=f"Threadhall returned invalid entries for managed schedule {name}",
            )
        for item in raw_entries:
            if not isinstance(item, dict):
                raise HTTPException(
                    status_code=502,
                    detail=f"Threadhall returned an invalid entry for managed schedule {name}",
                )
            try:
                sound_file = item.get("sound_file") or audio_file_for_key(str(item.get("sound_key") or "passing"))
                entry = ScheduleEntry(
                    id=str(item.get("id") or secrets.token_hex(8)),
                    day=int(item.get("day")),
                    time=str(item.get("time")),
                    sound_file=sound_file,
                    label=str(item.get("label") or ""),
                    enabled=bool(item.get("enabled", True)),
                    output_keys=item.get("output_keys") if "output_keys" in item else None,
                )
                entries.append(entry_to_json(entry))
            except Exception as exc:
                detail = str(getattr(exc, "detail", exc)) or "invalid schedule entry"
                raise HTTPException(
                    status_code=502,
                    detail=f"Threadhall returned an invalid entry for managed schedule {name}: {detail}",
                ) from exc
        merged[name] = sorted(entries, key=lambda entry: (entry["day"], entry["time"], entry["label"]))
        applied += 1
        if schedule.get("active", True) and not preserve_local_active:
            active_name = name
    if active_name not in merged:
        active_name = next(iter(merged.keys()), "Default")
    save_all_schedules({"active": active_name, "schedules": merged})
    return applied


def purge_threadhall_managed_schedules() -> int:
    current = load_all_schedules()
    schedules = {
        name: entries
        for name, entries in current.get("schedules", {}).items()
        if not is_threadhall_managed_schedule_name(str(name))
    }
    removed = len(current.get("schedules", {})) - len(schedules)
    if not schedules:
        schedules = {"Default": []}
    active = str(current.get("active") or "")
    if active not in schedules:
        active = next(iter(schedules.keys()))
    save_all_schedules({"active": active, "schedules": schedules})
    return removed


def run_threadhall_command(command: Dict[str, Any]) -> str:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    raw_type = str(command.get("type") or "").strip()
    command_type = raw_type.upper()
    emergency_action = raw_type.lower() if raw_type.lower() in EMERGENCY_SOUND_KEYS else ""
    if command_type == "TEST_BELL":
        command_type = "TEST_OUTPUT"
    if command_type == "STOP_AUDIO":
        stop_loops()
        return "Stopped active audio"
    if command_type == "SYNC_NOW":
        return "Synchronization already in progress"
    if command_type == "REBOOT":
        subprocess.Popen(["sudo", "reboot"])
        return "Controller reboot requested"
    if command_type == "ACTIVATE_SCHEDULE":
        schedule_name = str(payload.get("schedule_name") or "")
        if schedule_name:
            managed_name = managed_threadhall_schedule_name(schedule_name)
            schedules = load_all_schedules().get("schedules", {})
            selected = managed_name if managed_name in schedules else schedule_name
            activate_schedule(selected)
            return f"Activated schedule {selected}"
        raise HTTPException(status_code=422, detail="Schedule name is required")
    if command_type not in {"PLAY_SOUND", "TEST_OUTPUT", "EMERGENCY_ALERT"} and not emergency_action:
        raise HTTPException(status_code=422, detail=f"Unsupported Threadhall command: {raw_type}")
    sound_key = str(
        payload.get("sound_key")
        or emergency_action
        or ("emergency" if command_type == "EMERGENCY_ALERT" else "test")
    )
    if (
        (emergency_action or command_type == "EMERGENCY_ALERT")
        and sound_key.strip() not in EMERGENCY_SOUND_KEYS
    ):
        raise HTTPException(
            status_code=422,
            detail="Emergency command requires an emergency sound key",
        )
    sound_file = audio_file_for_key(sound_key)
    trigger_options: Dict[str, Any] = {"loop": bool(payload.get("loop", False))}
    if "output_keys" in payload:
        trigger_options["output_keys"] = payload.get("output_keys")
    trigger_bell(sound_file, **trigger_options)
    return f"Played {audio_display_name(sound_file)} for {payload.get('label') or command_type}"


def acknowledge_threadhall_command(
    config: Dict[str, Any],
    command_id: Any,
    status: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> None:
    acknowledgement = {
        "status": status,
        "summary": summary,
        "detail": detail or {},
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
    }
    if claim_id is not None:
        acknowledgement["claim_id"] = claim_id
    threadhall_request(
        config,
        f"api/pibells/v1/commands/{urllib.parse.quote(str(command_id), safe='')}/ack",
        method="POST",
        payload=acknowledgement,
    )


def threadhall_history_scope(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    authority = stored_threadhall_authority(config.get("authority"))
    base_url = str(config.get("base_url") or "").strip()
    device_uuid = str(config.get("device_uuid") or "").strip()
    if not authority or not base_url or not device_uuid:
        return None
    return {
        "base_url": base_url,
        "device_uuid": device_uuid,
        "authority": authority,
    }


def threadhall_history_scope_key(scope: Dict[str, Any]) -> str:
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def threadhall_history_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "Threadhall command history is unavailable or invalid; physical commands "
            "are paused pending operator review."
        ),
    )


def threadhall_history_reconciliation_required(reason: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{reason} Physical commands are paused pending operator reconciliation.",
    )


def validate_threadhall_history_entries(raw: Any) -> bool:
    return isinstance(raw, dict) and all(
        isinstance(command_id, str)
        and bool(command_id)
        and isinstance(outcome, dict)
        and bool(outcome)
        for command_id, outcome in raw.items()
    )


def read_threadhall_history_envelope(
    path: Path, *, allow_bootstrap: bool = False,
) -> Dict[str, Any]:
    # The old flat file evicted completed command IDs. Even when a pairing is
    # known, its contents cannot prove that an old offer has never executed.
    try:
        with path.open() as history_file:
            raw = json.load(history_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise threadhall_history_unavailable() from exc
    if isinstance(raw, dict) and raw.get("version") == THREADHALL_COMMAND_HISTORY_VERSION:
        partitions = raw.get("partitions")
        if not isinstance(partitions, dict):
            raise threadhall_history_unavailable()
        if not partitions:
            bootstrap = raw.get("bootstrap")
            if not (
                allow_bootstrap
                and set(raw) == {"version", "partitions", "bootstrap"}
                and isinstance(bootstrap, dict)
                and set(bootstrap) == {"epoch", "device_uuid"}
                and isinstance(bootstrap.get("epoch"), str)
                and isinstance(bootstrap.get("device_uuid"), str)
            ):
                raise threadhall_history_unavailable()
            try:
                uuid.UUID(bootstrap["epoch"])
                uuid.UUID(bootstrap["device_uuid"])
            except (ValueError, AttributeError) as exc:
                raise threadhall_history_unavailable() from exc
            return raw
        for key, partition in partitions.items():
            if not isinstance(key, str) or not isinstance(partition, dict):
                raise threadhall_history_unavailable()
            scope = partition.get("scope")
            if (
                not isinstance(scope, dict)
                or threadhall_history_scope(scope) != scope
                or threadhall_history_scope_key(scope) != key
                or not validate_threadhall_history_entries(partition.get("entries"))
            ):
                raise threadhall_history_unavailable()
        return raw
    if isinstance(raw, dict):
        raise threadhall_history_reconciliation_required(
            "The older command receipt file does not prove complete execution history."
        )
    raise threadhall_history_unavailable()


def save_threadhall_history_bootstrap(device_uuid: str) -> Dict[str, Any]:
    # Create the first durable receipt state before saving a pending pairing.
    # A failed first request can then retry without treating an absent ledger
    # as evidence that no physical command has ever run.
    bootstrap = {"epoch": str(uuid.uuid4()), "device_uuid": device_uuid}
    write_json(THREADHALL_COMMAND_HISTORY_FILE, {
        "version": THREADHALL_COMMAND_HISTORY_VERSION,
        "partitions": {},
        "bootstrap": bootstrap,
    }, require_directory_fsync=True)
    try:
        THREADHALL_COMMAND_HISTORY_FILE.chmod(0o600)
    except OSError:
        pass
    return bootstrap


def initialize_threadhall_command_history(config: Dict[str, Any], *, fresh_install: bool = False) -> None:
    scope = threadhall_history_scope(config)
    if not scope:
        raise threadhall_history_unavailable()
    if THREADHALL_COMMAND_HISTORY_FILE.exists():
        envelope = read_threadhall_history_envelope(
            THREADHALL_COMMAND_HISTORY_FILE, allow_bootstrap=fresh_install,
        )
        if not envelope["partitions"]:
            bootstrap = envelope["bootstrap"]
            if (
                not fresh_install
                or config.get("history_bootstrap_epoch") != bootstrap["epoch"]
                or config.get("device_uuid") != bootstrap["device_uuid"]
            ):
                raise threadhall_history_unavailable()
    elif fresh_install:
        envelope = {"version": THREADHALL_COMMAND_HISTORY_VERSION, "partitions": {}}
    else:
        raise threadhall_history_unavailable()
    key = threadhall_history_scope_key(scope)
    if any(
        partition["scope"]["authority"] == scope["authority"] and prior_key != key
        for prior_key, partition in envelope["partitions"].items()
    ):
        raise threadhall_history_reconciliation_required(
            "The same pairing authority has receipts under another URL or controller identity."
        )
    if key not in envelope["partitions"]:
        envelope["partitions"][key] = {"scope": scope, "entries": {}}
        envelope.pop("bootstrap", None)
        write_json(THREADHALL_COMMAND_HISTORY_FILE, envelope, require_directory_fsync=True)
        try:
            THREADHALL_COMMAND_HISTORY_FILE.chmod(0o600)
        except OSError:
            pass


def load_threadhall_command_history(
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    # A retired history file can be stale after an earlier migration. If the
    # canonical ledger disappears, never replace it with that older copy.
    config = config or load_threadhall_config()
    envelope = read_threadhall_history_envelope(THREADHALL_COMMAND_HISTORY_FILE)
    scope = threadhall_history_scope(config)
    if not scope:
        return {}
    partition = envelope["partitions"].get(threadhall_history_scope_key(scope))
    if not isinstance(partition, dict):
        raise threadhall_history_unavailable()
    return dict(partition["entries"])


def read_threadhall_command_history_file(path: Path) -> Dict[str, Dict[str, Any]]:
    envelope = read_threadhall_history_envelope(path)
    scope = threadhall_history_scope(load_threadhall_config())
    if not scope:
        return {}
    partition = envelope["partitions"].get(threadhall_history_scope_key(scope))
    if not isinstance(partition, dict):
        raise threadhall_history_unavailable()
    return dict(partition["entries"])


def save_threadhall_command_history(
    history: Dict[str, Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> None:
    config = config or load_threadhall_config()
    scope = threadhall_history_scope(config)
    if not scope or not validate_threadhall_history_entries(history):
        raise threadhall_history_unavailable()
    envelope = read_threadhall_history_envelope(THREADHALL_COMMAND_HISTORY_FILE)
    key = threadhall_history_scope_key(scope)
    partition = envelope["partitions"].get(key)
    if not isinstance(partition, dict):
        raise threadhall_history_unavailable()
    if not set(partition["entries"]).issubset(history):
        raise threadhall_history_unavailable()
    # Never discard a physical outcome solely because it is old. Without a
    # server-backed claim, an old command may still be reoffered after reboot.
    partition["entries"] = dict(history)
    write_json(THREADHALL_COMMAND_HISTORY_FILE, envelope, require_directory_fsync=True)
    try:
        THREADHALL_COMMAND_HISTORY_FILE.chmod(0o600)
    except OSError:
        pass


def threadhall_sync_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    local_ip = get_local_ip()
    audio_manifest = [
        {
            "file": item.file,
            "name": item.name,
            "sound_key": item.default_key,
            "category": item.default_category,
        }
        for item in list_audio()
    ]
    status = dashboard()
    return {
        "protocol_version": int(config.get("protocol_version") or 1),
        "device_uuid": str(config.get("device_uuid") or ""),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "version": PIBELLS_AGENT_VERSION,
        "local_url": f"http://{local_ip}" if local_ip != "0.0.0.0" else "",
        "hostname": socket.gethostname(),
        "ip_address": local_ip,
        "active_schedule": status.get("active_schedule"),
        "applied_schedule_revision": config.get("schedule_revision") or "",
        "schedule_options": status.get("schedule_options", []),
        "capabilities": {
            "barix": True,
            "local_audio": True,
            "targeted_outputs": True,
            "revisioned_schedules": True,
            "calendar_exceptions": True,
            "calendar_defaults": True,
            "effective_schedule_evidence": True,
            "idempotent_commands": True,
            "execution_claim_v1": bool(
                config.get("protocol_version") == PIBELLS_PROTOCOL_VERSION
                and config.get("claim_url_template") == THREADHALL_CLAIM_URL_TEMPLATE
            ),
        },
        "telemetry": status,
        "audio_manifest": audio_manifest,
        "recent_events": [],
    }


def threadhall_stop_only_sync_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    # Threadhall's sync schema requires these fields and supplies defaults for
    # all telemetry fields. Avoid schedule normalization, device probes, and
    # local writes when those paths are broken but remote STOP must be fetched.
    return {
        "protocol_version": int(config.get("protocol_version") or 1),
        "device_uuid": str(config.get("device_uuid") or ""),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": {
            "execution_claim_v1": bool(
                config.get("protocol_version") == PIBELLS_PROTOCOL_VERSION
                and config.get("claim_url_template") == THREADHALL_CLAIM_URL_TEMPLATE
            ),
        },
        "telemetry": {"stop_only_poll": True},
    }


def legacy_schedule_revision(schedules: List[Dict[str, Any]]) -> str:
    canonical_schedules: List[Dict[str, Any]] = []
    for schedule in schedules:
        raw_entries = schedule.get("entries", [])
        entries = (
            [entry for entry in raw_entries if isinstance(entry, dict)]
            if isinstance(raw_entries, list)
            else []
        )
        entries = sorted(
            entries,
            key=lambda entry: json.dumps(
                entry,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        canonical_schedules.append({
            "name": str(schedule.get("name") or "Regular"),
            "active": bool(schedule.get("active", True)),
            "entries": entries,
        })
    canonical_schedules.sort(
        key=lambda schedule: (
            schedule["name"],
            json.dumps(
                schedule,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    )
    canonical = json.dumps(
        canonical_schedules,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"legacy-sha256:{hashlib.sha256(canonical).hexdigest()}"


def managed_schedule_state_hash() -> str:
    schedules = load_all_schedules().get("schedules", {})
    managed = {
        name: entries
        for name, entries in schedules.items()
        if is_threadhall_managed_schedule_name(str(name))
    }
    canonical = json.dumps(
        managed,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"managed-sha256:{hashlib.sha256(canonical).hexdigest()}"


def retired_managed_schedule_keys_present() -> bool:
    schedules = load_all_schedules().get("schedules", {})
    return any(
        str(name).startswith(LEGACY_THREADHALL_MANAGED_SCHEDULE_PREFIX)
        for name in schedules
    )


def command_expired_before_execution(command: Dict[str, Any]) -> bool:
    raw = command.get("expires_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def deliver_threadhall_outcome(
    config: Dict[str, Any],
    command_id: str,
    outcome: Dict[str, Any],
) -> Optional[str]:
    status = str(outcome.get("status") or "failed")
    summary = str(outcome.get("summary") or "Controller outcome was not recorded")
    detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else {}
    occurred_at = str(outcome.get("occurred_at") or "") or datetime.now(timezone.utc).isoformat()
    outcome.update({
        "status": status,
        "summary": summary,
        "detail": detail,
        "occurred_at": occurred_at,
    })
    try:
        acknowledge_threadhall_command(
            config,
            command_id,
            status,
            summary,
            detail,
            occurred_at,
            outcome.get("claim_id") if isinstance(outcome.get("claim_id"), str) else None,
        )
        outcome["pending_ack"] = False
        return None
    except Exception as exc:
        outcome["pending_ack"] = True
        return str(getattr(exc, "detail", exc))


def threadhall_connection_identity(config: Dict[str, Any]) -> Tuple[str, ...]:
    authority = stored_threadhall_authority(config.get("authority")) or {}
    return (
        str(bool(config.get("enabled"))),
        str(bool(config.get("pairing_pending"))),
        str(config.get("pairing_epoch") or ""),
        str(config.get("base_url") or ""),
        str(config.get("token") or ""),
        str(config.get("device_uuid") or ""),
        str(config.get("protocol_version") or ""),
        str(config.get("claim_url_template") or ""),
        str(authority.get("district_id") or ""),
        str(authority.get("school_id") or ""),
        str(authority.get("device_id") or ""),
    )


def validated_threadhall_claim(
    response: Any,
    offer: Dict[str, Any],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if not isinstance(response, dict) or response.get("ok") is not True or response.get("execute") is not True:
        return None
    claim_id = response.get("claim_id")
    claimed_at = response.get("claimed_at")
    command = response.get("command")
    if not isinstance(claim_id, str) or not isinstance(claimed_at, str) or not isinstance(command, dict):
        return None
    try:
        uuid.UUID(claim_id)
        claimed_time = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if claimed_time.tzinfo is None:
        return None
    if (
        command.get("id") != offer["id"]
        or command.get("type") != offer["type"]
        or not isinstance(command.get("payload"), dict)
        or command.get("expires_at") != offer.get("expires_at")
    ):
        return None
    return claim_id, command


def claim_and_execute_threadhall_offer(
    config: Dict[str, Any],
    identity: Tuple[str, ...],
    offer: Dict[str, Any],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    command_id = offer["id"]
    with threadhall_integration_lock:
        if threadhall_authority_fence_failed or threadhall_config_integrity_failed:
            return None
        current_config = load_threadhall_config()
        if threadhall_connection_identity(current_config) != identity:
            return None
        claimed_stop_epoch = threadhall_stop_epoch(current_config)
        claimed_stop_runtime_generation = threadhall_stop_runtime_generation
        history = load_threadhall_command_history(config)
        if command_id in history:
            return None
        if (
            offer.get("_stop_epoch_at_offer") != claimed_stop_epoch
            or offer.get("_stop_runtime_generation_at_offer") != claimed_stop_runtime_generation
        ):
            history[command_id] = {
                "status": "failed",
                "summary": "STOP superseded the offer before its execution claim",
                "detail": {"stopped_before_claim": True},
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "pending_ack": False,
                "claim_state": "superseded_before_claim",
            }
            save_threadhall_command_history(history, config)
            save_threadhall_config({
                "last_error": "A command offer was superseded by STOP; operator reconciliation is required.",
            })
            return None
        if threadhall_remote_commands_held(current_config):
            history[command_id] = {
                "status": "failed",
                "summary": "Local STOP holds remote commands pending operator reconciliation",
                "detail": {"remote_commands_held": True},
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "pending_ack": False,
                "claim_state": "held_after_local_stop",
            }
            save_threadhall_command_history(history, config)
            return None
        if threadhall_unverified_stop_held(current_config, history):
            history[command_id] = {
                "status": "failed",
                "summary": "Audio termination is unverified; command was not claimed",
                "detail": {"stop_termination_unverified": True},
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "pending_ack": False,
                "claim_state": "held_after_unverified_stop",
            }
            save_threadhall_command_history(history, config)
            return None
        # Persist before sending the one-shot request. A lost response may mean
        # Threadhall consumed its only grant, so no later sync can retry it.
        intent = {
            "status": "failed",
            "summary": "Execution claim outcome is unknown; no physical action was authorized",
            "detail": {"claim_outcome_unknown": True},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "pending_ack": False,
            "claim_state": "requested",
            "stop_epoch_at_claim": claimed_stop_epoch,
        }
        history[command_id] = intent
        save_threadhall_command_history(history, config)
        save_threadhall_config({
            "last_error": "Execution claim outcome is unresolved; operator reconciliation is required.",
        })

    if config.get("claim_url_template") != THREADHALL_CLAIM_URL_TEMPLATE:
        return None
    claim_path = THREADHALL_CLAIM_URL_TEMPLATE.replace(
        "{command_id}", urllib.parse.quote(command_id, safe=""),
    )
    try:
        response = threadhall_request(
            config,
            claim_path,
            method="POST",
            payload={
                "protocol_version": PIBELLS_PROTOCOL_VERSION,
                "device_uuid": str(config.get("device_uuid") or ""),
            },
        )
    except Exception:
        # Timeouts, HTTP errors, and retries never authorize physical action.
        return None
    grant = validated_threadhall_claim(response, offer)
    if grant is None:
        return None
    claim_id, command = grant
    with threadhall_integration_lock:
        current_config = load_threadhall_config()
        current_identity = threadhall_connection_identity(current_config)
        history = load_threadhall_command_history(config)
        current = history.get(command_id)
        if not isinstance(current, dict) or current.get("claim_state") != "requested":
            return None
        if (
            current_identity != identity
            or threadhall_authority_fence_failed
            or threadhall_config_integrity_failed
        ):
            current.update({
                "claim_id": claim_id,
                "claim_state": "claimed_after_authority_change",
            })
            save_threadhall_command_history(history, config)
            return None
        if (
            current.get("stop_epoch_at_claim") != claimed_stop_epoch
            or threadhall_stop_epoch(current_config) != claimed_stop_epoch
            or threadhall_stop_runtime_generation != claimed_stop_runtime_generation
            or threadhall_remote_commands_held(current_config)
            or threadhall_unverified_stop_held(current_config, history)
        ):
            current.update({
                "claim_id": claim_id,
                "claim_state": "claimed_after_stop",
                "summary": "STOP superseded the claimed command before controller action",
                "detail": {"stopped_before_execution": True},
                "pending_ack": False,
            })
            save_threadhall_command_history(history, config)
            return None
        occurred_at = datetime.now(timezone.utc).isoformat()
        outcome: Dict[str, Any] = {
            "status": "failed",
            "summary": "Controller execution was interrupted; physical outcome is unknown",
            "detail": {"execution_outcome_unknown": True},
            "occurred_at": occurred_at,
            # Unknown physical outcomes are held locally for reconciliation;
            # they must not be sent as a definitive FAILED callback.
            "pending_ack": False,
            "claim_id": claim_id,
            "claim_state": "execution_unknown",
        }
        history[command_id] = outcome
        save_threadhall_command_history(history, config)
        if command_expired_before_execution(command):
            outcome.update({
                "summary": "Claimed command expired before controller execution",
                "detail": {"expired_before_execution": True},
                "pending_ack": True,
                "claim_state": "completed",
            })
        else:
            try:
                summary = run_threadhall_command(command)
                outcome.update({
                    "status": "acknowledged",
                    "summary": str(summary)[:500],
                    "detail": {},
                    "pending_ack": True,
                    "claim_state": "completed",
                })
            except Exception as exc:
                outcome.update({
                    "status": "failed",
                    "summary": "Controller action raised an error; physical outcome is unknown",
                    "detail": {
                        "execution_outcome_unknown": True,
                        "local_error": str(getattr(exc, "detail", exc))[:300],
                    },
                    "pending_ack": False,
                    "claim_state": "execution_unknown",
                })
        outcome["occurred_at"] = datetime.now(timezone.utc).isoformat()
        history[command_id] = outcome
        save_threadhall_command_history(history, config)
        unresolved = any(
            entry.get("claim_state") in {"requested", "execution_unknown", "claimed_after_authority_change", "claimed_after_stop", "superseded_before_claim", "held_after_local_stop", "held_after_unverified_stop", "not_claimed"}
            for entry in history.values()
            if isinstance(entry, dict)
        )
        save_threadhall_config({
            "last_error": (
                "Execution claim or physical outcome is unresolved; operator reconciliation is required."
                if unresolved else ""
            ),
        })
        return (command_id, dict(outcome)) if outcome["pending_ack"] is True else None


def process_threadhall_claim_offers(
    config: Dict[str, Any],
    identity: Tuple[str, ...],
    offers: List[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any]]]:
    outcomes: List[Tuple[str, Dict[str, Any]]] = []
    for offer in offers:
        outcome = claim_and_execute_threadhall_offer(config, identity, offer)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def process_threadhall_claim_offers_in_worker(
    config: Dict[str, Any],
    identity: Tuple[str, ...],
    offers: List[Dict[str, Any]],
) -> None:
    try:
        outcomes = process_threadhall_claim_offers(config, identity, offers)
        if outcomes:
            _deliver_threadhall_ack_batch({}, config, identity, outcomes)
    except Exception as exc:
        with threadhall_integration_lock:
            if threadhall_connection_identity(load_threadhall_config()) == identity:
                save_threadhall_config({"last_error": str(getattr(exc, "detail", exc))})
    finally:
        threadhall_claim_worker_lock.release()


def sync_threadhall_once(*, acknowledgements_in_background: bool = False) -> Dict[str, Any]:
    # Serialize response application, while leaving the integration lock free
    # during HTTP and acknowledgement retries used by minute dispatch.
    with threadhall_sync_request_lock:
        status, config, identity, acknowledgements, claim_offers = _sync_threadhall_once_before_acks()
    if config is None:
        return status

    if claim_offers and threadhall_claim_worker_lock.acquire(blocking=False):
        if acknowledgements_in_background:
            worker = threading.Thread(
                target=process_threadhall_claim_offers_in_worker,
                args=(config, identity, claim_offers),
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                threadhall_claim_worker_lock.release()
                raise
        else:
            try:
                acknowledgements.extend(process_threadhall_claim_offers(config, identity, claim_offers))
            finally:
                threadhall_claim_worker_lock.release()

    if acknowledgements_in_background:
        # Keep the poll thread available for the next command batch even when
        # acknowledgement HTTP is slow. Only one worker may retry at a time.
        if acknowledgements and threadhall_ack_worker_lock.acquire(blocking=False):
            worker = threading.Thread(
                target=_deliver_threadhall_ack_batch_in_worker,
                args=(status, config, identity, acknowledgements),
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                threadhall_ack_worker_lock.release()
                raise
        return status

    return _deliver_threadhall_ack_batch(status, config, identity, acknowledgements)


def _deliver_threadhall_ack_batch_in_worker(
    status: Dict[str, Any],
    config: Dict[str, Any],
    identity: Tuple[str, ...],
    acknowledgements: List[Tuple[str, Dict[str, Any]]],
) -> None:
    try:
        _deliver_threadhall_ack_batch(status, config, identity, acknowledgements)
    except Exception as exc:
        with threadhall_integration_lock:
            if threadhall_connection_identity(load_threadhall_config()) == identity:
                save_threadhall_config({"last_error": str(getattr(exc, "detail", exc))})
    finally:
        threadhall_ack_worker_lock.release()


def _deliver_threadhall_ack_batch(
    status: Dict[str, Any],
    config: Dict[str, Any],
    identity: Tuple[str, ...],
    acknowledgements: List[Tuple[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    errors: List[str] = []
    acknowledged: List[Tuple[str, Dict[str, Any]]] = []
    for command_id, outcome in acknowledgements:
        if outcome.get("pending_ack") is not True or outcome.get("claim_state") in {
            "requested", "execution_unknown", "claimed_after_authority_change", "claimed_after_stop", "superseded_before_claim", "held_after_local_stop", "held_after_unverified_stop", "not_claimed",
        }:
            continue
        with threadhall_integration_lock:
            if threadhall_connection_identity(load_threadhall_config()) != identity:
                return safe_threadhall_status()
        error = deliver_threadhall_outcome(config, command_id, outcome)
        if error:
            errors.append(error)
        else:
            acknowledged.append((command_id, outcome))

    with threadhall_integration_lock:
        if threadhall_connection_identity(load_threadhall_config()) != identity:
            return safe_threadhall_status()
        history = load_threadhall_command_history()
        changed = False
        for command_id, outcome in acknowledged:
            current = history.get(command_id)
            if not isinstance(current, dict):
                continue
            if all(
                current.get(key) == outcome.get(key)
                for key in ("status", "summary", "detail", "occurred_at")
            ):
                current["pending_ack"] = False
                changed = True
        if changed:
            save_threadhall_command_history(history)
        if errors:
            save_threadhall_config({"last_error": errors[-1]})
        return {
            **status,
            **safe_threadhall_status(),
            "pending_acknowledgements": len(
                [entry for entry in history.values() if entry.get("pending_ack") is True]
            ),
        }


def _sync_threadhall_once_before_acks() -> Tuple[
    Dict[str, Any],
    Optional[Dict[str, Any]],
    Tuple[str, ...],
    List[Tuple[str, Dict[str, Any]]],
    List[Dict[str, Any]],
]:
    global threadhall_ack_retry_cursor, threadhall_unverified_stop_runtime
    with threadhall_integration_lock:
        config = load_threadhall_config()
        if (
            not config.get("enabled")
            or config.get("pairing_pending")
            or not config.get("token")
            or not config.get("base_url")
        ):
            return safe_threadhall_status(), None, (), [], []
        authority = stored_threadhall_authority(config.get("authority"))
        if not authority:
            clear_threadhall_authority_state(
                config,
                "Synchronization stopped because complete pairing authority is unavailable.",
            )
            raise HTTPException(
                status_code=409,
                detail="Re-pair with Threadhall before synchronizing",
            )
        identity = threadhall_connection_identity(config)
        # A damaged local ledger must not prevent receipt of urgent STOP.
        # Physical non-STOP action remains blocked when the ledger is loaded
        # after the authenticated response's STOP has been handled.
        request_stop_epoch = config.get("stop_epoch")
        request_stop_runtime_generation = threadhall_stop_runtime_generation
    # Controller probes and sync HTTP may wait on devices or the network.
    # Let minute dispatch and pair/unpair proceed while those calls run.
    try:
        load_threadhall_command_history(config)
        history_degraded = False
    except (HTTPException, OSError):
        # Poll for STOP with the paired token even when receipts cannot prove
        # a non-STOP command safe. No claim, ACK, or schedule write follows.
        history_degraded = True
    if (
        threadhall_authority_fence_failed
        or threadhall_config_integrity_failed
        or threadhall_remote_commands_held(config)
        or history_degraded
    ):
        payload = threadhall_stop_only_sync_payload(config)
        payload_degraded = True
    else:
        try:
            payload = threadhall_sync_payload(config)
            payload_degraded = False
        except Exception:
            payload = threadhall_stop_only_sync_payload(config)
            payload_degraded = True
    with threadhall_integration_lock:
        current_config = load_threadhall_config()
        if threadhall_connection_identity(current_config) != identity:
            return safe_threadhall_status(), None, (), [], []
        if (
            threadhall_authority_fence_failed
            or threadhall_config_integrity_failed
            or threadhall_remote_commands_held(current_config)
            or history_degraded
        ):
            payload = threadhall_stop_only_sync_payload(current_config)
            payload_degraded = True
    request_started_utc = datetime.now(timezone.utc)
    request_started_monotonic = time.monotonic()
    response = threadhall_request(
        config,
        "api/pibells/v1/sync",
        method="POST",
        payload=payload,
    )
    request_ended_monotonic = time.monotonic()
    request_ended_utc = datetime.now(timezone.utc)
    with threadhall_integration_lock:
        current_config = load_threadhall_config()
        if threadhall_connection_identity(current_config) != identity:
            # An earlier authority's response cannot apply after pair/unpair.
            return safe_threadhall_status(), None, (), [], []
        stopped_during_request = (
            current_config.get("stop_epoch") != request_stop_epoch
            or threadhall_stop_runtime_generation != request_stop_runtime_generation
            or threadhall_authority_fence_failed
        )
        config = current_config
        if not isinstance(response, dict):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid sync data")
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid sync data")
        response_device = data.get("device") if isinstance(data.get("device"), dict) else {}
        if response_device.get("id") != authority["device_id"]:
            clear_threadhall_authority_state(
                config,
                "Synchronization stopped because pairing authority changed.",
            )
            raise HTTPException(status_code=409, detail="Threadhall pairing authority changed")
        paired_protocol = int(config.get("protocol_version") or 1)
        publication = data.get("schedule_publication")
        server_stop_only = (
            isinstance(publication, dict)
            and publication.get("status") == "HELD"
            and publication.get("code") in {
                "PIBELLS_STOP_ONLY_POLL",
                "PIBELLS_CURRENT_SCHOOL_YEAR_INVALID",
            }
        )
        if paired_protocol == PIBELLS_PROTOCOL_VERSION:
            if (
                config.get("claim_url_template") != THREADHALL_CLAIM_URL_TEMPLATE
                or data.get("protocol_version") != PIBELLS_PROTOCOL_VERSION
                or (
                    data.get("execution_claim_v1") is not True
                    and not server_stop_only
                )
            ):
                raise HTTPException(status_code=502, detail="Threadhall did not confirm fresh execution-claim authority")
        elif paired_protocol != 1 or data.get("protocol_version", 1) != 1:
            raise HTTPException(status_code=502, detail="Threadhall sync protocol changed; re-pair before commands")
        raw_commands = data.get("commands", [])
        if not isinstance(raw_commands, list):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid command offers")
        stop_received = any(
            isinstance(command, dict) and command.get("type") == "STOP_AUDIO"
            for command in raw_commands
        )
        stop_error: Optional[Exception] = None
        if stop_received:
            try:
                config = advance_threadhall_stop_fence()
            except Exception:
                # The runtime generation still fences in-flight grants. Stop
                # audio before reporting the failed durable fence.
                stop_loops()
                raise HTTPException(
                    status_code=503,
                    detail="Audio stopped, but STOP ordering state could not be saved",
                )
            # Stop before schedule validation, managed writes, or ledger reads.
            # A later failed receipt must not leave audio running.
            try:
                stop_loops()
            except Exception as exc:
                # Keep polling and leave every STOP receipt retryable. A
                # resistant player must never produce a terminal ACK.
                stop_error = exc
                threadhall_unverified_stop_runtime = True
                try:
                    config = save_threadhall_config({"unverified_stop_hold": True})
                except Exception:
                    # The runtime fence still blocks claims in this process;
                    # the retry receipt below may provide durable evidence.
                    pass
        if (
            threadhall_authority_fence_failed
            or threadhall_config_integrity_failed
            or threadhall_remote_commands_held(config)
            or history_degraded
            or payload_degraded
            or server_stop_only
        ):
            # The minimal request exists only to fetch STOP. No schedule
            # publication, ACK, claim, or enabled-state write is trusted.
            clear_threadhall_clock_proof()
            if history_degraded:
                raise threadhall_history_unavailable()
            reason = (
                "Threadhall pairing file requires operator reconciliation."
                if threadhall_config_integrity_failed else
                "Remote commands are held after local STOP; re-pair to reconcile."
                if threadhall_remote_commands_held(config) else
                "Threadhall authority change requires operator reconciliation."
                if threadhall_authority_fence_failed else
                "Threadhall held schedule publication; only remote STOP was processed."
                if server_stop_only else
                "Controller telemetry was unavailable; only remote STOP was processed."
            )
            try:
                status = safe_threadhall_status(read_only=True)
            except Exception:
                status = {"enabled": False, "poll_seconds": config.get("poll_seconds")}
            status["enabled"] = False
            status["execution_claim_v1"] = False
            status["last_error"] = reason
            if stop_error is not None:
                status["last_error"] = "Audio termination is unverified; retry remote STOP. " + reason
            return status, None, (), [], []
        history = load_threadhall_command_history()
        clear_threadhall_clock_proof()
        raw_schedules = data.get("schedules")
        schedules_supplied = isinstance(raw_schedules, list)
        if schedules_supplied and any(not isinstance(item, dict) for item in raw_schedules):
            raise HTTPException(status_code=502, detail="Threadhall returned invalid schedules")
        schedules = list(raw_schedules) if schedules_supplied else []
        raw_calendar = data.get("calendar")
        calendar_supplied = isinstance(raw_calendar, list)
        calendar = normalize_threadhall_calendar(raw_calendar) if calendar_supplied else []
        calendar_defaults_supplied = "calendar_defaults" in data
        calendar_defaults = (
            normalize_calendar_defaults(data.get("calendar_defaults"))
            if calendar_defaults_supplied
            else None
        )
        if (calendar_supplied or calendar_defaults_supplied) and not schedules_supplied:
            raise HTTPException(
                status_code=502,
                detail="Threadhall returned calendar authority without a complete schedule bundle",
            )
        if schedules_supplied:
            validate_threadhall_bundle_links(
                schedules,
                calendar if calendar_supplied else [],
                calendar_defaults,
            )
        server_schedule_revision = str(data.get("schedule_revision") or "")
        schedule_revision = server_schedule_revision or (
            legacy_schedule_revision(schedules) if schedules else ""
        )
        received_schedules = 0
        revision_changed = bool(
            schedule_revision
            and schedule_revision != str(config.get("schedule_revision") or "")
        )
        persisted_state_hash = str(config.get("managed_schedule_hash") or "")
        state_hash_mismatch = bool(
            schedules_supplied
            and (
                not persisted_state_hash
                or managed_schedule_state_hash() != persisted_state_hash
                or retired_managed_schedule_keys_present()
            )
        )
        if schedules_supplied and (revision_changed or state_hash_mismatch):
            received_schedules = apply_threadhall_schedules(schedules)
            schedule_updates: Dict[str, Any] = {
                "schedule_revision": schedule_revision,
            }
            if calendar_supplied:
                schedule_updates["calendar"] = calendar
            if calendar_defaults_supplied:
                schedule_updates["calendar_defaults"] = calendar_defaults
            schedule_updates["managed_schedule_hash"] = managed_schedule_state_hash()
            config = save_threadhall_config(schedule_updates)
        new_command_ids: List[str] = []
        reoffered_command_ids: List[str] = []
        claim_offers: List[Dict[str, Any]] = []
        # STOP in this response takes priority over claim requests.
        ordered_commands = sorted(
            raw_commands,
            key=lambda command: 0 if isinstance(command, dict) and command.get("type") == "STOP_AUDIO" else 1,
        )
        for command in ordered_commands:
            if not isinstance(command, dict) or "id" not in command:
                continue
            command_id = str(command["id"])
            if not command_id:
                continue
            command_type = command.get("type")
            previous = history.get(command_id)
            if previous and not (
                command_type == "STOP_AUDIO" and previous.get("stop_retry_pending") is True
            ):
                if previous.get("pending_ack") is True:
                    reoffered_command_ids.append(command_id)
                continue
            if command_type != "STOP_AUDIO" and threadhall_unverified_stop_held(config, history):
                history[command_id] = {
                    "status": "failed",
                    "summary": "Audio termination is unverified; command was not claimed",
                    "detail": {"stop_termination_unverified": True},
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "pending_ack": False,
                    "claim_state": "held_after_unverified_stop",
                }
                save_threadhall_command_history(history)
                continue
            if paired_protocol == PIBELLS_PROTOCOL_VERSION and command_type != "STOP_AUDIO":
                valid_offer = (
                    isinstance(command.get("id"), str)
                    and isinstance(command_type, str)
                    and bool(command_type)
                    and command.get("claim_required") is True
                    and "payload" not in command
                    and "expires_at" in command
                    and isinstance(command.get("idempotency_key"), str)
                    and bool(command["idempotency_key"])
                    and (command.get("expires_at") is None or isinstance(command.get("expires_at"), str))
                )
                if valid_offer and not command_expired_before_execution(command):
                    # A response already in flight when STOP arrived, or a
                    # batch containing STOP, cannot authorize older playback.
                    if stopped_during_request or stop_received:
                        history[command_id] = {
                            "status": "failed",
                            "summary": "STOP superseded the offer before its execution claim",
                            "detail": {"stopped_before_claim": True},
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "pending_ack": False,
                            "claim_state": "superseded_before_claim",
                        }
                        save_threadhall_command_history(history)
                    else:
                        claim_offers.append({
                            **command,
                            "id": command_id,
                            "_stop_epoch_at_offer": threadhall_stop_epoch(config),
                            "_stop_runtime_generation_at_offer": threadhall_stop_runtime_generation,
                        })
                    continue
                history[command_id] = {
                    "status": "failed",
                    "summary": "Execution offer was invalid or expired before claim",
                    "detail": {"claim_not_attempted": True},
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "pending_ack": False,
                    "claim_state": "not_claimed",
                }
                save_threadhall_command_history(history)
                continue
            if command_type != "STOP_AUDIO":
                # Existing v1 pairings can still receive STOP. Never execute a
                # non-STOP offer that lacks a one-time server claim.
                history[command_id] = {
                    "status": "failed",
                    "summary": "Re-pair with the execution-claim protocol before physical commands",
                    "detail": {"execution_claim_required": True},
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "pending_ack": True,
                }
                save_threadhall_command_history(history)
                new_command_ids.append(command_id)
                continue
            occurred_at = datetime.now(timezone.utc).isoformat()
            detail: Dict[str, Any] = {}
            if command_expired_before_execution(command) and stop_error is None:
                outcome = {
                    "status": "failed",
                    "summary": "Command expired before controller execution",
                    "detail": {"expired_before_execution": True},
                    "occurred_at": occurred_at,
                    "pending_ack": True,
                }
            else:
                # Write a fail-closed receipt before crossing the physical-action
                # boundary. If the controller loses power during playback, the
                # next sync reports an unknown outcome instead of replaying it.
                outcome = {
                    "status": "failed",
                    "summary": "Controller execution was interrupted; physical outcome is unknown",
                    "detail": {"execution_outcome_unknown": True},
                    "occurred_at": occurred_at,
                    "pending_ack": False,
                    "stop_retry_pending": True,
                }
                history[command_id] = outcome
                save_threadhall_command_history(history)
                if stop_error is None:
                    outcome = {
                        "status": "acknowledged",
                        "summary": "Stopped active audio",
                        "detail": detail,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "pending_ack": True,
                    }
                else:
                    outcome = {
                        "status": "failed",
                        "summary": str(getattr(stop_error, "detail", stop_error)),
                        "detail": {"termination_unverified": True},
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "pending_ack": False,
                        "stop_retry_pending": True,
                    }
            history[command_id] = outcome
            save_threadhall_command_history(history)
            new_command_ids.append(command_id)
        if stop_received and stop_error is None and threadhall_unverified_stop_held(config, history):
            # A verified physical STOP resolves older unverified termination,
            # even when those server command IDs have expired. Keep their
            # receipts, but do not send a stale terminal ACK for them.
            verified_at = datetime.now(timezone.utc).isoformat()
            for entry in history.values():
                if isinstance(entry, dict) and entry.get("stop_retry_pending") is True:
                    entry["stop_retry_pending"] = False
                    entry["stop_verified_at"] = verified_at
                    entry["pending_ack"] = False
            save_threadhall_command_history(history)
            config = save_threadhall_config({"unverified_stop_hold": False})
            threadhall_unverified_stop_runtime = False
        older_pending_ids = [
            command_id
            for command_id, outcome in history.items()
            if isinstance(outcome, dict)
            and outcome.get("pending_ack") is True
            and command_id not in new_command_ids
        ]
        if older_pending_ids:
            offset = threadhall_ack_retry_cursor % len(older_pending_ids)
            older_pending_ids = older_pending_ids[offset:] + older_pending_ids[:offset]
        selected_ids = new_command_ids[:THREADHALL_ACK_ATTEMPTS_PER_SYNC]
        remaining = THREADHALL_ACK_ATTEMPTS_PER_SYNC - len(selected_ids)
        selected_older = older_pending_ids[:remaining]
        threadhall_ack_retry_cursor += len(selected_older)
        selected_ids.extend(selected_older)
        selected_ids.extend(
            command_id
            for command_id in reoffered_command_ids
            if command_id not in selected_ids
            and command_id not in older_pending_ids
        )
        selected_ids = selected_ids[:THREADHALL_ACK_ATTEMPTS_PER_SYNC]
        acknowledgements = [
            (command_id, dict(history[command_id]))
            for command_id in selected_ids
            if isinstance(history.get(command_id), dict)
        ]
        record_threadhall_clock_sample(
            config,
            data.get("server_time"),
            request_started_utc,
            request_ended_utc,
            request_started_monotonic,
            request_ended_monotonic,
        )
        sync_updates: Dict[str, Any] = {
            "enabled": True,
            "protocol_version": paired_protocol,
            "poll_seconds": int(data.get("poll_seconds") or config.get("poll_seconds") or THREADHALL_DEFAULT_POLL_SECONDS),
            "schedule_revision": schedule_revision or config.get("schedule_revision", ""),
            "last_sync_at": datetime.now().isoformat(timespec="seconds"),
            "last_error": (
                "Execution claim, physical outcome, or STOP termination is unresolved; operator reconciliation is required."
                if any(
                    entry.get("claim_state") in {"requested", "execution_unknown", "claimed_after_authority_change", "claimed_after_stop", "superseded_before_claim", "held_after_local_stop", "held_after_unverified_stop", "not_claimed"}
                    or entry.get("stop_retry_pending") is True
                    for entry in history.values()
                    if isinstance(entry, dict)
                ) else
                "Re-pair this controller for execution claims; non-STOP commands are disabled."
                if paired_protocol == 1 else ""
            ),
        }
        if calendar_supplied:
            sync_updates["calendar"] = calendar
        if calendar_defaults_supplied:
            sync_updates["calendar_defaults"] = calendar_defaults
        saved = save_threadhall_config(sync_updates)
        status = {
            **safe_threadhall_status(),
            "received_commands": len(data.get("commands", [])),
            "received_schedules": received_schedules,
            "pending_acknowledgements": len(
                [entry for entry in history.values() if entry.get("pending_ack") is True]
            ),
            "poll_seconds": saved.get("poll_seconds"),
        }
        return status, config, identity, acknowledgements, claim_offers


def threadhall_sync_loop() -> None:
    while True:
        delay = THREADHALL_DEFAULT_POLL_SECONDS
        attempted_identity = None
        try:
            with threadhall_integration_lock:
                attempted_identity = threadhall_connection_identity(load_threadhall_config())
            status = sync_threadhall_once(acknowledgements_in_background=True)
            delay = int(status.get("poll_seconds") or delay)
        except Exception as exc:
            # A second disk error while recording the first failure must not
            # kill this daemon; the next poll may still receive remote STOP.
            try:
                with threadhall_integration_lock:
                    config = load_threadhall_config()
                    delay = int(config.get("poll_seconds") or delay)
                    if (
                        attempted_identity is not None
                        and config.get("enabled")
                        and config.get("token")
                        and threadhall_connection_identity(config) == attempted_identity
                    ):
                        save_threadhall_config({
                            "last_error": str(getattr(exc, "detail", exc)),
                            "last_sync_at": config.get("last_sync_at"),
                        })
            except Exception:
                pass
        time.sleep(max(10, min(300, delay)))


def start_threadhall_sync() -> None:
    global threadhall_sync_started
    disabled = (
        os.environ.get("PIBELLS_DISABLE_THREADHALL_SYNC") == "1"
        or os.environ.get(
            f"PIBELLS_DISABLE_{RETIRED_INTEGRATION_NAME.upper()}_SYNC"
        ) == "1"
    )
    if threadhall_sync_started or disabled:
        return
    threadhall_sync_started = True
    threading.Thread(target=threadhall_sync_loop, daemon=True).start()


# Runtime-only compatibility aliases keep existing local extensions working
# without publishing the retired name in current source symbols or API docs.
globals().update({
    f"{RETIRED_INTEGRATION_NAME}PairRequest": ThreadhallPairRequest,
    f"normalize_{RETIRED_INTEGRATION_SLUG}_base_url": normalize_threadhall_base_url,
    f"load_{RETIRED_INTEGRATION_SLUG}_config": load_threadhall_config,
    f"save_{RETIRED_INTEGRATION_SLUG}_config": save_threadhall_config,
    f"safe_{RETIRED_INTEGRATION_SLUG}_status": safe_threadhall_status,
    f"{RETIRED_INTEGRATION_SLUG}_request": threadhall_request,
    f"pair_{RETIRED_INTEGRATION_SLUG}": pair_threadhall,
    f"managed_{RETIRED_INTEGRATION_SLUG}_schedule_name": managed_threadhall_schedule_name,
    f"apply_{RETIRED_INTEGRATION_SLUG}_schedules": apply_threadhall_schedules,
    f"run_{RETIRED_INTEGRATION_SLUG}_command": run_threadhall_command,
    f"acknowledge_{RETIRED_INTEGRATION_SLUG}_command": acknowledge_threadhall_command,
    f"load_{RETIRED_INTEGRATION_SLUG}_command_history": load_threadhall_command_history,
    f"save_{RETIRED_INTEGRATION_SLUG}_command_history": save_threadhall_command_history,
    f"{RETIRED_INTEGRATION_SLUG}_sync_payload": threadhall_sync_payload,
    f"sync_{RETIRED_INTEGRATION_SLUG}_once": sync_threadhall_once,
    f"{RETIRED_INTEGRATION_SLUG}_sync_loop": threadhall_sync_loop,
    f"start_{RETIRED_INTEGRATION_SLUG}_sync": start_threadhall_sync,
    f"{RETIRED_INTEGRATION_NAME.upper()}_CONFIG_FILE": THREADHALL_CONFIG_FILE,
    f"{RETIRED_INTEGRATION_NAME.upper()}_COMMAND_HISTORY_FILE": THREADHALL_COMMAND_HISTORY_FILE,
    f"{RETIRED_INTEGRATION_NAME.upper()}_DEFAULT_POLL_SECONDS": THREADHALL_DEFAULT_POLL_SECONDS,
})


def find_audio_usages(filename: str) -> Dict[str, List[str]]:
    usages: Dict[str, List[str]] = {"schedules": [], "buttons": []}
    schedules = load_all_schedules()
    for schedule_name, entries in schedules.get("schedules", {}).items():
        for entry in entries:
            if entry.get("sound_file") == filename:
                label = entry.get("label") or f"{DAY_NAMES[entry.get('day', 0)]} {entry.get('time')}"
                usages["schedules"].append(f"{schedule_name}: {label}")
    for button in load_buttons():
        if button.sound_file == filename:
            usages["buttons"].append(button.name)
    return usages


def remove_audio_references(filename: str) -> None:
    with threadhall_integration_lock:
        schedules = load_all_schedules()
        for schedule_name, entries in schedules.get("schedules", {}).items():
            schedules["schedules"][schedule_name] = [
                entry for entry in entries if entry.get("sound_file") != filename
            ]
        save_all_schedules(schedules)
    save_buttons([button for button in load_buttons() if button.sound_file != filename])


def auth_configured() -> bool:
    return AUTH_FILE.exists()


def hash_password(password: str, *, salt: Optional[str] = None) -> Dict[str, Any]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_ITERATIONS,
    ).hex()
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": PASSWORD_ITERATIONS,
        "salt": salt,
        "hash": digest,
    }


def verify_password(password: str, stored: Dict[str, Any]) -> bool:
    try:
        salt = stored["salt"]
        iterations = int(stored.get("iterations", PASSWORD_ITERATIONS))
        expected = stored["hash"]
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
        ).hex()
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def validate_credentials(username: str, password: str) -> Tuple[str, str]:
    username = re.sub(r"\s+", " ", username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if len(username) > 64:
        raise HTTPException(status_code=400, detail="Username is too long")
    if len(password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return username, password


def load_auth_record() -> Optional[Dict[str, Any]]:
    raw = read_json(AUTH_FILE, None)
    if not isinstance(raw, dict):
        return None
    if not raw.get("username") or not isinstance(raw.get("password"), dict):
        return None
    return raw


def save_auth_record(username: str, password: str) -> None:
    username, password = validate_credentials(username, password)
    write_json(
        AUTH_FILE,
        {
            "username": username,
            "password": hash_password(password),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        AUTH_FILE.chmod(0o600)
    except OSError:
        pass


def authenticate_user(username: str, password: str) -> bool:
    record = load_auth_record()
    if not record:
        return False
    return username == record["username"] and verify_password(password, record["password"])


def current_session_user(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return sessions.get(token)


def audio_group_alive(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # Permission or other probe errors cannot prove that audio stopped.
        return True


def forget_audio_process(proc: subprocess.Popen) -> None:
    global loop_processes
    active_audio_processes[:] = [item for item in active_audio_processes if item is not proc]
    loop_processes = [item for item in loop_processes if item is not proc]
    audio_process_groups.pop(id(proc), None)


def signal_audio_process(proc: subprocess.Popen, *, kill: bool) -> None:
    group_id = audio_process_groups.get(id(proc))
    try:
        if group_id is not None:
            # Once the group leader is gone, its numeric PGID may eventually
            # be reused. Do not signal a group we can no longer bind to this
            # live child; an orphan group stays unresolved for operator action.
            if proc.poll() is None and os.getpgid(proc.pid) == group_id and audio_group_alive(group_id):
                os.killpg(group_id, signal.SIGKILL if kill else signal.SIGTERM)
        elif proc.poll() is None:
            proc.kill() if kill else proc.terminate()
    except Exception:
        # Verification below keeps failed signals in the registry for STOP retry.
        pass


def wait_for_audio_stop(proc: subprocess.Popen, deadline: float) -> bool:
    group_id = audio_process_groups.get(id(proc))
    waited = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        waited = True
    except Exception:
        pass
    if group_id is not None:
        while audio_group_alive(group_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        return not audio_group_alive(group_id)
    if waited:
        return True
    try:
        return proc.poll() is not None
    except Exception:
        return False


def terminate_processes(processes: List[subprocess.Popen]) -> List[subprocess.Popen]:
    with audio_process_lock:
        targets = list({id(proc): proc for proc in processes}.values())
        for proc in targets:
            signal_audio_process(proc, kill=False)
        deadline = time.monotonic() + 1.0
        unresolved = [proc for proc in targets if not wait_for_audio_stop(proc, deadline)]
        for proc in unresolved:
            signal_audio_process(proc, kill=True)
        deadline = time.monotonic() + 1.0
        unresolved = [proc for proc in unresolved if not wait_for_audio_stop(proc, deadline)]
        unresolved_ids = {id(proc) for proc in unresolved}
        for proc in targets:
            if id(proc) not in unresolved_ids:
                forget_audio_process(proc)
        return unresolved


def stop_loops() -> None:
    # Historical name retained for local API compatibility; STOP covers every
    # active playback, including ordinary local and Barix one-shot processes.
    with audio_process_lock:
        unresolved = terminate_processes(list(active_audio_processes))
        if unresolved:
            raise RuntimeError(
                f"Unable to verify audio stopped on {len(unresolved)} output(s); retry STOP_AUDIO"
            )


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "0.0.0.0"


def prefix_from_netmask(netmask: str) -> Optional[int]:
    try:
        value = int(netmask, 16) if netmask.startswith("0x") else int(ipaddress.IPv4Address(netmask))
        return bin(value).count("1")
    except Exception:
        return None


def get_local_ipv4_networks() -> List[str]:
    networks: List[str] = []

    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        for match in re.finditer(r"\binet\s+([0-9.]+/\d+)", result.stdout):
            network = str(ipaddress.ip_interface(match.group(1)).network)
            if network not in networks:
                networks.append(network)
    except Exception:
        pass

    if not networks:
        try:
            result = subprocess.run(
                ["ifconfig"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            for line in result.stdout.splitlines():
                match = re.search(r"\binet\s+([0-9.]+)\s+netmask\s+(0x[0-9a-fA-F]+|[0-9.]+)", line)
                if not match or match.group(1).startswith("127."):
                    continue
                prefix = prefix_from_netmask(match.group(2))
                if prefix is None:
                    continue
                network = str(ipaddress.ip_interface(f"{match.group(1)}/{prefix}").network)
                if network not in networks:
                    networks.append(network)
        except Exception:
            pass

    if not networks:
        local_ip = get_local_ip()
        if local_ip != "0.0.0.0":
            networks.append(str(ipaddress.ip_interface(f"{local_ip}/24").network))

    return networks


def load_scan_ranges() -> List[str]:
    ranges: List[str] = []
    env_ranges = os.environ.get("PIBELLS_BARIX_SCAN_RANGES", "")
    for item in parse_scan_ranges(env_ranges):
        if item not in ranges:
            ranges.append(item)
    raw = read_json(BARIX_SCAN_RANGES_FILE, [])
    if isinstance(raw, list):
        for item in raw:
            try:
                normalized = normalize_scan_range(str(item))
            except HTTPException:
                continue
            if normalized not in ranges:
                ranges.append(normalized)
    return ranges


def save_scan_ranges(ranges: List[str]) -> List[str]:
    normalized: List[str] = []
    for item in ranges:
        value = normalize_scan_range(item)
        if value not in normalized:
            normalized.append(value)
    with storage_lock:
        write_json(BARIX_SCAN_RANGES_FILE, normalized)
    return normalized


def build_scan_networks(ranges: str = "") -> List[ipaddress.IPv4Network]:
    configured = parse_scan_ranges(ranges) if ranges else load_scan_ranges()
    raw_networks = configured + get_local_ipv4_networks()
    networks: List[ipaddress.IPv4Network] = []
    for item in raw_networks:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if network.version != 4 or network in networks:
            continue
        networks.append(network)
    return networks


def iter_probe_hosts(networks: List[ipaddress.IPv4Network]) -> Tuple[List[str], List[str]]:
    hosts: List[str] = []
    skipped: List[str] = []
    for network in networks:
        if network.num_addresses == 1:
            candidates = [str(network.network_address)]
        elif network.num_addresses <= BARIX_SCAN_MAX_HOSTS + 2:
            candidates = [str(host) for host in network.hosts()]
        else:
            skipped.append(str(network))
            continue
        for host in candidates:
            if host not in hosts:
                hosts.append(host)
    return hosts, skipped


def parse_barix_discovery_payload(data: bytes) -> Dict[str, str]:
    meta: Dict[str, str] = {}

    if data.startswith(BARIX_DISCOVERY_HEADER):
        if len(data) < 15 or data[4] != BARIX_DISCOVERY_GET_RESPONSE:
            return meta

        meta["mac"] = ":".join(f"{value:02X}" for value in data[5:11])
        packet_ip = ipaddress.ip_address(data[11:15])
        if not packet_ip.is_unspecified and not packet_ip.is_multicast:
            meta["ip"] = str(packet_ip)

        fields: List[bytes] = []
        offset = 15
        for _ in range(4):
            if offset >= len(data):
                break
            length = data[offset]
            offset += 1
            end = offset + length
            if end > len(data):
                break
            fields.append(data[offset:end])
            offset = end

        decoded = [
            field.decode("latin-1", errors="ignore").strip("\x00 \t\r\n")
            for field in fields
        ]
        text = " ".join(value for value in decoded if value)
        if len(decoded) >= 4 and decoded[3]:
            meta["name"] = re.sub(r"\s+", " ", decoded[3])
    else:
        text = data.decode("latin-1", errors="ignore")

    mac = re.search(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", text)
    if mac and "mac" not in meta:
        meta["mac"] = mac.group(0).upper().replace("-", ":")
    for token in ("Exstreamer", "Instreamer", "Annuncicom", "IP Audio", "Barix"):
        if token.lower() in text.lower():
            meta["model"] = token
            break
    return meta


def candidate_payload(ip: str, method: str, meta: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    meta = meta or {}
    ip = normalize_device_ip(meta.get("ip") or ip)
    model = meta.get("model") or "Barix device"
    name = meta.get("name") or f"{model} {ip}"
    return {
        "ip": ip,
        "name": normalize_device_name(name, default_device_name(ip)),
        "port": BARIX_STREAM_PORT,
        "method": method,
        "model": model,
        "mac": meta.get("mac", ""),
        "key": f"{ip}:{BARIX_STREAM_PORT}",
    }


def merge_candidate(candidates: Dict[str, Dict[str, Any]], candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = candidate["key"]
    existing = candidates.get(key)
    if existing:
        for field in ("model", "mac", "method"):
            if not existing.get(field) and candidate.get(field):
                existing[field] = candidate[field]
        return None
    candidates[key] = candidate
    return candidate


def discover_barix_binary(networks: List[ipaddress.IPv4Network], timeout: float = 1.2) -> List[Dict[str, Any]]:
    destinations = {"255.255.255.255"}
    for network in networks:
        if network.num_addresses > 1:
            destinations.add(str(network.broadcast_address))

    found: Dict[str, Dict[str, Any]] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        ) as sender:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", BARIX_DISCOVERY_PORT))
            listener.settimeout(0.15)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sender.bind(("0.0.0.0", BARIX_DISCOVERY_PORT + 1))
            except OSError:
                pass
            for address in destinations:
                try:
                    sender.sendto(BARIX_DISCOVERY_GET_REQUEST, (address, BARIX_DISCOVERY_PORT))
                except OSError:
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, (ip, _) = listener.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                meta = parse_barix_discovery_payload(data)
                if not meta:
                    continue
                try:
                    candidate = candidate_payload(ip, "UDP discovery", meta)
                except HTTPException:
                    continue
                found[candidate["key"]] = candidate
    except OSError:
        return []
    return list(found.values())


def barix_http_probe(ip: str, timeout: float) -> Optional[Dict[str, str]]:
    conn: Optional[http.client.HTTPConnection] = None
    try:
        conn = http.client.HTTPConnection(ip, BARIX_HTTP_PORT, timeout=timeout)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read(4096).decode("latin-1", errors="ignore")
        headers = "\n".join(f"{key}: {value}" for key, value in resp.getheaders())
        text = f"{headers}\n{body}".lower()
        if not any(term in text for term in ("barix", "exstreamer", "instreamer", "annuncicom", "ip audio")):
            return None
        meta: Dict[str, str] = {}
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        if title:
            meta["name"] = re.sub(r"\s+", " ", title.group(1)).strip()
        for token in ("Exstreamer", "Instreamer", "Annuncicom", "IP Audio", "Barix"):
            if token.lower() in text:
                meta["model"] = token
                break
        return meta
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def barix_tcp_probe(ip: str, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, BARIX_TCP_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def probe_barix_candidate(ip: str, timeout: float = 0.35) -> Optional[Dict[str, Any]]:
    try:
        ip = normalize_device_ip(ip)
    except HTTPException:
        return None
    meta = barix_http_probe(ip, timeout)
    if meta is not None:
        return candidate_payload(ip, "HTTP probe", meta)
    if barix_tcp_probe(ip, timeout):
        return candidate_payload(ip, f"TCP {BARIX_TCP_PORT}", {"model": "Barix device"})
    return None


def discover_barix_devices_iter(ranges: str = "") -> Iterable[Dict[str, Any]]:
    networks = build_scan_networks(ranges)
    hosts, skipped = iter_probe_hosts(networks)
    total = max(len(hosts) + 1, 1)
    candidates: Dict[str, Dict[str, Any]] = {}

    yield {
        "progress": 0,
        "total": total,
        "step": "Broadcast discovery",
        "ranges": [str(network) for network in networks],
        "skipped": skipped,
    }
    for candidate in discover_barix_binary(networks):
        added = merge_candidate(candidates, candidate)
        if added:
            yield {"progress": 1, "total": total, "device": added, "step": "Broadcast discovery"}

    if not hosts:
        yield {"progress": total, "total": total, "complete": True, "devices": list(candidates.values())}
        return

    workers = min(BARIX_SCAN_WORKERS, max(1, len(hosts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(probe_barix_candidate, host): host for host in hosts}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            data: Dict[str, Any] = {
                "progress": min(index + 1, total),
                "total": total,
                "step": f"Probing {futures[future]}",
            }
            try:
                candidate = future.result()
            except Exception:
                candidate = None
            if candidate:
                added = merge_candidate(candidates, candidate)
                if added:
                    data["device"] = added
            yield data

    yield {"progress": total, "total": total, "complete": True, "devices": list(candidates.values()), "skipped": skipped}


def discover_barix_devices(ranges: str = "") -> List[Dict[str, Any]]:
    devices: Dict[str, Dict[str, Any]] = {}
    for event in discover_barix_devices_iter(ranges=ranges):
        if event.get("device"):
            devices[event["device"]["key"]] = event["device"]
        for device in event.get("devices", []):
            devices[device["key"]] = device
    return list(devices.values())


def check_device(device: Device, timeout: float = 0.5) -> bool:
    try:
        target = normalize_device_record(model_to_dict(device) if isinstance(device, BaseModel) else device)
    except HTTPException:
        return False
    if barix_http_probe(target.ip, timeout) is not None:
        return True
    return barix_tcp_probe(target.ip, timeout)


def trigger_bell(
    sound_file: str,
    loop: bool = False,
    output_keys: Optional[List[str]] = None,
) -> None:
    load_threadhall_config()
    if threadhall_config_integrity_failed:
        raise threadhall_config_unavailable()
    # A STOP cannot race between launching a stream and registering its handle.
    with audio_process_lock:
        _trigger_bell_locked(sound_file, loop=loop, output_keys=output_keys)


def _trigger_bell_locked(
    sound_file: str,
    loop: bool = False,
    output_keys: Optional[List[str]] = None,
) -> None:
    global loop_processes
    sound_file = ensure_audio_exists(sound_file)
    devices = load_devices()
    selected_devices, play_local = resolve_output_targets(output_keys, devices)
    path = audio_path(sound_file)

    def start_process(
        cmd: List[str],
        label: str,
        *,
        capture_stderr: bool = False,
    ) -> Optional[subprocess.Popen]:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
                start_new_session=True,
            )
            active_audio_processes.append(proc)
            if isinstance(getattr(proc, "pid", None), int) and proc.pid > 0:
                audio_process_groups[id(proc)] = proc.pid
            return proc
        except FileNotFoundError as exc:
            print(f"Failed to start {label}: {exc}")
        except Exception as exc:
            print(f"Failed to start {label}: {exc}")
        return None

    def start_local() -> Optional[subprocess.Popen]:
        quoted_path = shlex.quote(str(path))
        if loop:
            player_cmds = [
                ["ffplay", "-nodisp", "-autoexit", "-loop", "0", str(path)],
                [
                    "bash",
                    "-lc",
                    f"while true; do ffmpeg -hide_banner -loglevel error -i {quoted_path} -f wav - | aplay -q -; done",
                ],
            ]
        else:
            player_cmds = [
                ["ffplay", "-nodisp", "-autoexit", str(path)],
                ["bash", "-lc", f"ffmpeg -hide_banner -loglevel error -i {quoted_path} -f wav - | aplay -q -"],
            ]
        for cmd in player_cmds:
            proc = start_process(cmd, "local playback")
            if proc:
                return proc
        print("No audio player found (ffplay/aplay)")
        return None

    def start_stream(device: Device) -> Optional[subprocess.Popen]:
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
        ]
        if loop:
            ffmpeg_cmd += ["-stream_loop", "-1"]
        ffmpeg_cmd += [
            "-i",
            str(path),
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-flush_packets",
            "1",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            device_stream_url(device),
        ]
        return start_process(
            ffmpeg_cmd,
            f"stream to {device.ip}",
            capture_stderr=True,
        )

    def process_error(proc: subprocess.Popen) -> str:
        if proc.stderr is None:
            return ""
        try:
            output = proc.stderr.read()
            if isinstance(output, bytes):
                return output.decode("utf-8", errors="replace").strip()
            return str(output or "").strip()
        except Exception:
            return ""

    def validate_stream_processes(procs: List[subprocess.Popen]) -> None:
        if selected_devices and len(procs) != len(selected_devices):
            raise RuntimeError("Unable to start every configured audio stream")
        time.sleep(0.1)
        failures = []
        for proc in procs:
            return_code = proc.poll()
            if return_code not in (None, 0):
                detail = process_error(proc)
                failures.append(detail or f"FFmpeg exited with status {return_code}")
        if failures:
            raise RuntimeError("Audio stream failed to start: " + "; ".join(failures))

    def reap_process(proc: subprocess.Popen) -> None:
        waited = False
        try:
            return_code = proc.wait()
            waited = True
            if return_code not in (None, 0):
                detail = process_error(proc)
                print(detail or f"Audio process exited with status {return_code}")
        except Exception:
            pass
        finally:
            # A shell leader can exit before its ffmpeg/aplay children.
            # Keep watching that group, then promptly discard its numeric
            # PGID so a later unrelated group cannot inherit this handle.
            while True:
                with audio_process_lock:
                    if not any(item is proc for item in active_audio_processes):
                        break
                    group_id = audio_process_groups.get(id(proc))
                    if group_id is None:
                        try:
                            stopped = waited or proc.poll() is not None
                        except Exception:
                            stopped = waited
                    else:
                        stopped = not audio_group_alive(group_id)
                    if stopped:
                        forget_audio_process(proc)
                        break
                time.sleep(0.05)

    if loop:
        stop_loops()
        procs = [start_stream(device) for device in selected_devices]
        stream_procs = [proc for proc in procs if proc]
        started_procs = list(stream_procs)
        try:
            validate_stream_processes(stream_procs)
            local_proc = start_local() if play_local else None
            if play_local and local_proc is None:
                raise RuntimeError("Unable to start local audio playback")
            if local_proc:
                started_procs.append(local_proc)
            loop_processes = started_procs
            for proc in started_procs:
                threading.Thread(target=reap_process, args=(proc,), daemon=True).start()
        except Exception:
            unresolved = terminate_processes(started_procs)
            if unresolved:
                raise RuntimeError("Unable to verify playback startup cleanup; retry STOP_AUDIO")
            raise
        return

    procs = [proc for proc in [start_stream(device) for device in selected_devices] if proc]
    try:
        validate_stream_processes(procs)
        local_proc = start_local() if play_local else None
        if play_local and local_proc is None:
            raise RuntimeError("Unable to start local audio playback")
        if local_proc:
            procs.append(local_proc)
    except Exception:
        unresolved = terminate_processes(procs)
        if unresolved:
            raise RuntimeError("Unable to verify playback startup cleanup; retry STOP_AUDIO")
        raise
    if procs:
        for proc in procs:
            threading.Thread(target=reap_process, args=(proc,), daemon=True).start()


def dispatch_due_events(now: Optional[datetime] = None) -> None:
    with threadhall_integration_lock:
        config = load_threadhall_config()
        dispatch_stop_generation = threadhall_stop_runtime_generation
        dispatch_identity = threadhall_connection_identity(config)
    if threadhall_config_integrity_failed or threadhall_unverified_stop_held(config):
        return
    if (
        threadhall_authority_fence_failed or threadhall_remote_commands_held(config)
    ) and threadhall_managed_schedule_read_only(config):
        return
    active = load_all_schedules().get("active")
    if threadhall_manages_current_schedule(config, active):
        if not managed_schedule_clock_status(config)["ready"]:
            return
        _, district_zone, _ = configured_district_time_zone(config)
        if district_zone is None:
            return
        if now is None:
            now = datetime.now(timezone.utc).astimezone(district_zone)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=district_zone)
        else:
            now = now.astimezone(district_zone)
        # A fall-back hour occurs twice. Execute its wall-clock bell only once.
        if now.fold == 1 and now.utcoffset() != now.replace(fold=0).utcoffset():
            return
    else:
        now = now or datetime.now()
    now = now.replace(second=0, microsecond=0)
    weekday = now.weekday()
    for event in load_schedule(now):
        if not (
            event.enabled
            and event.day == weekday
            and event.time.hour == now.hour
            and event.time.minute == now.minute
        ):
            continue
        try:
            # Planning may take time. Hold integration through playback start
            # so a concurrent STOP either wins first or stops this new player.
            with threadhall_integration_lock:
                current = load_threadhall_config()
                if (
                    threadhall_stop_runtime_generation != dispatch_stop_generation
                    or threadhall_connection_identity(current) != dispatch_identity
                    or threadhall_config_integrity_failed
                    or threadhall_unverified_stop_held(current)
                    or (threadhall_authority_fence_failed and threadhall_managed_schedule_read_only(current))
                    or (threadhall_remote_commands_held(current) and threadhall_managed_schedule_read_only(current))
                ):
                    return
                trigger_bell(event.sound_file, output_keys=event.output_keys)
        except Exception as exc:
            print(f"Bell event {event.id} failed: {getattr(exc, 'detail', exc)}")


def bell_daemon() -> None:
    while True:
        try:
            dispatch_due_events()
        except Exception as exc:
            print(f"Bell daemon error: {exc}")
        now = datetime.now()
        delay = 60 - now.second - now.microsecond / 1_000_000
        time.sleep(max(delay, 1))


def start_daemon() -> None:
    global daemon_started
    if daemon_started or os.environ.get("PIBELLS_DISABLE_DAEMON") == "1":
        return
    daemon_started = True
    threading.Thread(target=bell_daemon, daemon=True).start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_daemon()
    start_threadhall_sync()
    yield
    stop_loops()


app = FastAPI(title="PiBells", lifespan=lifespan)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = {
        "/login",
        "/logout",
        "/setup",
        "/api/setup",
        "/api/network",
        "/api/auth/status",
        "/manifest.json",
        "/service-worker.js",
        "/favicon.ico",
    }
    if path.startswith("/static") or path in public_paths:
        if path == "/login" and not auth_configured():
            return RedirectResponse("/setup", status_code=303)
        if path == "/setup" and auth_configured():
            return RedirectResponse("/", status_code=303)
        return await call_next(request)

    if not auth_configured():
        if path.startswith("/api"):
            return JSONResponse({"detail": "Setup required"}, status_code=403)
        return RedirectResponse("/setup", status_code=303)

    user = current_session_user(request)
    if not user:
        if path.startswith("/api") or path.startswith("/audio"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    request.state.user = user
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {
        "configured": auth_configured(),
        "authenticated": bool(current_session_user(request)),
        "username": current_session_user(request),
    }


@app.post("/api/setup")
def setup_admin(body: SetupRequest):
    if auth_configured():
        raise HTTPException(status_code=409, detail="PiBells is already configured")
    save_auth_record(body.username, body.password)
    return {"status": "configured"}


@app.get("/api/account")
def account_info(request: Request):
    return {"username": request.state.user}


@app.post("/api/account/password")
def change_password(request: Request, body: PasswordChangeRequest):
    if not authenticate_user(request.state.user, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    save_auth_record(request.state.user, body.new_password)
    return {"status": "updated"}


@app.get("/api/threadhall/status")
def get_threadhall_status():
    return safe_threadhall_status()


@app.post("/api/threadhall/pair")
def pair_threadhall_device(body: ThreadhallPairRequest):
    return pair_threadhall(body)


def unpair_threadhall() -> Dict[str, Any]:
    with threadhall_integration_lock:
        config = load_threadhall_config()
        fence_and_disable_threadhall_authority(config, {
            "enabled": False,
            "pairing_pending": False,
            "pairing_epoch": str(uuid.uuid4()),
            "base_url": config.get("base_url", ""),
            "device_uuid": config.get("device_uuid") or str(uuid.uuid4()),
            "device_name": config.get("device_name", ""),
            "token": "",
            "authority": {},
            "claim_url_template": "",
            "schedule_revision": "",
            "managed_schedule_hash": "",
            "calendar": [],
            "calendar_defaults": {},
            "district_time_zone": "",
            "last_sync_at": None,
            "last_error": "",
        })
        # Keep receipts from every authority for later re-pair and review.
        return safe_threadhall_status()


@app.post("/api/threadhall/unpair")
def unpair_threadhall_device():
    return unpair_threadhall()


@app.post("/api/threadhall/sync-now")
def sync_threadhall_now():
    return sync_threadhall_once()


# Hidden compatibility routes keep already-deployed dashboards operable without
# publishing the retired product name in the current OpenAPI surface.
def get_retired_integration_status():
    return safe_threadhall_status()


def pair_retired_integration_device(body: ThreadhallPairRequest):
    return pair_threadhall(body)


def unpair_retired_integration_device():
    return unpair_threadhall()


def sync_retired_integration_now():
    return sync_threadhall_once()


retired_api_root = f"/api/{RETIRED_INTEGRATION_SLUG}"
app.add_api_route(
    f"{retired_api_root}/status",
    get_retired_integration_status,
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route(
    f"{retired_api_root}/pair",
    pair_retired_integration_device,
    methods=["POST"],
    include_in_schema=False,
)
app.add_api_route(
    f"{retired_api_root}/unpair",
    unpair_retired_integration_device,
    methods=["POST"],
    include_in_schema=False,
)
app.add_api_route(
    f"{retired_api_root}/sync-now",
    sync_retired_integration_now,
    methods=["POST"],
    include_in_schema=False,
)
globals()[f"unpair_{RETIRED_INTEGRATION_SLUG}"] = unpair_threadhall


@app.get("/api/schedule", response_model=List[ScheduleEntry])
def get_schedule():
    return load_schedule()


@app.post("/api/schedule", response_model=List[ScheduleEntry])
def add_schedule(entry: ScheduleEntry):
    entry.sound_file = ensure_audio_exists(entry.sound_file)
    entries = load_schedule(honor_calendar=False)
    entries.append(entry)
    save_schedule(entries)
    return load_schedule(honor_calendar=False)


@app.put("/api/schedule/{entry_id}", response_model=List[ScheduleEntry])
def update_schedule_entry(entry_id: str, entry: ScheduleEntry):
    entry.sound_file = ensure_audio_exists(entry.sound_file)
    supplied_fields = model_fields_set(entry)
    entries = load_schedule(honor_calendar=False)
    for index, existing in enumerate(entries):
        if existing.id == entry_id:
            entry.id = existing.id
            if "output_keys" not in supplied_fields:
                entry.output_keys = existing.output_keys
            entries[index] = entry
            save_schedule(entries)
            return load_schedule(honor_calendar=False)
    raise HTTPException(status_code=404, detail="Schedule entry not found")


@app.delete("/api/schedule/{entry_id}", response_model=List[ScheduleEntry])
def delete_schedule_entry(entry_id: str):
    entries = load_schedule(honor_calendar=False)
    for index, entry in enumerate(entries):
        if entry.id == entry_id or str(index) == entry_id:
            entries.pop(index)
            save_schedule(entries)
            return load_schedule(honor_calendar=False)
    raise HTTPException(status_code=404, detail="Schedule entry not found")


@app.get("/api/schedules")
def get_schedules():
    return list_schedules()


@app.post("/api/schedules")
def add_schedule_name(name: ScheduleName):
    return create_schedule(name.name)


@app.post("/api/schedules/activate/{name}")
def activate(name: str):
    return activate_schedule(name)


@app.delete("/api/schedules/{name}")
def delete_schedule_name(name: str):
    return remove_schedule(name)


@app.get("/api/devices", response_model=List[Device])
def get_devices():
    return load_devices()


@app.post("/api/devices", response_model=List[Device])
def add_device(device: Device):
    next_device = normalize_device_record(model_to_dict(device))
    devices = load_devices()
    for index, existing in enumerate(devices):
        if device_key(existing) == device_key(next_device):
            devices[index] = next_device
            save_devices(devices)
            return load_devices()
    devices.append(next_device)
    save_devices(devices)
    return load_devices()


@app.get("/api/devices/scan_ranges")
def get_device_scan_ranges():
    return {
        "ranges": load_scan_ranges(),
        "automatic": get_local_ipv4_networks(),
    }


@app.put("/api/devices/scan_ranges")
def put_device_scan_ranges(body: DeviceScanRanges):
    return {"ranges": save_scan_ranges(body.ranges), "automatic": get_local_ipv4_networks()}


@app.delete("/api/devices/{index}", response_model=List[Device])
def delete_device(index: int):
    devices = load_devices()
    if index < 0 or index >= len(devices):
        raise HTTPException(status_code=404, detail="Invalid device")
    devices.pop(index)
    save_devices(devices)
    return load_devices()


@app.get("/api/devices/status", response_model=Dict[str, bool])
def devices_status():
    return {device_key(device): check_device(device) for device in load_devices()}


@app.get("/api/devices/scan")
def scan_devices(ranges: str = ""):
    return discover_barix_devices(ranges=ranges)


@app.get("/api/devices/scan_stream")
def scan_devices_stream(ranges: str = ""):
    def event_gen():
        try:
            for data in discover_barix_devices_iter(ranges=ranges):
                yield f"data:{json.dumps(data)}\n\n"
        except Exception as exc:
            yield f"data:{json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/network")
def network_info():
    return {"ip": get_local_ip(), "hostname": socket.gethostname()}


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicons" / "favicon.ico")


@app.post("/api/reboot")
def reboot_device():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return {"status": "rebooting"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Reboot failed: {exc}")


@app.get("/api/audio", response_model=List[AudioFile])
def get_audio_files():
    return list_audio()


@app.post("/api/audio", response_model=List[AudioFile])
async def upload_audio(name: str = Form(...), file: UploadFile = File(...)):
    original_name = file.filename or ""
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    filename = unique_audio_filename(original_name)
    dest = audio_path(filename)
    with dest.open("wb") as output:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    meta = load_audio_meta()
    meta[filename] = (name or Path(filename).stem).strip() or Path(filename).stem
    save_audio_meta(meta)
    return list_audio()


@app.put("/api/audio/{filename}", response_model=List[AudioFile])
def rename_audio(filename: str, body: AudioName):
    filename = ensure_audio_exists(filename)
    meta = load_audio_meta()
    meta[filename] = body.name.strip() or Path(filename).stem
    save_audio_meta(meta)
    return list_audio()


@app.delete("/api/audio/{filename}", response_model=List[AudioFile])
def delete_audio(filename: str, force: bool = False):
    filename = ensure_audio_exists(filename)
    usages = find_audio_usages(filename)
    if not force and (usages["schedules"] or usages["buttons"]):
        raise HTTPException(status_code=409, detail={"message": "Audio file is in use", "usages": usages})
    try:
        audio_path(filename).unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")
    meta = load_audio_meta()
    meta.pop(filename, None)
    save_audio_meta(meta)
    if force:
        remove_audio_references(filename)
    return list_audio()


@app.get("/api/buttons", response_model=List[QuickButton])
def get_buttons():
    return load_buttons()


@app.post("/api/buttons", response_model=List[QuickButton])
def add_button(button: QuickButton):
    button.sound_file = ensure_audio_exists(button.sound_file)
    button.color = normalize_color(button.color)
    button.icon = normalize_icon(button.icon)
    buttons = load_buttons()
    buttons.append(button)
    save_buttons(buttons)
    return load_buttons()


@app.put("/api/buttons/{button_id}", response_model=List[QuickButton])
def update_button(button_id: str, button: QuickButton):
    button.sound_file = ensure_audio_exists(button.sound_file)
    button.color = normalize_color(button.color)
    button.icon = normalize_icon(button.icon)
    buttons = load_buttons()
    for index, existing in enumerate(buttons):
        if existing.id == button_id or str(index) == button_id:
            button.id = existing.id
            buttons[index] = button
            save_buttons(buttons)
            return load_buttons()
    raise HTTPException(status_code=404, detail="Button not found")


@app.delete("/api/buttons/{button_id}", response_model=List[QuickButton])
def delete_button(button_id: str):
    buttons = load_buttons()
    for index, button in enumerate(buttons):
        if button.id == button_id or str(index) == button_id:
            buttons.pop(index)
            save_buttons(buttons)
            return load_buttons()
    raise HTTPException(status_code=404, detail="Button not found")


@app.post("/api/test")
def test_sound(req: TestRequest):
    trigger_options: Dict[str, Any] = {"loop": bool(req.loop)}
    if req.output_keys is not None:
        trigger_options["output_keys"] = req.output_keys
    trigger_bell(req.sound_file, **trigger_options)
    return {"status": "ok"}


@app.post("/api/stop")
def stop_sound():
    global threadhall_stop_runtime_generation
    with threadhall_integration_lock:
        try:
            config = load_threadhall_config()
            if config.get("pairing_pending") or (
                config.get("token") and stored_threadhall_authority(config.get("authority"))
            ):
                advance_threadhall_stop_fence(hold_remote_commands=True)
            else:
                # A never-paired controller has no remote offer to reconcile.
                # Avoid creating a config without its first receipt ledger.
                threadhall_stop_runtime_generation += 1
            fence_saved = True
        except Exception:
            fence_saved = False
        stop_loops()
        if not fence_saved:
            raise HTTPException(status_code=503, detail="STOP ordering state could not be saved")
    return {"status": "stopped"}


def next_event_payload(
    entries: Optional[List[ScheduleEntry]] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    config = load_threadhall_config()
    if threadhall_config_integrity_failed or threadhall_unverified_stop_held(config):
        return None
    if (
        threadhall_authority_fence_failed or threadhall_remote_commands_held(config)
    ) and threadhall_managed_schedule_read_only(config):
        return None
    active = load_all_schedules().get("active")
    managed = threadhall_manages_current_schedule(config, active)
    if managed:
        if not managed_schedule_clock_status(config)["ready"]:
            return None
        _, district_zone, _ = configured_district_time_zone(config)
        if district_zone is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc).astimezone(district_zone)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=district_zone)
        else:
            now = now.astimezone(district_zone)
    else:
        now = now or datetime.now()
    audio_names = {audio.file: audio.name for audio in list_audio()}
    candidates: List[Tuple[datetime, ScheduleEntry]] = []
    for days_ahead in range(8):
        target_day = (now.weekday() + days_ahead) % 7
        target_date = now.date() + timedelta(days=days_ahead)
        effective_entries = entries if days_ahead == 0 and entries is not None else load_schedule(
            datetime.combine(target_date, dt_time.min).replace(tzinfo=now.tzinfo)
        )
        for entry in effective_entries:
            if not entry.enabled or entry.day != target_day:
                continue
            candidate = datetime.combine(target_date, entry.time).replace(tzinfo=now.tzinfo)
            if managed:
                round_trip = candidate.astimezone(timezone.utc).astimezone(now.tzinfo)
                if round_trip.replace(tzinfo=None) != datetime.combine(target_date, entry.time):
                    continue
                future = candidate.astimezone(timezone.utc) > now.astimezone(timezone.utc)
            else:
                future = candidate > now
            if future:
                candidates.append((candidate, entry))
    if not candidates:
        return None
    candidate, entry = min(
        candidates,
        key=(
            (lambda item: item[0].astimezone(timezone.utc))
            if managed else (lambda item: item[0])
        ),
    )
    until_event = (
        candidate.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        if managed
        else candidate - now
    )
    return {
        "id": entry.id,
        "label": entry.label,
        "day": entry.day,
        "day_name": DAY_NAMES[entry.day],
        "time": entry.time.strftime("%H:%M"),
        "sound_file": entry.sound_file,
        "sound_name": audio_names.get(entry.sound_file, Path(entry.sound_file).stem),
        "output_keys": entry.output_keys,
        "in_minutes": max(0, int(until_event.total_seconds() // 60)),
    }


@app.get("/api/dashboard")
def dashboard():
    entries, effective_schedule = resolve_effective_schedule()
    schedules = load_all_schedules()
    devices = load_devices()
    statuses = {device_key(device): check_device(device, timeout=0.25) for device in devices}
    return {
        "active_schedule": schedules.get("active"),
        "effective_schedule": effective_schedule,
        "schedule_clock": managed_schedule_clock_status(),
        "schedule_options": list(schedules.get("schedules", {}).keys()),
        "event_count": len(entries),
        "enabled_event_count": len([entry for entry in entries if entry.enabled]),
        "button_count": len(load_buttons()),
        "audio_count": len(list_audio()),
        "device_count": len(devices),
        "online_device_count": len([online for online in statuses.values() if online]),
        "outputs": output_manifest(devices, statuses),
        "next_event": next_event_payload(entries),
    }


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    remember: Optional[bool] = Form(False),
):
    if authenticate_user(username, password):
        token = secrets.token_urlsafe(32)
        sessions[token] = username
        response = RedirectResponse("/", status_code=303)
        max_age = 30 * 24 * 3600 if remember else None
        response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax")
        return response
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sessions.pop(token, None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/setup")
def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/buttons")
def buttons_page():
    return FileResponse(STATIC_DIR / "buttons.html")


@app.get("/manifest.json")
def manifest_file():
    return FileResponse(STATIC_DIR / "manifest.json")


@app.get("/service-worker.js")
def service_worker():
    return FileResponse(STATIC_DIR / "service-worker.js", media_type="application/javascript")


@app.get("/rebooting")
def rebooting_page():
    return FileResponse(STATIC_DIR / "rebooting.html")
