#!/usr/bin/env bash
# Build and flash the firmware, sourcing secrets from .env.
#   ./flash.sh            build + upload
#   ./flash.sh -m         build + upload, then open the serial monitor
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "No .env found — copy .env.example to .env and fill in your values:" >&2
  echo "  cp .env.example .env" >&2
  exit 1
fi

# Export .env so PlatformIO (inject_secrets.py) sees the values.
set -a
# shellcheck disable=SC1091
source .env
set +a

ENVIRONMENT="esp32-s3-epaper-154"

echo "Building + flashing ($ENVIRONMENT)..."
pio run -e "$ENVIRONMENT" -t upload ${UPLOAD_PORT:+--upload-port "$UPLOAD_PORT"}

if [[ "${1:-}" == "-m" || "${1:-}" == "--monitor" ]]; then
  pio device monitor -e "$ENVIRONMENT" ${UPLOAD_PORT:+--port "$UPLOAD_PORT"}
fi
