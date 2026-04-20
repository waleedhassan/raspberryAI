#!/usr/bin/env bash
# install.sh — one-shot installer. Run on the Pi as the `pi` user (or with sudo where noted).
set -euo pipefail

APP_DIR="/home/pi/ai-pdf"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/venv"

echo "==> creating $APP_DIR layout"
mkdir -p "$APP_DIR/input" "$APP_DIR/models" "$APP_DIR/cache" "$APP_DIR/fonts"

echo "==> copying app files"
cp "$REPO_DIR/ai_screen.py" "$APP_DIR/ai_screen.py"
cp "$REPO_DIR/start.sh"     "$APP_DIR/start.sh"
cp "$REPO_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/start.sh"

echo "==> apt prerequisites (needs sudo)"
sudo apt-get update
sudo apt-get install -y \
    python3-venv python3-pip python3-dev \
    build-essential cmake pkg-config \
    libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0 libsdl2-mixer-2.0-0 \
    libfreetype6-dev libjpeg-dev \
    fonts-noto-core fonts-noto-cjk fonts-liberation \
    fonts-hosny-amiri

echo "==> python venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> systemd unit"
sudo cp "$REPO_DIR/ai-pdf-screen.service" /etc/systemd/system/ai-pdf-screen.service
sudo systemctl daemon-reload
sudo systemctl enable ai-pdf-screen.service

cat <<EOF

Install complete.

Next steps:
  1. Download a quantized model (e.g. gemma-2b-it Q4_K_M gguf) into:
         $APP_DIR/models/
     Name it gemma-2b-it-q4_k_m.gguf, or set AI_PDF_MODEL=<filename> in the service.
  2. Drop a PDF in:
         $APP_DIR/input/
  3. Start the service:
         sudo systemctl start ai-pdf-screen.service
     Or reboot — it is already enabled at boot.

Logs:    journalctl -u ai-pdf-screen.service -f
Stop:    sudo systemctl stop ai-pdf-screen.service
EOF
