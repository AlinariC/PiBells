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

## Permanent Installation on a Raspberry Pi 4

To run PiBells automatically at boot, create a systemd service on your Pi:

1. **Install dependencies** (FastAPI and Uvicorn) if they are not already present:

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip -y
   pip3 install fastapi uvicorn
   ```

2. **Clone the repository** to the home directory of the `pi` user:

   ```bash
   git clone https://github.com/your-org/PiBells.git ~/PiBells
   cd ~/PiBells
   ```

3. **Create the service file** `/etc/systemd/system/pibells.service` with the following
   contents:

   ```ini
   [Unit]
   Description=PiBells Server
   After=network.target

   [Service]
   User=pi
   WorkingDirectory=/home/pi/PiBells
   ExecStart=/usr/bin/env uvicorn app.main:app --host 0.0.0.0
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

4. **Enable and start** the service:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable pibells
   sudo systemctl start pibells
   ```

PiBells will now automatically start on boot. Access the web interface at
`http://<raspberrypi-ip>:8000`.
