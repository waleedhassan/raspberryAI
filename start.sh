#!/usr/bin/env bash
# start.sh — launch AI PDF Brain in fullscreen on the Pi's framebuffer / KMS display.
set -euo pipefail

APP_DIR="${AI_PDF_APP_DIR:-/home/waleed/ai-pdf}"
VENV="${AI_PDF_VENV:-$APP_DIR/venv}"
PY="$VENV/bin/python"

export AI_PDF_ROOT="$APP_DIR"
export PYTHONUNBUFFERED=1
# MHS35 TFT (fbtft / SPI) is driven by the X server configured by MHS35-show.
# Pygame talks to X, X renders onto /dev/fb1. Do NOT use kmsdrm here — it
# replaces the fbdev stack and blanks the TFT.
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-x11}"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/waleed/.Xauthority}"
export SDL_NOMOUSE=1

cd "$APP_DIR"

if [[ ! -x "$PY" ]]; then
    echo "python venv missing at $VENV — run install.sh first" >&2
    exit 1
fi

exec "$PY" "$APP_DIR/ai_screen.py"
