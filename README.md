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
- Bundled default sample pack for routine bells and plain-language emergency prompts
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

## Default audio samples

PiBells ships with replaceable default samples for Threadhall sound keys:

- Routine transitions: `start_day`, `passing`, `lunch`, `end_day`, and `test`
- Emergency prompts: `emergency`, `hold`, `secure`, `lockdown`, `evacuate`, `shelter`, `medical`, and `all_clear`

The emergency files are generic sample recordings, not district policy. Districts can upload their own approved audio from the PiBells admin page and point schedules or buttons at those files. To regenerate the bundled samples on macOS with `say` and `ffmpeg` available:

```bash
python3 scripts/generate_default_audio.py
```

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
- Set `PIBELLS_DISTRICT_TIME_ZONE` to the exact Threadhall district IANA zone (for example, `America/Phoenix`) before enabling managed bells. An owner-managed `district_time_zone` value in `threadhall-pairing.json` is also accepted; the environment setting takes precedence. Threadhall's current sync response does not attest the district zone, so deployment must independently compare this setting with the district record.
- Managed bell dispatch uses district-local calendar dates and wall times, independent of the controller's host time zone. It stays silent until a successful sync supplies a `server_time` sample within a five-second worst-case skew bound. The sample expires after 120 seconds, and a controller clock step over five seconds blocks dispatch until another valid sync. The repeated hour at the end of daylight saving time plays a scheduled bell once. Local schedules remain usable without this managed-clock proof.
- `/api/threadhall/status` and `/api/dashboard` expose `schedule_clock` with the configured district zone, district and host UTC offsets, clock estimate/bound, sample age, and dispatch readiness reason. Keep the controller's system clock synchronized and verify a district-local timestamp and observed physical bell during deployment; a source-level or sync receipt alone does not prove output timing.
