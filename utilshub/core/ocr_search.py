"""Smart cached OCR store and multi-strategy similarity search engine.

No Tk / GUI imports: safe to call from worker threads.  Reuses `core.ocr`, `core.readers`,
`core.textutil`, and `core.fuzzy`.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .fuzzy import lev_ratio
from .ocr import OcrConfig
from .readers import DocInfo, ReadOptions, SCAN_IMAGE_EXTS, file_hash, read_document, supported_extensions
from .settings import data_dir
from .textutil import normalise_name

try:  # optional fast fuzzy scorer
    from rapidfuzz import fuzz as _rf_fuzz
except ImportError:
    _rf_fuzz = None

STORE_SCHEMA_VERSION = 1
_SKIP_DIRS = {"_duplicates", "$RECYCLE.BIN", "System Volume Information", "__pycache__", "node_modules", ".git"}
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}

FILE_TYPE_GROUPS: dict[str, set[str] | None] = {
    "all": None,
    "pdf": {".pdf"},
    "images": set(SCAN_IMAGE_EXTS),
    "office": {".docx", ".pptx", ".doc", ".ppt", ".rtf", ".odt", ".odp"},
    "text": {".txt", ".md", ".csv", ".log"},
}

SEARCH_MODES: list[tuple[str, str]] = [
    ("smart", "Smart (Auto)"),
    ("fuzzy", "Fuzzy phrase & typos"),
    ("semantic", "Document similarity"),
    ("exact", "Exact / Regex"),
]

CHANGE_POLICIES: list[tuple[str, str]] = [
    ("ask", "Ask before re-OCRing changed files"),
    ("auto", "Automatically re-OCR changed files"),
    ("keep", "Keep cached OCR text (manual re-OCR only)"),
]


# --------------------------------------------------------------------------- text & path helpers
def norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def ocr_config_signature(cfg: OcrConfig) -> str:
    """Deterministic fingerprint of OCR settings that affect extracted text."""
    ai_tag = cfg.ai_mode if cfg.ai_active else "rules"
    return (
        f"v{STORE_SCHEMA_VERSION}|lang={cfg.languages}|dpi={cfg.dpi}|"
        f"en={int(cfg.enabled)}|rot={int(cfg.autorotate)}|deskew={int(cfg.deskew)}|"
        f"maxp={cfg.max_pages}|minc={cfg.min_chars}|ai={ai_tag}"
    )


def normalize_for_search(text: str, ocr_tolerant: bool = True) -> str:
    """Normalise text for search (optionally repairs hyphenated line breaks first)."""
    if not text:
        return ""
    if ocr_tolerant:
        text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return normalise_name(text)


def ocr_confusion_fold(norm_text: str) -> str:
    """Fold frequent OCR character confusions (used only as a discounted secondary signal)."""
    if not norm_text:
        return ""
    return (
        norm_text.replace("rn", "m")
        .replace("vv", "w")
        .replace("0", "o")
        .replace("1", "l")
        .replace("5", "s")
    )


def band_of(score: float) -> str:
    if score >= 0.97:
        return "Exact"
    if score >= 0.85:
        return "High"
    if score >= 0.70:
        return "Medium"
    return "Low"


def _token_set_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if _rf_fuzz is not None:
        return float(_rf_fuzz.token_set_ratio(a, b)) / 100.0
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return (2.0 * inter) / (len(sa) + len(sb))


# --------------------------------------------------------------------------- records
@dataclass
class CachedDoc:
    path: str
    norm_path: str
    size: int = 0
    mtime_ns: int = 0
    sha1: str = ""
    ocr_sig: str = ""
    kind: str = "other"
    pages: int = 1
    used_ocr: bool = False
    text: str = ""
    page_texts: list[str] = field(default_factory=list)
    norm_text: str = ""
    word_count: int = 0
    char_count: int = 0
    error: str = ""
    updated_at: float = 0.0
    origin: str = "cached"          # cached | reused | fresh | pasted

    @property
    def is_virtual(self) -> bool:
        return self.origin == "pasted"

    @property
    def name(self) -> str:
        return self.path if self.is_virtual else Path(self.path).name

    @property
    def folder(self) -> str:
        return "" if self.is_virtual else str(Path(self.path).parent)

    @property
    def source_label(self) -> str:
        if self.origin == "pasted":
            return "Pasted text"
        base = "OCR" if self.used_ocr else "Text layer"
        if self.origin in ("cached", "reused", "fresh"):
            return f"{base} ({self.origin})"
        return base


def make_pasted_doc(title: str, text: str, key: str | None = None) -> CachedDoc:
    """Create an in-memory virtual CachedDoc for pasted text snippets."""
    clean_title = (title or "Pasted text").strip() or "Pasted text"
    vkey = key or f"pasted://{clean_title}"
    nt = normalize_for_search(text, ocr_tolerant=True)
    return CachedDoc(
        path=clean_title,
        norm_path=vkey,
        kind="text",
        pages=1,
        used_ocr=False,
        text=text,
        page_texts=[text],
        norm_text=nt,
        word_count=len(nt.split()),
        char_count=len(text.strip()),
        updated_at=time.time(),
        origin="pasted",
    )


@dataclass
class ChangedFile:
    path: str
    norm_path: str
    reason: str                     # modified | ocr_settings
    old_size: int
    new_size: int
    old_mtime: float
    new_mtime: float
    old_sha1: str
    new_sha1: str
    cached_doc: CachedDoc


@dataclass
class FilePlan:
    cached: list[CachedDoc] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    changed: list[ChangedFile] = field(default_factory=list)
    reused_count: int = 0
    missing: list[str] = field(default_factory=list)


@dataclass
class SearchOptions:
    mode: str = "smart"             # smart | fuzzy | semantic | exact
    threshold: float = 0.60
    ocr_tolerant: bool = True
    ignore_case: bool = True
    match_all_lines: bool = False
    top_k: int = 200
    file_type: str = "all"
    exclude_terms: list[str] = field(default_factory=list)
    context_chars: int = 140


@dataclass
class SearchHit:
    path: str
    name: str
    folder: str
    score: float
    band: str
    query: str
    page: int
    line: int
    matched_text: str
    snippet: str
    hit_count: int
    matching_pages: list[int]
    source_kind: str
    why: str
    highlight_terms: list[str] = field(default_factory=list)
    is_virtual: bool = False


# --------------------------------------------------------------------------- persistent SQLite OCR store
class OcrTextStore:
    """Persistent thread-safe SQLite store for extracted/OCR document text."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._is_memory = str(db_path) == ":memory:"
        self.db_path = Path(db_path) if (db_path is not None and not self._is_memory) else (
            Path(":memory:") if self._is_memory else (data_dir() / "ocr_cache.sqlite")
        )
        self._lock = threading.RLock()
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn_target = ":memory:" if self._is_memory else str(self.db_path)
        self._conn = sqlite3.connect(conn_target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            if not self._is_memory:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL;")
                except sqlite3.DatabaseError:
                    pass
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ocr_docs (
                    norm_path TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha1 TEXT NOT NULL,
                    ocr_sig TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    pages INTEGER NOT NULL,
                    used_ocr INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    page_texts_json TEXT NOT NULL,
                    norm_text TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_docs_sha1 ON ocr_docs(sha1)")
            self._conn.commit()

    @staticmethod
    def _row_to_doc(row: sqlite3.Row, origin: str = "cached") -> CachedDoc:
        try:
            page_texts = json.loads(row["page_texts_json"])
            if not isinstance(page_texts, list):
                page_texts = [row["text"]]
        except (ValueError, TypeError):
            page_texts = [row["text"]] if row["text"] else []
        return CachedDoc(
            path=row["path"],
            norm_path=row["norm_path"],
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            sha1=row["sha1"],
            ocr_sig=row["ocr_sig"],
            kind=row["kind"],
            pages=int(row["pages"]),
            used_ocr=bool(row["used_ocr"]),
            text=row["text"],
            page_texts=[str(p) for p in page_texts],
            norm_text=row["norm_text"],
            word_count=int(row["word_count"]),
            char_count=int(row["char_count"]),
            error=row["error"],
            updated_at=float(row["updated_at"]),
            origin=origin,
        )

    def get_by_path(self, path: str | Path) -> CachedDoc | None:
        np = norm_path(path)
        with self._lock:
            row = self._conn.execute("SELECT * FROM ocr_docs WHERE norm_path = ?", (np,)).fetchone()
            return self._row_to_doc(row) if row is not None else None

    def get_by_sha1(self, sha1: str, ocr_sig: str | None = None) -> CachedDoc | None:
        if not sha1:
            return None
        with self._lock:
            if ocr_sig:
                row = self._conn.execute(
                    "SELECT * FROM ocr_docs WHERE sha1 = ? AND error = '' AND ocr_sig = ? ORDER BY updated_at DESC LIMIT 1",
                    (sha1, ocr_sig),
                ).fetchone()
                if row is not None:
                    return self._row_to_doc(row, origin="reused")
            row = self._conn.execute(
                "SELECT * FROM ocr_docs WHERE sha1 = ? AND error = '' ORDER BY updated_at DESC LIMIT 1",
                (sha1,),
            ).fetchone()
            return self._row_to_doc(row, origin="reused") if row is not None else None

    def put(self, doc: CachedDoc) -> None:
        pages_json = json.dumps(doc.page_texts if doc.page_texts else ([doc.text] if doc.text else []), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO ocr_docs (
                    norm_path, path, size, mtime_ns, sha1, ocr_sig, kind, pages, used_ocr,
                    text, page_texts_json, norm_text, word_count, char_count, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(norm_path) DO UPDATE SET
                    path=excluded.path,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    sha1=excluded.sha1,
                    ocr_sig=excluded.ocr_sig,
                    kind=excluded.kind,
                    pages=excluded.pages,
                    used_ocr=excluded.used_ocr,
                    text=excluded.text,
                    page_texts_json=excluded.page_texts_json,
                    norm_text=excluded.norm_text,
                    word_count=excluded.word_count,
                    char_count=excluded.char_count,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    doc.norm_path,
                    doc.path,
                    int(doc.size),
                    int(doc.mtime_ns),
                    doc.sha1,
                    doc.ocr_sig,
                    doc.kind,
                    int(doc.pages),
                    1 if doc.used_ocr else 0,
                    doc.text,
                    pages_json,
                    doc.norm_text,
                    int(doc.word_count),
                    int(doc.char_count),
                    doc.error,
                    float(doc.updated_at or time.time()),
                ),
            )
            self._conn.commit()

    def touch_stat(self, norm_p: str, path: str, size: int, mtime_ns: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ocr_docs SET path = ?, size = ?, mtime_ns = ?, updated_at = ? WHERE norm_path = ?",
                (str(path), int(size), int(mtime_ns), time.time(), norm_p),
            )
            self._conn.commit()

    def delete(self, paths: Iterable[str | Path]) -> int:
        keys = []
        for p in paths:
            s = str(p)
            keys.append(s)
            keys.append(norm_path(s))
        keys = list(dict.fromkeys(keys))
        if not keys:
            return 0
        with self._lock:
            cur = self._conn.executemany("DELETE FROM ocr_docs WHERE norm_path = ?", [(k,) for k in keys])
            self._conn.commit()
            return cur.rowcount or 0

    def all_docs(self, existing_only: bool = False) -> list[CachedDoc]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM ocr_docs ORDER BY updated_at DESC").fetchall()
        docs = [self._row_to_doc(r) for r in rows]
        if existing_only:
            docs = [d for d in docs if Path(d.path).is_file()]
        return docs

    def list_summaries(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT norm_path, path, size, mtime_ns, sha1, ocr_sig, kind, pages,
                       used_ocr, word_count, char_count, error, updated_at
                FROM ocr_docs
                ORDER BY updated_at DESC
                """
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            p = r["path"]
            out.append(
                {
                    "norm_path": r["norm_path"],
                    "path": p,
                    "name": Path(p).name,
                    "folder": str(Path(p).parent),
                    "size": int(r["size"]),
                    "mtime_ns": int(r["mtime_ns"]),
                    "sha1": r["sha1"],
                    "ocr_sig": r["ocr_sig"],
                    "kind": r["kind"],
                    "pages": int(r["pages"]),
                    "used_ocr": bool(r["used_ocr"]),
                    "word_count": int(r["word_count"]),
                    "char_count": int(r["char_count"]),
                    "error": r["error"],
                    "updated_at": float(r["updated_at"]),
                    "exists": Path(p).is_file(),
                }
            )
        return out

    def prune_missing(self) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT norm_path, path FROM ocr_docs").fetchall()
            gone = [r["norm_path"] for r in rows if not Path(r["path"]).is_file()]
            if not gone:
                return 0
            self._conn.executemany("DELETE FROM ocr_docs WHERE norm_path = ?", [(k,) for k in gone])
            self._conn.commit()
            return len(gone)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM ocr_docs")
            self._conn.commit()
            if not self._is_memory:
                try:
                    self._conn.execute("VACUUM")
                except sqlite3.DatabaseError:
                    pass

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS docs, COALESCE(SUM(pages), 0) AS pages, COALESCE(SUM(char_count), 0) AS chars FROM ocr_docs"
            ).fetchone()
        db_bytes = 0
        if not self._is_memory and self.db_path.exists():
            try:
                db_bytes = self.db_path.stat().st_size
            except OSError:
                db_bytes = 0
        return {
            "docs": int(row["docs"] if row else 0),
            "pages": int(row["pages"] if row else 0),
            "chars": int(row["chars"] if row else 0),
            "db_bytes": db_bytes,
            "db_path": ":memory:" if self._is_memory else str(self.db_path),
        }

    def export_to_txt(self, dest: Path | str) -> int:
        docs = self.all_docs()
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        chunks: list[str] = []
        for d in docs:
            chunks.append(f"=== {d.path} ({d.pages} page(s), {'OCR' if d.used_ocr else 'text'}) ===")
            chunks.append(d.text.strip())
            chunks.append("")
        dest_p.write_text("\n".join(chunks), encoding="utf-8")
        return len(docs)

    def export_to_json(self, dest: Path | str) -> int:
        docs = self.all_docs()
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "path": d.path,
                "kind": d.kind,
                "pages": d.pages,
                "used_ocr": d.used_ocr,
                "sha1": d.sha1,
                "size": d.size,
                "char_count": d.char_count,
                "word_count": d.word_count,
                "updated_at": d.updated_at,
                "text": d.text,
                "page_texts": d.page_texts,
            }
            for d in docs
        ]
        dest_p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(docs)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass


# --------------------------------------------------------------------------- file discovery & smart caching
def collect_search_files(
    sources: Iterable[str | Path],
    recursive: bool = True,
    extensions: set[str] | None = None,
    file_type: str = "all",
) -> list[Path]:
    """Collect supported, deduplicated files from folders/files respecting filters."""
    allowed = set(supported_extensions())
    group = FILE_TYPE_GROUPS.get(file_type)
    if group is not None:
        allowed &= group
    if extensions:
        norm_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions if e.strip()}
        if norm_exts:
            allowed &= norm_exts

    seen: set[str] = set()
    out: list[Path] = []

    def take(p: Path) -> None:
        lname = p.name.lower()
        if p.name.startswith("~$") or lname in _JUNK_FILES:
            return
        if p.suffix.lower() not in allowed:
            return
        key = norm_path(p)
        if key in seen:
            return
        try:
            if not p.is_file():
                return
        except OSError:
            return
        seen.add(key)
        out.append(p)

    for src in sources:
        if not src:
            continue
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


def classify_files(
    paths: Iterable[str | Path],
    store: OcrTextStore,
    ocr_sig: str = "",
    check_ocr_sig: bool = False,
) -> FilePlan:
    """Compare candidate files against `store` using stat fast-path + SHA-1 verification."""
    plan = FilePlan()
    for raw in paths:
        p = Path(raw)
        try:
            st = p.stat()
            if not p.is_file():
                plan.missing.append(str(p))
                continue
        except OSError:
            plan.missing.append(str(p))
            continue

        np = norm_path(p)
        existing = store.get_by_path(p)

        # Fast path: identical size + mtime_ns and clean cached read
        if existing is not None and not existing.error and existing.size == st.st_size and existing.mtime_ns == st.st_mtime_ns:
            if check_ocr_sig and ocr_sig and existing.ocr_sig != ocr_sig:
                plan.changed.append(
                    ChangedFile(
                        path=str(p),
                        norm_path=np,
                        reason="ocr_settings",
                        old_size=existing.size,
                        new_size=st.st_size,
                        old_mtime=existing.mtime_ns / 1e9,
                        new_mtime=st.st_mtime_ns / 1e9,
                        old_sha1=existing.sha1,
                        new_sha1=existing.sha1,
                        cached_doc=existing,
                    )
                )
            else:
                existing.origin = "cached"
                plan.cached.append(existing)
            continue

        sha1 = file_hash(p)

        if existing is not None:
            if sha1 and existing.sha1 == sha1 and not existing.error:
                if check_ocr_sig and ocr_sig and existing.ocr_sig != ocr_sig:
                    plan.changed.append(
                        ChangedFile(
                            path=str(p),
                            norm_path=np,
                            reason="ocr_settings",
                            old_size=existing.size,
                            new_size=st.st_size,
                            old_mtime=existing.mtime_ns / 1e9,
                            new_mtime=st.st_mtime_ns / 1e9,
                            old_sha1=existing.sha1,
                            new_sha1=sha1,
                            cached_doc=existing,
                        )
                    )
                else:
                    store.touch_stat(np, str(p), st.st_size, st.st_mtime_ns)
                    existing.path = str(p)
                    existing.size = st.st_size
                    existing.mtime_ns = st.st_mtime_ns
                    existing.origin = "reused"
                    plan.cached.append(existing)
                    plan.reused_count += 1
            else:
                plan.changed.append(
                    ChangedFile(
                        path=str(p),
                        norm_path=np,
                        reason="modified",
                        old_size=existing.size,
                        new_size=st.st_size,
                        old_mtime=existing.mtime_ns / 1e9,
                        new_mtime=st.st_mtime_ns / 1e9,
                        old_sha1=existing.sha1,
                        new_sha1=sha1,
                        cached_doc=existing,
                    )
                )
            continue

        # New path: check if identical bytes were already OCR'd elsewhere
        dup = store.get_by_sha1(sha1, ocr_sig or None) if sha1 else None
        if dup is not None and not dup.error:
            cloned = CachedDoc(
                path=str(p),
                norm_path=np,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                sha1=sha1,
                ocr_sig=dup.ocr_sig,
                kind=dup.kind,
                pages=dup.pages,
                used_ocr=dup.used_ocr,
                text=dup.text,
                page_texts=list(dup.page_texts),
                norm_text=dup.norm_text,
                word_count=dup.word_count,
                char_count=dup.char_count,
                error="",
                updated_at=time.time(),
                origin="reused",
            )
            store.put(cloned)
            plan.cached.append(cloned)
            plan.reused_count += 1
        else:
            plan.new_files.append(str(p))

    return plan


def index_files(
    paths: Iterable[str | Path],
    store: OcrTextStore,
    cfg: OcrConfig,
    opts: ReadOptions | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    reader_fn: Callable[[Path | str, OcrConfig, ReadOptions | None], DocInfo] | None = None,
) -> list[CachedDoc]:
    """Read/OCR `paths` and store clean results in `store`."""
    items = [str(p) for p in paths]
    if not items:
        return []
    opts = opts or ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)
    ocr_sig = ocr_config_signature(cfg)
    reader = reader_fn or read_document
    cancelled = cancelled or (lambda: False)
    progress = progress or (lambda _i, _t, _m: None)
    out: list[CachedDoc] = []

    for i, raw in enumerate(items):
        if cancelled():
            break
        p = Path(raw)
        progress(i, len(items), p.name)
        doc_info = reader(p, cfg, opts)
        try:
            st = p.stat()
            size, mtime_ns = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime_ns = 0, 0
        sha1 = doc_info.hash or (file_hash(p) if p.is_file() else "")
        page_texts = list(doc_info.page_texts) if doc_info.page_texts else ([doc_info.text] if doc_info.text else [])
        text = doc_info.text or "\n".join(page_texts)
        nt = normalize_for_search(text, ocr_tolerant=True)
        cdoc = CachedDoc(
            path=str(p),
            norm_path=norm_path(p),
            size=size,
            mtime_ns=mtime_ns,
            sha1=sha1,
            ocr_sig=ocr_sig,
            kind=doc_info.kind,
            pages=max(1, int(doc_info.pages or len(page_texts) or 1)),
            used_ocr=bool(doc_info.used_ocr),
            text=text,
            page_texts=page_texts,
            norm_text=nt,
            word_count=len(nt.split()),
            char_count=len(text.strip()),
            error=doc_info.error or "",
            updated_at=time.time(),
            origin="fresh",
        )
        if not cdoc.error:
            store.put(cdoc)
        out.append(cdoc)

    progress(len(out), len(items), "Done")
    return out


# --------------------------------------------------------------------------- query parsing & corpus index
def parse_query_and_excludes(
    raw_query: str,
    extra_excludes: Iterable[str] | None = None,
    strip_minus_tokens: bool = True,
) -> tuple[list[str], list[str]]:
    """Return `(query_lines, exclude_terms)` from raw input + explicit exclude list."""
    excludes: list[str] = []
    for item in extra_excludes or ():
        for tok in re.split(r"[,;\s]+", str(item).strip()):
            tok = tok.lstrip("-").strip()
            if tok:
                excludes.append(tok)

    lines: list[str] = []
    for raw_line in (raw_query or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if strip_minus_tokens:
            kept: list[str] = []
            for tok in line.split():
                if len(tok) > 1 and tok.startswith("-") and not tok.startswith("--"):
                    term = tok[1:].strip()
                    if term:
                        excludes.append(term)
                else:
                    kept.append(tok)
            cleaned = " ".join(kept).strip()
            if cleaned:
                lines.append(cleaned)
        else:
            lines.append(line)

    # deduplicate preserving order
    seen_ex: set[str] = set()
    uniq_ex: list[str] = []
    for ex in excludes:
        k = ex.casefold()
        if k not in seen_ex:
            seen_ex.add(k)
            uniq_ex.append(ex)
    return lines, uniq_ex


def _word_ngrams(words: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter(words)
    for i in range(len(words) - 1):
        counts[f"{words[i]} {words[i + 1]}"] += 1
    return counts


def _char_4grams(norm_text: str) -> Counter[str]:
    compact = f" {norm_text} "
    if len(compact) <= 4:
        return Counter({compact: 1}) if compact.strip() else Counter()
    return Counter(compact[i : i + 4] for i in range(len(compact) - 3))


@dataclass
class _DocVec:
    doc: CachedDoc
    norm: str
    words: list[str]
    word_set: set[str]
    tfidf: dict[str, float]
    tfidf_norm: float
    char_grams: dict[str, float]
    char_norm: float


class CorpusIndex:
    """Precomputed TF-IDF + character n-gram vectors for fast similarity scoring."""

    def __init__(self, docs: Iterable[CachedDoc], ocr_tolerant: bool = True) -> None:
        self.ocr_tolerant = ocr_tolerant
        doc_list = list(docs)
        raw_terms: list[tuple[CachedDoc, str, list[str], Counter[str], Counter[str]]] = []
        df: Counter[str] = Counter()

        for d in doc_list:
            nt = d.norm_text if (ocr_tolerant and d.norm_text) else normalize_for_search(d.text, ocr_tolerant)
            words = nt.split()
            w_counts = _word_ngrams(words)
            c_counts = _char_4grams(nt)
            for term in w_counts:
                df[term] += 1
            raw_terms.append((d, nt, words, w_counts, c_counts))

        n_docs = max(1, len(raw_terms))
        self.idf: dict[str, float] = {t: math.log((n_docs + 1.0) / (c + 1.0)) + 1.0 for t, c in df.items()}
        self.vectors: dict[str, _DocVec] = {}

        for d, nt, words, w_counts, c_counts in raw_terms:
            tfidf = {t: (1.0 + math.log(cnt)) * self.idf.get(t, 1.0) for t, cnt in w_counts.items()}
            t_norm = math.sqrt(sum(v * v for v in tfidf.values()))
            cf = {k: float(v) for k, v in c_counts.items()}
            c_norm = math.sqrt(sum(v * v for v in cf.values()))
            self.vectors[d.norm_path] = _DocVec(d, nt, words, set(words), tfidf, t_norm, cf, c_norm)

    def query_vector(self, text: str) -> tuple[str, list[str], dict[str, float], float, dict[str, float], float]:
        nt = normalize_for_search(text, self.ocr_tolerant)
        words = nt.split()
        w_counts = _word_ngrams(words)
        default_idf = math.log((max(1, len(self.vectors)) + 1.0) / 1.0) + 1.0
        tfidf = {t: (1.0 + math.log(cnt)) * self.idf.get(t, default_idf) for t, cnt in w_counts.items()}
        t_norm = math.sqrt(sum(v * v for v in tfidf.values()))
        c_counts = {k: float(v) for k, v in _char_4grams(nt).items()}
        c_norm = math.sqrt(sum(v * v for v in c_counts.values()))
        return nt, words, tfidf, t_norm, c_counts, c_norm


def _cosine(a: dict[str, float], na: float, b: dict[str, float], nb: float) -> float:
    if na <= 0.0 or nb <= 0.0 or not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    return max(0.0, min(1.0, dot / (na * nb)))


# --------------------------------------------------------------------------- snippet & highlight helpers
def _make_snippet(line: str, match_text: str = "", max_chars: int = 140) -> str:
    clean = " ".join(line.split())
    if len(clean) <= max_chars:
        return clean
    if match_text:
        idx = clean.casefold().find(match_text.casefold())
        if idx >= 0:
            half = max(20, (max_chars - len(match_text)) // 2)
            start = max(0, idx - half)
            end = min(len(clean), start + max_chars)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(clean) else ""
            return prefix + clean[start:end].strip() + suffix
    return clean[: max_chars - 1].rstrip() + "…"


def format_doc_pages(doc: CachedDoc) -> tuple[str, list[tuple[int, int]]]:
    """Format pages with headers and return `(display_text, [(page_num, start_line_1based)])`."""
    pages = doc.page_texts or ([doc.text] if doc.text else [])
    if not pages:
        return "(no text could be read)", [(1, 1)]
    out_lines: list[str] = []
    starts: list[tuple[int, int]] = []
    multi = len(pages) > 1
    for idx, page_txt in enumerate(pages, 1):
        starts.append((idx, len(out_lines) + 1))
        if multi:
            out_lines.append(f"──── page {idx} ────")
        body = page_txt.strip()
        if body:
            out_lines.extend(body.splitlines())
        out_lines.append("")
    return "\n".join(out_lines).rstrip() + "\n", starts


def find_highlight_spans(display_text: str, highlight_terms: Iterable[str], ignore_case: bool = True) -> list[tuple[int, int, int]]:
    """Return sorted non-overlapping `(line_1based, col_start, col_end)` spans for Tk text tagging."""
    terms = [t.strip() for t in highlight_terms if t and len(t.strip()) >= 2]
    if not display_text or not terms:
        return []
    flags = re.IGNORECASE if ignore_case else 0
    ordered = sorted(dict.fromkeys(terms), key=len, reverse=True)
    rx = re.compile("|".join(re.escape(t) for t in ordered), flags)
    spans: list[tuple[int, int, int]] = []
    for lno, line in enumerate(display_text.splitlines(), 1):
        for m in rx.finditer(line):
            if m.start() < m.end():
                spans.append((lno, m.start(), m.end()))
    if spans:
        return spans

    # Fallback: folded-word matching when OCR / Arabic / accent spelling differs from raw text
    folded_terms = {normalize_for_search(t, ocr_tolerant=True) for t in ordered}
    folded_terms.discard("")
    for lno, line in enumerate(display_text.splitlines(), 1):
        for m in re.finditer(r"\S+", line):
            tok_norm = normalize_for_search(m.group(0), ocr_tolerant=True)
            if tok_norm and any(tok_norm == ft or tok_norm in ft.split() for ft in folded_terms):
                spans.append((lno, m.start(), m.end()))
    return spans


# --------------------------------------------------------------------------- search strategies
def _doc_matches_exclude(doc: CachedDoc, excludes: list[str], ocr_tolerant: bool) -> bool:
    if not excludes:
        return False
    hay_raw = doc.text.casefold()
    hay_norm = f" {doc.norm_text or normalize_for_search(doc.text, ocr_tolerant)} "
    for ex in excludes:
        if ex.casefold() in hay_raw:
            return True
        ex_norm = normalize_for_search(ex, ocr_tolerant)
        if ex_norm and f" {ex_norm} " in hay_norm:
            return True
    return False


def _search_exact(
    docs: list[CachedDoc],
    patterns: list[str],
    opts: SearchOptions,
    query_label: str,
) -> list[SearchHit]:
    flags = (re.I | re.M) if opts.ignore_case else re.M
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for pat in patterns:
        try:
            compiled.append((pat, re.compile(pat, flags)))
        except re.error as exc:
            raise ValueError(f"{pat}: {exc}") from exc

    hits: list[SearchHit] = []
    for doc in docs:
        pages = doc.page_texts or ([doc.text] if doc.text else [])
        per_pat_best: list[tuple[int, int, str, str, int, set[int]]] = []
        matched_terms: list[str] = []
        total_count = 0
        all_pages: set[int] = set()

        for pat, rx in compiled:
            pat_count = 0
            pat_pages: set[int] = set()
            first_match: tuple[int, int, str, str] | None = None
            for pno, ptext in enumerate(pages, 1):
                lines = ptext.splitlines()
                starts = [0]
                for ln in lines:
                    starts.append(starts[-1] + len(ln) + 1)
                for m in rx.finditer(ptext):
                    if not m.group(0):
                        continue
                    pat_count += 1
                    pat_pages.add(pno)
                    matched_terms.append(m.group(0))
                    if first_match is None:
                        lno = max(0, sum(1 for s in starts[1:] if s <= m.start()))
                        line_txt = lines[lno] if lno < len(lines) else m.group(0)
                        first_match = (pno, lno + 1, m.group(0), line_txt)
            if pat_count > 0 and first_match is not None:
                total_count += pat_count
                all_pages |= pat_pages
                per_pat_best.append((*first_match, pat_count, pat_pages))

        if not per_pat_best:
            continue
        if opts.match_all_lines and len(per_pat_best) < len(compiled):
            continue

        pno, lno, mtext, line_txt, _pc, _pp = per_pat_best[0]
        score = 1.0
        why = f"Exact/regex match ({total_count} hit{'s' if total_count != 1 else ''})"
        hits.append(
            SearchHit(
                path=doc.path,
                name=doc.name,
                folder=doc.folder,
                score=score,
                band=band_of(score),
                query=query_label or ", ".join(patterns),
                page=pno,
                line=lno,
                matched_text=mtext,
                snippet=_make_snippet(line_txt, mtext, opts.context_chars),
                hit_count=total_count,
                matching_pages=sorted(all_pages),
                source_kind=doc.source_label,
                why=why,
                highlight_terms=list(dict.fromkeys(matched_terms))[:20],
                is_virtual=doc.is_virtual,
            )
        )
    return hits


def _query_to_window_score(
    q_words: list[str],
    q_fold_words: list[str],
    norm_q: str,
    fold_q: str,
    norm_win: str,
    ocr_tolerant: bool,
) -> float:
    """Asymmetric similarity from `q_words` to `norm_win` (penalises missing query words)."""
    w_words = norm_win.split()
    if not q_words or not w_words:
        return 0.0
    if norm_win == norm_q:
        return 1.0
    w_fold_words = [ocr_confusion_fold(w) for w in w_words] if ocr_tolerant else w_words

    num = den = 0.0
    for qw, qfw in zip(q_words, q_fold_words):
        best = 0.0
        for ww, wfw in zip(w_words, w_fold_words):
            if ww == qw:
                best = 1.0
                break
            r = lev_ratio(qw, ww)
            if ocr_tolerant:
                r = max(r, 0.95 * lev_ratio(qfw, wfw))
            if r > best:
                best = r
        wlen = max(1, len(qw))
        num += best * wlen
        den += wlen
    cov_score = num / den if den else 0.0
    if len(w_words) > len(q_words):
        cov_score *= (len(q_words) / len(w_words)) ** 0.25
    str_score = lev_ratio(norm_q, norm_win)
    if ocr_tolerant and fold_q:
        fold_win = ocr_confusion_fold(norm_win)
        str_score = max(str_score, 0.95 if fold_win == fold_q else 0.95 * lev_ratio(fold_q, fold_win))
    return max(cov_score, str_score)


def _fuzzy_line_match(
    line: str,
    norm_q: str,
    fold_q: str,
    q_words: list[str],
    threshold: float,
    ocr_tolerant: bool,
) -> tuple[float, str] | None:
    norm_line = normalize_for_search(line, ocr_tolerant=False)
    if not norm_line:
        return None
    if f" {norm_q} " in f" {norm_line} ":
        return 1.0, line.strip()

    q_fold_words = [ocr_confusion_fold(w) for w in q_words] if ocr_tolerant else q_words
    words = line.split()
    k = max(1, len(q_words))
    best_score = 0.0
    best_text = ""

    for size in sorted({max(1, k - 1), k, k + 1}):
        if size > len(words):
            continue
        for i in range(0, len(words) - size + 1):
            raw_win = " ".join(words[i : i + size])
            norm_win = normalize_for_search(raw_win, ocr_tolerant=False)
            if not norm_win:
                continue
            s = _query_to_window_score(q_words, q_fold_words, norm_q, fold_q, norm_win, ocr_tolerant)
            if s >= threshold and s > best_score:
                best_score = s
                best_text = raw_win

    return (best_score, best_text) if best_score >= threshold else None


def _fuzzy_page_scattered(
    page_text: str,
    q_words: list[str],
    threshold: float,
    ocr_tolerant: bool,
) -> tuple[float, int, str, str] | None:
    if len(q_words) < 2:
        return None
    lines = page_text.splitlines()
    page_tokens: list[tuple[str, str, int, str]] = []
    for lno, line in enumerate(lines, 1):
        for w in line.split():
            nw = normalize_for_search(w, ocr_tolerant=False)
            if nw:
                page_tokens.append((nw, ocr_confusion_fold(nw) if ocr_tolerant else nw, lno, line))
    if not page_tokens:
        return None

    matched_scores: list[float] = []
    matched_lines: list[tuple[int, str, str]] = []
    for qw in q_words:
        fq = ocr_confusion_fold(qw) if ocr_tolerant else qw
        best_w = 0.0
        best_info: tuple[int, str, str] | None = None
        for nw, fnw, lno, line in page_tokens:
            if nw == qw:
                best_w = 1.0
                best_info = (lno, line, qw)
                break
            r = lev_ratio(qw, nw)
            if ocr_tolerant:
                r = max(r, 0.95 * lev_ratio(fq, fnw))
            if r > best_w:
                best_w = r
                best_info = (lno, line, nw)
        if best_w >= max(0.75, threshold):
            matched_scores.append(best_w)
            if best_info is not None:
                matched_lines.append(best_info)

    if len(matched_scores) < len(q_words):
        return None
    score = min(1.0, 0.90 * (sum(matched_scores) / len(q_words)))
    if score < threshold:
        return None
    lno, line, _ = matched_lines[0]
    return score, lno, " ".join(q_words), line


def _search_fuzzy(
    docs: list[CachedDoc],
    terms: list[str],
    opts: SearchOptions,
    query_label: str,
) -> list[SearchHit]:
    prepped: list[tuple[str, str, str, list[str]]] = []
    for t in terms:
        nt = normalize_for_search(t, opts.ocr_tolerant)
        if nt:
            prepped.append((t, nt, ocr_confusion_fold(nt) if opts.ocr_tolerant else nt, nt.split()))
    if not prepped:
        return []

    hits: list[SearchHit] = []
    for doc in docs:
        pages = doc.page_texts or ([doc.text] if doc.text else [])
        per_term_best: list[tuple[float, int, int, str, str]] = []
        matched_words: list[str] = []
        all_pages: set[int] = set()
        total_hits = 0

        for raw_t, norm_q, fold_q, q_words in prepped:
            best_for_term: tuple[float, int, int, str, str] | None = None
            term_hits = 0
            for pno, page_txt in enumerate(pages, 1):
                page_matched = False
                for lno, line in enumerate(page_txt.splitlines(), 1):
                    res = _fuzzy_line_match(line, norm_q, fold_q, q_words, opts.threshold, opts.ocr_tolerant)
                    if res is not None:
                        s, mtxt = res
                        term_hits += 1
                        page_matched = True
                        all_pages.add(pno)
                        matched_words.append(mtxt)
                        if best_for_term is None or s > best_for_term[0]:
                            best_for_term = (s, pno, lno, mtxt, line)
                if not page_matched:
                    scat = _fuzzy_page_scattered(page_txt, q_words, opts.threshold, opts.ocr_tolerant)
                    if scat is not None:
                        s, lno, mtxt, line = scat
                        term_hits += 1
                        all_pages.add(pno)
                        matched_words.extend(q_words)
                        if best_for_term is None or s > best_for_term[0]:
                            best_for_term = (s, pno, lno, mtxt, line)
            if best_for_term is not None:
                total_hits += term_hits
                per_term_best.append(best_for_term)

        if not per_term_best:
            continue
        if opts.match_all_lines and len(per_term_best) < len(prepped):
            continue

        per_term_best.sort(key=lambda x: -x[0])
        best_s, pno, lno, mtxt, line_txt = per_term_best[0]
        score = (sum(x[0] for x in per_term_best) / len(per_term_best)) if opts.match_all_lines else best_s
        why = f"Fuzzy match ({best_s:.0%}, {total_hits} hit{'s' if total_hits != 1 else ''})"
        hits.append(
            SearchHit(
                path=doc.path,
                name=doc.name,
                folder=doc.folder,
                score=score,
                band=band_of(score),
                query=query_label or ", ".join(terms),
                page=pno,
                line=lno,
                matched_text=mtxt,
                snippet=_make_snippet(line_txt, mtxt, opts.context_chars),
                hit_count=total_hits,
                matching_pages=sorted(all_pages),
                source_kind=doc.source_label,
                why=why,
                highlight_terms=list(dict.fromkeys(matched_words + terms))[:25],
                is_virtual=doc.is_virtual,
            )
        )
    return hits


def _passage_score(
    q_words: list[str],
    q_norm: str,
    q_set: set[str],
    q_weights: dict[str, float],
    page_text: str,
    ocr_tolerant: bool,
) -> tuple[float, int, str, list[str]]:
    """Best similarity between query and ~100-word sliding passages on one page."""
    p_norm = normalize_for_search(page_text, ocr_tolerant)
    p_words = p_norm.split()
    if not p_words or not q_words:
        return 0.0, 1, "", []

    q_total_w = sum(q_weights.get(w, 1.0) for w in q_set) or 1.0
    win_len = max(40, min(140, len(q_words) * 2))
    step = max(25, win_len // 2)

    if len(p_words) <= win_len:
        passages = [p_words]
    else:
        passages = [p_words[i : i + win_len] for i in range(0, len(p_words), step)]

    best_pass_score = 0.0
    best_overlap_terms: list[str] = []

    for chunk in passages:
        c_set = set(chunk)
        covered = 0.0
        hits_here: list[str] = []
        for qw in q_set:
            if qw in c_set:
                covered += q_weights.get(qw, 1.0)
                hits_here.append(qw)
            elif ocr_tolerant and len(qw) >= 5:
                best_tok = max((lev_ratio(qw, cw) for cw in c_set if abs(len(cw) - len(qw)) <= 2), default=0.0)
                if best_tok >= 0.82:
                    covered += q_weights.get(qw, 1.0) * best_tok
                    hits_here.append(qw)
        cov_ratio = covered / q_total_w
        tsr = _token_set_ratio(q_norm, " ".join(chunk)) if len(q_words) <= 60 else cov_ratio
        s = 0.65 * cov_ratio + 0.35 * tsr
        if s > best_pass_score:
            best_pass_score = s
            best_overlap_terms = hits_here

    # Pick the most relevant single line on this page for snippet & line number
    best_lno = 1
    best_line = ""
    best_line_score = -1.0
    for lno, line in enumerate(page_text.splitlines(), 1):
        ln_words = set(normalize_for_search(line, ocr_tolerant).split())
        if not ln_words:
            continue
        overlap = sum(q_weights.get(w, 1.0) for w in (q_set & ln_words))
        if ocr_tolerant and overlap == 0:
            for qw in q_set:
                if len(qw) >= 5 and any(lev_ratio(qw, lw) >= 0.82 for lw in ln_words):
                    overlap += 0.8 * q_weights.get(qw, 1.0)
        if overlap > best_line_score:
            best_line_score = overlap
            best_lno = lno
            best_line = line

    if not best_line:
        first_nonempty = next(((i, l) for i, l in enumerate(page_text.splitlines(), 1) if l.strip()), (1, ""))
        best_lno, best_line = first_nonempty

    return best_pass_score, best_lno, best_line, best_overlap_terms


def _search_semantic(
    docs: list[CachedDoc],
    query_text: str,
    opts: SearchOptions,
    query_label: str,
    index: CorpusIndex | None = None,
    boost_verbatim: bool = False,
) -> list[SearchHit]:
    idx = index or CorpusIndex(docs, opts.ocr_tolerant)
    q_norm, q_words, q_tfidf, q_tnorm, q_cgrams, q_cnorm = idx.query_vector(query_text)
    if not q_words:
        return []
    q_set = set(q_words)
    q_fold = ocr_confusion_fold(q_norm) if opts.ocr_tolerant else ""
    q_weights = {w: idx.idf.get(w, 1.5) for w in q_set}

    hits: list[SearchHit] = []
    for doc in docs:
        vec = idx.vectors.get(doc.norm_path)
        if vec is None:
            continue
        if q_norm == vec.norm:
            score = 1.0
            w_tfidf = w_char = w_pass = 1.0
        else:
            w_tfidf = _cosine(q_tfidf, q_tnorm, vec.tfidf, vec.tfidf_norm)
            w_char = _cosine(q_cgrams, q_cnorm, vec.char_grams, vec.char_norm)
            pages = doc.page_texts or ([doc.text] if doc.text else [])
            best_pno = 1
            best_lno = 1
            best_line = ""
            best_terms: list[str] = []
            w_pass = 0.0
            matching_pages: list[int] = []
            for pno, ptext in enumerate(pages, 1):
                ps, lno, line, oterms = _passage_score(q_words, q_norm, q_set, q_weights, ptext, opts.ocr_tolerant)
                if ps >= max(0.35, opts.threshold * 0.7):
                    matching_pages.append(pno)
                if ps > w_pass:
                    w_pass, best_pno, best_lno, best_line, best_terms = ps, pno, lno, line, oterms

            blended = 0.45 * w_tfidf + 0.35 * w_char + 0.20 * w_pass
            passage_boost = (0.85 if len(q_words) <= 40 else 0.72) * w_pass
            score = min(1.0, max(blended, passage_boost))

            if boost_verbatim and q_norm:
                if f" {q_norm} " in f" {vec.norm} ":
                    score = max(score, 0.94)
                elif q_fold and f" {q_fold} " in f" {ocr_confusion_fold(vec.norm)} ":
                    score = max(score, 0.92)
        if q_norm == vec.norm:
            pages = doc.page_texts or ([doc.text] if doc.text else [])
            best_pno = 1
            best_lno = 1
            best_line = next((l for l in (pages[0].splitlines() if pages else []) if l.strip()), "")
            best_terms = sorted(q_set, key=lambda w: -q_weights.get(w, 1.0))[:12]
            matching_pages = list(range(1, len(pages) + 1)) or [1]

        if score < opts.threshold:
            continue

        top_terms = sorted(set(best_terms) | (q_set & vec.word_set), key=lambda w: -q_weights.get(w, 1.0))[:15]
        why = f"Text {w_tfidf:.0%} • Char {w_char:.0%} • Passage {w_pass:.0%}"
        hits.append(
            SearchHit(
                path=doc.path,
                name=doc.name,
                folder=doc.folder,
                score=score,
                band=band_of(score),
                query=query_label or (query_text.strip().splitlines()[0][:60] if query_text.strip() else ""),
                page=best_pno,
                line=best_lno,
                matched_text=", ".join(top_terms[:5]),
                snippet=_make_snippet(best_line or doc.text[:200], top_terms[0] if top_terms else "", opts.context_chars),
                hit_count=max(1, len(matching_pages)),
                matching_pages=matching_pages or [best_pno],
                source_kind=doc.source_label,
                why=why,
                highlight_terms=top_terms,
                is_virtual=doc.is_virtual,
            )
        )
    return hits


def _filter_corpus(
    docs: Iterable[CachedDoc],
    file_type: str,
    exclude_terms: list[str],
    ocr_tolerant: bool,
    exclude_norm_path: str = "",
) -> list[CachedDoc]:
    allowed = FILE_TYPE_GROUPS.get(file_type)
    out: list[CachedDoc] = []
    for d in docs:
        if exclude_norm_path and d.norm_path == exclude_norm_path:
            continue
        if not d.text.strip():
            continue
        if allowed is not None:
            if d.is_virtual or Path(d.path).suffix.lower() not in allowed:
                continue
        if _doc_matches_exclude(d, exclude_terms, ocr_tolerant):
            continue
        out.append(d)
    return out


def search_documents(
    docs: Iterable[CachedDoc],
    query: str,
    opts: SearchOptions | None = None,
    query_label: str = "",
    index: CorpusIndex | None = None,
    exclude_norm_path: str = "",
) -> list[SearchHit]:
    """Search `docs` using `opts.mode` (`smart | fuzzy | semantic | exact`)."""
    opts = opts or SearchOptions()
    strip_minus = opts.mode != "exact"
    lines, excludes = parse_query_and_excludes(query, opts.exclude_terms, strip_minus_tokens=strip_minus)
    if not lines:
        return []

    filtered = _filter_corpus(docs, opts.file_type, excludes, opts.ocr_tolerant, exclude_norm_path)
    if not filtered:
        return []

    mode = opts.mode
    if mode == "smart":
        if all(len(normalize_for_search(ln, opts.ocr_tolerant).split()) <= 6 for ln in lines):
            hits = _search_fuzzy(filtered, lines, opts, query_label)
        else:
            hits = _search_semantic(filtered, "\n".join(lines), opts, query_label, index=index, boost_verbatim=True)
    elif mode == "exact":
        hits = _search_exact(filtered, lines, opts, query_label)
    elif mode == "fuzzy":
        hits = _search_fuzzy(filtered, lines, opts, query_label)
    else:
        hits = _search_semantic(filtered, "\n".join(lines), opts, query_label, index=index, boost_verbatim=False)

    hits.sort(key=lambda h: (-h.score, -h.hit_count, h.name.lower()))
    return hits[: opts.top_k] if opts.top_k > 0 else hits


def search_by_documents(
    corpus_docs: Iterable[CachedDoc],
    query_docs: Iterable[CachedDoc],
    opts: SearchOptions | None = None,
    exclude_self: bool = True,
) -> list[SearchHit]:
    """Compare each document in `query_docs` against `corpus_docs` with one shared CorpusIndex."""
    opts = opts or SearchOptions()
    corpus_list = list(corpus_docs)
    idx = CorpusIndex(corpus_list, opts.ocr_tolerant)
    all_hits: list[SearchHit] = []
    for qdoc in query_docs:
        if not qdoc.text.strip():
            continue
        q_opts = opts
        if opts.mode == "smart" and qdoc.word_count > 6:
            q_opts = SearchOptions(
                mode="semantic",
                threshold=opts.threshold,
                ocr_tolerant=opts.ocr_tolerant,
                ignore_case=opts.ignore_case,
                match_all_lines=opts.match_all_lines,
                top_k=opts.top_k,
                file_type=opts.file_type,
                exclude_terms=list(opts.exclude_terms),
                context_chars=opts.context_chars,
            )
        hits = search_documents(
            corpus_list,
            qdoc.text,
            q_opts,
            query_label=qdoc.name,
            index=idx,
            exclude_norm_path=qdoc.norm_path if exclude_self else "",
        )
        all_hits.extend(hits)
    all_hits.sort(key=lambda h: (-h.score, h.query.lower(), h.name.lower()))
    return all_hits[: opts.top_k] if opts.top_k > 0 else all_hits
