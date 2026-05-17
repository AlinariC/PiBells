#!/usr/bin/env bash

# PiBells installation script
# Run with sudo on Raspberry Pi OS.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run with sudo or as root" >&2
  exit 1
fi

apt-get update
apt-get upgrade -y
apt-get install -y python3 python3-pip python3-venv git nginx ffmpeg alsa-utils netcat-openbsd

TARGET_USER=${SUDO_USER:-pibells}
HOME_DIR=$(eval echo "~$TARGET_USER")
VENV_DIR="$HOME_DIR/pibells-venv"
INSTALL_DIR="$HOME_DIR/PiBells"

if [ ! -d "$VENV_DIR" ]; then
  sudo -u "$TARGET_USER" python3 -m venv "$VENV_DIR"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing PiBells repo in $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "Cloning PiBells repo to $INSTALL_DIR"
  git clone https://github.com/alinaric/PiBells.git "$INSTALL_DIR"
fi

chown -R "$TARGET_USER":"$TARGET_USER" "$INSTALL_DIR"

sudo -u "$TARGET_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$TARGET_USER" "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

SERVICE_FILE=/etc/systemd/system/pibells.service

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=PiBells Server
After=network-online.target
Wants=network-online.target

[Service]
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable pibells
systemctl restart pibells

NGINX_CONF=/etc/nginx/sites-available/pibells
cat > "$NGINX_CONF" <<'NGINX'
server {
    listen 80;
    server_name _;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
NGINX

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/pibells
rm -f /etc/nginx/sites-enabled/default
systemctl restart nginx

BANNER_DIR=/etc/pibells
mkdir -p "$BANNER_DIR"
cat > "$BANNER_DIR/pibells-ascii.txt" <<'EOF'
       _____
      /     \
     /       \
    |  () ()  |
     \   ^   /
      |_____|
      /     \
     /_______\
EOF

cat > /usr/local/bin/pibells-banner.sh <<'EOF'
#!/usr/bin/env bash
IP=$(hostname -I | awk '{print $1}')
{
  cat /etc/pibells/pibells-ascii.txt
  echo
  echo "PiBells: http://$IP/"
} > /etc/issue
EOF
chmod +x /usr/local/bin/pibells-banner.sh

cat > /etc/systemd/system/pibells-banner.service <<'EOF'
[Unit]
Description=PiBells login banner
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pibells-banner.sh

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pibells-banner
systemctl start pibells-banner

IP_ADDRESS=$(hostname -I | awk '{print $1}')
echo "PiBells installation complete."
echo "Open http://$IP_ADDRESS/ and create the first admin account."
