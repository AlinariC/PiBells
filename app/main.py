import json
import socket
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, List, Optional, Iterable, Tuple
from urllib import request, parse

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = BASE_DIR / "schedule.json"
DEVICES_FILE = BASE_DIR / "devices.json"
BUTTONS_FILE = BASE_DIR / "buttons.json"
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
AUDIO_META_FILE = BASE_DIR / "audio.json"
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class ScheduleEntry(BaseModel):
    day: int  # 0=Monday
    time: dt_time
    sound_file: str


class ScheduleName(BaseModel):
    name: str


class QuickButton(BaseModel):
    name: str
    sound_file: str
    color: str = "#ff0000"
    icon: str = ""


class AudioFile(BaseModel):
    file: str
    name: str


def load_all_schedules() -> Dict[str, object]:
    if not SCHEDULE_FILE.exists():
        return {"active": "Default", "schedules": {"Default": []}}
    with open(SCHEDULE_FILE) as f:
        return json.load(f)


def save_all_schedules(data: Dict[str, object]):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(data, f, default=str)

def load_schedule() -> List[ScheduleEntry]:
    data = load_all_schedules()
    active = data.get("active")
    entries = data.get("schedules", {}).get(active, [])
    return [ScheduleEntry(**item) for item in entries]

def save_schedule(entries: List[ScheduleEntry]):
    data = load_all_schedules()
    active = data.get("active")
    data.setdefault("schedules", {})[active] = [e.dict() for e in entries]
    save_all_schedules(data)

def load_devices() -> List[str]:
    if not DEVICES_FILE.exists():
        return []
    with open(DEVICES_FILE) as f:
        return json.load(f)


def save_devices(devices: List[str]):
    with open(DEVICES_FILE, "w") as f:
        json.dump(devices, f)


def discover_barix_devices_iter(
    network: Optional[str] = None, timeout: float = 0.2
) -> Iterable[Tuple[int, Optional[str]]]:
    """Yield progress while scanning for Barix devices.

    The returned iterator yields a tuple ``(index, ip)`` for every host that is
    checked where ``index`` is the current host number (1-254) and ``ip`` is the
    discovered device IP or ``None`` if no device was found at that address.
    """
    if network is None:
        local_ip = get_local_ip()
        if local_ip == "0.0.0.0":
            return
        subnet = ".".join(local_ip.split(".")[:-1])
    else:
        subnet = network.strip()
        if subnet.endswith(".0/24"):
            subnet = subnet[:-4]
        if subnet.endswith("."):
            subnet = subnet[:-1]
        parts = subnet.split(".")
        if len(parts) != 3:
            raise ValueError("Network must be like '192.168.1'")
    for i in range(1, 255):
        target = f"{subnet}.{i}"
        found_ip = None
        try:
            with socket.create_connection((target, 80), timeout=timeout) as sock:
                sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
                data = sock.recv(200).decode("utf-8", errors="ignore")
                if "Barix" in data:
                    found_ip = target
        except Exception:
            pass
        yield i, found_ip


def discover_barix_devices(network: Optional[str] = None, timeout: float = 0.2) -> List[str]:
    """Scan a /24 network for Barix devices and return the found IPs."""
    return [ip for _, ip in discover_barix_devices_iter(network, timeout) if ip]


def load_buttons() -> List[QuickButton]:
    if not BUTTONS_FILE.exists():
        return []
    with open(BUTTONS_FILE) as f:
        data = json.load(f)
    return [QuickButton(**item) for item in data]


def save_buttons(buttons: List[QuickButton]):
    with open(BUTTONS_FILE, "w") as f:
        json.dump([b.dict() for b in buttons], f)


def load_audio_meta() -> Dict[str, str]:
    if not AUDIO_META_FILE.exists():
        return {}
    with open(AUDIO_META_FILE) as f:
        return json.load(f)


def save_audio_meta(meta: Dict[str, str]):
    with open(AUDIO_META_FILE, "w") as f:
        json.dump(meta, f)


