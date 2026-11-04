"""Duplicate finder engine: exact copies, same-text documents and look-alike scans.  No Tk, safe in worker threads.

    result = scan(collect_files([folder], recursive=True), DupOptions(), cache=FingerprintCache(path))
    for g in result.groups:  g.keeper, g.members      # each member has .score (0..1) and .why

How a match is scored
  Every file gets a *fingerprint* (cached on disk, so a second scan is fast):
    bytes   SHA-1 of the file (only computed for files that share a size with another file)
    text    MinHash of the words (PDF text, Word, PowerPoint, .txt) - survives re-saving / re-exporting
    image   perceptual hashes (difference + average hash, 4 rotations) of images and of scanned PDF pages
  Two fingerprints give a *score*: the estimated probability (0..1) that the files are duplicates.
    identical bytes  -> 100 %            same normalised text -> 99.5 %
    similar text     -> capped at 93 %   (two forms that differ only by a name are NOT duplicates)
    look-alike image -> up to 98.5 %     (the "strict" check compares the real strokes and lowers it
                                         when a place differs - another name in a form - or the scan is shifted)
  The score is a calibrated-by-hand heuristic, not a statistical guarantee; that is why Auto mode only acts above a
  threshold you choose (default 95 %), which similar-but-not-identical text can never reach.

Groups are *star shaped*: the file you keep (by rule: largest, oldest ...) and the files that each match IT directly.
That avoids the classic chain problem (A~B, B~C but A is not C).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageOps, ImageStat

from .imaging import FITZ_LOCK as _FITZ_LOCK
from .imaging import IMAGE_EXTS
from .textutil import arabic_fold, fold_accents, normalise

try:
    import numpy as np
except ImportError:                                         # optional: only a speed-up / the strict check
    np = None
try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

log = logging.getLogger(__name__)

FP_VERSION = 2
QUARANTINE_NAME = "_duplicates"
TEXT_EXTS = {".txt", ".md", ".csv", ".log"}
OFFICE_EXTS = {".docx", ".pptx", ".doc", ".ppt", ".rtf", ".odt"}
MIN_TEXT_CHARS = 60                  # fewer letters than this = "no text" (a scan, a cover page ...)
MAX_TEXT_CHARS = 20000
NPERM, BANDS, ROWS = 64, 16, 4       # MinHash: 64 values, 16 bands of 4 rows
AUTO_CAP_SIMILAR_TEXT = 0.93         # not-identical text can never be "certain"
INK_SIZE, INK_RADIUS = 900, 1        # strict check: render size (px) and stroke tolerance (px)
GATE_HAMMING = 16                    # image candidates: dHash differs in at most 16 of 64 bits (any rotation)
KEEP_RULES = [("largest", "Largest / best quality"), ("oldest", "Oldest"), ("newest", "Newest"),
              ("smallest", "Smallest"), ("shortest", "Shortest path")]
_SKIP_DIRS = {QUARANTINE_NAME, "$RECYCLE.BIN", "System Volume Information", "__pycache__", "node_modules"}


# --------------------------------------------------------------------------- options
@dataclass
class DupOptions:
    use_text: bool = True              # compare words of PDFs / documents
    use_images: bool = True            # compare pictures and scanned pages
    strict: bool = True                # extra check that lowers the score when only one area differs
    rotation_invariant: bool = True    # a scan turned by 90 / 180 / 270 still matches
    ocr_cfg: object = None             # an ocr.OcrConfig -> also read the text of scans (slow); None = off
    review_threshold: float = 0.70     # show matches from this score up
    auto_threshold: float = 0.95       # "confident" from this score up
    keep: str = "largest"              # KEEP_RULES
    protect: str = ""                  # files below this folder are always kept, never offered for removal
    workers: int = 4

    def signature(self) -> str:
        return f"v{FP_VERSION}t{int(self.use_text)}i{int(self.use_images)}o{int(self.ocr_cfg is not None)}"


@dataclass
class Fingerprint:
    path: str
    size: int = 0
    mtime_ns: int = 0
    kind: str = "other"                # image | pdf | text | office | other
    sha1: str = ""
    pages: int = 1
    width: int = 0
    height: int = 0
    n_chars: int = 0
    text_hash: str = ""
    minhash: list[int] = field(default_factory=list)
    page_hashes: list[list[int]] = field(default_factory=list)   # per sampled page [d0, a0, d90, a90, d180, a180, d270, a270]
    flat: bool = False                 # blank / uniform picture: hashes say nothing
    note: str = ""
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Fingerprint":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def kind_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    if ext in OFFICE_EXTS:
        return "office"
    return "other"


# --------------------------------------------------------------------------- hashing
_ROT = (None, Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_180, Image.Transpose.ROTATE_270)


def dhash(g: Image.Image) -> int:
    """64-bit 'difference hash': is each pixel brighter than its right neighbour (9x8 thumbnail)."""
    px = g.resize((9, 8), Image.Resampling.BOX).tobytes()
    bits = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def ahash(g: Image.Image) -> int:
    """64-bit 'average hash': is each pixel of an 8x8 thumbnail brighter than the mean."""
    px = g.resize((8, 8), Image.Resampling.BOX).tobytes()
    mean = sum(px) / 64
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v > mean else 0)
    return bits


def hash_page(g: Image.Image) -> tuple[list[int], bool]:
    """([d0, a0, d90, a90, d180, a180, d270, a270], is_flat) for a grey page thumbnail."""
    g = g.convert("L")
    g.thumbnail((256, 256))
    flat = ImageStat.Stat(g).stddev[0] < 4.0
    g = ImageOps.autocontrast(g, cutoff=1)
    out: list[int] = []
    for rot in _ROT:
        v = g if rot is None else g.transpose(rot)
        out += [dhash(v), ahash(v)]
    return out, flat


def _flatten_alpha(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg
    return im


def gray_thumb(path: str | Path, kind: str, page: int = 0, size: int = 256):
    """(grey thumbnail, (width, height) of the full page, number of pages) or raises."""
    if kind == "image":
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1)
            if page:
                im.seek(min(page, frames - 1))
            w, h = im.size
            if im.getexif().get(274) in (5, 6, 7, 8):          # EXIF says "turned": report the upright size
                w, h = h, w
            try:
                im.draft("L", (size * 2, size * 2))             # JPEG: decode at reduced size (much faster)
            except Exception:
                pass
            im = _flatten_alpha(ImageOps.exif_transpose(im))
            g = im.convert("L")
        g.thumbnail((size, size))
        return g, (w, h), frames
    if kind == "pdf" and fitz is not None:
        with _FITZ_LOCK, fitz.open(path) as doc:
            n = len(doc)
            pg = doc[min(page, n - 1)]
            r = pg.rect
            zoom = size / max(r.width, r.height, 1.0)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
            g = Image.frombytes("L", (pix.width, pix.height), pix.samples, "raw", "L", pix.stride)
            return g, (int(r.width), int(r.height)), n
    raise ValueError("no picture available")


# --------------------------------------------------------------------------- text signature
_P = (1 << 61) - 1
_rng = random.Random(0xD0C5)
_A = [_rng.randrange(1, _P) for _ in range(NPERM)]
_B = [_rng.randrange(0, _P) for _ in range(NPERM)]


def words_of(text: str) -> list[str]:
    return normalise(arabic_fold(fold_accents(text))).split()


def shingle_set(words: list[str]) -> set[str]:
    if len(words) >= 30:
        return {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
    joined = " ".join(words)
    return {joined[i:i + 5] for i in range(max(1, len(joined) - 4))}


def minhash(shingles: set[str]) -> list[int]:
    if not shingles:
        return []
    hs = [int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") for s in shingles]
    return [min((a * h + b) % _P for h in hs) for a, b in zip(_A, _B)]


def _set_text(fp: Fingerprint, text: str) -> None:
    words = words_of(text[:MAX_TEXT_CHARS])
    fp.n_chars = sum(len(w) for w in words)
    if fp.n_chars < MIN_TEXT_CHARS:
        return
    fp.text_hash = hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()
    fp.minhash = minhash(shingle_set(words))


# --------------------------------------------------------------------------- fingerprinting
def _sample_pages(n: int) -> list[int]:
    return sorted({0, n // 2, n - 1}) if n > 0 else [0]


def _fp_image(fp: Fingerprint, opts: DupOptions) -> None:
    g, (fp.width, fp.height), fp.pages = gray_thumb(fp.path, "image")
    hashes, fp.flat = hash_page(g)
    fp.page_hashes = [hashes]
    if opts.ocr_cfg is not None and not fp.flat:
        from . import ocr
        from .imaging import open_rgb
        _set_text(fp, ocr.safe_ocr(open_rgb(fp.path), opts.ocr_cfg))


def _fp_pdf(fp: Fingerprint, opts: DupOptions) -> None:
    if fitz is None:
        fp.note = "PyMuPDF is not installed: compared by file content only"
        return
    with _FITZ_LOCK, fitz.open(fp.path) as doc:
        if doc.needs_pass:
            fp.note = "encrypted"
            return
        n = len(doc)
        fp.pages = n
        if n:
            r = doc[0].rect
            fp.width, fp.height = int(r.width), int(r.height)
        if opts.use_text:
            chunks, total = [], 0
            for page in doc:
                t = page.get_text()
                chunks.append(t)
                total += len(t)
                if total >= MAX_TEXT_CHARS:
                    break
            _set_text(fp, "\n".join(chunks))
    scanned = fp.n_chars < MIN_TEXT_CHARS
    if scanned and opts.use_images and fp.pages:
        flats = []
        for idx in _sample_pages(fp.pages):
            g, _size, _n = gray_thumb(fp.path, "pdf", idx)
            hashes, flat = hash_page(g)
            fp.page_hashes.append(hashes)
            flats.append(flat)
        fp.flat = all(flats)
        if opts.ocr_cfg is not None and not fp.flat:
            from . import ocr
            with _FITZ_LOCK, fitz.open(fp.path) as doc:
                parts = []
                for idx in range(min(2, len(doc))):
                    pix = doc[idx].get_pixmap(dpi=150, alpha=False)
                    parts.append(ocr.safe_ocr(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), opts.ocr_cfg))
            _set_text(fp, "\n".join(parts))


def _fp_text(fp: Fingerprint, opts: DupOptions) -> None:
    if fp.kind == "text":
        _set_text(fp, Path(fp.path).read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_CHARS * 2])
        return
    from . import ocr, readers
    cfg = ocr.OcrConfig(enabled=False)
    info = readers.read_document(fp.path, cfg, readers.ReadOptions(ocr=False, rotate=False, mrz=False, deskew=False))
    if info.error and not info.text:
        fp.note = info.error
        return
    fp.pages = info.pages or 1
    _set_text(fp, info.text)


def fingerprint(path: str | Path, opts: DupOptions | None = None) -> Fingerprint:
    """Read one file.  Never raises: problems end up in `.error` and the file is then compared by bytes only."""
    opts = opts or DupOptions()
    p = Path(path)
    fp = Fingerprint(str(p))
    try:
        st = p.stat()
        fp.size, fp.mtime_ns = st.st_size, st.st_mtime_ns
    except OSError as exc:
        fp.error = f"{exc.__class__.__name__}: {exc}"
        return fp
    fp.kind = kind_of(p)
    try:
        if fp.kind == "image" and opts.use_images:
            _fp_image(fp, opts)
        elif fp.kind == "pdf":
            _fp_pdf(fp, opts)
        elif fp.kind in ("text", "office") and opts.use_text:
            _fp_text(fp, opts)
    except Exception as exc:                                  # corrupt file, odd format ...
        fp.error = f"{exc.__class__.__name__}: {exc}"
        fp.minhash, fp.page_hashes, fp.text_hash = [], [], ""
    return fp


def file_sha1(path: str | Path, cancelled: Callable[[], bool] | None = None) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                if cancelled and cancelled():
                    return ""
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


class FingerprintCache:
    """Remembers fingerprints between runs (keyed by path + size + modified time).  `path=None` keeps them in memory."""

    def __init__(self, path: Path | None, signature: str = "") -> None:
        self.path, self.signature = path, signature
        self._items: dict[str, dict] = {}
        self._dirty = False
        if path and Path(path).exists():
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                if raw.get("sig") == signature:
                    self._items = raw.get("items", {})
            except (OSError, ValueError):
                log.warning("Fingerprint cache unreadable - starting fresh")

    def __len__(self) -> int:
        return len(self._items)

    def get(self, path: str, size: int, mtime_ns: int) -> Fingerprint | None:
        d = self._items.get(path)
        if d and d.get("size") == size and d.get("mtime_ns") == mtime_ns:
            try:
                return Fingerprint.from_dict(d)
            except TypeError:
                return None
        return None

    def put(self, fp: Fingerprint) -> None:
        self._items[fp.path] = asdict(fp)
        self._dirty = True

    def save(self) -> None:
        if not (self.path and self._dirty):
            return
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps({"sig": self.signature, "items": self._items}), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
        except OSError:
            log.exception("Could not save the fingerprint cache")

    def clear(self) -> None:
        self._items.clear()
        self._dirty = True
        self.save()


# --------------------------------------------------------------------------- comparing two fingerprints
@dataclass
class Match:
    score: float                       # 0..1: how likely it is that the two files are duplicates
    why: str
    signals: dict = field(default_factory=dict)
    rot: int = 0                       # image signal: the second file is turned by rot * 90 degrees

    @property
    def band(self) -> str:
        return band_of(self.score)


def band_of(score: float) -> str:
    if score >= 0.97:
        return "Certain"
    if score >= 0.90:
        return "Very likely"
    if score >= 0.75:
        return "Likely"
    return "Possible"


def _logistic(x: float, mid: float, k: float) -> float:
    z = -k * (x - mid)
    if z > 60:
        return 0.0
    return 1.0 / (1.0 + math.exp(z))


def text_similarity(a: Fingerprint, b: Fingerprint) -> float | None:
    if not (a.minhash and b.minhash):
        return None
    if a.text_hash and a.text_hash == b.text_hash:
        return 1.0
    same = sum(1 for x, y in zip(a.minhash, b.minhash) if x == y)
    return same / min(len(a.minhash), len(b.minhash))


def image_similarity(a: Fingerprint, b: Fingerprint, rotation_invariant: bool = True) -> tuple[float | None, int]:
    """(similarity 0..1, rotation of b) - or (None, 0) when there is nothing comparable."""
    if not (a.page_hashes and b.page_hashes) or a.flat or b.flat:
        return None, 0
    same_pages = a.pages == b.pages and len(a.page_hashes) == len(b.page_hashes)
    n = len(a.page_hashes) if same_pages else 1
    best, best_r = -1.0, 0
    for r in (range(4) if rotation_invariant else (0,)):
        tot = 0.0
        for i in range(n):
            ha, hb = a.page_hashes[i], b.page_hashes[i]
            d = (ha[0] ^ hb[2 * r]).bit_count()
            m = (ha[1] ^ hb[2 * r + 1]).bit_count()
            tot += 0.6 * (1 - d / 64) + 0.4 * (1 - m / 64)
        if tot / n > best:
            best, best_r = tot / n, r
    sim = best
    if not same_pages:
        sim *= (min(a.pages, b.pages) / max(a.pages, b.pages, 1)) ** 0.5
    if a.width and a.height and b.width and b.height:
        ra, rb = a.width / a.height, b.width / b.height
        if best_r % 2:
            rb = 1 / rb
        if min(ra, rb) / max(ra, rb) < 0.92:                    # clearly a different shape
            sim *= 0.85
    return sim, best_r


def compare(a: Fingerprint, b: Fingerprint, opts: DupOptions | None = None) -> Match | None:
    """Score two files, or None when nothing about them is comparable."""
    opts = opts or DupOptions()
    if a.sha1 and a.sha1 == b.sha1:
        return Match(1.0, "Identical file", {"bytes": 1.0})
    signals: dict[str, float] = {}
    probs: list[float] = []
    notes: list[str] = []
    rot = 0
    cap = 0.99
    if opts.use_text:
        t = text_similarity(a, b)
        if t is not None:
            signals["text"] = t
            if t >= 1.0:
                probs.append(0.995)
                cap = 0.995
                notes.append("Same text")
            else:
                probs.append(min(AUTO_CAP_SIMILAR_TEXT, _logistic(t, 0.8, 22)))
                notes.append(f"Similar text ({t:.0%})")
    if opts.use_images:
        s, rot = image_similarity(a, b, opts.rotation_invariant)
        if s is not None:
            signals["image"] = s
            probs.append(min(0.985, _logistic(s, 0.86, 35)))
            notes.append(f"Looks the same ({s:.0%})" + (f", turned {rot * 90}°" if rot else ""))
    if not probs:
        return None
    score = 1.0
    for p in probs:
        score *= 1.0 - p
    return Match(min(cap, 1.0 - score), " + ".join(notes), signals, rot)


def _dilate(mask, radius: int = 2):
    """True wherever `mask` is True within `radius` pixels (square neighbourhood)."""
    h, w = mask.shape
    padded = np.pad(mask, radius)
    out = np.zeros_like(mask)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out |= padded[dy:dy + h, dx:dx + w]
    return out


def ink_mismatch(a: Fingerprint, b: Fingerprint, rot: int = 0, size: int = INK_SIZE,
                 radius: int = INK_RADIUS) -> tuple[float, float, float] | None:
    """Compare the dark strokes of two pages: (share of all ink without a partner, worst cell, median cell).

    A stroke counts as matching when the other page has ink within 1 px, so re-compression and re-sizing pass.
    A form with another name differs a lot in *one cell* but hardly at all overall; a re-scan that shifted by a few
    pixels differs a bit everywhere.  Returns None when it cannot be measured.
    """
    if np is None:
        return None
    try:
        ga, _s, _n = gray_thumb(a.path, a.kind, 0, size)
        gb, _s, _n = gray_thumb(b.path, b.kind, 0, size)
        if rot:
            gb = gb.transpose(_ROT[rot])
        gb = gb.resize(ga.size, Image.Resampling.BOX)
        ink = [np.asarray(ImageOps.autocontrast(g, cutoff=1)) < 140 for g in (ga, gb)]
        miss_a = ink[0] & ~_dilate(ink[1], radius)
        miss_b = ink[1] & ~_dilate(ink[0], radius)
        total = int(ink[0].sum() + ink[1].sum())
        if total < 200:
            return None                                         # (almost) no strokes: a photo, nothing to compare
        mean = float(miss_a.sum() + miss_b.sum()) / total
        cell = max(30, size // 10)
        h, w = ink[0].shape
        ch, cw = max(1, h // cell), max(1, w // cell)

        def cells(m):
            return m[:ch * cell, :cw * cell].reshape(ch, cell, cw, cell).sum(axis=(1, 3)).astype(np.float32)

        ink_c, miss_c = cells(ink[0]) + cells(ink[1]), cells(miss_a) + cells(miss_b)
        busy = ink_c >= 80
        ratios = miss_c[busy] / ink_c[busy] if busy.any() else np.zeros(1, dtype=np.float32)
        return mean, float(ratios.max()), float(np.median(ratios))
    except Exception:
        return None


def verify_match(a: Fingerprint, b: Fingerprint, m: Match, opts: DupOptions) -> Match:
    """The 'strict' check for look-alike pictures: lower the score when the strokes do not really line up.

    Measured on test pages (see tests/test_similarity.py):
      re-compressed / re-sized copy      mismatch 0.000 everywhere            -> no change
      another name in one field          worst cell 0.15-0.18, median 0       -> -25..-50 %  (review, never auto)
      re-scan that shifted a few pixels  0.3-0.5 of all ink                  -> -20 %       ("Likely", never auto)
      small unexplained differences                                           -> -10 %       (never auto)
    """
    if not opts.strict or "image" not in m.signals or "bytes" in m.signals or m.signals.get("text", 0) >= 1.0:
        return m
    res = ink_mismatch(a, b, m.rot)
    if res is None:
        return m
    mean, worst, median = res
    signals = {**m.signals, "ink_mean": mean, "ink_worst": worst}
    if mean < 0.005 and worst < 0.04:
        return replace(m, signals=signals)
    if mean < 0.05 and worst - median >= 0.06:
        penalty, note = 0.25 + 0.25 * min(1.0, (worst - 0.06) / 0.2), " – differs in one place"
    elif mean >= 0.05:
        penalty, note = 0.20, " – not pixel-identical"
    else:
        penalty, note = 0.10, " – small differences"
    return replace(m, score=m.score * (1 - penalty), why=m.why + note, signals=signals)


# --------------------------------------------------------------------------- candidate pairs
def _popcount(x):
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return table[x.view(np.uint8).reshape(x.shape + (8,))].sum(axis=-1, dtype=np.uint8)


def _image_candidates(prints: list[Fingerprint], idx: list[int], rotation_invariant: bool,
                      cancelled: Callable[[], bool] | None) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    n = len(idx)
    rots = range(4) if rotation_invariant else (0,)
    if np is None or n < 2:
        for x in range(n):
            if cancelled and cancelled():
                break
            ha = prints[idx[x]].page_hashes[0]
            for y in range(x + 1, n):
                hb = prints[idx[y]].page_hashes[0]
                if min((ha[0] ^ hb[2 * r]).bit_count() for r in rots) <= GATE_HAMMING:
                    out.add((idx[x], idx[y]))
        return out
    h0 = np.array([prints[i].page_hashes[0][0] for i in idx], dtype=np.uint64)
    hr = np.array([[prints[i].page_hashes[0][2 * r] for r in rots] for i in idx], dtype=np.uint64)
    block = max(1, 16_000_000 // (n * len(rots) * 8))
    for start in range(0, n, block):
        if cancelled and cancelled():
            break
        x = h0[start:start + block, None, None] ^ hr[None, :, :]
        pc = _popcount(np.ascontiguousarray(x)).min(axis=2)
        rows, cols = np.nonzero(pc <= GATE_HAMMING)
        for r, c in zip(rows.tolist(), cols.tolist()):
            i = start + r
            if c > i:
                out.add((idx[i], idx[c]))
    return out


def candidate_pairs(prints: list[Fingerprint], opts: DupOptions,
                    cancelled: Callable[[], bool] | None = None) -> set[tuple[int, int]]:
    """Index pairs worth comparing: same bytes, shared MinHash band, or close picture hashes."""
    pairs: set[tuple[int, int]] = set()

    def add_all(members: list[int], limit: int = 300, window: int = 20) -> None:
        if len(members) <= limit:
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    pairs.add((min(members[x], members[y]), max(members[x], members[y])))
        else:                                       # a huge family of look-alikes: neighbours only (bounded work)
            ordered = sorted(members, key=lambda i: (prints[i].n_chars, prints[i].path))
            for x in range(len(ordered)):
                for y in range(x + 1, min(len(ordered), x + 1 + window)):
                    pairs.add((min(ordered[x], ordered[y]), max(ordered[x], ordered[y])))

    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, fp in enumerate(prints):
        if fp.sha1:
            by_sha[fp.sha1].append(i)
    for members in by_sha.values():
        if len(members) > 1:
            add_all(members, limit=200)
    if opts.use_text:
        bands: dict[tuple, list[int]] = defaultdict(list)
        for i, fp in enumerate(prints):
            if len(fp.minhash) >= BANDS * ROWS:
                for b in range(BANDS):
                    bands[(b, tuple(fp.minhash[b * ROWS:(b + 1) * ROWS]))].append(i)
        for members in bands.values():
            if len(members) > 1:
                add_all(members)
    if opts.use_images:
        idx = [i for i, fp in enumerate(prints) if fp.page_hashes and not fp.flat]
        pairs |= _image_candidates(prints, idx, opts.rotation_invariant, cancelled)
    return pairs


# --------------------------------------------------------------------------- groups
@dataclass
class Member:
    path: str
    score: float = 1.0
    why: str = ""
    size: int = 0
    mtime: float = 0.0
    protected: bool = False

    @property
    def band(self) -> str:
        return band_of(self.score)


@dataclass
class Group:
    keeper: Member
    members: list[Member]

    @property
    def reclaimable(self) -> int:
        return sum(m.size for m in self.members if not m.protected)


@dataclass
class ScanResult:
    groups: list[Group] = field(default_factory=list)
    files: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0
    cancelled: bool = False
    prints: dict[str, Fingerprint] = field(default_factory=dict)
    opts: DupOptions = field(default_factory=DupOptions)

    def make_keeper(self, group: Group, path: str) -> None:
        """Keep `path` instead; every other file of the group is re-scored against it."""
        everyone = [group.keeper] + group.members
        new = next((m for m in everyone if m.path == path), None)
        if new is None or new is group.keeper:
            return
        members = []
        for m in everyone:
            if m is new:
                continue
            match = compare(self.prints[new.path], self.prints[m.path], self.opts)
            m2 = replace(m, score=match.score if match else 0.0, why=match.why if match else "(not alike)")
            members.append(m2)
        members.sort(key=lambda m: -m.score)
        group.keeper = replace(new, score=1.0, why="Kept")
        group.members = members


def is_under(path: str, folder: str) -> bool:
    if not folder:
        return False
    base = os.path.normcase(os.path.abspath(folder)).rstrip("\\/")
    return os.path.normcase(os.path.abspath(path)).startswith(base + os.sep)


def _keep_key(fp: Fingerprint, rule: str, protected: bool) -> tuple:
    quality = fp.width * fp.height if fp.kind == "image" else 0
    base = {"largest": (-quality, -fp.size, fp.mtime_ns), "oldest": (fp.mtime_ns, -fp.size),
            "newest": (-fp.mtime_ns, -fp.size), "smallest": (fp.size, fp.mtime_ns),
            "shortest": (len(fp.path), fp.mtime_ns)}.get(rule, (-fp.size, fp.mtime_ns))
    return (0 if protected else 1, *base, len(fp.path), fp.path)


def _member(fp: Fingerprint, score: float, why: str, protected: bool) -> Member:
    return Member(fp.path, score, why, fp.size, fp.mtime_ns / 1e9, protected)


def build_groups(prints: list[Fingerprint], pairs: list[tuple[int, int, Match]], opts: DupOptions,
                 cancelled: Callable[[], bool] | None = None) -> list[Group]:
    """Star-shaped groups: best file first, then every unassigned file that matches *it* well enough."""
    protected = [is_under(fp.path, opts.protect) for fp in prints]
    adj: dict[int, list[tuple[int, Match]]] = defaultdict(list)
    for i, j, m in pairs:
        adj[i].append((j, m))
        adj[j].append((i, m))
    order = sorted(range(len(prints)), key=lambda i: _keep_key(prints[i], opts.keep, protected[i]))
    assigned: set[int] = set()
    groups: list[Group] = []
    for i in order:
        if cancelled and cancelled():
            break
        if i in assigned or i not in adj:
            continue
        members: list[Member] = []
        taken: list[int] = []
        for j, m in sorted((t for t in adj[i] if t[0] not in assigned), key=lambda t: -t[1].score):
            m = verify_match(prints[i], prints[j], m, opts)
            if m.score >= opts.review_threshold:
                members.append(_member(prints[j], m.score, m.why, protected[j]))
                taken.append(j)
        if members:
            assigned.add(i)
            assigned.update(taken)
            groups.append(Group(_member(prints[i], 1.0, "Kept", protected[i]), members))
    groups.sort(key=lambda g: (-g.reclaimable, g.keeper.path))
    return groups


# --------------------------------------------------------------------------- scanning
def collect_files(sources: Iterable[str | Path], recursive: bool = True, exts: set[str] | None = None,
                  min_size: int = 1) -> list[Path]:
    """Every file in the given folders / files (de-duplicated), skipping the quarantine folder and junk."""
    seen: set[str] = set()
    out: list[Path] = []

    def take(p: Path) -> None:
        if p.name.startswith("~$") or p.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
            return
        if exts and p.suffix.lower() not in exts:
            return
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            return
        try:
            if p.stat().st_size < min_size:
                return
        except OSError:
            return
        seen.add(key)
        out.append(p)

    for src in sources:
        p = Path(src)
        if p.is_file():
            take(p)
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
                for name in sorted(files):
                    take(Path(root) / name)
                if not recursive:
                    break
    return out


def scan(files: list[Path], opts: DupOptions | None = None, cache: FingerprintCache | None = None,
         cancelled: Callable[[], bool] | None = None,
         progress: Callable[[int, int, str], None] | None = None) -> ScanResult:
    """Find duplicate groups among `files`."""
    opts = opts or DupOptions()
    t0 = time.monotonic()
    cancelled = cancelled or (lambda: False)
    progress = progress or (lambda d, t, m: None)
    res = ScanResult(files=len(files), opts=opts)
    prints: list[Fingerprint] = [Fingerprint(str(f)) for f in files]

    # 1) fingerprints (cache first, the rest in a few threads)
    todo: list[int] = []
    for i, f in enumerate(files):
        try:
            st = f.stat()
        except OSError as exc:
            prints[i].error = str(exc)
            continue
        hit = cache.get(str(f), st.st_size, st.st_mtime_ns) if cache else None
        if hit:
            prints[i] = hit
        else:
            todo.append(i)
    done = len(files) - len(todo)
    progress(done, len(files), f"Reading {len(todo)} new files" if todo else "Everything was cached")
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, opts.workers), thread_name_prefix="fp") as pool:
            futures = {pool.submit(fingerprint, files[i], opts): i for i in todo}
            for fut in as_completed(futures):
                i = futures[fut]
                if cancelled():
                    for other in futures:
                        other.cancel()
                    break
                try:
                    prints[i] = fut.result()
                except Exception as exc:                        # fingerprint() should not raise, but never lose the run
                    prints[i].error = str(exc)
                done += 1
                if cache:
                    cache.put(prints[i])
                progress(done, len(files), Path(prints[i].path).name)
    if cancelled():
        res.cancelled = True
        res.seconds = time.monotonic() - t0
        return res

    # 2) exact copies: hash only the files that share a size with another file
    by_size: dict[int, list[int]] = defaultdict(list)
    for i, fp in enumerate(prints):
        if fp.size > 0:
            by_size[fp.size].append(i)
    need = [i for group in by_size.values() if len(group) > 1 for i in group if not prints[i].sha1]
    for n, i in enumerate(need, 1):
        if cancelled():
            res.cancelled = True
            return res
        progress(n, len(need), f"Comparing bytes: {Path(prints[i].path).name}")
        prints[i].sha1 = file_sha1(prints[i].path, cancelled)
        if cache:
            cache.put(prints[i])
    if cache:
        cache.save()

    # 3) candidate pairs -> scores
    progress(0, 1, "Looking for look-alikes")
    scored: list[tuple[int, int, Match]] = []
    for i, j in candidate_pairs(prints, opts, cancelled):
        m = compare(prints[i], prints[j], opts)
        if m and m.score >= opts.review_threshold:
            scored.append((i, j, m))
    if cancelled():
        res.cancelled = True
        return res

    # 4) groups (this is also where the strict picture check runs)
    progress(0, 1, f"Grouping {len(scored)} matches")
    res.groups = build_groups(prints, scored, opts, cancelled)
    res.prints = {fp.path: fp for fp in prints}
    res.problems = [(fp.path, fp.error) for fp in prints if fp.error]
    res.seconds = time.monotonic() - t0
    res.cancelled = cancelled()
    return res
