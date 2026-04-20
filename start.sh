#!/usr/bin/env bash
# start.sh — launch AI PDF Brain in fullscreen on the Pi's framebuffer / KMS display.
set -euo pipefail

APP_DIR="${AI_PDF_APP_DIR:-/home/pi/ai-pdf}"
VENV="${AI_PDF_VENV:-$APP_DIR/venv}"
PY="$VENV/bin/python"

export AI_PDF_ROOT="$APP_DIR"
export PYTHONUNBUFFERED=1
# SDL / pygame on a Pi without a desktop: prefer KMSDRM, fall back to FBCON.
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-kmsdrm}"
export SDL_FBDEV="${SDL_FBDEV:-/dev/fb1}"
export SDL_NOMOUSE=1
# Hide the cursor on any tty we own.
if command -v setterm >/dev/null 2>&1; then
    setterm -cursor off >/dev/tty1 2>/dev/null || true
fi

cd "$APP_DIR"

if [[ ! -x "$PY" ]]; then
    echo "python venv missing at $VENV — run install.sh first" >&2
    exit 1
fi

exec "$PY" "$APP_DIR/ai_screen.py"
