"""OCR (Tesseract) helpers shared by every tool: reading, orientation, skew, passport MRZ."""
from __future__ import annotations

import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from . import aimode, llm

log = logging.getLogger(__name__)

WINDOWS_TESSERACT = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                     str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe")]
MAC_TESSERACT = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]


class OcrUnavailable(Exception):
    """Tesseract (or pytesseract / a language pack) is missing or failing."""


@dataclass
class OcrConfig:
    enabled: bool = True
    cmd: str = ""
    languages: str = "eng"
    dpi: int = 200
    min_chars: int = 40            # OCR a page if it has fewer text characters than this
    max_pages: int = 8             # OCR at most this many pages per file (0 = no limit)
    autorotate: bool = True        # detect sideways / upside-down pages
    deskew: bool = True            # straighten slightly tilted scans
    mrz: bool = True               # second pass for passport MRZ lines
    libreoffice_cmd: str = ""
    # --- AI (local Ollama) - see core/aimode.py; "rules" = Tesseract only, exactly as before AI existed
    ai_mode: str = "rules"         # rules | ai | rules_ai | ai_rules
    ai_min_chars: int = 15         # Tesseract text shorter than this counts as "found nothing" (rules_ai mode)
    llm: "llm.LlmConfig | None" = None

    @classmethod
    def from_settings(cls, s) -> "OcrConfig":
        return cls(s.get_bool("ocr.enabled", True), s.get("ocr.tesseract_cmd", ""), s.get("ocr.languages", "eng"),
                   s.get_int("ocr.dpi", 200), s.get_int("ocr.min_chars", 40), s.get_int("ocr.max_pages", 8),
                   s.get_bool("ocr.autorotate", True), s.get_bool("ocr.deskew", True), s.get_bool("ocr.mrz", True),
                   s.get("ocr.libreoffice_cmd", ""),
                   ai_mode=aimode.mode_for(s, "ocr"), ai_min_chars=max(0, s.get_int("ai.ocr_min_chars", 15)),
                   llm=llm.LlmConfig.from_settings(s))

    @property
    def ai_active(self) -> bool:
        return self.llm is not None and self.llm.enabled and aimode.uses_ai(self.ai_mode)


def find_tesseract(cmd: str = "") -> str:
    """Configured path, PATH lookup or a well-known install location ('' when not found)."""
    if cmd and Path(cmd).is_file():
        return cmd
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (WINDOWS_TESSERACT if sys.platform.startswith("win") else MAC_TESSERACT):
        if Path(candidate).is_file():
            return candidate
    return ""


def find_libreoffice(cmd: str = "") -> str:
    if cmd and Path(cmd).is_file():
        return cmd
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform.startswith("win"):
        for base in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
            if Path(base).is_file():
                return base
    elif sys.platform == "darwin" and Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        return "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return ""


def _engine(cfg: OcrConfig):
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailable("pytesseract is not installed (pip install pytesseract)") from exc
    pytesseract.pytesseract.tesseract_cmd = find_tesseract(cfg.cmd) or "tesseract"
    return pytesseract


def check_tesseract(cfg: OcrConfig) -> str:
    """Return the Tesseract version string or raise OcrUnavailable."""
    engine = _engine(cfg)
    try:
        return str(engine.get_tesseract_version())
    except Exception as exc:
        raise OcrUnavailable("Tesseract executable not found. Install it and/or set its path in "
                             "Settings > OCR.") from exc


def installed_languages(cfg: OcrConfig) -> list[str]:
    try:
        return sorted(_engine(cfg).get_languages(config=""))
    except Exception:
        return []


def validate(cfg: OcrConfig) -> list[tuple[str, str]]:
    """[(level, message)] describing OCR set-up problems (level: fail | warn)."""
    out: list[tuple[str, str]] = []
    if not cfg.enabled:
        return out
    if cfg.ai_active and cfg.ai_mode == aimode.AI:      # AI only: Tesseract is not needed for page text
        return out
    try:
        check_tesseract(cfg)
    except OcrUnavailable as exc:
        return [("warn", f"{exc} Scanned files cannot be read until it is installed "
                         "(text PDFs and Word files still work).")]
    have = set(installed_languages(cfg))
    for lang in [x for x in cfg.languages.split("+") if x]:
        if have and lang not in have:
            out.append(("fail", f"OCR language '{lang}' is not installed in Tesseract "
                                f"(installed: {', '.join(sorted(have))})."))
    return out


