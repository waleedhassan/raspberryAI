# AI PDF Brain

A fullscreen Raspberry Pi app that surfaces one deep, non-obvious insight at a
time from a PDF, using a small local LLM. Designed for a 3.3" TFT (480×320),
boots straight into the UI with no desktop, works offline, and supports both
Arabic and English PDFs.

## Layout on the Pi

```
/home/waleed/ai-pdf/
├── input/          # drop a PDF here; the app auto-detects changes
├── cache/          # extracted PDF chunks, keyed by file fingerprint
├── fonts/          # optional: bundle Inter / Amiri TTFs here
├── ai_screen.py    # the app
└── start.sh        # launcher used by systemd
```

Models live under `/usr/share/ollama/.ollama/models` — managed by Ollama, not
this app.

## Install

On the Pi, from this repo:

```bash
./install.sh
```

This:

- copies the app into `/home/waleed/ai-pdf/`,
- installs runtime libs + Arabic fonts via apt,
- installs **Ollama** (prebuilt aarch64 binary — no source compile) and enables
  its systemd service,
- `ollama pull gemma4:31b-cloud` (override via `OLLAMA_MODEL=<tag> ./install.sh`),
- creates a venv with pygame, PyMuPDF, arabic-reshaper, python-bidi,
- enables the `ai-pdf-screen.service` systemd unit (ordered after `ollama.service`).

Then:

1. Place a PDF in `/home/waleed/ai-pdf/input/`.
2. `sudo systemctl start ai-pdf-screen.service` — or reboot.

## How it behaves

- On boot, systemd starts `ollama.service` first, then `start.sh`, which runs
  pygame fullscreen on the Pi's KMSDRM display with the cursor hidden.
- A watcher polls `input/` every ~1.5 s. When a PDF appears or is replaced,
  the app re-extracts text, detects language, caches chunks, and generates the
  first insight.
- Inference runs entirely locally via the **Ollama** HTTP API on
  `127.0.0.1:11434`. The first generation picks a random chunk; subsequent taps
  of the "Another Insight" button pick a different chunk and a new random seed,
  so insights don't repeat.
- Arabic PDFs produce Arabic insights (shaped + bidi for correct display).
  English PDFs produce English. Language is auto-detected from the extracted
  text.
- If Ollama is unreachable or the model isn't pulled, the app falls back to
  surfacing a striking sentence pulled directly from the document so the
  screen still works.

## Tunables (environment variables)

| Var | Default | Meaning |
| --- | --- | --- |
| `AI_PDF_ROOT` | `/home/waleed/ai-pdf` | App root directory |
| `AI_PDF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server URL |
| `AI_PDF_MODEL` | `gemma4:31b-cloud` | Ollama model tag (must be `ollama pull`ed) |
| `AI_PDF_CTX` | `2048` | context window (`num_ctx`) |
| `AI_PDF_THREADS` | `4` | CPU threads (`num_thread`) |
| `AI_PDF_MAX_TOKENS` | `160` | insight length cap (`num_predict`) |
| `AI_PDF_GEN_TIMEOUT` | `180` | HTTP timeout for one generation, seconds |
| `AI_PDF_W` / `AI_PDF_H` | `480` / `320` | screen resolution |
| `AI_PDF_WINDOWED` | unset | if `1`, run windowed (useful on a dev machine) |

## Dev run on a laptop

Install Ollama from <https://ollama.com> and pull the model once:

```bash
ollama pull gemma4:31b-cloud
```

Then:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
AI_PDF_WINDOWED=1 AI_PDF_ROOT=$PWD/.devroot python ai_screen.py
```

It will create `.devroot/{input,cache,fonts}`. Drop a PDF in `.devroot/input/`.

## Logs

```bash
journalctl -u ai-pdf-screen.service -f
journalctl -u ollama.service -f
```
