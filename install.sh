#!/usr/bin/env bash

# PiBells installation script
# Run this script with sudo to install PiBells as a systemd service

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run with sudo or as root" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-pip git

pip3 install fastapi uvicorn

TARGET_USER=${SUDO_USER:-pi}
HOME_DIR=$(eval echo "~$TARGET_USER")
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
ExecStart=/usr/bin/env uvicorn app.main:app --host 0.0.0.0
Restart=always

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable pibells
systemctl restart pibells

echo "PiBells installation complete. Service is running."
