#!/usr/bin/env bash
# Install this project as a systemd service on a Raspberry Pi
# (start on boot, auto restart on crash).
#
# Usage (on the Raspberry Pi):
#   cd ~/raspberry-health-monitor/rpi/deploy
#   sudo bash install-service.sh
#
# NOTE: this script is pure ASCII on purpose. Any non-ASCII byte in a
# .sh/.bat/.ps1 file can be decoded with the wrong code page on another
# machine and break the parser (see the project troubleshooting notes).
set -euo pipefail

SERVICE_NAME="health-monitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_RPI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/${SERVICE_NAME}.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
DATA_DIR="/home/pi/health-monitor-data"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: please run as root: sudo bash install-service.sh"
  exit 1
fi

if [ ! -f "${SERVICE_SRC}" ]; then
  echo "ERROR: ${SERVICE_SRC} not found"
  exit 1
fi

echo "[1/5] Checking python3 ..."
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c "import sys; assert sys.version_info >= (3, 9), 'need python 3.9+'"

echo "[2/5] Creating data dir ${DATA_DIR} ..."
mkdir -p "${DATA_DIR}"
chown -R pi:pi "${DATA_DIR}" 2>/dev/null || true

echo "[3/5] Installing unit to ${SERVICE_DST} ..."
cp "${SERVICE_SRC}" "${SERVICE_DST}"

echo "[4/5] Adjusting paths to this machine (${PROJECT_RPI_DIR}) ..."
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=${PROJECT_RPI_DIR}#" "${SERVICE_DST}"
sed -i "s#^ExecStart=.*#ExecStart=/usr/bin/python3 -m health_monitor serve --real --host 0.0.0.0 --port 8080 --store ${DATA_DIR}/history.db#" "${SERVICE_DST}"

echo "[5/5] Enabling and starting service ..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --full status "${SERVICE_NAME}" || true

echo ""
echo "Done. Useful commands:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo "  curl http://127.0.0.1:8080/api/v1/current"
echo ""
echo "NOTE: if the service keeps restarting, run this first to see the real error:"
echo "  cd ${PROJECT_RPI_DIR} && python3 -m health_monitor selfcheck --real"
