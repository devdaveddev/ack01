#!/usr/bin/env bash
# Run on your computer. Builds the site and packs only what the server needs.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd site && npm run build >/dev/null)
mkdir -p deploy/out
tar --exclude='__pycache__' --transform='s|^deploy/||' -czf deploy/out/ack01-hosted.tar.gz \
  ack/__init__.py ack/hosted site/dist deploy/requirements-hosted.txt deploy/ack-hosted.service deploy/Caddyfile deploy/setup.sh
echo "wrote deploy/out/ack01-hosted.tar.gz ($(du -h deploy/out/ack01-hosted.tar.gz | cut -f1))"
