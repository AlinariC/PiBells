import json
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib import request, parse

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

SCHEDULE_FILE = Path("schedule.json")
DEVICES_FILE = Path("devices.json")
AUDIO_DIR = Path("audio")
AUDIO_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path("static")

app = FastAPI()
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class ScheduleEntry(BaseModel):
    time: datetime
    sound_file: str


class ScheduleName(BaseModel):
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
        events = load_schedule()
        for event in events:
            if event.time.replace(second=0, microsecond=0) == now:
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


@app.get("/api/network")
def network_info():
    """Return network information such as the current IP address."""
    return {"ip": get_local_ip()}


def list_audio() -> List[str]:
    return [f.name for f in AUDIO_DIR.iterdir() if f.is_file()]


@app.get("/api/audio", response_model=List[str])
def get_audio_files():
    return list_audio()


@app.post("/api/audio", response_model=List[str])
async def upload_audio(file: UploadFile = File(...)):
    dest = AUDIO_DIR / file.filename
    with dest.open("wb") as f:
        content = await file.read()
        f.write(content)
    return list_audio()


class TestRequest(BaseModel):
    sound_file: str


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
