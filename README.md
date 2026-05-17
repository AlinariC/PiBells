<p align="center">
  <img src="static/pibells-logo.png" width="160" alt="PiBells logo"/>
</p>

<h1 align="center">PiBells</h1>

<p align="center">
  <b>Network bell scheduling for Raspberry Pi</b>
</p>

PiBells turns a Raspberry Pi into a browser-managed bell controller. It can play audio locally, stream bells to Barix devices, scan the local network for devices, and maintain multiple weekly schedules from a compact web console.

## What changed in this revival

- Removed the retired licensing server integration and all registration UI/API code.
- Replaced deprecated Linux shadow-password authentication with a first-run PiBells admin account.
- Added stable schedule and quick-button IDs so events can be edited safely.
- Added safer JSON persistence, file upload naming, duplicate device handling, and path traversal protection.
- Reworked the interface into a modern dashboard with shared navigation, responsive layouts, dark/light themes, and clearer operational states.

## Features

- Multiple named schedules with a weekly event board
- Enable, disable, edit, play, and delete individual bell events
- Quick-play buttons with colors, icons, and optional loop-until-stopped playback
- Audio upload, rename, test, and protected delete behavior
- Barix device management with nickname support, online status checks, and selectable multi-range discovery
- Local Raspberry Pi playback through `ffplay` or `aplay`
- UDP streaming to Barix devices on port `3030`
- First-run admin setup and password change from the settings page
- Reboot control from the web UI
- PWA manifest and cached static assets for resilient local use

## Hardware

- Raspberry Pi 4 or newer recommended
- Raspberry Pi OS Lite 64-bit
- Network connection by Ethernet or Wi-Fi
- Optional Barix Exstreamer devices or a local speaker

## Installation

Run the installer on the Pi with `sudo`:

```bash
curl -L https://raw.githubusercontent.com/alinaric/PiBells/main/install.sh | sudo bash
```

The script installs system dependencies, creates a Python virtual environment, clones or updates the repo, installs Python packages from `requirements.txt`, and configures PiBells behind nginx as a systemd service.

After installation, open:

```text
http://<raspberrypi-ip>/
```

On first launch PiBells redirects to `/setup`, where you create the local admin account. No external licensing server is required.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
PIBELLS_DISABLE_DAEMON=1 uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/`.

Runtime data such as `pibells-auth.json`, `audio.json`, and `buttons.json` is intentionally ignored by git.

## Testing

```bash
PIBELLS_DISABLE_DAEMON=1 pytest
```

## Notes

- Existing `schedule.json` files without event IDs are migrated automatically.
- Existing `buttons.json` files without button IDs are migrated automatically.
- Uploaded audio filenames are sanitized and de-duplicated.
- Deleting audio that is used by schedules or quick buttons requires confirmation in the UI and removes those references when forced.
- Barix UDP discovery uses port `30718`. Active subnet discovery can be narrowed with saved CIDR ranges such as `10.80.2.0/24`.
