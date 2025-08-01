import json
import socket
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, List, Optional, Iterable, Tuple
from urllib import request, parse
import subprocess
import shlex
import secrets
import spwd
import crypt

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = BASE_DIR / "schedule.json"
DEVICES_FILE = BASE_DIR / "devices.json"
BUTTONS_FILE = BASE_DIR / "buttons.json"
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
AUDIO_META_FILE = BASE_DIR / "audio.json"
LICENSE_FILE = BASE_DIR / "license.json"
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# session token -> username mapping
sessions: Dict[str, str] = {}

# processes for looping playback
loop_processes: List[subprocess.Popen] = []


def stop_loops():
    """Terminate any looping playback processes."""
    global loop_processes
    for proc in loop_processes:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in loop_processes:
        try:
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    loop_processes = []


def authenticate_user(username: str, password: str) -> bool:
    """Return True if the provided credentials match a local account."""
    try:
        shadow = spwd.getspnam(username)
    except KeyError:
        return False
    return crypt.crypt(password, shadow.sp_pwdp) == shadow.sp_pwdp


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/static")
        or path.startswith("/audio")
        or path == "/login"
        or path == "/logout"
        or path == "/api/network"
        or path == "/rebooting"
    ):
        return await call_next(request)
    token = request.cookies.get("session")
    if not token or token not in sessions:
        return RedirectResponse("/login")
    request.state.user = sessions[token]
    response = await call_next(request)
    return response


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
    loop: bool = False


class AudioFile(BaseModel):
    file: str
    name: str


class AudioName(BaseModel):
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
    timeout: float = 0.2,
) -> Iterable[Tuple[int, Optional[str]]]:
    """Yield progress while scanning for Barix devices on the local subnet.

    The returned iterator yields a tuple ``(index, ip)`` for every host that is
    checked where ``index`` is the current host number (1-254) and ``ip`` is the
    discovered device IP or ``None`` if no device was found at that address. The
    scan looks for hosts with port 2020 open, which is the port used by Barix
    devices to stream audio.
    """
    local_ip = get_local_ip()
    if local_ip == "0.0.0.0":
        return
    subnet = ".".join(local_ip.split(".")[:-1])
    for i in range(1, 255):
        target = f"{subnet}.{i}"
        found_ip = None
        try:
            with socket.create_connection((target, 2020), timeout=timeout):
                found_ip = target
        except Exception:
            pass
        yield i, found_ip


def discover_barix_devices(timeout: float = 0.2) -> List[str]:
    """Scan the local /24 network for Barix devices and return their IPs."""
    return [ip for _, ip in discover_barix_devices_iter(timeout=timeout) if ip]


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


def load_license() -> Dict[str, str]:
    """Return saved license information or an unlicensed status."""
    if not LICENSE_FILE.exists():
        return {"status": "UNLICENSED"}
    try:
        with open(LICENSE_FILE) as f:
            data = json.load(f)
        if (
            data.get("status") == "VALID"
            and not data.get("name")
            and data.get("email")
            and data.get("key")
        ):
            refreshed = check_license(data["email"], data["key"])
            if refreshed.get("status") == "VALID":
                data.update(refreshed)
                save_license(data)
        return data
    except Exception:
        return {"status": "UNLICENSED"}


def save_license(data: Dict[str, str]):
    with open(LICENSE_FILE, "w") as f:
        json.dump(data, f)


def check_license(email: str, key: str) -> Dict[str, str]:
    """Verify a license key with the PixelPacific licensing server."""
    url = f"http://pixelpacific.com:5000/check/{key}?email={parse.quote(email)}"
    try:
        with request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        return {
            "status": data.get("status", "INVALID"),
            "expires": data.get("expires", ""),
            "name": data.get("name", ""),
        }
    except Exception as e:
        print("License check failed:", e)
        return {"status": "INVALID", "expires": ""}


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
    return {
        "active": data.get("active"),
        "schedules": list(data.get("schedules", {}).keys()),
    }


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


