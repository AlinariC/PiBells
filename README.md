# PiBells

A lightweight bell scheduling server using FastAPI. The web interface lets you pick a time and sound file for each bell. When a bell is due, the server sends play requests to Barix devices on the network.

## Requirements

* Python 3 with FastAPI and Uvicorn (both included in this environment).

## Running

Start the server with:

```bash
uvicorn app.main:app --reload
```

Open your browser at `http://localhost:8000` to manage the schedule.

Bell times are stored in `schedule.json`. The server polls this file every 30 seconds and triggers the configured devices.

Device IPs are stored in `devices.json` and can be managed from the admin page at `http://localhost:8000/admin`.