def get_local_ip() -> str:
    """Return the IP address of the primary network interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def list_schedules() -> Dict[str, object]:
    data = load_all_schedules()
    return {"active": data.get("active"), "schedules": list(data.get("schedules", {}).keys())}


def create_schedule(name: str):
    data = load_all_schedules()
    if name not in data.get("schedules", {}):
        data.setdefault("schedules", {})[name] = []
    data["active"] = name  # newly created becomes active
    save_all_schedules(data)


def activate_schedule(name: str):
    data = load_all_schedules()
    if name not in data.get("schedules", {}):
        raise HTTPException(status_code=404, detail="Schedule not found")
    data["active"] = name
    save_all_schedules(data)


def remove_schedule(name: str):
    data = load_all_schedules()
    if name not in data.get("schedules", {}):
        raise HTTPException(status_code=404, detail="Schedule not found")
    if name == data.get("active"):
        raise HTTPException(status_code=400, detail="Cannot delete active schedule")
    data["schedules"].pop(name)
    save_all_schedules(data)


def trigger_bell(sound_file: str):
    devices = load_devices()
    for device in devices:
        url = f"http://{device}/play"  # Example endpoint
        data = parse.urlencode({"file": sound_file}).encode()
        try:
            request.urlopen(url, data=data, timeout=2)
        except Exception as e:
            print(f"Failed to contact {device}: {e}")

def bell_daemon():
    while True:
        now = datetime.now().replace(second=0, microsecond=0)
        weekday = now.weekday()
        events = load_schedule()
        for event in events:
            if event.day == weekday and event.time.hour == now.hour and event.time.minute == now.minute:
                trigger_bell(event.sound_file)
        time.sleep(30)

def start_daemon():
    thread = threading.Thread(target=bell_daemon, daemon=True)
    thread.start()

@app.on_event("startup")
def on_startup():
    start_daemon()

@app.get("/api/schedule", response_model=List[ScheduleEntry])
def get_schedule():
    return load_schedule()


@app.post("/api/schedule", response_model=List[ScheduleEntry])
def add_schedule(entry: ScheduleEntry):
    entries = load_schedule()
    entries.append(entry)
    save_schedule(entries)
    return entries


@app.delete("/api/schedule/{index}", response_model=List[ScheduleEntry])
def delete_schedule(index: int):
    entries = load_schedule()
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="Invalid index")
    entries.pop(index)
    save_schedule(entries)
    return entries


@app.get("/api/schedules")
def get_schedules():
    return list_schedules()


@app.post("/api/schedules")
def add_schedule_name(name: ScheduleName):
    create_schedule(name.name)
    return list_schedules()


@app.post("/api/schedules/activate/{name}")
def activate(name: str):
    activate_schedule(name)
    return list_schedules()


@app.delete("/api/schedules/{name}")
def delete_schedule_name(name: str):
    remove_schedule(name)
    return list_schedules()


class Device(BaseModel):
    ip: str


@app.get("/api/devices", response_model=List[str])
def get_devices():
    return load_devices()


@app.post("/api/devices", response_model=List[str])
def add_device(device: Device):
    devices = load_devices()
    devices.append(device.ip)
    save_devices(devices)
    return devices


@app.delete("/api/devices/{index}", response_model=List[str])
def delete_device(index: int):
    devices = load_devices()
    if index < 0 or index >= len(devices):
        raise HTTPException(status_code=404, detail="Invalid index")
    devices.pop(index)
    save_devices(devices)
    return devices


@app.get("/api/devices/scan", response_model=List[str])
def scan_devices(network: Optional[str] = None):
    """Discover Barix devices on the specified network."""
    try:
        return discover_barix_devices(network=network)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/devices/scan_stream")
def scan_devices_stream(network: Optional[str] = None):
    """Stream progress while scanning for Barix devices."""

    def event_gen():
        devices: List[str] = []
        try:
            for idx, ip in discover_barix_devices_iter(network=network):
                data = {"progress": idx}
                if ip:
                    devices.append(ip)
                    data["device"] = ip
                yield f"data:{json.dumps(data)}\n\n"
            yield f"data:{json.dumps({'complete': True, 'devices': devices})}\n\n"
        except ValueError as e:
            yield f"data:{json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/network")
def network_info():
    """Return network information such as the current IP address."""
    return {"ip": get_local_ip()}


def list_audio() -> List[AudioFile]:
    meta = load_audio_meta()
    files: List[AudioFile] = []
    changed = False
    for f in AUDIO_DIR.iterdir():
        if not f.is_file() or f.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
            continue
        name = meta.get(f.name, f.stem)
        if f.name not in meta:
            meta[f.name] = name
            changed = True
        files.append(AudioFile(file=f.name, name=name))
    missing = set(meta.keys()) - {af.file for af in files}
    if missing:
        for k in missing:
            meta.pop(k, None)
        changed = True
    if changed:
        save_audio_meta(meta)
    return files


@app.get("/api/audio", response_model=List[AudioFile])
def get_audio_files():
    return list_audio()


@app.post("/api/audio", response_model=List[AudioFile])
async def upload_audio(name: str = Form(...), file: UploadFile = File(...)):
    if file.filename == "" or Path(file.filename).suffix.lower() not in SUPPORTED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    dest = AUDIO_DIR / file.filename
    with dest.open("wb") as f:
        content = await file.read()
        f.write(content)
    meta = load_audio_meta()
    meta[file.filename] = name or Path(file.filename).stem
    save_audio_meta(meta)
    return list_audio()


class TestRequest(BaseModel):
    sound_file: str


@app.get("/api/buttons", response_model=List[QuickButton])
def get_buttons():
    return load_buttons()


@app.post("/api/buttons", response_model=List[QuickButton])
def add_button(btn: QuickButton):
    buttons = load_buttons()
    buttons.append(btn)
    save_buttons(buttons)
    return buttons


@app.put("/api/buttons/{index}", response_model=List[QuickButton])
def update_button(index: int, btn: QuickButton):
    buttons = load_buttons()
    if index < 0 or index >= len(buttons):
        raise HTTPException(status_code=404, detail="Invalid index")
    buttons[index] = btn
    save_buttons(buttons)
    return buttons


@app.delete("/api/buttons/{index}", response_model=List[QuickButton])
def delete_button(index: int):
    buttons = load_buttons()
    if index < 0 or index >= len(buttons):
        raise HTTPException(status_code=404, detail="Invalid index")
    buttons.pop(index)
    save_buttons(buttons)
    return buttons


@app.post("/api/test")
def test_sound(req: TestRequest):
    if req.sound_file not in list_audio():
        raise HTTPException(status_code=404, detail="Sound file not found")
    trigger_bell(req.sound_file)
    return {"status": "ok"}

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/admin")
def admin():
    return FileResponse("static/admin.html")


@app.get("/buttons")
def buttons_page():
    return FileResponse("static/buttons.html")
