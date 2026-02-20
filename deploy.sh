#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/react-frontend"
BACKEND_DIR="$ROOT_DIR/python-backend"

APP_DIR="${RC_APP_DIR:-/opt/rc-control}"
WWW_DIR="${RC_WWW_DIR:-/var/www/rc-dire}"
SERVICE_FILE="${RC_SERVICE_FILE:-/etc/systemd/system/rc-control.service}"
ENV_FILE="${RC_ENV_FILE:-/etc/rc-control.env}"
NGINX_SITE_AVAILABLE="${RC_NGINX_AVAILABLE:-/etc/nginx/sites-available/rc.dire.et.conf}"
NGINX_SITE_ENABLED="${RC_NGINX_ENABLED:-/etc/nginx/sites-enabled/rc.dire.et.conf}"

echo "[1/8] Building React frontend..."
npm --prefix "$FRONTEND_DIR" ci
npm --prefix "$FRONTEND_DIR" run build

echo "[2/8] Installing backend and static assets..."
sudo mkdir -p "$APP_DIR/backend" "$WWW_DIR"
sudo rsync -a --delete "$BACKEND_DIR/" "$APP_DIR/backend/"
sudo rsync -a --delete "$FRONTEND_DIR/dist/" "$WWW_DIR/"

echo "[3/8] Installing environment file (if missing)..."
if [[ ! -f "$ENV_FILE" ]]; then
  sudo cp "$ROOT_DIR/rc-control.env.example" "$ENV_FILE"
  sudo chmod 640 "$ENV_FILE"
  echo "Created $ENV_FILE. Update RC_ADMIN_TOKEN before exposing to internet."
fi

echo "[4/8] Installing systemd service..."
sudo cp "$ROOT_DIR/rc-control.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable rc-control
sudo systemctl restart rc-control

echo "[5/8] Installing sudoers template (if missing)..."
if [[ ! -f /etc/sudoers.d/rc-control ]]; then
  sudo cp "$ROOT_DIR/rc-control.sudoers.example" /etc/sudoers.d/rc-control
  sudo chmod 440 /etc/sudoers.d/rc-control
fi

echo "[6/8] Installing nginx site..."
sudo cp "$ROOT_DIR/nginx.rc.dire.et.conf" "$NGINX_SITE_AVAILABLE"
sudo ln -sf "$NGINX_SITE_AVAILABLE" "$NGINX_SITE_ENABLED"
sudo nginx -t
sudo systemctl reload nginx

echo "[7/8] Service status:"
sudo systemctl --no-pager status rc-control | sed -n '1,25p'

echo "[8/8] Done."
echo "Next:"
echo "  - Set RC_ADMIN_TOKEN in $ENV_FILE"
echo "  - Restart service: sudo systemctl restart rc-control"
echo "  - Issue TLS cert: sudo certbot --nginx -d rc.dire.et --agree-tos --redirect"
