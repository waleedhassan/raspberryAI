#!/usr/bin/env bash
# install.sh — one-shot installer. Run on the Pi as the `pi` user (or with sudo where noted).
set -euo pipefail

APP_DIR="/home/pi/ai-pdf"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/venv"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:31b-cloud}"

echo "==> creating $APP_DIR layout"
mkdir -p "$APP_DIR/input" "$APP_DIR/cache" "$APP_DIR/fonts"

echo "==> copying app files"
cp "$REPO_DIR/ai_screen.py" "$APP_DIR/ai_screen.py"
cp "$REPO_DIR/start.sh"     "$APP_DIR/start.sh"
cp "$REPO_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/start.sh"

echo "==> apt prerequisites (needs sudo)"
sudo apt-get update
sudo apt-get install -y \
    python3-venv python3-pip python3-dev \
    libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0 libsdl2-mixer-2.0-0 \
    libfreetype6 libjpeg62-turbo libportmidi0 \
    fonts-dejavu-core fonts-noto-core fonts-noto-cjk fonts-liberation \
    fonts-hosny-amiri \
    curl ca-certificates

echo "==> checking CPU architecture"
# uname -m tells the kernel arch. dpkg tells the userland arch, which is what
# actually matters — some Pi OS images have a 64-bit kernel but 32-bit userland.
DPKG_ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
KERNEL_ARCH="$(uname -m)"
if [ "$DPKG_ARCH" != "arm64" ] && [ "$DPKG_ARCH" != "aarch64" ]; then
    cat >&2 <<ERR

ERROR: Your Pi userland is 32-bit (dpkg arch: $DPKG_ARCH, kernel: $KERNEL_ARCH).
Ollama requires a 64-bit userland (arm64).

Your kernel is already 64-bit, but the OS image you flashed was 32-bit.
Re-flash your SD card with the 64-bit Raspberry Pi OS:
  https://www.raspberrypi.com/software/
  Choose: "Raspberry Pi OS (64-bit)" or "Raspberry Pi OS Lite (64-bit)"

After flashing and booting, run ./install.sh again.
ERR
    exit 1
fi

echo "==> installing Ollama (prebuilt aarch64 binary — no compile)"
if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
# The Ollama installer usually registers a systemd unit named `ollama.service`.
# Make sure it's running before we try to pull.
sudo systemctl enable --now ollama.service
# Wait briefly for the daemon to start listening on :11434.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "==> pulling model $OLLAMA_MODEL (one-time download)"
ollama pull "$OLLAMA_MODEL"

echo "==> python venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel

# Pure-Python packages — always fast, no compile risk.
echo "==> installing pure-python deps"
"$VENV/bin/pip" install arabic-reshaper==3.0.0 python-bidi==0.4.2

# pygame and PyMuPDF both have prebuilt aarch64 wheels on PyPI.
# --only-binary=:all: makes pip take the wheel or fail immediately with a
# clear error — it will NEVER attempt a source compile that hangs the Pi.
echo "==> installing pygame (wheel only)"
"$VENV/bin/pip" install --only-binary=:all: pygame==2.6.1 || {
    echo "  no pygame wheel found — falling back to system package"
    sudo apt-get install -y python3-pygame
}

echo "==> installing PyMuPDF (wheel only)"
"$VENV/bin/pip" install --only-binary=:all: PyMuPDF==1.24.9 || {
    echo "  PyMuPDF 1.24.9 wheel not found for this Python — trying latest"
    "$VENV/bin/pip" install --only-binary=:all: PyMuPDF || {
        echo "  still no wheel — installing system libmupdf and building with apt headers"
        sudo apt-get install -y libmupdf-dev
        "$VENV/bin/pip" install PyMuPDF
    }
}

echo "==> systemd unit"
sudo cp "$REPO_DIR/ai-pdf-screen.service" /etc/systemd/system/ai-pdf-screen.service
sudo systemctl daemon-reload
sudo systemctl enable ai-pdf-screen.service

cat <<EOF

Install complete.

Next steps:
  1. Drop a PDF in:
         $APP_DIR/input/
  2. Start the service:
         sudo systemctl start ai-pdf-screen.service
     Or reboot — it is already enabled at boot.

Model:
  Current: $OLLAMA_MODEL  (managed by Ollama, stored under /usr/share/ollama)
  Swap:    ollama pull <tag>
           then set Environment=AI_PDF_MODEL=<tag> in ai-pdf-screen.service

Logs:
  App:    journalctl -u ai-pdf-screen.service -f
  Ollama: journalctl -u ollama.service -f
EOF