def trigger_bell(sound_file: str, loop: bool = False):
    """Play ``sound_file`` on all devices and the local sound card."""
    global loop_processes
    devices = load_devices()
    path = AUDIO_DIR / sound_file

    def play_local() -> Optional[subprocess.Popen]:
        """Play the audio file on the local sound card if possible."""
        if loop:
            player_cmds = [
                ["ffplay", "-nodisp", "-autoexit", "-loop", "0", str(path)],
                [
                    "bash",
                    "-c",
                    f"while true; do aplay {shlex.quote(str(path))}; done",
                ],
            ]
        else:
            player_cmds = [
                ["ffplay", "-nodisp", "-autoexit", str(path)],
                ["aplay", str(path)],
            ]
        for cmd in player_cmds:
            try:
                return subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                continue
        print("No audio player found (ffplay/aplay)")
        return None

    def send(device: str) -> Optional[subprocess.Popen]:
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-fflags",
            "+nobuffer",
            "-flush_packets",
            "1",
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
            f"udp://{device}:3020",
        ]
        try:
            if loop:
                return subprocess.Popen(
                    ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                subprocess.run(
                    ffmpeg_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
        except FileNotFoundError as e:
            print(f"Failed to stream to {device}: {e}")
        return None

    # Start local playback and send requests concurrently
    if loop:
        stop_loops()
        procs = [send(d) for d in devices]
        local_proc = play_local()
        for p in procs:
            if p:
                loop_processes.append(p)
        if local_proc:
            loop_processes.append(local_proc)
    else:
        threads = [
            threading.Thread(target=send, args=(d,), daemon=True) for d in devices
        ]
        for t in threads:
            t.start()
        threading.Thread(target=play_local, daemon=True).start()
        for t in threads:
            t.join()


def bell_daemon():
    """Check the schedule once per minute at the top of the minute."""
    while True:
        now = datetime.now().replace(second=0, microsecond=0)
        weekday = now.weekday()
        events = load_schedule()
        for event in events:
            if (
                event.day == weekday
                and event.time.hour == now.hour
                and event.time.minute == now.minute
            ):
                trigger_bell(event.sound_file)
        # Sleep until the next minute starts
        now = datetime.now()
        delay = 60 - now.second - now.microsecond / 1_000_000
        time.sleep(delay)


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


def check_device(ip: str, timeout: float = 0.5) -> bool:
    """Return True if the device responds on port 2020."""
    try:
        with socket.create_connection((ip, 2020), timeout=timeout):
            return True
    except Exception:
        return False


@app.get("/api/devices/status", response_model=Dict[str, bool])
def devices_status():
    """Check online status of configured devices."""
    devices = load_devices()
    status = {ip: check_device(ip) for ip in devices}
    return status


@app.get("/api/devices/scan", response_model=List[str])
def scan_devices():
    """Discover Barix devices on the local network."""
    return discover_barix_devices()


@app.get("/api/devices/scan_stream")
def scan_devices_stream():
    """Stream progress while scanning for Barix devices on the local network."""

    def event_gen():
        devices: List[str] = []
        try:
            for idx, ip in discover_barix_devices_iter():
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
    """Return network information such as the current IP address and hostname."""
    return {"ip": get_local_ip(), "hostname": socket.gethostname()}


def get_latest_version() -> str:
    """Fetch the latest released version tag from GitHub."""
    url = "https://api.github.com/repos/alinaric/PiBells/releases/latest"
    try:
        with request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        tag = data.get("tag_name")
        if tag:
            return tag.lstrip("v")
    except Exception as e:
        print("Failed to fetch latest version:", e)
    return __version__


@app.get("/api/version")
def version_info():
    """Return current and latest PiBells version."""
    return {"current": __version__, "latest": get_latest_version()}


class LicenseBody(BaseModel):
    email: str
    key: str


@app.get("/api/license")
def get_license_info():
    """Return current license status."""
    return load_license()


@app.post("/api/license")
def register_license(body: LicenseBody):
    result = check_license(body.email, body.key)
    if result.get("status") == "VALID":
        save_license(
            {
                "email": body.email,
                "key": body.key,
                "expires": result.get("expires", ""),
                "name": result.get("name", ""),
                "status": "VALID",
            }
        )
    return result


@app.post("/api/update")
def update_repo():
    """Pull the latest code from GitHub."""
    try:
        subprocess.run(["git", "-C", str(BASE_DIR), "pull"], check=True)
        return {"status": "updated"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")


@app.get("/api/update_stream")
def update_repo_stream():
    """Stream progress while updating the repository."""

    def event_gen():
        steps = [
            (0, "Fetching latest code", ["git", "-C", str(BASE_DIR), "fetch", "--all"]),
            (50, "Pulling updates", ["git", "-C", str(BASE_DIR), "pull"]),
        ]
        for progress, step, cmd in steps:
            yield f"data:{json.dumps({'progress': progress, 'step': step})}\n\n"
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                yield f"data:{json.dumps({'error': 'Update failed at: ' + step})}\n\n"
                return
        yield f"data:{json.dumps({'progress': 100, 'step': 'Done', 'complete': True})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/reboot")
def reboot_device():
    """Reboot the host system."""
    try:
        subprocess.Popen(["sudo", "reboot"])
        return {"status": "rebooting"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reboot failed: {e}")


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
    if (
        file.filename == ""
        or Path(file.filename).suffix.lower() not in SUPPORTED_AUDIO_EXTS
    ):
        raise HTTPException(status_code=400, detail="Unsupported file type")
    dest = AUDIO_DIR / file.filename
    with dest.open("wb") as f:
        content = await file.read()
        f.write(content)
    meta = load_audio_meta()
    meta[file.filename] = name or Path(file.filename).stem
    save_audio_meta(meta)
    return list_audio()


@app.put("/api/audio/{filename}", response_model=List[AudioFile])
def rename_audio(filename: str, body: AudioName):
    file_path = AUDIO_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    meta = load_audio_meta()
    meta[filename] = body.name
    save_audio_meta(meta)
    return list_audio()


@app.delete("/api/audio/{filename}", response_model=List[AudioFile])
def delete_audio(filename: str):
    file_path = AUDIO_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        file_path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    meta = load_audio_meta()
    meta.pop(filename, None)
    save_audio_meta(meta)
    return list_audio()


class TestRequest(BaseModel):
    sound_file: str
    loop: Optional[bool] = False


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
    trigger_bell(req.sound_file, loop=bool(req.loop))
    return {"status": "ok"}


@app.post("/api/stop")
def stop_sound():
    stop_loops()
    return {"status": "stopped"}


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    remember: Optional[bool] = Form(False),
):
    if authenticate_user(username, password):
        token = secrets.token_hex(16)
        sessions[token] = username
        resp = RedirectResponse("/", status_code=303)
        max_age = 30 * 24 * 3600 if remember else None
        resp.set_cookie("session", token, max_age=max_age, httponly=True)
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        sessions.pop(token, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/admin")
def admin():
    return FileResponse("static/admin.html")


@app.get("/buttons")
def buttons_page():
    return FileResponse("static/buttons.html")


@app.get("/manifest.json")
def manifest_file():
    return FileResponse("static/manifest.json")


@app.get("/service-worker.js")
def service_worker():
    return FileResponse("static/service-worker.js", media_type="application/javascript")


@app.get("/rebooting")
def rebooting_page():
    return FileResponse("static/rebooting.html")
