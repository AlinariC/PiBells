import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shlex
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
PIXELSIS_CONFIG_FILE = BASE_DIR / "pixelsis-pairing.json"
LEGACY_THREADHALL_CONFIG_FILE = BASE_DIR / "threadhall-pairing.json"
PIXELSIS_COMMAND_HISTORY_FILE = BASE_DIR / "pixelsis-command-history.json"
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
PIXELSIS_DEFAULT_POLL_SECONDS = max(
    10,
    int(
        os.environ.get(
            "PIBELLS_PIXELSIS_POLL_SECONDS",
            os.environ.get("PIBELLS_THREADHALL_POLL_SECONDS", "20"),
        )
    ),
)
PIBELLS_PROTOCOL_VERSION = 1
PIBELLS_AGENT_VERSION = "pibells-pixelsis-v1"
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
sessions: Dict[str, str] = {}
loop_processes: List[subprocess.Popen] = []
daemon_started = False
pixelsis_sync_started = False

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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)


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


class PixelSISPairRequest(BaseModel):
    base_url: str
    pairing_code: str
    name: Optional[str] = None


# Compatibility for integrations that imported the legacy request model.
ThreadhallPairRequest = PixelSISPairRequest


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
    data = load_all_schedules()
    active = data.get("active")
    if honor_calendar:
        config = load_pixelsis_config()
        effective_day = (effective_date or datetime.now()).date()
        date_key = effective_day.isoformat()
        calendar_day = next(
            (
                item
                for item in config.get("calendar", [])
                if isinstance(item, dict) and str(item.get("date") or "") == date_key
            ),
            None,
        )
        if calendar_day:
            if not bool(calendar_day.get("instructional", True)):
                return []
            managed_name = managed_pixelsis_schedule_name(
                str(calendar_day.get("schedule_name") or "Regular")
            )
            if managed_name not in data.get("schedules", {}):
                return []
            active = managed_name
        else:
            calendar_defaults = config.get("calendar_defaults")
            if isinstance(calendar_defaults, dict) and calendar_defaults.get("managed") is True:
                default_key = (
                    "weekday_schedule_name"
                    if effective_day.weekday() < 5
                    else "weekend_schedule_name"
                )
                raw_name = calendar_defaults.get(default_key)
                if not isinstance(raw_name, str) or not raw_name.strip():
                    return []
                managed_name = managed_pixelsis_schedule_name(raw_name)
                if managed_name not in data.get("schedules", {}):
                    return []
                active = managed_name
    entries = data.get("schedules", {}).get(active, [])
    return [ScheduleEntry(**item) for item in entries]


def save_schedule(entries: List[ScheduleEntry]) -> None:
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
    name = normalize_schedule_name(name)
    data = load_all_schedules()
    data.setdefault("schedules", {}).setdefault(name, [])
    data["active"] = name
    save_all_schedules(data)
    return list_schedules()


def activate_schedule(name: str) -> Dict[str, Any]:
    name = normalize_schedule_name(name)
    data = load_all_schedules()
    if name not in data.get("schedules", {}):
        raise HTTPException(status_code=404, detail="Schedule not found")
    data["active"] = name
    save_all_schedules(data)
    return list_schedules()


def remove_schedule(name: str) -> Dict[str, Any]:
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


