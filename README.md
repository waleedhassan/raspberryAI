# AI PDF Brain

A fullscreen Raspberry Pi app that surfaces one deep, non-obvious insight at a
time from a PDF, using a small local LLM. Designed for a 3.3" TFT (480×320),
boots straight into the UI with no desktop, works offline, and supports both
Arabic and English PDFs.

## Layout on the Pi

```
/home/pi/ai-pdf/
├── input/          # drop a PDF here; the app auto-detects changes
├── models/         # quantized GGUF model(s) — e.g. gemma-2b-it-q4_k_m.gguf
├── cache/          # extracted PDF chunks, keyed by file fingerprint
├── fonts/          # optional: bundle Inter / Amiri TTFs here
├── ai_screen.py    # the app
└── start.sh        # launcher used by systemd
```

## Install

On the Pi, from this repo:

```bash
./install.sh
```

This copies the app into `/home/pi/ai-pdf/`, creates a venv, installs deps
(pygame, PyMuPDF, llama-cpp-python, arabic-reshaper, python-bidi), installs
Arabic fonts system-wide, and enables the `ai-pdf-screen.service` systemd unit.

Then:

1. Place a quantized GGUF model in `/home/pi/ai-pdf/models/`. Default filename
   is `gemma-2b-it-q4_k_m.gguf`; override with `AI_PDF_MODEL=<name>` in the
   service environment.
2. Place a PDF in `/home/pi/ai-pdf/input/`.
3. `sudo systemctl start ai-pdf-screen.service` — or reboot.

## How it behaves

- On boot, systemd launches `start.sh`, which runs pygame fullscreen on the
  Pi's KMSDRM display with the cursor hidden.
- A watcher polls `input/` every ~1.5 s. When a PDF appears or is replaced,
  the app re-extracts text, detects language, caches chunks, and generates the
  first insight.
- The model runs entirely locally via `llama-cpp-python`. The first generation
  picks a random chunk; subsequent taps of the "Another Insight" button pick a
  different chunk and a new random seed, so insights don't repeat.
- Arabic PDFs produce Arabic insights (shaped + bidi for correct display).
  English PDFs produce English. Language is auto-detected from the extracted
  text.
- If the model is missing or fails to load, the app falls back to surfacing a
  striking sentence pulled directly from the document so the screen still
  works.

## Tunables (environment variables)

| Var | Default | Meaning |
| --- | --- | --- |
| `AI_PDF_ROOT` | `/home/pi/ai-pdf` | App root directory |
| `AI_PDF_MODEL` | `gemma-2b-it-q4_k_m.gguf` | GGUF filename inside `models/` |
| `AI_PDF_CTX` | `2048` | llama context size |
| `AI_PDF_THREADS` | `4` | llama CPU threads |
| `AI_PDF_MAX_TOKENS` | `160` | insight length cap |
| `AI_PDF_W` / `AI_PDF_H` | `480` / `320` | screen resolution |
| `AI_PDF_WINDOWED` | unset | if `1`, run windowed (useful on a dev machine) |

## Dev run on a laptop

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
AI_PDF_WINDOWED=1 AI_PDF_ROOT=$PWD/.devroot python ai_screen.py
```

It will create `.devroot/{input,models,cache,fonts}`. Drop a PDF in
`.devroot/input/` and a GGUF in `.devroot/models/`.

## Logs

```bash
journalctl -u ai-pdf-screen.service -f
```
