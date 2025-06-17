import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List
from urllib import request, parse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

SCHEDULE_FILE = Path("schedule.json")
DEVICES_FILE = Path("devices.json")

app = FastAPI()

class ScheduleEntry(BaseModel):
    time: datetime
    sound_file: str

def load_schedule() -> List[ScheduleEntry]:
    if not SCHEDULE_FILE.exists():
        return []
    with open(SCHEDULE_FILE) as f:
        data = json.load(f)
    return [ScheduleEntry(**item) for item in data]

def save_schedule(entries: List[ScheduleEntry]):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump([e.dict() for e in entries], f, default=str)

def load_devices() -> List[str]:
    if not DEVICES_FILE.exists():
        return []
    with open(DEVICES_FILE) as f:
        return json.load(f)


def save_devices(devices: List[str]):
    with open(DEVICES_FILE, "w") as f:
        json.dump(devices, f)


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

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/admin")
def admin():
    return FileResponse("static/admin.html")
