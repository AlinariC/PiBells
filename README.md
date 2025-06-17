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

Bell schedules are stored in `schedule.json`. Multiple schedules can be created and the active one is chosen from the web interface. The server polls this file every 30 seconds and triggers the configured devices for the active schedule.

Device IPs are stored in `devices.json` and can be managed from the admin page at `http://localhost:8000/admin`.

Audio files used for bells can be uploaded from the admin page as well. Uploaded
files are stored in the `audio/` directory and are selectable when creating
schedule entries.
