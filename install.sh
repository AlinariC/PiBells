#!/usr/bin/env bash

# PiBells installation script
# Run this script with sudo to install PiBells as a systemd service

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run with sudo or as root" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-pip python3-venv git

TARGET_USER=${SUDO_USER:-pi}
HOME_DIR=$(eval echo "~$TARGET_USER")

# set up python virtual environment for the target user
VENV_DIR="$HOME_DIR/pibells-venv"
if [ ! -d "$VENV_DIR" ]; then
  sudo -u "$TARGET_USER" python3 -m venv "$VENV_DIR"
fi

# install required packages inside the virtual environment
sudo -u "$TARGET_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$TARGET_USER" "$VENV_DIR/bin/pip" install fastapi uvicorn
INSTALL_DIR="$HOME_DIR/PiBells"

if [ -d "$INSTALL_DIR" ]; then
  echo "Updating existing PiBells repo in $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull
else
  echo "Cloning PiBells repo to $INSTALL_DIR"
  git clone https://github.com/alinaric/PiBells.git "$INSTALL_DIR"
fi

SERVICE_FILE=/etc/systemd/system/pibells.service

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=PiBells Server
After=network.target

[Service]
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/uvicorn app.main:app --host 0.0.0.0
Restart=always

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable pibells
systemctl restart pibells

echo "PiBells installation complete. Service is running."