def normalize_pixelsis_base_url(value: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Enter a valid PixelSIS URL")
    return base_url


def load_pixelsis_config() -> Dict[str, Any]:
    if not PIXELSIS_CONFIG_FILE.exists() and LEGACY_THREADHALL_CONFIG_FILE.exists():
        migrated = read_json(LEGACY_THREADHALL_CONFIG_FILE, {})
        if isinstance(migrated, dict):
            write_json(PIXELSIS_CONFIG_FILE, migrated)
            try:
                PIXELSIS_CONFIG_FILE.chmod(0o600)
            except OSError:
                pass
    raw = read_json(PIXELSIS_CONFIG_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    env_base_url = os.environ.get(
        "PIBELLS_PIXELSIS_BASE_URL",
        os.environ.get("THREADHALL_BASE_URL", ""),
    ).strip()
    env_token = os.environ.get(
        "PIBELLS_PIXELSIS_DEVICE_TOKEN",
        os.environ.get("THREADHALL_PIBELLS_TOKEN", ""),
    ).strip()
    if env_base_url:
        raw["base_url"] = normalize_pixelsis_base_url(env_base_url)
    if env_token:
        raw["token"] = env_token
        raw["enabled"] = True
    raw.setdefault("enabled", bool(raw.get("token")))
    raw.setdefault("device_uuid", str(uuid.uuid4()))
    raw.setdefault("poll_seconds", PIXELSIS_DEFAULT_POLL_SECONDS)
    raw.setdefault("protocol_version", PIBELLS_PROTOCOL_VERSION)
    raw.setdefault("calendar", [])
    return raw


def save_pixelsis_config(config: Dict[str, Any]) -> Dict[str, Any]:
    config = {
        **load_pixelsis_config(),
        **config,
    }
    config["poll_seconds"] = max(10, min(300, int(config.get("poll_seconds") or PIXELSIS_DEFAULT_POLL_SECONDS)))
    write_json(PIXELSIS_CONFIG_FILE, config)
    try:
        PIXELSIS_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return config


def safe_pixelsis_status() -> Dict[str, Any]:
    config = load_pixelsis_config()
    return {
        "enabled": bool(config.get("enabled") and config.get("token")),
        "paired": bool(config.get("token")),
        "base_url": config.get("base_url", ""),
        "device_uuid": config.get("device_uuid", ""),
        "device_name": config.get("device_name", ""),
        "poll_seconds": config.get("poll_seconds", PIXELSIS_DEFAULT_POLL_SECONDS),
        "protocol_version": config.get("protocol_version", PIBELLS_PROTOCOL_VERSION),
        "schedule_revision": config.get("schedule_revision", ""),
        "last_sync_at": config.get("last_sync_at"),
        "last_error": config.get("last_error", ""),
    }


def pixelsis_request(
    config: Dict[str, Any],
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    base_url = normalize_pixelsis_base_url(str(config.get("base_url") or ""))
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
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
            detail = parsed.get("message") or parsed.get("detail") or body
        except Exception:
            detail = body or exc.reason
        raise HTTPException(status_code=502, detail=f"PixelSIS returned {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"PixelSIS connection failed: {exc.reason}")
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="PixelSIS returned invalid JSON")


def pair_pixelsis(body: PixelSISPairRequest) -> Dict[str, Any]:
    config = load_pixelsis_config()
    config["base_url"] = normalize_pixelsis_base_url(body.base_url)
    device_uuid = str(config.get("device_uuid") or uuid.uuid4())
    name = (body.name or config.get("device_name") or socket.gethostname() or "PiBells Controller").strip()
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
            "outbound_pixelsis_sync": True,
            "revisioned_schedules": True,
            "calendar_exceptions": True,
            "calendar_defaults": True,
            "idempotent_commands": True,
        },
        "status": dashboard(),
    }
    response = pixelsis_request(config, "api/pibells/v1/pair", method="POST", payload=payload, token="")
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    token = str(data.get("token") or "")
    if not token:
        raise HTTPException(status_code=502, detail="PixelSIS did not return a device token")
    save_pixelsis_config({
        "enabled": True,
        "base_url": config["base_url"],
        "token": token,
        "device_uuid": device_uuid,
        "device_name": name,
        "protocol_version": int(data.get("protocol_version") or PIBELLS_PROTOCOL_VERSION),
        "poll_seconds": int(data.get("poll_seconds") or PIXELSIS_DEFAULT_POLL_SECONDS),
        "schedule_revision": "",
        "managed_schedule_hash": "",
        "calendar": [],
        "calendar_defaults": {},
        "paired_at": datetime.now().isoformat(timespec="seconds"),
        "last_sync_at": None,
        "last_error": "",
    })
    save_pixelsis_command_history({})
    return safe_pixelsis_status()


def audio_file_for_key(sound_key: str) -> str:
    preferred = DEFAULT_AUDIO_KEY_FILES.get((sound_key or "").strip(), "standard-bells.mp3")
    try:
        return ensure_audio_exists(preferred)
    except HTTPException:
        audio_files = list_audio()
        if audio_files:
            return audio_files[0].file
        raise


def managed_pixelsis_schedule_name(value: str) -> str:
    name = normalize_schedule_name(value or "Regular")
    return name if name.startswith("PixelSIS - ") else f"PixelSIS - {name}"


def normalize_calendar_defaults(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("managed"), bool):
        raise HTTPException(
            status_code=502,
            detail="PixelSIS returned invalid calendar defaults",
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
                detail="PixelSIS returned invalid calendar defaults",
            )
        cleaned = re.sub(r"\s+", " ", raw_name).strip()
        if len(cleaned) > 80:
            raise HTTPException(
                status_code=502,
                detail="PixelSIS returned invalid calendar defaults",
            )
        normalized[key] = cleaned or None
    return normalized


def apply_pixelsis_schedules(schedules: List[Dict[str, Any]]) -> int:
    current = load_all_schedules()
    current_active = str(current.get("active") or "")
    merged = {
        name: entries
        for name, entries in current.get("schedules", {}).items()
        if not str(name).startswith("Threadhall") and not str(name).startswith("PixelSIS - ")
    }
    preserve_local_active = bool(
        current_active
        and current_active != "Default"
        and not current_active.startswith("Threadhall")
        and not current_active.startswith("PixelSIS - ")
        and current_active in merged
    )
    active_name = current_active if preserve_local_active else (current_active or "Default")
    applied = 0
    for schedule in schedules:
        name = managed_pixelsis_schedule_name(str(schedule.get("name") or "Regular"))
        entries: List[Dict[str, Any]] = []
        for item in schedule.get("entries", []):
            if not isinstance(item, dict):
                continue
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
            except Exception:
                continue
        merged[name] = sorted(entries, key=lambda entry: (entry["day"], entry["time"], entry["label"]))
        applied += 1
        if schedule.get("active", True) and not preserve_local_active:
            active_name = name
    if active_name not in merged:
        active_name = next(iter(merged.keys()), "Default")
    save_all_schedules({"active": active_name, "schedules": merged})
    return applied


def run_pixelsis_command(command: Dict[str, Any]) -> str:
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
            managed_name = managed_pixelsis_schedule_name(schedule_name)
            schedules = load_all_schedules().get("schedules", {})
            selected = managed_name if managed_name in schedules else schedule_name
            activate_schedule(selected)
            return f"Activated schedule {selected}"
        raise HTTPException(status_code=422, detail="Schedule name is required")
    if command_type not in {"PLAY_SOUND", "TEST_OUTPUT", "EMERGENCY_ALERT"} and not emergency_action:
        raise HTTPException(status_code=422, detail=f"Unsupported PixelSIS command: {raw_type}")
    sound_key = str(
        payload.get("sound_key")
        or emergency_action
        or ("emergency" if command_type == "EMERGENCY_ALERT" else "test")
    )
    sound_file = audio_file_for_key(sound_key)
    trigger_options: Dict[str, Any] = {"loop": bool(payload.get("loop", False))}
    if "output_keys" in payload:
        trigger_options["output_keys"] = payload.get("output_keys")
    trigger_bell(sound_file, **trigger_options)
    return f"Played {audio_display_name(sound_file)} for {payload.get('label') or command_type}"


def acknowledge_pixelsis_command(
    config: Dict[str, Any],
    command_id: Any,
    status: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    pixelsis_request(
        config,
        f"api/pibells/v1/commands/{command_id}/ack",
        method="POST",
        payload={
            "status": status,
            "summary": summary,
            "detail": detail or {},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def load_pixelsis_command_history() -> Dict[str, Dict[str, Any]]:
    raw = read_json(PIXELSIS_COMMAND_HISTORY_FILE, {})
    return raw if isinstance(raw, dict) else {}


def save_pixelsis_command_history(history: Dict[str, Dict[str, Any]]) -> None:
    entries = sorted(
        history.items(),
        key=lambda item: str(item[1].get("occurred_at") or ""),
        reverse=True,
    )[:500]
    write_json(PIXELSIS_COMMAND_HISTORY_FILE, dict(entries))
    try:
        PIXELSIS_COMMAND_HISTORY_FILE.chmod(0o600)
    except OSError:
        pass


def pixelsis_sync_payload(config: Dict[str, Any]) -> Dict[str, Any]:
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
        "protocol_version": PIBELLS_PROTOCOL_VERSION,
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
            "idempotent_commands": True,
        },
        "telemetry": status,
        "audio_manifest": audio_manifest,
        "recent_events": [],
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
        if str(name).startswith("PixelSIS - ")
    }
    canonical = json.dumps(
        managed,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"managed-sha256:{hashlib.sha256(canonical).hexdigest()}"


def sync_pixelsis_once() -> Dict[str, Any]:
    config = load_pixelsis_config()
    if not config.get("enabled") or not config.get("token") or not config.get("base_url"):
        return safe_pixelsis_status()
    response = pixelsis_request(
        config,
        "api/pibells/v1/sync",
        method="POST",
        payload=pixelsis_sync_payload(config),
    )
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    raw_schedules = data.get("schedules")
    schedules_supplied = isinstance(raw_schedules, list)
    schedules = [item for item in raw_schedules if isinstance(item, dict)] if schedules_supplied else []
    raw_calendar = data.get("calendar")
    calendar_supplied = isinstance(raw_calendar, list)
    calendar = [item for item in raw_calendar if isinstance(item, dict)] if calendar_supplied else []
    calendar_defaults_supplied = "calendar_defaults" in data
    calendar_defaults = (
        normalize_calendar_defaults(data.get("calendar_defaults"))
        if calendar_defaults_supplied
        else None
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
        )
    )
    if schedules_supplied and (revision_changed or state_hash_mismatch):
        received_schedules = apply_pixelsis_schedules(schedules)
        schedule_updates: Dict[str, Any] = {
            "schedule_revision": schedule_revision,
        }
        if calendar_supplied:
            schedule_updates["calendar"] = calendar
        if calendar_defaults_supplied:
            schedule_updates["calendar_defaults"] = calendar_defaults
        schedule_updates["managed_schedule_hash"] = managed_schedule_state_hash()
        config = save_pixelsis_config(schedule_updates)
    history = load_pixelsis_command_history()
    for command in data.get("commands", []):
        if not isinstance(command, dict) or "id" not in command:
            continue
        command_id = str(command["id"])
        previous = history.get(command_id)
        if previous:
            acknowledge_pixelsis_command(
                config,
                command["id"],
                str(previous.get("status") or "acknowledged"),
                str(previous.get("summary") or "Previously executed"),
                {"duplicate_delivery": True},
            )
            continue
        try:
            summary = run_pixelsis_command(command)
            outcome = {
                "status": "acknowledged",
                "summary": summary,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            outcome = {
                "status": "failed",
                "summary": str(getattr(exc, "detail", exc)),
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
        history[command_id] = outcome
        save_pixelsis_command_history(history)
        acknowledge_pixelsis_command(
            config,
            command["id"],
            outcome["status"],
            outcome["summary"],
        )
    sync_updates: Dict[str, Any] = {
        "enabled": True,
        "protocol_version": int(data.get("protocol_version") or PIBELLS_PROTOCOL_VERSION),
        "poll_seconds": int(data.get("poll_seconds") or config.get("poll_seconds") or PIXELSIS_DEFAULT_POLL_SECONDS),
        "schedule_revision": schedule_revision or config.get("schedule_revision", ""),
        "last_sync_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": "",
    }
    if calendar_supplied:
        sync_updates["calendar"] = calendar
    if calendar_defaults_supplied:
        sync_updates["calendar_defaults"] = calendar_defaults
    saved = save_pixelsis_config(sync_updates)
    return {
        **safe_pixelsis_status(),
        "received_commands": len(data.get("commands", [])),
        "received_schedules": received_schedules,
        "poll_seconds": saved.get("poll_seconds"),
    }


def pixelsis_sync_loop() -> None:
    while True:
        delay = PIXELSIS_DEFAULT_POLL_SECONDS
        try:
            status = sync_pixelsis_once()
            delay = int(status.get("poll_seconds") or delay)
        except Exception as exc:
            config = load_pixelsis_config()
            if config.get("enabled") and config.get("token"):
                save_pixelsis_config({
                    "last_error": str(getattr(exc, "detail", exc)),
                    "last_sync_at": config.get("last_sync_at"),
                })
            delay = int(config.get("poll_seconds") or delay)
        time.sleep(max(10, min(300, delay)))


def start_pixelsis_sync() -> None:
    global pixelsis_sync_started
    disabled = os.environ.get("PIBELLS_DISABLE_PIXELSIS_SYNC") == "1" or os.environ.get("PIBELLS_DISABLE_THREADHALL_SYNC") == "1"
    if pixelsis_sync_started or disabled:
        return
    pixelsis_sync_started = True
    threading.Thread(target=pixelsis_sync_loop, daemon=True).start()


# Compatibility aliases for existing PiBells deployments and local extensions.
normalize_threadhall_base_url = normalize_pixelsis_base_url
load_threadhall_config = load_pixelsis_config
save_threadhall_config = save_pixelsis_config
safe_threadhall_status = safe_pixelsis_status
threadhall_request = pixelsis_request
pair_threadhall = pair_pixelsis
apply_threadhall_schedules = apply_pixelsis_schedules
run_threadhall_command = run_pixelsis_command
acknowledge_threadhall_command = acknowledge_pixelsis_command
sync_threadhall_once = sync_pixelsis_once
threadhall_sync_loop = pixelsis_sync_loop
start_threadhall_sync = start_pixelsis_sync


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


def terminate_processes(processes: List[subprocess.Popen]) -> None:
    for proc in processes:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in processes:
        try:
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                continue
            try:
                proc.wait(timeout=1)
            except Exception:
                pass


def stop_loops() -> None:
    global loop_processes
    processes = loop_processes
    loop_processes = []
    terminate_processes(processes)


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
            return subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            )
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

    def reap_processes(procs: List[subprocess.Popen]) -> None:
        for proc in procs:
            try:
                return_code = proc.wait()
                if return_code not in (None, 0):
                    detail = process_error(proc)
                    print(detail or f"Audio process exited with status {return_code}")
            except Exception:
                pass

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
        except Exception:
            loop_processes = []
            terminate_processes(started_procs)
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
        terminate_processes(procs)
        raise
    if procs:
        threading.Thread(target=reap_processes, args=(procs,), daemon=True).start()


def dispatch_due_events(now: Optional[datetime] = None) -> None:
    now = (now or datetime.now()).replace(second=0, microsecond=0)
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
    start_pixelsis_sync()
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


@app.get("/api/pixelsis/status")
def get_pixelsis_status():
    return safe_pixelsis_status()


@app.post("/api/pixelsis/pair")
def pair_pixelsis_device(body: PixelSISPairRequest):
    return pair_pixelsis(body)


def unpair_pixelsis() -> Dict[str, Any]:
    config = load_pixelsis_config()
    save_pixelsis_config({
        "enabled": False,
        "base_url": config.get("base_url", ""),
        "device_uuid": config.get("device_uuid") or str(uuid.uuid4()),
        "device_name": config.get("device_name", ""),
        "token": "",
        "last_error": "",
    })
    save_pixelsis_command_history({})
    return safe_pixelsis_status()


@app.post("/api/pixelsis/unpair")
def unpair_pixelsis_device():
    return unpair_pixelsis()


@app.post("/api/pixelsis/sync-now")
def sync_pixelsis_now():
    return sync_pixelsis_once()


# Deprecated route aliases keep already-deployed dashboards operable during migration.
@app.get("/api/threadhall/status", deprecated=True)
def get_threadhall_status():
    return safe_pixelsis_status()


@app.post("/api/threadhall/pair", deprecated=True)
def pair_threadhall_device(body: PixelSISPairRequest):
    return pair_pixelsis(body)


@app.post("/api/threadhall/unpair", deprecated=True)
def unpair_threadhall_device():
    return unpair_pixelsis()


@app.post("/api/threadhall/sync-now", deprecated=True)
def sync_threadhall_now():
    return sync_pixelsis_once()


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
    stop_loops()
    return {"status": "stopped"}


def next_event_payload(
    entries: Optional[List[ScheduleEntry]] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
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
            if candidate > now:
                candidates.append((candidate, entry))
    if not candidates:
        return None
    candidate, entry = sorted(candidates, key=lambda item: item[0])[0]
    return {
        "id": entry.id,
        "label": entry.label,
        "day": entry.day,
        "day_name": DAY_NAMES[entry.day],
        "time": entry.time.strftime("%H:%M"),
        "sound_file": entry.sound_file,
        "sound_name": audio_names.get(entry.sound_file, Path(entry.sound_file).stem),
        "output_keys": entry.output_keys,
        "in_minutes": max(0, int((candidate - now).total_seconds() // 60)),
    }


@app.get("/api/dashboard")
def dashboard():
    entries = load_schedule()
    schedules = load_all_schedules()
    devices = load_devices()
    statuses = {device_key(device): check_device(device, timeout=0.25) for device in devices}
    return {
        "active_schedule": schedules.get("active"),
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