def preprocess(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("L")
    if min(image.size) < 1000:                         # small scans OCR much better when upscaled
        image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    elif max(image.size) > 3200:
        image = image.copy()
        image.thumbnail((3200, 3200))
    return ImageOps.autocontrast(image)


def _tesseract_image(image: Image.Image, cfg: OcrConfig, config: str = "") -> str:
    engine = _engine(cfg)
    try:
        return engine.image_to_string(preprocess(image), lang=cfg.languages or "eng", config=config)
    except engine.TesseractNotFoundError as exc:
        raise OcrUnavailable("Tesseract executable not found (see Settings > OCR).") from exc
    except engine.TesseractError as exc:
        raise OcrUnavailable(f"Tesseract failed: {exc}") from exc


def ai_ocr_image(image: Image.Image, cfg: OcrConfig) -> str:
    """Read an image with the local vision model (raises llm.LlmError)."""
    if cfg.llm is None:
        raise llm.LlmUnavailable("AI is not configured.")
    return llm.ocr_image(cfg.llm, ImageOps.exif_transpose(image), cfg.languages or "")


def ocr_image(image: Image.Image, cfg: OcrConfig, config: str = "") -> str:
    """Text of an image.  Which engine reads it depends on `cfg.ai_mode` (Tesseract / AI / one then the other).

    A non-empty `config` is a special Tesseract pass (e.g. the MRZ alphabet) and always uses Tesseract.
    """
    if config or not cfg.ai_active:
        return _tesseract_image(image, cfg, config)
    out = aimode.run(cfg.ai_mode, lambda: _tesseract_image(image, cfg), lambda: ai_ocr_image(image, cfg),
                     empty="", ok=lambda t: len((t or "").strip()) >= cfg.ai_min_chars)
    if out.error:
        log.warning("OCR: %s", out.error)
    if (out.value or "").strip() or out.source:
        return out.value or ""
    if out.exc is not None and isinstance(out.exc, OcrUnavailable):
        raise out.exc
    raise OcrUnavailable(out.error or "No OCR engine could read the image.")


def safe_ocr(image: Image.Image, cfg: OcrConfig) -> str:
    """OCR that degrades gracefully where plain text may already exist."""
    if not cfg.enabled:
        return ""
    try:
        return ocr_image(image, cfg)
    except OcrUnavailable as exc:
        log.warning("OCR skipped: %s", exc)
        return ""


def ocr_mrz(image: Image.Image, cfg: OcrConfig) -> str:
    """Second OCR pass with a restricted alphabet (plain OCR turns the '<' filler into letters)."""
    try:
        engine = _engine(cfg)
        im = image.copy()
        im.thumbnail((2400, 2400))
        txt = engine.image_to_string(
            ImageOps.autocontrast(im.convert("L")), lang="eng",
            config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")
        out = []
        for line in (re.sub(r"\s+", "", x) for x in txt.splitlines()):
            if len(line) >= 30:
                out.append(line.ljust(44, "<")[:44] if line.startswith("P<") and 40 <= len(line) <= 47 else line)
        return "\n".join(out)
    except Exception:
        return ""


def detect_rotation(image: Image.Image, cfg: OcrConfig) -> int:
    """Clockwise degrees (90/180/270) that make a text page upright, else 0.

    Decided by reading: Tesseract's own orientation guess is unreliable on pages with little text, so we
    count the words it reads with high confidence in each orientation.  Photos without text score ~0
    everywhere and are left alone.
    """
    try:
        engine = _engine(cfg)
        small = image.copy()
        small.thumbnail((1400, 1400))
        res: dict[int, int] = {}

        def count(r: int) -> int:
            if r not in res:
                im = small if r == 0 else small.rotate(-r, expand=True, fillcolor=(255, 255, 255))
                d = engine.image_to_data(ImageOps.autocontrast(im.convert("L")), lang=cfg.languages or "eng",
                                         output_type=engine.Output.DICT)
                res[r] = sum(1 for t, c in zip(d["text"], d["conf"]) if len(t.strip()) >= 3 and float(c) >= 60)
            return res[r]

        if count(0) >= 40 and 0.7 * count(0) <= count(90) <= count(0) / 0.7:   # both readable alike = upright
            return 0
        for r in (90, 180, 270):
            count(r)
        mx = max(res.values())
        if mx < 8:
            return 0
        ready = {r for r, v in res.items() if v >= 0.7 * mx}
        if len(ready) == 1:
            return next(iter(ready))
        if len(ready) == 2:
            for x in ready:
                if (x + 90) % 360 in ready:
                    return x
        return 0
    except Exception:
        return 0


def detect_skew(image: Image.Image) -> float:
    """Small tilt in degrees (PIL rotate() angle that straightens the text), 0 if none/unsure. Needs numpy."""
    try:
        import numpy as np
    except ImportError:
        return 0.0
    try:
        g = image.convert("L")
        g.thumbnail((700, 700))
        a = np.asarray(g, dtype=np.float32)
        bw = Image.fromarray(((a < min(165.0, a.mean() - 20)) * 255).astype("uint8"))

        def score_at(ang: float) -> float:
            return float(np.var(np.asarray(bw.rotate(ang, resample=Image.BILINEAR)).sum(axis=1)))

        base = score_at(0)
        if base <= 0:
            return 0.0
        best = max(((score_at(x), x) for x in np.arange(-8, 8.01, 0.5)))[1]
        best = max(((score_at(x), x) for x in np.arange(best - 0.5, best + 0.51, 0.1)))[1]
        return round(float(best), 1) if abs(best) >= 0.5 and score_at(best) > base * 1.05 else 0.0
    except Exception:
        return 0.0


def make_searchable_pdf(image: Image.Image, cfg: OcrConfig) -> bytes:
    """One-page PDF with an invisible text layer (for 'make PDF searchable')."""
    engine = _engine(cfg)
    try:
        return engine.image_to_pdf_or_hocr(preprocess(image).convert("RGB"), lang=cfg.languages or "eng",
                                           extension="pdf")
    except Exception as exc:
        raise OcrUnavailable(f"Tesseract failed: {exc}") from exc
