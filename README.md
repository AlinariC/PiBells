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
- The command receipt file is bound to the Threadhall URL, controller UUID, district, school, and device. Receipts remain on disk across unpairing and uncertain pairing responses. A missing file, an older flat receipt file, or an unbound `{}` file pauses physical commands and blocks re-pairing until an operator reconciles the controller; do not delete or reset the file to clear this condition. Older flat files may have evicted completed command IDs and cannot establish a complete execution history. A changed URL or controller identity for the same pairing authority also requires reconciliation before a new receipt partition can be used.
- A first pairing writes a durable empty bootstrap receipt with a controller UUID before requesting Threadhall authority. If that first request fails, pairing can resume only when this bootstrap matches its pending local pairing, or when the bootstrap exists before any local pairing config was saved. Normal command-history reads still reject an empty bootstrap; a missing or mismatched receipt needs operator reconciliation.
- New pairings require Threadhall's v2 execution-claim protocol. A non-STOP offer has no executable payload; PiBells records a one-shot claim intent before requesting its payload and acts only after validating the first grant for the current pairing. A lost claim response, stale pairing, crash, or uncertain output remains unresolved locally and is never retried or automatically acknowledged as a definite outcome. STOP_AUDIO remains available without a claim. The candidate requires manual controller and output validation before deployment.
- Existing paired controllers without a stored protocol version continue as v1 until they are paired again; v1 non-STOP commands are quarantined while STOP_AUDIO remains available. Threadhall connections require HTTPS, except HTTP loopback addresses for local development, and reject redirects before forwarding a device bearer.
- STOP_AUDIO tracks ordinary and looping local/Barix playback, terminates the player process groups, and acknowledges success only after termination can be verified. Remote and local STOP advance a stored ordering fence before stopping audio; a valid claim response that arrives after STOP cannot start older playback. An uncertain stop keeps its process handles and receipt, sends no terminal acknowledgement, and retries if Threadhall reoffers that STOP command. If a group leader exits while audio children survive, the controller leaves STOP unresolved rather than risk signaling a reused process group ID; operator intervention may be needed. A controller process restart loses the in-memory process registry; verify the physical output and STOP behavior on the target controller before release.
- Local STOP on a paired controller also writes a durable hold on all later Threadhall non-STOP offers. The controller continues STOP-only polls and does not resume remote commands or managed bells until an operator completes a new v2 pairing. A scheduled bell already being planned is discarded if STOP wins before playback starts; if playback starts first, STOP waits for the launch lock and then terminates it.
- If STOP_AUDIO cannot verify termination, a separate durable hold and its retained command receipt block all later remote claims and scheduled bells, including after restart and after the server stops reoffering an expired STOP. A later verified STOP or a successful re-pair that verifies local termination resolves the hold. An expired STOP with uncertain termination is never acknowledged as terminal.
- An authenticated remote STOP is handled before schedule validation and command-history reads, so a damaged receipt file cannot delay the physical stop. Pairing state, STOP fences, and command receipts require successful file and parent-directory fsync; a filesystem that cannot prove these writes blocks execution claims and needs operator reconciliation.
- If dashboard or schedule collection fails before a sync request, the controller sends a minimal sync payload with `telemetry.stop_only_poll: true` so Threadhall can return STOP_AUDIO without schedule publication. Normal sync omits that marker. The controller processes only STOP from a degraded response and does not publish schedules, claim other commands, or send acknowledgements. A failed authority-clear write also fences managed bell dispatch and claims in this process until operator reconciliation; the sync poller survives a second failure while saving its error state.
- Threadhall may report `execution_claim_v1: false` in a held STOP-only sync response, including when the server independently holds a normal poll because the current school year is invalid. The controller accepts that capability value only with the authenticated, version-matched held STOP-only publication; it processes STOP_AUDIO and refuses non-STOP commands, schedules, and acknowledgements. Normal non-STOP offers still require `execution_claim_v1: true`.
- A malformed, unreadable, or unexpectedly missing pairing file triggers a physical STOP and an operator-reconciliation state. With a last known valid identity still in memory, the controller uses that identity only for STOP-only polls; it blocks playback and pairing-state writes until explicit re-pairing or disabling. After a cold restart without a recoverable token, remote polling cannot authenticate, so the local physical STOP and file repair are required. A storage failure that prevents persisting a STOP hold cannot prove the hold survives power loss; do not restart or release that controller before reconciling it.
