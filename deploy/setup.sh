#!/usr/bin/env bash
# Run on a fresh Ubuntu VM as root, from the extracted bundle:
#   sudo ./setup.sh               # serves plain HTTP on port 80 (no domain yet)
#   sudo ./setup.sh example.com   # serves HTTPS on that domain (DNS must point here first)
set -euo pipefail
DOMAIN="${1:-}"
SRC="$(cd "$(dirname "$0")" && pwd)"

apt-get update -y
apt-get install -y python3 python3-venv caddy

id ack >/dev/null 2>&1 || useradd --system --home /opt/ack01 --shell /usr/sbin/nologin ack
mkdir -p /opt/ack01/data
cp -r "$SRC/ack" "$SRC/site" /opt/ack01/
cp "$SRC/requirements-hosted.txt" /opt/ack01/

python3 -m venv /opt/ack01/.venv
/opt/ack01/.venv/bin/pip install -q -r /opt/ack01/requirements-hosted.txt
chown -R ack:ack /opt/ack01/data

cp "$SRC/ack-hosted.service" /etc/systemd/system/ack-hosted.service
systemctl daemon-reload
systemctl enable --now ack-hosted

SITE="${DOMAIN:-:80}"
sed "s|__SITE__|$SITE|" "$SRC/Caddyfile" > /etc/caddy/Caddyfile
systemctl restart caddy

echo "Done. Check: curl -s http://127.0.0.1/api/health"
