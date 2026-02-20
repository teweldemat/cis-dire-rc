#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/react-frontend"
BACKEND_DIR="$ROOT_DIR/python-backend"

APP_DIR="${RC_APP_DIR:-/opt/rc-control}"
WWW_DIR="${RC_WWW_DIR:-/var/www/rc-dire}"
SERVICE_FILE="${RC_SERVICE_FILE:-/etc/systemd/system/rc-control.service}"
HELPER_SERVICE_FILE="${RC_HELPER_SERVICE_FILE:-/etc/systemd/system/rc-control-helper.service}"
ENV_FILE="${RC_ENV_FILE:-/etc/rc-control.env}"
NGINX_SITE_AVAILABLE="${RC_NGINX_AVAILABLE:-/etc/nginx/sites-available/rc.dire.et.conf}"
NGINX_SITE_ENABLED="${RC_NGINX_ENABLED:-/etc/nginx/sites-enabled/rc.dire.et.conf}"

echo "[1/9] Building React frontend..."
npm --prefix "$FRONTEND_DIR" ci
npm --prefix "$FRONTEND_DIR" run build

echo "[2/9] Installing backend and static assets..."
sudo mkdir -p "$APP_DIR/backend" "$WWW_DIR"
sudo rsync -a --delete "$BACKEND_DIR/" "$APP_DIR/backend/"
sudo rsync -a --delete "$FRONTEND_DIR/dist/" "$WWW_DIR/"

echo "[3/9] Installing environment file (if missing)..."
sudo mkdir -p "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
  sudo cp "$ROOT_DIR/rc-control.env.example" "$ENV_FILE"
  sudo chmod 640 "$ENV_FILE"
  echo "Created $ENV_FILE. Update RC_ADMIN_TOKEN before exposing to internet."
fi

echo "[4/9] Installing systemd services..."
sudo mkdir -p "$(dirname "$HELPER_SERVICE_FILE")" "$(dirname "$SERVICE_FILE")"
sudo cp "$ROOT_DIR/rc-control-helper.service" "$HELPER_SERVICE_FILE"
sudo cp "$ROOT_DIR/rc-control.service" "$SERVICE_FILE"

echo "[5/9] Reloading systemd and restarting services..."
HAS_SYSTEMCTL=0
if command -v systemctl >/dev/null 2>&1; then
  HAS_SYSTEMCTL=1
  sudo systemctl daemon-reload
  sudo systemctl enable rc-control-helper
  sudo systemctl enable rc-control
  sudo systemctl restart rc-control-helper
  sudo systemctl restart rc-control
else
  echo "systemctl not found; skipped service enable/restart."
fi

echo "[6/9] Checking legacy sudoers template..."
if [[ ! -f /etc/sudoers.d/rc-control ]]; then
  echo "Legacy /etc/sudoers.d/rc-control not present (not required with helper service)."
else
  echo "Legacy /etc/sudoers.d/rc-control exists; helper service design no longer requires it."
fi

echo "[7/9] Installing nginx site..."
sudo mkdir -p "$(dirname "$NGINX_SITE_AVAILABLE")" "$(dirname "$NGINX_SITE_ENABLED")"
sudo cp "$ROOT_DIR/nginx.rc.dire.et.conf" "$NGINX_SITE_AVAILABLE"
sudo ln -sf "$NGINX_SITE_AVAILABLE" "$NGINX_SITE_ENABLED"
if command -v nginx >/dev/null 2>&1; then
  sudo nginx -t
  if [[ "$HAS_SYSTEMCTL" -eq 1 ]]; then
    sudo systemctl reload nginx
  else
    sudo nginx -s reload
  fi
else
  echo "nginx not found; copied site config but skipped validation/reload."
fi

echo "[8/9] Service status:"
if [[ "$HAS_SYSTEMCTL" -eq 1 ]]; then
  sudo systemctl --no-pager status rc-control-helper | sed -n '1,25p'
  sudo systemctl --no-pager status rc-control | sed -n '1,25p'
else
  echo "systemctl not found; status output skipped."
fi

echo "[9/9] Done."
echo "Next:"
echo "  - Set RC_ADMIN_TOKEN in $ENV_FILE"
echo "  - Restart services: sudo systemctl restart rc-control-helper rc-control"
echo "  - Issue TLS cert: sudo certbot --nginx -d rc.dire.et --agree-tos --redirect"
