#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/aegis-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: aegis-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Aegis UI service..."
echo "Make sure you've edited aegis-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aegis-ui
sudo systemctl start aegis-ui
sudo systemctl status aegis-ui
