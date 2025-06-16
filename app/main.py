import json
import os
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
DEVICES = ["192.168.1.10", "192.168.1.11"]  # Example Barix device IPs

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

def trigger_bell(sound_file: str):
    for device in DEVICES:
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

@app.get("/")
def index():
    return FileResponse("static/index.html")
