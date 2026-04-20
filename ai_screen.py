"""AI PDF Brain — fullscreen Pi app that surfaces insights from a PDF."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import random
import re
import signal
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pygame

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

ROOT = Path(os.environ.get("AI_PDF_ROOT", "/home/pi/ai-pdf"))
INPUT_DIR = ROOT / "input"
CACHE_DIR = ROOT / "cache"
FONTS_DIR = ROOT / "fonts"

SCREEN_W = int(os.environ.get("AI_PDF_W", 480))
SCREEN_H = int(os.environ.get("AI_PDF_H", 320))
FPS = 30

OLLAMA_URL = os.environ.get("AI_PDF_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL_TAG = os.environ.get("AI_PDF_MODEL", "gemma4:31b-cloud")
MODEL_CTX = int(os.environ.get("AI_PDF_CTX", 2048))
MODEL_THREADS = int(os.environ.get("AI_PDF_THREADS", 4))
MAX_TOKENS = int(os.environ.get("AI_PDF_MAX_TOKENS", 160))
GEN_TIMEOUT = float(os.environ.get("AI_PDF_GEN_TIMEOUT", 180))

MAX_CHUNK_CHARS = 1800
MAX_CHUNKS_CACHED = 64

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

BG_TOP = (8, 12, 28)
BG_BOTTOM = (18, 8, 36)
ACCENT_A = (128, 90, 255)
ACCENT_B = (70, 180, 255)
CARD_FILL = (255, 255, 255, 18)
CARD_EDGE = (255, 255, 255, 55)
TEXT_MAIN = (240, 244, 255)
TEXT_DIM = (170, 180, 210)
BTN_TOP = (96, 80, 230)
BTN_BOT = (60, 140, 230)
BTN_GLOW = (140, 120, 255)

# ---------------------------------------------------------------------------
# Arabic / language support
# ---------------------------------------------------------------------------

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    def shape_rtl(text: str) -> str:
        return get_display(arabic_reshaper.reshape(text))
except Exception:  # pragma: no cover - optional
    def shape_rtl(text: str) -> str:
        return text


ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def detect_language(text: str) -> str:
    """Return 'ar' for Arabic-dominant text, 'en' otherwise."""
    sample = text[:4000]
    if not sample:
        return "en"
    arabic_chars = len(ARABIC_RE.findall(sample))
    letters = sum(1 for ch in sample if ch.isalpha())
    if letters and arabic_chars / letters > 0.3:
        return "ar"
    return "en"


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


def _file_fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha1()
    h.update(str(path).encode())
    h.update(str(st.st_size).encode())
    h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()[:16]


def _split_chunks(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= limit:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            if len(para) <= limit:
                buf = para
            else:
                for i in range(0, len(para), limit):
                    chunks.append(para[i : i + limit])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def extract_pdf(path: Path) -> dict:
    """Return dict with chunks, language, fingerprint. Uses on-disk cache."""
    import fitz  # PyMuPDF

    fp = _file_fingerprint(path)
    cache_file = CACHE_DIR / f"{fp}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    doc = fitz.open(path)
    parts: list[str] = []
    for page in doc:
        parts.append(page.get_text("text"))
    doc.close()
    full = "\n\n".join(parts)
    chunks = _split_chunks(full)
    if len(chunks) > MAX_CHUNKS_CACHED:
        stride = len(chunks) / MAX_CHUNKS_CACHED
        chunks = [chunks[int(i * stride)] for i in range(MAX_CHUNKS_CACHED)]
    lang = detect_language(full)
    payload = {
        "fingerprint": fp,
        "path": str(path),
        "language": lang,
        "chunks": chunks,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

EN_SYSTEM = (
    "You are a contemplative reader who surfaces hidden truths from texts. "
    "Given an excerpt, produce ONE deep, non-obvious insight — a wisdom-like "
    "statement that reveals something the reader would not see on first pass. "
    "Do NOT summarize. Do NOT list. One or two sentences. Finish with a period."
)
AR_SYSTEM = (
    "أنت قارئ متأمل يستخرج الحقائق الخفية من النصوص. اقرأ المقطع واستخرج "
    "فكرة واحدة عميقة وغير واضحة، تُصاغ كحكمة تكشف ما لا يظهر من القراءة الأولى. "
    "لا تلخص ولا تسرد. جملة أو جملتان فقط تنتهيان بنقطة."
)


class InsightEngine:
    """Talks to a local Ollama server for generation. Thread-safe via internal lock."""

    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._load_error: Optional[str] = None
        self._ready = False

    def load(self) -> None:
        if self._ready or self._load_error:
            return
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            self._load_error = f"Ollama not reachable at {self.base_url}: {e}"
            return
        tags = {m.get("name", "") for m in body.get("models", [])}
        # Ollama tags are "<name>:<variant>". Accept an exact match, or any
        # variant of the requested base name (e.g. "gemma4" matches "gemma4:31b-cloud").
        base = self.model.split(":", 1)[0]
        if self.model not in tags and not any(t.split(":", 1)[0] == base for t in tags):
            self._load_error = (
                f"Ollama model '{self.model}' not pulled. "
                f"Run: ollama pull {self.model}"
            )
            return
        self._ready = True

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def generate(self, chunk: str, language: str, seed: int) -> str:
        if self._load_error:
            return self._fallback(chunk, language)
        if not self._ready:
            self.load()
        if not self._ready:
            return self._fallback(chunk, language)

        system = AR_SYSTEM if language == "ar" else EN_SYSTEM
        user = (
            f"النص:\n{chunk}\n\nاكتب البصيرة العميقة الآن:" if language == "ar"
            else f"Excerpt:\n{chunk}\n\nWrite the one deep insight now:"
        )
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "num_ctx": MODEL_CTX,
                "num_thread": MODEL_THREADS,
                "num_predict": MAX_TOKENS,
                "temperature": 0.85,
                "top_p": 0.92,
                "top_k": 50,
                "repeat_penalty": 1.15,
                "seed": seed,
            },
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._lock:
            try:
                with urllib.request.urlopen(req, timeout=GEN_TIMEOUT) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return self._fallback(chunk, language, error=str(e))
        text = (body.get("message") or {}).get("content", "").strip()
        return self._clean(text, language) or self._fallback(chunk, language)

    @staticmethod
    def _clean(text: str, language: str) -> str:
        text = text.strip().strip('"').strip("“”").strip()
        text = re.sub(r"^\s*(Insight|Wisdom|البصيرة|الحكمة)\s*[:：-]\s*", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 280:
            cut = text[:280]
            last = max(cut.rfind("."), cut.rfind("۔"), cut.rfind("。"))
            text = cut[: last + 1] if last > 80 else cut + "…"
        return text

    @staticmethod
    def _fallback(chunk: str, language: str, error: str = "") -> str:
        sentences = re.split(r"(?<=[.!?۔])\s+", chunk.strip())
        sentences = [s for s in sentences if 40 < len(s) < 220]
        if sentences:
            return random.choice(sentences)
        if language == "ar":
            return "بين السطور يسكن ما لا تقوله الكلمات."
        return "Between the lines lives what words do not say."


# ---------------------------------------------------------------------------
# PDF watcher
# ---------------------------------------------------------------------------


class PDFWatcher(threading.Thread):
    """Polls the input directory and fires a callback when the active PDF changes."""

    def __init__(self, directory: Path, on_change):
        super().__init__(daemon=True)
        self.directory = directory
        self.on_change = on_change
        self._stop = threading.Event()
        self._last_fp: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                pdf = self._latest_pdf()
                fp = _file_fingerprint(pdf) if pdf else None
                if fp != self._last_fp:
                    self._last_fp = fp
                    self.on_change(pdf)
            except Exception as e:
                print(f"[watcher] {e}", file=sys.stderr)
            self._stop.wait(1.5)

    def _latest_pdf(self) -> Optional[Path]:
        if not self.directory.exists():
            return None
        pdfs = sorted(
            (p for p in self.directory.iterdir() if p.suffix.lower() == ".pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return pdfs[0] if pdfs else None


# ---------------------------------------------------------------------------
# UI primitives
# ---------------------------------------------------------------------------


def _pick_font(candidates: list[str], size: int, bold: bool = False) -> pygame.font.Font:
    for name in candidates:
        p = FONTS_DIR / name
        if p.exists():
            f = pygame.font.Font(str(p), size)
            f.set_bold(bold)
            return f
    # fall back to a system font that probably supports Arabic
    sysname = pygame.font.match_font("notosansarabic,amiri,dejavusans,arial")
    f = pygame.font.Font(sysname, size) if sysname else pygame.font.SysFont(None, size)
    f.set_bold(bold)
    return f


@dataclass
class Fonts:
    latin_body: pygame.font.Font
    latin_body_small: pygame.font.Font
    arabic_body: pygame.font.Font
    arabic_body_small: pygame.font.Font
    ui: pygame.font.Font
    footer: pygame.font.Font

    @classmethod
    def build(cls) -> "Fonts":
        latin = ["Inter-SemiBold.ttf", "Inter-Medium.ttf"]
        arabic = ["Amiri-Regular.ttf", "NotoNaskhArabic-Regular.ttf"]
        return cls(
            latin_body=_pick_font(latin, 24, bold=True),
            latin_body_small=_pick_font(latin, 20, bold=True),
            arabic_body=_pick_font(arabic, 26),
            arabic_body_small=_pick_font(arabic, 22),
            ui=_pick_font(latin, 18, bold=True),
            footer=_pick_font(latin, 11),
        )


def wrap_lines(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for w in words:
        trial = f"{current} {w}".strip()
        if font.size(trial)[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            if font.size(w)[0] > max_width:
                # hard break a very long token
                buf = ""
                for ch in w:
                    if font.size(buf + ch)[0] <= max_width:
                        buf += ch
                    else:
                        lines.append(buf)
                        buf = ch
                current = buf
            else:
                current = w
    if current:
        lines.append(current)
    return lines


def vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> pygame.Surface:
    surf = pygame.Surface(size)
    h = size[1]
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        pygame.draw.line(surf, (r, g, b), (0, y), (size[0], y))
    return surf


def round_rect(surface: pygame.Surface, color, rect, radius: int) -> None:
    pygame.draw.rect(surface, color, rect, border_radius=radius)


class AnimatedBackground:
    """Two slow-moving colored blobs on top of a static gradient."""

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.base = vertical_gradient(size, BG_TOP, BG_BOTTOM)
        self.blob = self._make_blob(220, ACCENT_A, 60)
        self.blob2 = self._make_blob(260, ACCENT_B, 45)

    @staticmethod
    def _make_blob(diameter: int, color: tuple, max_alpha: int) -> pygame.Surface:
        surf = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        r = diameter // 2
        for i in range(r, 0, -2):
            alpha = int(max_alpha * (1 - i / r) ** 2.2)
            pygame.draw.circle(surf, (*color, alpha), (r, r), i)
        return surf

    def draw(self, target: pygame.Surface, t: float) -> None:
        target.blit(self.base, (0, 0))
        w, h = self.size
        x1 = int(w * 0.25 + math.sin(t * 0.35) * w * 0.18)
        y1 = int(h * 0.30 + math.cos(t * 0.28) * h * 0.18)
        x2 = int(w * 0.75 + math.cos(t * 0.22) * w * 0.20)
        y2 = int(h * 0.70 + math.sin(t * 0.31) * h * 0.16)
        target.blit(self.blob, self.blob.get_rect(center=(x1, y1)), special_flags=pygame.BLEND_PREMULTIPLIED if False else 0)
        target.blit(self.blob2, self.blob2.get_rect(center=(x2, y2)))


class Spinner:
    def __init__(self, radius: int = 14):
        self.radius = radius

    def draw(self, target: pygame.Surface, center: tuple[int, int], t: float) -> None:
        cx, cy = center
        for i in range(12):
            a = t * 5 + i * (math.pi / 6)
            px = cx + math.cos(a) * self.radius
            py = cy + math.sin(a) * self.radius
            alpha = int(60 + 180 * ((i / 12)))
            s = pygame.Surface((6, 6), pygame.SRCALPHA)
            pygame.draw.circle(s, (220, 225, 255, alpha), (3, 3), 3)
            target.blit(s, (px - 3, py - 3))


class Button:
    def __init__(self, rect: pygame.Rect):
        self.rect = rect
        self.pressed = False
        self.glow_phase = 0.0

    def handle(self, event) -> bool:
        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
            pos = self._event_pos(event)
            if self.rect.collidepoint(pos):
                self.pressed = True
        elif event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
            pos = self._event_pos(event)
            was = self.pressed
            self.pressed = False
            if was and self.rect.collidepoint(pos):
                return True
        return False

    def _event_pos(self, event) -> tuple[int, int]:
        if hasattr(event, "pos"):
            return event.pos
        if hasattr(event, "x") and hasattr(event, "y"):
            w, h = pygame.display.get_surface().get_size()
            return int(event.x * w), int(event.y * h)
        return (-1, -1)

    def draw(self, target: pygame.Surface, label: str, font: pygame.font.Font, t: float, enabled: bool) -> None:
        self.glow_phase = (self.glow_phase + 0.04) % (math.pi * 2)
        glow_alpha = int(70 + 40 * math.sin(self.glow_phase)) if enabled else 30
        glow = pygame.Surface((self.rect.w + 40, self.rect.h + 40), pygame.SRCALPHA)
        pygame.draw.rect(
            glow,
            (*BTN_GLOW, glow_alpha),
            glow.get_rect(),
            border_radius=self.rect.h // 2 + 12,
        )
        target.blit(glow, (self.rect.x - 20, self.rect.y - 20))

        grad = vertical_gradient((self.rect.w, self.rect.h), BTN_TOP, BTN_BOT)
        mask = pygame.Surface(self.rect.size, pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=self.rect.h // 2)
        grad.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        if self.pressed:
            grad.set_alpha(215)
        if not enabled:
            grad.set_alpha(120)
        target.blit(grad, self.rect.topleft)

        label_surf = font.render(label, True, (255, 255, 255))
        target.blit(label_surf, label_surf.get_rect(center=self.rect.center))


# ---------------------------------------------------------------------------
# Insight state machine
# ---------------------------------------------------------------------------


class InsightState:
    IDLE = "idle"
    GENERATING = "generating"


@dataclass
class Insight:
    text: str
    language: str


class InsightController:
    """Owns the worker thread that produces insights; the UI polls its queue."""

    def __init__(self, engine: InsightEngine):
        self.engine = engine
        self.document: Optional[dict] = None
        self.status_message = "Waiting for PDF…"
        self.error: Optional[str] = None
        self.state = InsightState.IDLE
        self.current: Optional[Insight] = None
        self._used_indices: list[int] = []
        self._job_queue: queue.Queue[str] = queue.Queue()
        self._result_queue: queue.Queue[Insight] = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- document management -------------------------------------------------
    def set_document(self, path: Optional[Path]) -> None:
        if path is None:
            self.document = None
            self.current = None
            self._used_indices.clear()
            self.state = InsightState.IDLE
            self.status_message = "Place a PDF in input/ to begin."
            return
        try:
            self.status_message = f"Reading {path.name}…"
            doc = extract_pdf(path)
        except Exception as e:
            self.error = f"Could not read PDF: {e}"
            self.document = None
            return
        if not doc["chunks"]:
            self.error = "PDF has no extractable text."
            self.document = None
            return
        self.error = None
        self.document = doc
        self._used_indices.clear()
        self.status_message = f"Loaded {path.name} ({doc['language'].upper()})"
        self.request_new()

    # -- generation ----------------------------------------------------------
    def request_new(self) -> None:
        if not self.document or self.state == InsightState.GENERATING:
            return
        self.state = InsightState.GENERATING
        self._job_queue.put("go")

    def poll(self) -> None:
        try:
            while True:
                ins = self._result_queue.get_nowait()
                self.current = ins
                self.state = InsightState.IDLE
        except queue.Empty:
            pass

    def _run(self) -> None:
        while True:
            self._job_queue.get()
            doc = self.document
            if not doc:
                continue
            idx = self._next_index(len(doc["chunks"]))
            chunk = doc["chunks"][idx]
            seed = random.randint(1, 2**31 - 1)
            text = self.engine.generate(chunk, doc["language"], seed)
            self._result_queue.put(Insight(text=text, language=doc["language"]))

    def _next_index(self, n: int) -> int:
        if n == 1:
            return 0
        pool = [i for i in range(n) if i not in self._used_indices]
        if not pool:
            self._used_indices.clear()
            pool = list(range(n))
        idx = random.choice(pool)
        self._used_indices.append(idx)
        return idx


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class InsightRenderer:
    def __init__(self, fonts: Fonts, area: pygame.Rect):
        self.fonts = fonts
        self.area = area
        self._cached_key: tuple = ()
        self._cached_surface: Optional[pygame.Surface] = None

    def _render(self, text: str, language: str) -> pygame.Surface:
        is_ar = language == "ar"
        display_text = shape_rtl(text) if is_ar else text
        body = self.fonts.arabic_body if is_ar else self.fonts.latin_body
        body_small = self.fonts.arabic_body_small if is_ar else self.fonts.latin_body_small

        max_w = self.area.w - 40
        for font in (body, body_small):
            lines = wrap_lines(display_text, font, max_w)
            line_h = font.get_linesize()
            total_h = line_h * len(lines)
            if total_h <= self.area.h - 24 and len(lines) <= 6:
                chosen_font = font
                chosen_lines = lines
                break
        else:
            chosen_font = body_small
            chosen_lines = wrap_lines(display_text, body_small, max_w)[:6]

        surf = pygame.Surface(self.area.size, pygame.SRCALPHA)
        line_h = chosen_font.get_linesize()
        total_h = line_h * len(chosen_lines)
        y = (self.area.h - total_h) // 2
        for line in chosen_lines:
            rendered = chosen_font.render(line, True, TEXT_MAIN)
            rect = rendered.get_rect()
            if is_ar:
                rect.topright = (self.area.w - 20, y)
            else:
                rect.centerx = self.area.w // 2
                rect.y = y
            surf.blit(rendered, rect)
            y += line_h
        return surf

    def get(self, text: str, language: str) -> pygame.Surface:
        key = (text, language)
        if key != self._cached_key:
            self._cached_surface = self._render(text, language)
            self._cached_key = key
        return self._cached_surface  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class App:
    def __init__(self):
        self._ensure_dirs()
        self._init_pygame()
        self.clock = pygame.time.Clock()
        self.fonts = Fonts.build()
        self.bg = AnimatedBackground((SCREEN_W, SCREEN_H))
        self.spinner = Spinner(radius=12)

        btn_w, btn_h = 240, 48
        self.button = Button(
            pygame.Rect(
                (SCREEN_W - btn_w) // 2,
                SCREEN_H - btn_h - 28,
                btn_w,
                btn_h,
            )
        )

        card_margin_x = 22
        card_top = 26
        card_bottom = self.button.rect.top - 14
        self.card_rect = pygame.Rect(
            card_margin_x,
            card_top,
            SCREEN_W - card_margin_x * 2,
            card_bottom - card_top,
        )
        self.text_area = self.card_rect.inflate(-28, -28)
        self.renderer = InsightRenderer(self.fonts, pygame.Rect(0, 0, self.text_area.w, self.text_area.h))

        self.engine = InsightEngine(MODEL_TAG, OLLAMA_URL)
        threading.Thread(target=self.engine.load, daemon=True).start()

        self.controller = InsightController(self.engine)
        self.watcher = PDFWatcher(INPUT_DIR, self._on_pdf_change)
        self.watcher.start()

        self.fade_alpha = 0
        self._displayed_insight: Optional[Insight] = None
        self._pending_insight: Optional[Insight] = None
        self._fade_state = "in"  # or "out"

        signal.signal(signal.SIGTERM, lambda *_: self._quit())
        signal.signal(signal.SIGINT, lambda *_: self._quit())
        self._running = True

    # -- setup ---------------------------------------------------------------
    @staticmethod
    def _ensure_dirs() -> None:
        for d in (INPUT_DIR, CACHE_DIR, FONTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def _init_pygame(self) -> None:
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        pygame.init()
        pygame.display.set_caption("AI PDF Brain")
        flags = pygame.FULLSCREEN | pygame.SCALED
        if os.environ.get("AI_PDF_WINDOWED") == "1":
            flags = pygame.SCALED
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), flags)
        pygame.mouse.set_visible(False)
        pygame.event.set_blocked(pygame.MOUSEMOTION)

    # -- callbacks -----------------------------------------------------------
    def _on_pdf_change(self, pdf: Optional[Path]) -> None:
        self.controller.set_document(pdf)
        self._displayed_insight = None
        self._pending_insight = None
        self.fade_alpha = 0
        self._fade_state = "in"

    def _quit(self) -> None:
        self._running = False

    # -- frame logic ---------------------------------------------------------
    def _button_label(self) -> str:
        lang = self._displayed_insight.language if self._displayed_insight else "en"
        if lang == "ar":
            return shape_rtl("بصيرة أخرى")
        return "Another Insight"

    def _status_text(self) -> Optional[str]:
        if self.engine.load_error:
            return self.engine.load_error
        if self.controller.error:
            return self.controller.error
        if not self.controller.document:
            return self.controller.status_message
        return None

    def _update_transition(self) -> None:
        self.controller.poll()
        new = self.controller.current
        if new is None:
            return
        if self._displayed_insight is None:
            # first-time load: fade in
            self._displayed_insight = new
            self._fade_state = "in"
            self.fade_alpha = 0
        elif new is not self._displayed_insight and self._pending_insight is None and new.text != self._displayed_insight.text:
            self._pending_insight = new
            self._fade_state = "out"

        if self._fade_state == "in":
            self.fade_alpha = min(255, self.fade_alpha + 18)
        elif self._fade_state == "out":
            self.fade_alpha = max(0, self.fade_alpha - 22)
            if self.fade_alpha == 0 and self._pending_insight:
                self._displayed_insight = self._pending_insight
                self._pending_insight = None
                self._fade_state = "in"

    # -- drawing -------------------------------------------------------------
    def _draw_card(self, t: float) -> None:
        card = pygame.Surface(self.card_rect.size, pygame.SRCALPHA)
        round_rect(card, CARD_FILL, card.get_rect(), radius=20)
        pygame.draw.rect(card, CARD_EDGE, card.get_rect(), width=1, border_radius=20)
        # subtle top highlight
        hl = pygame.Surface((self.card_rect.w, 2), pygame.SRCALPHA)
        hl.fill((255, 255, 255, 45))
        card.blit(hl, (0, 1))
        self.screen.blit(card, self.card_rect.topleft)

        generating = self.controller.state == InsightState.GENERATING
        status = self._status_text()

        if status:
            label = self.fonts.ui.render(status, True, TEXT_DIM)
            self.screen.blit(label, label.get_rect(center=self.card_rect.center))
        elif self._displayed_insight:
            surf = self.renderer.get(self._displayed_insight.text, self._displayed_insight.language)
            faded = surf.copy()
            faded.set_alpha(self.fade_alpha)
            self.screen.blit(faded, self.text_area.topleft)

        if generating and self._displayed_insight is None:
            self.spinner.draw(self.screen, self.card_rect.center, t)
        elif generating:
            self.spinner.draw(
                self.screen,
                (self.card_rect.right - 22, self.card_rect.top + 22),
                t,
            )

    def _draw_footer(self) -> None:
        label = self.fonts.footer.render("AI PDF Brain", True, TEXT_DIM)
        rect = label.get_rect()
        rect.midbottom = (SCREEN_W // 2, SCREEN_H - 6)
        self.screen.blit(label, rect)

    def _draw(self, t: float) -> None:
        self.bg.draw(self.screen, t)
        self._draw_card(t)
        enabled = self.controller.document is not None and self.controller.state == InsightState.IDLE
        self.button.draw(self.screen, self._button_label(), self.fonts.ui, t, enabled)
        self._draw_footer()

    # -- main loop -----------------------------------------------------------
    def run(self) -> None:
        t0 = time.monotonic()
        while self._running:
            t = time.monotonic() - t0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._quit()
                elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                    if os.environ.get("AI_PDF_WINDOWED") == "1":
                        self._quit()
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE:
                    self.controller.request_new()
                else:
                    if self.button.handle(event):
                        self.controller.request_new()

            self._update_transition()
            self._draw(t)
            pygame.display.flip()
            self.clock.tick(FPS)

        self.watcher.stop()
        pygame.quit()


def main() -> None:
    try:
        App().run()
    except Exception as e:
        print(f"[fatal] {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
