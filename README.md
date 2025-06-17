![PiBells Logo](static/pibells-logo.png)

# PiBells

A lightweight bell scheduling server using FastAPI. The web interface lets you pick a time and sound file for each bell. When a bell is due, the server sends play requests to Barix devices on the network.

## Requirements

* Python 3 with FastAPI and Uvicorn (both included in this environment).

## Running

Start the server with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

To expose the application on the standard HTTP port configure nginx to
reverse proxy requests to the FastAPI server. An example configuration is
provided in `nginx/pibells.conf`.

Open your browser at `http://<server-ip>/` to manage the schedule.

Bell schedules are stored in `schedule.json`. Multiple schedules can be created and the active one is chosen from the web interface. The server polls this file every 30 seconds and triggers the configured devices for the active schedule.

Device IPs are stored in `devices.json` and can be managed from the admin page at `http://<server-ip>/admin`.

Audio files used for bells can be uploaded from the admin page as well. Uploaded
files are stored in the `audio/` directory and are selectable when creating
schedule entries.

## Permanent Installation on a Raspberry Pi 4

PiBells can run automatically at boot using a systemd service. The easiest way
to set this up is with the provided `install.sh` script. Download the script
directly and run it with `sudo`:

```bash
curl -L https://raw.githubusercontent.com/alinaric/PiBells/main/install.sh | sudo bash
```

Or with `wget`:

```bash
wget -O - https://raw.githubusercontent.com/alinaric/PiBells/main/install.sh | sudo bash
```

The script installs required packages, clones/updates the repository for the
`pibells` user and creates a `pibells.service` file in `/etc/systemd/system`. The
service is then enabled and started so PiBells launches automatically on boot.

If you prefer to perform these steps manually, the commands executed by the
script are shown below for reference.

1. **Install dependencies** (FastAPI, Uvicorn and nginx) if they are not already present:

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip python3-venv git nginx -y
   python3 -m venv ~/pibells-venv
   source ~/pibells-venv/bin/activate
   pip install fastapi uvicorn python-multipart
   ```

2. **Clone the repository** to the home directory of the `pibells` user:

   ```bash
   git clone https://github.com/alinaric/PiBells.git ~/PiBells
   cd ~/PiBells
   ```

3. **Create the service file** `/etc/systemd/system/pibells.service` with the following
   contents:

   ```ini
   [Unit]
   Description=PiBells Server
   After=network.target

   [Service]
   User=pibells
   WorkingDirectory=/home/pibells/PiBells
   ExecStart=/home/pibells/pibells-venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
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

5. **Configure nginx** to proxy requests on port 80 to the FastAPI server:

   ```bash
   sudo cp nginx/pibells.conf /etc/nginx/sites-available/pibells
   sudo ln -sf /etc/nginx/sites-available/pibells /etc/nginx/sites-enabled/pibells
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo systemctl restart nginx
   ```

PiBells will now automatically start on boot and be available via nginx at
`http://<raspberrypi-ip>/`.
