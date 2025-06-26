<p align="center">
  <img src="static/pibells-logo.png" width="160" alt="PiBells logo"/>
</p>

<h1 align="center">PiBells</h1>

<p align="center">
  <b>Network bell scheduling for Raspberry Pi</b><br>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PPPL%201.0-blue" alt="License"></a>
  <a href="https://pixelpacific.com"><img src="https://img.shields.io/badge/PixelPacific-Website-blue" alt="PixelPacific"></a>
</p>

PiBells is a lightweight bell scheduler built with FastAPI. It turns a Raspberry Pi into a dedicated bell controller that you manage entirely from your browser. Upload sounds, create schedules and configure your Barix devices without touching the command line. Designed for Raspberry Pi OS Lite (64‑bit), PiBells runs quietly in the background and continues to work even without an internet connection.

## Features
- 📅 Create multiple schedules from your browser
- 🔊 Stream bells to Barix devices using UDP on port 3020 or play audio locally
- 🌐 Scan your network to discover devices automatically
- 🎵 Upload MP3, WAV, OGG or M4A files as bell sounds
- 🗑 Delete old audio files to free up space
- 🔘 Create custom quick-play buttons
- 🔄 Update the software from the admin page
- 🔐 Password-protected web interface
- 📈 Login banner displays the Pi's IP address
- ⚙️ Works offline after installation
- ✨ Modern animated UI with light and dark themes

## Hardware
- 🤖 Raspberry Pi 4 Model B (4 GB+ recommended)
- 💾 microSD card (8 GB or larger) with Raspberry Pi OS Lite (64-bit)
- 🌐 Network connection (Ethernet or Wi-Fi)
- 🔈 Optional Barix Exstreamer or local speaker

## Installation
Run the install script on your Pi with `sudo`:

```bash
curl -L https://raw.githubusercontent.com/alinaric/PiBells/main/install.sh | sudo bash
```

The script installs dependencies, sets up a virtual environment, clones the repo and creates a `systemd` service so PiBells starts on boot. You can run the same command again at any time to update.

Once the service is running, open `http://<raspberrypi-ip>/` in your browser to log in and configure your bells.

### Manual steps
If you prefer to do things yourself, see [`install.sh`](install.sh) for the commands.

## Preparing the Raspberry Pi
1. Use **Raspberry Pi Imager** and select the **Raspberry Pi OS Lite (64-bit)** image.
2. In the Imager's advanced options, set the username to **pibells**, choose a password, configure your network and enable SSH if desired.
3. Insert the card into the Pi, power it on and wait for it to connect to your network.
4. Before installing, add your PiBells account to the `shadow` group so the service can verify passwords:

   ```bash
   sudo usermod -aG shadow pibells
   ```

5. Log in as the user you configured and run the install script above. It will update the system and install all dependencies.

## Versioning
The current version is automatically detected from the latest git tag. The admin page checks the newest GitHub release and lets you upgrade with a single click.

---

Distributed under the [PixelPacific Public License (PPPL 1.0)](LICENSE).  
Learn more about PixelPacific at [pixelpacific.com](https://pixelpacific.com).
