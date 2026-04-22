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

echo "==> kiosk autoboot: console + autologin + xinit (no desktop)"

# Boot to console instead of the LXDE desktop.
sudo systemctl set-default multi-user.target
# If an older install enabled the graphical service, take it out of the boot path.
sudo systemctl disable ai-pdf-screen.service 2>/dev/null || true

# Auto-login user pi on tty1 via a systemd getty override.
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pi --noclear %I \$TERM
EOF

# On tty1, bash -> startx -> xinit -> our app. No display manager, no DE.
# The while loop restarts X if the app dies (equivalent to Restart=always).
PROFILE="/home/pi/.bash_profile"
if ! grep -q 'AI_PDF_KIOSK' "$PROFILE" 2>/dev/null; then
    cat >>"$PROFILE" <<'PROFILE_EOF'

# AI_PDF_KIOSK — auto-start the fullscreen app on tty1 login.
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    while :; do
        startx -- -nocursor
        sleep 2
    done
fi
PROFILE_EOF
fi
chown pi:pi "$PROFILE"

# .xinitrc is the X session. With no WM and no panel, the app is the entire UI.
# When it exits, X exits, the while loop restarts it.
cat >/home/pi/.xinitrc <<'XINIT_EOF'
#!/bin/sh
xset s off
xset -dpms
xset s noblank
exec /home/pi/ai-pdf/start.sh
XINIT_EOF
chown pi:pi /home/pi/.xinitrc
chmod +x /home/pi/.xinitrc

sudo systemctl daemon-reload

cat <<EOF

Install complete.

Next steps:
  1. Drop a PDF in:
         $APP_DIR/input/
  2. Reboot — on boot, the Pi goes straight to the app (no desktop).
         sudo reboot

Model:
  Current: $OLLAMA_MODEL  (managed by Ollama, stored under /usr/share/ollama)
  Swap:    ollama pull <tag>
           then export AI_PDF_MODEL=<tag> in start.sh

Logs:
  App:    runs on tty1 — Ctrl+Alt+F2 to get a shell, 'journalctl -b' for this boot
  Ollama: journalctl -u ollama.service -f

To undo kiosk mode and get the desktop back:
  sudo systemctl set-default graphical.target
  sudo rm /etc/systemd/system/getty@tty1.service.d/autologin.conf
  # then remove the AI_PDF_KIOSK block from /home/pi/.bash_profile
EOF
