"""Cross-platform printing engine (no Tk): printer discovery, spooler control, WYSIWYG PDF composer & Win32 GDI."""
from __future__ import annotations

import ctypes
import html
import logging
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from . import arabic
from .imaging import IMAGE_EXTS
from .office import OFFICE_EXTS, OfficeError, docx_fallback_pdf, to_pdf_cached
from .settings import data_dir, temp_dir

log = logging.getLogger(__name__)

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

TEXT_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".ini", ".cfg", ".py", ".html", ".htm", ".yaml", ".yml"}

PAPER_SIZES: list[tuple[str, str]] = [
    ("default", "Printer default (A4 preview)"),
    ("a4", "A4 (210 × 297 mm)"),
    ("letter", "Letter (8.5 × 11 in)"),
    ("legal", "Legal (8.5 × 14 in)"),
    ("a5", "A5 (148 × 210 mm)"),
]

DUPLEX_MODES: list[tuple[str, str]] = [
    ("simplex", "One-sided (Simplex)"),
    ("long", "Two-sided (Flip on long edge)"),
    ("short", "Two-sided (Flip on short edge)"),
]

COLOR_MODES: list[tuple[str, str]] = [
    ("color", "Color"),
    ("grayscale", "Grayscale / Black & White"),
]

ORIENTATIONS: list[tuple[str, str]] = [
    ("auto", "Auto (match page)"),
    ("portrait", "Portrait"),
    ("landscape", "Landscape"),
]

SCALE_MODES: list[tuple[str, str]] = [
    ("fit", "Fit to page"),
    ("shrink", "Shrink oversized only"),
    ("actual", "Actual size (100%)"),
    ("fill", "Fill page (crop edges)"),
]

PAGE_SCOPES: list[tuple[str, str]] = [
    ("all", "All pages"),
    ("first", "First page only"),
    ("range", "Custom range"),
]

PAGE_SUBSETS: list[tuple[str, str]] = [
    ("all", "All pages"),
    ("odd", "Odd pages only"),
    ("even", "Even pages only"),
]

SEPARATOR_MODES: list[tuple[str, str]] = [
    ("none", "None"),
    ("per_name", "Cover sheet per person / name"),
    ("per_file", "Cover sheet before each file"),
]

JOB_MODES: list[tuple[str, str]] = [
    ("combined", "Combined single print job"),
    ("per_name", "Separate job per person / name"),
    ("per_file", "Separate job per file"),
]

PRINT_METHODS: list[tuple[str, str]] = [
    ("direct", "Direct engine (full layout & options)"),
    ("shell", "System default app (shell print)"),
]

SORT_ORDERS: list[tuple[str, str]] = [
    ("keep", "Keep list order"),
    ("name", "Sort by person / name"),
    ("file", "Sort by file name"),
    ("type", "Sort by file type"),
]


class PrintError(Exception):
    """A user-facing printing failure."""


@dataclass
class PrinterInfo:
    name: str
    driver: str = ""
    port: str = ""
    status: str = "Ready"
    is_default: bool = False
    is_shared: bool = False
    jobs: int = 0
    comment: str = ""


@dataclass
class PrintJobInfo:
    job_id: int
    document: str
    owner: str = ""
    pages: int = 0
    status: str = ""


@dataclass
class PrintItem:
    path: Path
    label: str = ""
    copies: int = 0            # 0 = use global settings.copies
    page_range: str = ""       # "" = use global settings.page_scope / page_range
    enabled: bool = True
    page_count: int | None = None


@dataclass
class PrintSettings:
    printer: str = ""
    copies: int = 1
    collate: bool = True
    duplex: str = "simplex"           # simplex | long | short
    color_mode: str = "color"         # color | grayscale
    orientation: str = "auto"         # auto | portrait | landscape
    paper_size: str = "default"       # default | a4 | letter | legal | a5
    scale_mode: str = "fit"           # fit | fill | actual | shrink
    margin_mm: float = 6.0
    margin_left_mm: float | None = None
    margin_top_mm: float | None = None
    margin_right_mm: float | None = None
    margin_bottom_mm: float | None = None
    nup: int = 1                      # 1 | 2 | 4
    nup_border: bool = True
    page_scope: str = "all"           # all | first | range
    page_range: str = ""
    page_subset: str = "all"          # all | odd | even
    reverse_order: bool = False
    separator: str = "none"           # none | per_name | per_file
    header_text: str = ""
    footer_text: str = ""
    job_mode: str = "combined"        # combined | per_file | per_name
    method: str = "direct"            # direct | shell
    delay_seconds: float = 0.0
    wait_for_job: bool = False
    dpi: int = 200
    libreoffice_cmd: str = ""

    def margins_mm(self) -> tuple[float, float, float, float]:
        """Return (left, top, right, bottom) margins in mm."""
        base = max(0.0, float(self.margin_mm if self.margin_mm is not None else 6.0))
        return tuple(
            max(0.0, float(v if v is not None else base))
            for v in (self.margin_left_mm, self.margin_top_mm, self.margin_right_mm, self.margin_bottom_mm)
        )  # type: ignore[return-value]


@dataclass
class ComposedPrint:
    pdf_path: Path
    total_sheets: int
    source_pages: int
    file_pages: dict[str, int] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    sheet_labels: list[str] = field(default_factory=list)
    sheet_groups: list[tuple[str, str]] = field(default_factory=list)
    unfiltered_sheets: int = 0


@dataclass
class PrintSummary:
    total_files: int
    printed_files: int
    failed_files: int
    total_sheets: int
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


# --------------------------------------------------------------------------- Win32 ctypes bindings
if IS_WINDOWS:
    from ctypes import wintypes

    _PRINTER_STATUS_MAP = (
        (0x00000001, "Paused"),
        (0x00000002, "Error"),
        (0x00000004, "Deleting"),
        (0x00000008, "Paper Jam"),
        (0x00000010, "Paper Out"),
        (0x00000020, "Manual Feed"),
        (0x00000040, "Paper Problem"),
        (0x00000080, "Offline"),
        (0x00000400, "Printing"),
        (0x00000800, "Output Bin Full"),
        (0x00001000, "Not Available"),
        (0x00002000, "Waiting"),
        (0x00004000, "Processing"),
        (0x00020000, "Toner Low"),
        (0x00040000, "No Toner"),
        (0x00100000, "User Intervention"),
        (0x00400000, "Door Open"),
    )

    _JOB_STATUS_MAP = (
        (0x0001, "Paused"),
        (0x0002, "Error"),
        (0x0004, "Deleting"),
        (0x0008, "Spooling"),
        (0x0010, "Printing"),
        (0x0020, "Offline"),
        (0x0040, "Paper Out"),
        (0x0080, "Printed"),
        (0x0100, "Deleted"),
        (0x0200, "Blocked"),
        (0x0400, "Intervention"),
    )

    class _PRINTER_INFO_2W(ctypes.Structure):
        _fields_ = [
            ("pServerName", wintypes.LPWSTR),
            ("pPrinterName", wintypes.LPWSTR),
            ("pShareName", wintypes.LPWSTR),
            ("pPortName", wintypes.LPWSTR),
            ("pDriverName", wintypes.LPWSTR),
            ("pComment", wintypes.LPWSTR),
            ("pLocation", wintypes.LPWSTR),
            ("pDevMode", ctypes.c_void_p),
            ("pSepFile", wintypes.LPWSTR),
            ("pPrintProcessor", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR),
            ("pParameters", wintypes.LPWSTR),
            ("pSecurityDescriptor", ctypes.c_void_p),
            ("Attributes", wintypes.DWORD),
            ("Priority", wintypes.DWORD),
            ("DefaultPriority", wintypes.DWORD),
            ("StartTime", wintypes.DWORD),
            ("UntilTime", wintypes.DWORD),
            ("Status", wintypes.DWORD),
            ("cJobs", wintypes.DWORD),
            ("AveragePPM", wintypes.DWORD),
        ]

    class _SYSTEMTIME(ctypes.Structure):
        _fields_ = [
            ("wYear", wintypes.WORD),
            ("wMonth", wintypes.WORD),
            ("wDayOfWeek", wintypes.WORD),
            ("wDay", wintypes.WORD),
            ("wHour", wintypes.WORD),
            ("wMinute", wintypes.WORD),
            ("wSecond", wintypes.WORD),
            ("wMilliseconds", wintypes.WORD),
        ]

    class _JOB_INFO_1W(ctypes.Structure):
        _fields_ = [
            ("JobId", wintypes.DWORD),
            ("pPrinterName", wintypes.LPWSTR),
            ("pMachineName", wintypes.LPWSTR),
            ("pUserName", wintypes.LPWSTR),
            ("pDocument", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR),
            ("pStatus", wintypes.LPWSTR),
            ("Status", wintypes.DWORD),
            ("Priority", wintypes.DWORD),
            ("Position", wintypes.DWORD),
            ("TotalPages", wintypes.DWORD),
            ("PagesPrinted", wintypes.DWORD),
            ("Submitted", _SYSTEMTIME),
        ]

    class _DEVMODEW_HEAD(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmOrientation", ctypes.c_short),
            ("dmPaperSize", ctypes.c_short),
            ("dmPaperLength", ctypes.c_short),
            ("dmPaperWidth", ctypes.c_short),
            ("dmScale", ctypes.c_short),
            ("dmCopies", ctypes.c_short),
            ("dmDefaultSource", ctypes.c_short),
            ("dmPrintQuality", ctypes.c_short),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
        ]

    class _DOCINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_int),
            ("lpszDocName", wintypes.LPCWSTR),
            ("lpszOutput", wintypes.LPCWSTR),
            ("lpszDatatype", wintypes.LPCWSTR),
            ("fwType", wintypes.DWORD),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    _winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32.dll", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32.dll", use_last_error=True)

    _winspool.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _winspool.GetDefaultPrinterW.restype = wintypes.BOOL
    _winspool.EnumPrintersW.argtypes = [
        wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _winspool.EnumPrintersW.restype = wintypes.BOOL
    _winspool.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    _winspool.OpenPrinterW.restype = wintypes.BOOL
    _winspool.ClosePrinter.argtypes = [wintypes.HANDLE]
    _winspool.ClosePrinter.restype = wintypes.BOOL
    _winspool.DocumentPropertiesW.argtypes = [
        wintypes.HWND, wintypes.HANDLE, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ]
    _winspool.DocumentPropertiesW.restype = wintypes.LONG
    _winspool.EnumJobsW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _winspool.EnumJobsW.restype = wintypes.BOOL
    _winspool.SetJobW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _winspool.SetJobW.restype = wintypes.BOOL
    _winspool.SetPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _winspool.SetPrinterW.restype = wintypes.BOOL

    _gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    _gdi32.CreateDCW.restype = wintypes.HDC
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    _gdi32.GetDeviceCaps.restype = ctypes.c_int
    _gdi32.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(_DOCINFOW)]
    _gdi32.StartDocW.restype = ctypes.c_int
    _gdi32.EndDoc.argtypes = [wintypes.HDC]
    _gdi32.EndDoc.restype = ctypes.c_int
    _gdi32.AbortDoc.argtypes = [wintypes.HDC]
    _gdi32.AbortDoc.restype = ctypes.c_int
    _gdi32.StartPage.argtypes = [wintypes.HDC]
    _gdi32.StartPage.restype = ctypes.c_int
    _gdi32.EndPage.argtypes = [wintypes.HDC]
    _gdi32.EndPage.restype = ctypes.c_int
    _gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    _gdi32.SetStretchBltMode.restype = ctypes.c_int
    _gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    _gdi32.SetBrushOrgEx.restype = wintypes.BOOL
    _gdi32.StretchDIBits.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD,
    ]
    _gdi32.StretchDIBits.restype = ctypes.c_int

    _shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]
    _shell32.ShellExecuteW.restype = wintypes.HINSTANCE


def _decode_flags(value: int, table: tuple[tuple[int, str], ...], default: str) -> str:
    hits = [label for bit, label in table if value & bit]
    return ", ".join(hits) if hits else default


# --------------------------------------------------------------------------- printer & queue discovery
def default_printer() -> str:
    """Return the system's default printer name, or '' if none."""
    if IS_WINDOWS:
        size = wintypes.DWORD(0)
        _winspool.GetDefaultPrinterW(None, ctypes.byref(size))
        if size.value > 0:
            buf = ctypes.create_unicode_buffer(size.value)
            if _winspool.GetDefaultPrinterW(buf, ctypes.byref(size)):
                return buf.value
        return ""
    try:
        res = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=4)
        m = re.search(r":\s*(.+)$", res.stdout.strip())
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def list_printers() -> list[PrinterInfo]:
    """Return installed printers, default printer first."""
    default = default_printer()
    printers: list[PrinterInfo] = []
    if IS_WINDOWS:
        flags = 0x00000002 | 0x00000004  # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
        needed = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        _winspool.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        if needed.value > 0:
            raw = (ctypes.c_byte * needed.value)()
            if _winspool.EnumPrintersW(flags, None, 2, raw, needed.value, ctypes.byref(needed), ctypes.byref(returned)):
                arr = ctypes.cast(raw, ctypes.POINTER(_PRINTER_INFO_2W))
                for i in range(returned.value):
                    p = arr[i]
                    name = p.pPrinterName or ""
                    if not name:
                        continue
                    printers.append(PrinterInfo(
                        name=name,
                        driver=p.pDriverName or "",
                        port=p.pPortName or "",
                        status=_decode_flags(p.Status, _PRINTER_STATUS_MAP, "Ready"),
                        is_default=(name.casefold() == default.casefold()) if default else False,
                        is_shared=bool(p.Attributes & 0x00000008),
                        jobs=int(p.cJobs),
                        comment=p.pComment or "",
                    ))
    else:
        try:
            res = subprocess.run(["lpstat", "-p"], capture_output=True, text=True, timeout=4)
            for line in res.stdout.splitlines():
                m = re.match(r"printer\s+(\S+)\s+(.*)", line)
                if m:
                    name = m.group(1)
                    st = "Paused" if "disabled" in m.group(2).lower() else "Ready"
                    printers.append(PrinterInfo(name=name, status=st, is_default=(name == default)))
        except Exception:
            pass
    printers.sort(key=lambda p: (not p.is_default, p.name.casefold()))
    return printers


def list_print_jobs(printer: str) -> list[PrintJobInfo]:
    """List currently spooled jobs on `printer`."""
    if not printer:
        return []
    jobs: list[PrintJobInfo] = []
    if IS_WINDOWS:
        h = wintypes.HANDLE()
        if not _winspool.OpenPrinterW(printer, ctypes.byref(h), None):
            return []
        try:
            needed = wintypes.DWORD(0)
            returned = wintypes.DWORD(0)
            _winspool.EnumJobsW(h, 0, 100, 1, None, 0, ctypes.byref(needed), ctypes.byref(returned))
            if needed.value > 0:
                raw = (ctypes.c_byte * needed.value)()
                if _winspool.EnumJobsW(h, 0, 100, 1, raw, needed.value, ctypes.byref(needed), ctypes.byref(returned)):
                    arr = ctypes.cast(raw, ctypes.POINTER(_JOB_INFO_1W))
                    for i in range(returned.value):
                        j = arr[i]
                        jobs.append(PrintJobInfo(
                            job_id=int(j.JobId),
                            document=j.pDocument or "(untitled)",
                            owner=j.pUserName or "",
                            pages=int(j.TotalPages),
                            status=(j.pStatus or "").strip() or _decode_flags(j.Status, _JOB_STATUS_MAP, "Queued"),
                        ))
        finally:
            _winspool.ClosePrinter(h)
    else:
        try:
            res = subprocess.run(["lpq", "-P", printer], capture_output=True, text=True, timeout=4)
            for line in res.stdout.splitlines()[2:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2].isdigit():
                    jobs.append(PrintJobInfo(job_id=int(parts[2]), document=" ".join(parts[3:-2] or [parts[3]]),
                                             owner=parts[1], status=parts[0]))
        except Exception:
            pass
    return jobs


def cancel_print_job(printer: str, job_id: int) -> None:
    """Cancel a spooled print job by ID."""
    if IS_WINDOWS:
        h = wintypes.HANDLE()
        if not _winspool.OpenPrinterW(printer, ctypes.byref(h), None):
            raise PrintError(f"Could not open printer '{printer}'.")
        try:
            if not _winspool.SetJobW(h, job_id, 0, None, 5):  # JOB_CONTROL_DELETE = 5
                raise PrintError(f"Could not cancel job #{job_id}.")
        finally:
            _winspool.ClosePrinter(h)
    else:
        subprocess.run(["cancel", str(job_id)], check=True, capture_output=True, timeout=5)


def pause_printer(printer: str, pause: bool) -> None:
    """Pause or resume a printer queue."""
    if not printer:
        return
    if IS_WINDOWS:
        h = wintypes.HANDLE()
        if not _winspool.OpenPrinterW(printer, ctypes.byref(h), None):
            raise PrintError(f"Could not open printer '{printer}'.")
        try:
            cmd = 1 if pause else 2  # PRINTER_CONTROL_PAUSE=1, PRINTER_CONTROL_RESUME=2
            if not _winspool.SetPrinterW(h, 0, None, cmd):
                raise PrintError(f"Windows denied {'pausing' if pause else 'resuming'} '{printer}' (check permissions).")
        finally:
            _winspool.ClosePrinter(h)
    else:
        subprocess.run(["cupsdisable" if pause else "cupsenable", printer], check=True, capture_output=True, timeout=5)


def open_printer_queue(printer: str) -> None:
    if IS_WINDOWS and printer:
        subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/o", "/n", printer])


def open_printer_properties(printer: str) -> None:
    if IS_WINDOWS and printer:
        subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/p", "/n", printer])


# --------------------------------------------------------------------------- pure selection, sorting & estimation
def parse_page_range(spec: str, total: int) -> list[int]:
    """Parse 1-based page range string ('1-3, 5, 7-') into deduplicated 0-based page indices."""
    if total <= 0:
        return []
    spec = (spec or "").strip()
    if not spec:
        return list(range(total))
    seen: set[int] = set()
    out: list[int] = []
    for part in re.split(r"[,\s;]+", spec):
        if not part:
            continue
        if "-" in part:
            a_s, _, b_s = part.partition("-")
            try:
                start = int(a_s) if a_s.strip() else 1
                end = int(b_s) if b_s.strip() else total
            except ValueError:
                continue
            step = 1 if start <= end else -1
            for n in range(start, end + step, step):
                idx = n - 1
                if 0 <= idx < total and idx not in seen:
                    seen.add(idx)
                    out.append(idx)
        else:
            try:
                idx = int(part) - 1
            except ValueError:
                continue
            if 0 <= idx < total and idx not in seen:
                seen.add(idx)
                out.append(idx)
    return out


def select_page_indices(total: int, scope: str = "all", range_str: str = "",
                        subset: str = "all", reverse: bool = False) -> list[int]:
    """Select 0-based page indices given scope ('all'|'first'|'range'), subset ('all'|'odd'|'even') and reverse."""
    if total <= 0:
        return []
    if scope == "first":
        indices = [0]
    elif scope == "range" and (range_str or "").strip():
        indices = parse_page_range(range_str, total)
    else:
        indices = list(range(total))
    if subset == "odd":
        indices = [i for i in indices if (i + 1) % 2 == 1]
    elif subset == "even":
        indices = [i for i in indices if (i + 1) % 2 == 0]
    if reverse:
        indices = list(reversed(indices))
    return indices


def sort_print_items(items: list[PrintItem], order: str) -> list[PrintItem]:
    """Return a sorted copy of `items` ('keep'|'name'|'file'|'type')."""
    if order == "name":
        return sorted(items, key=lambda it: (it.label.casefold(), it.path.name.casefold()))
    if order == "file":
        return sorted(items, key=lambda it: it.path.name.casefold())
    if order == "type":
        def rank(p: Path) -> tuple[int, str, str]:
            ext = p.suffix.lower()
            grp = 0 if ext == ".pdf" else 1 if ext in IMAGE_EXTS else 2 if ext in OFFICE_EXTS else 3
            return (grp, ext, p.name.casefold())
        return sorted(items, key=lambda it: rank(it.path))
    return list(items)


def count_file_pages(path: Path, libreoffice_cmd: str = "") -> int | None:
    """Fast page count for PDF, images, text, or cached Office documents."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            with fitz.open(path) as doc:
                return len(doc)
        if ext in IMAGE_EXTS:
            with Image.open(path) as im:
                return max(1, int(getattr(im, "n_frames", 1)))
        if ext in TEXT_EXTS:
            lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            return max(1, math.ceil(max(1, lines) / 48))
        if ext in OFFICE_EXTS:
            pdf = to_pdf_cached(path, data_dir() / "cache", libreoffice_cmd)
            with fitz.open(pdf) as doc:
                return len(doc)
    except Exception:
        return None
    return None


def _item_effective_indices(it: PrintItem, settings: PrintSettings, total: int) -> list[int]:
    if (it.page_range or "").strip():
        return select_page_indices(total, "range", it.page_range, settings.page_subset, settings.reverse_order)
    return select_page_indices(total, settings.page_scope, settings.page_range,
                               settings.page_subset, settings.reverse_order)


def estimate_sheets(items: list[PrintItem], settings: PrintSettings) -> tuple[int, int]:
    """Estimate (logical_pages_in_one_copy_pass, total_physical_sheets) for enabled items."""
    enabled = [it for it in items if it.enabled]
    if not enabled:
        return (0, 0)
    nup = max(1, int(settings.nup or 1))
    global_copies = max(1, int(settings.copies or 1))
    total_logical = 0
    total_faces = 0
    last_label = object()

    for it in enabled:
        pages_guess = it.page_count if (it.page_count is not None and it.page_count > 0) else 1
        sel = _item_effective_indices(it, settings, pages_guess)
        item_pages = len(sel)
        item_copies = max(1, int(it.copies)) if it.copies > 0 else 1

        sep_faces = 0
        if settings.separator == "per_file":
            sep_faces = 1
        elif settings.separator == "per_name" and it.label != last_label:
            sep_faces = 1
        last_label = it.label

        faces_one = sep_faces + math.ceil(item_pages / nup)
        total_logical += item_pages * item_copies
        total_faces += faces_one * item_copies

    if settings.duplex in ("long", "short"):
        sheets_per_pass = math.ceil(total_faces / 2)
    else:
        sheets_per_pass = total_faces
    return (total_logical * global_copies, sheets_per_pass * global_copies)


# --------------------------------------------------------------------------- document normalization & composer
def _paper_points(paper_size: str) -> tuple[float, float]:
    key = (paper_size or "a4").strip().lower()
    if key == "default" or key not in ("a4", "letter", "legal", "a5"):
        key = "a4"
    w, h = fitz.paper_size(key)
    return (float(w), float(h))


def _text_to_pdf_doc(path: Path, paper_pts: tuple[float, float]) -> "fitz.Document":
    import io

    raw = path.read_text(encoding="utf-8", errors="replace")
    paras = []
    for line in raw.splitlines():
        esc = html.escape(line) if line.strip() else "&nbsp;"
        paras.append(f"<p dir='auto' style='margin:0 0 3pt 0'>{esc}</p>")
    if not paras:
        paras.append("<p>&nbsp;</p>")
    body = "<body style='font-size:10pt;line-height:1.35;font-family:sans-serif'>" + "".join(paras) + "</body>"
    buf = io.BytesIO()
    story = fitz.Story(body)
    writer = fitz.DocumentWriter(buf)
    rect = fitz.Rect(0, 0, paper_pts[0], paper_pts[1])
    where = rect + (42, 42, -42, -42)
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return fitz.open(stream=buf.getvalue(), filetype="pdf")


def source_to_pdf_doc(path: Path, paper_pts: tuple[float, float], libreoffice_cmd: str = "") -> "fitz.Document":
    """Convert any printable file into an open `fitz.Document`."""
    if not path.is_file():
        raise PrintError(f"File not found: {path}")
    ext = path.suffix.lower()
    if ext == ".pdf":
        return fitz.open(path)
    if ext in IMAGE_EXTS:
        doc = fitz.open()
        with Image.open(path) as raw:
            frames = max(1, int(getattr(raw, "n_frames", 1)))
            for idx in range(frames):
                raw.seek(idx)
                im = ImageOps.exif_transpose(raw.copy())
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                import io
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=92)
                w_pt = max(72.0, im.width * 72.0 / 150.0)
                h_pt = max(72.0, im.height * 72.0 / 150.0)
                pg = doc.new_page(width=w_pt, height=h_pt)
                pg.insert_image(fitz.Rect(0, 0, w_pt, h_pt), stream=buf.getvalue())
        return doc
    if ext in OFFICE_EXTS:
        try:
            pdf = to_pdf_cached(path, data_dir() / "cache", libreoffice_cmd)
            return fitz.open(pdf)
        except OfficeError:
            if ext == ".docx":
                with tempfile.TemporaryDirectory(dir=temp_dir(), ignore_cleanup_errors=True) as tmp:
                    out_pdf = Path(tmp) / "doc.pdf"
                    docx_fallback_pdf(path, out_pdf)
                    return fitz.open(stream=out_pdf.read_bytes(), filetype="pdf")
            raise PrintError(f"LibreOffice is needed to print {ext} directly (or use 'System shell print' engine).")
    if ext in TEXT_EXTS:
        return _text_to_pdf_doc(path, paper_pts)
    raise PrintError(f"Unsupported file type for direct printing: {ext or path.name}")


def draw_separator_page(doc: "fitz.Document", paper_pts: tuple[float, float], title: str, subtitle: str,
                        entries: list[tuple[str, str]], timestamp: str,
                        margins_pt: tuple[float, float, float, float] | None = None) -> None:
    """Append a crisp cover / separator sheet (supports Arabic + English)."""
    pw, ph = paper_pts
    page = doc.new_page(width=pw, height=ph)
    ml, mt, mr, mb = margins_pt if margins_pt is not None else (44.0, 44.0, 44.0, 44.0)
    page.draw_rect(fitz.Rect(ml, mt, pw - mr, ph - mb), color=(0.25, 0.35, 0.55), width=1.4)
    page.draw_rect(fitz.Rect(ml, mt, pw - mr, mt + 62),
                   color=(0.15, 0.38, 0.85), fill=(0.93, 0.96, 1.0), width=0.8)
    arabic.draw_text(page, ml + 16, mt + 28, title or "Print Packet", size=18, bold=True,
                     color=(0.10, 0.24, 0.55), right=pw - mr - 16)
    if subtitle:
        arabic.draw_text(page, ml + 16, mt + 48, subtitle, size=10.5,
                         color=(0.25, 0.30, 0.40), right=pw - mr - 16)
    y = mt + 92
    arabic.draw_text(page, ml + 16, y, f"Files included ({len(entries)}):", size=11, bold=True)
    y += 22
    for idx, (fname, info_txt) in enumerate(entries[:32], 1):
        line = f"{idx}. {fname}" + (f"   ({info_txt})" if info_txt else "")
        arabic.draw_text(page, ml + 22, y, line[:95], size=9.5, right=pw - mr - 16)
        y += 17
        if y > ph - mb - 45:
            arabic.draw_text(page, ml + 22, y, f"… and {len(entries) - idx} more file(s)", size=9.5,
                             color=(0.4, 0.4, 0.4))
            break
    arabic.draw_text(page, ml + 16, ph - mb - 14, f"Utils Hub  •  {timestamp}", size=8.5,
                     color=(0.45, 0.45, 0.5))


def _oriented_paper(base_pts: tuple[float, float], orientation: str, src_w: float, src_h: float, nup: int) -> tuple[float, float]:
    short_s, long_s = min(base_pts), max(base_pts)
    if orientation == "portrait":
        return (short_s, long_s)
    if orientation == "landscape":
        return (long_s, short_s)
    if nup == 2:
        return (long_s, short_s)
    if nup == 4:
        return (short_s, long_s)
    return (long_s, short_s) if src_w > src_h else (short_s, long_s)


def _nup_grid(nup: int, pw: float, ph: float) -> tuple[int, int]:
    if nup == 2:
        return (2, 1) if pw >= ph else (1, 2)
    if nup == 4:
        return (2, 2)
    return (1, 1)


def _place_page_in_cell(dst_page, src_doc, src_idx: int, cell: "fitz.Rect", scale_mode: str) -> None:
    sr = src_doc[src_idx].rect
    sw, sh = max(1.0, sr.width), max(1.0, sr.height)
    cw, ch = max(1.0, cell.width), max(1.0, cell.height)
    if scale_mode == "actual":
        target = fitz.Rect(
            cell.x0 + (cw - sw) / 2.0,
            cell.y0 + (ch - sh) / 2.0,
            cell.x0 + (cw + sw) / 2.0,
            cell.y0 + (ch + sh) / 2.0,
        )
        if sw <= cw and sh <= ch:
            dst_page.show_pdf_page(target, src_doc, src_idx, keep_proportion=True)
        else:
            clip_w = min(sw, cw)
            clip_h = min(sh, ch)
            clip_r = fitz.Rect((sw - clip_w) / 2.0, (sh - clip_h) / 2.0,
                               (sw + clip_w) / 2.0, (sh + clip_h) / 2.0)
            fit_target = fitz.Rect(cell.x0 + (cw - clip_w) / 2.0, cell.y0 + (ch - clip_h) / 2.0,
                                   cell.x0 + (cw + clip_w) / 2.0, cell.y0 + (ch + clip_h) / 2.0)
            dst_page.show_pdf_page(fit_target, src_doc, src_idx, keep_proportion=True, clip=clip_r)
        return
    if scale_mode == "shrink" and sw <= cw and sh <= ch:
        target = fitz.Rect(
            cell.x0 + (cw - sw) / 2.0,
            cell.y0 + (ch - sh) / 2.0,
            cell.x0 + (cw + sw) / 2.0,
            cell.y0 + (ch + sh) / 2.0,
        )
        dst_page.show_pdf_page(target, src_doc, src_idx, keep_proportion=True)
        return
    if scale_mode == "fill":
        scale = max(cw / sw, ch / sh)
        vis_w, vis_h = cw / scale, ch / scale
        clip_r = fitz.Rect((sw - vis_w) / 2.0, (sh - vis_h) / 2.0,
                           (sw + vis_w) / 2.0, (sh + vis_h) / 2.0)
        dst_page.show_pdf_page(cell, src_doc, src_idx, keep_proportion=False, clip=clip_r)
        return
    # "fit"
    dst_page.show_pdf_page(cell, src_doc, src_idx, keep_proportion=True)


def _format_stamp(tpl: str, label: str, fname: str, page_num: int, total_pages: int, date_str: str) -> str:
    return (
        tpl.replace("{name}", label or "")
        .replace("{file}", fname or "")
        .replace("{page}", str(page_num))
        .replace("{total}", str(total_pages))
        .replace("{date}", date_str)
    )


def _convert_pdf_grayscale(doc: "fitz.Document", dpi: int = 180) -> "fitz.Document":
    gray_doc = fitz.open()
    zoom = max(1.0, min(3.0, dpi / 72.0))
    mat = fitz.Matrix(zoom, zoom)
    for pg in doc:
        pix = pg.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
        out_pg = gray_doc.new_page(width=pg.rect.width, height=pg.rect.height)
        out_pg.insert_image(out_pg.rect, stream=pix.tobytes("png"))
    return gray_doc


def compose_print_pdf(
    items: list[PrintItem],
    settings: PrintSettings,
    out_pdf: Path,
    progress_cb: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    include_copies: bool = False,
    include_item_copies: bool = False,
    excluded_sheets: set[int] | None = None,
    sheet_offset: int = 0,
    allow_empty: bool = False,
) -> ComposedPrint:
    """Compose enabled `items` into one WYSIWYG print-ready PDF matching all layout/selection/stamp settings."""
    enabled = [it for it in items if it.enabled]
    if not enabled:
        raise PrintError("No files are selected for printing.")

    base_pts = _paper_points(settings.paper_size)
    ml_mm, mt_mm, mr_mm, mb_mm = settings.margins_mm()
    pt_per_mm = 72.0 / 25.4
    ml_pt, mt_pt, mr_pt, mb_pt = ml_mm * pt_per_mm, mt_mm * pt_per_mm, mr_mm * pt_per_mm, mb_mm * pt_per_mm
    sep_margins = (max(28.0, ml_pt), max(28.0, mt_pt), max(28.0, mr_pt), max(28.0, mb_pt))
    nup = settings.nup if settings.nup in (1, 2, 4) else 1
    header_tpl = (settings.header_text or "").strip()
    footer_tpl = (settings.footer_text or "").strip()
    top_extra = 16.0 if header_tpl else 0.0
    bot_extra = 16.0 if footer_tpl else 0.0
    date_str = datetime.now().strftime("%Y-%m-%d")
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    out_doc = fitz.open()
    file_pages: dict[str, int] = {}
    skipped: list[tuple[str, str]] = []
    sheet_meta: list[tuple[str, str]] = []  # (label, file_name) per content sheet (empty tuple for separator)
    sheet_labels: list[str] = []
    sheet_groups: list[tuple[str, str]] = []
    total_source_pages = 0
    preview_idx = sheet_offset

    # Group items ahead for per_name separator summaries
    name_groups: dict[str, list[PrintItem]] = {}
    for it in enabled:
        name_groups.setdefault(it.label, []).append(it)
    seen_name_sep: set[str] = set()

    for idx, it in enumerate(enabled, 1):
        if cancelled and cancelled():
            break
        if progress_cb:
            progress_cb(idx, len(enabled), it.path.name)
        try:
            src_doc = source_to_pdf_doc(it.path, base_pts, settings.libreoffice_cmd)
        except Exception as exc:
            skipped.append((it.path.name, str(exc)))
            continue

        try:
            total_src = len(src_doc)
            file_pages[str(it.path)] = total_src
            indices = _item_effective_indices(it, settings, total_src)
            if not indices:
                continue

            batch_id = str(sheet_offset + idx)
            batch_title = f"{it.label} — {it.path.name}" if it.label else it.path.name
            start_preview_idx = preview_idx
            item_copies = max(1, int(it.copies)) if ((include_copies or include_item_copies) and it.copies > 0) else 1
            for cp_i in range(item_copies):
                local_i = 0
                if settings.separator == "per_name" and it.label not in seen_name_sep:
                    seen_name_sep.add(it.label)
                    base_idx = start_preview_idx + local_i
                    local_i += 1
                    if not (excluded_sheets and base_idx in excluded_sheets):
                        grp_entries = [(g.path.name, "") for g in name_groups.get(it.label, [it])]
                        draw_separator_page(
                            out_doc, base_pts,
                            title=it.label or it.path.stem,
                            subtitle="Document Packet",
                            entries=grp_entries,
                            timestamp=ts_str,
                            margins_pt=sep_margins,
                        )
                        sheet_meta.append(("", ""))
                        sheet_labels.append("Cover")
                        sheet_groups.append((batch_id, batch_title))
                elif settings.separator == "per_file":
                    base_idx = start_preview_idx + local_i
                    local_i += 1
                    if not (excluded_sheets and base_idx in excluded_sheets):
                        draw_separator_page(
                            out_doc, base_pts,
                            title=it.path.name,
                            subtitle=f"For: {it.label}" if it.label else "",
                            entries=[(it.path.name, f"{len(indices)} page(s)")],
                            timestamp=ts_str,
                            margins_pt=sep_margins,
                        )
                        sheet_meta.append(("", ""))
                        sheet_labels.append("Cover")
                        sheet_groups.append((batch_id, batch_title))

                total_source_pages += len(indices)

                for chunk_start in range(0, len(indices), nup):
                    chunk = indices[chunk_start:chunk_start + nup]
                    base_idx = start_preview_idx + local_i
                    local_i += 1
                    if excluded_sheets and base_idx in excluded_sheets:
                        continue
                    first_rect = src_doc[chunk[0]].rect
                    pw, ph = _oriented_paper(base_pts, settings.orientation, first_rect.width, first_rect.height, nup)
                    cols, rows = _nup_grid(nup, pw, ph)
                    dst_pg = out_doc.new_page(width=pw, height=ph)
                    sheet_meta.append((it.label, it.path.name))
                    pg_tag = f"p.{chunk[0] + 1}" if len(chunk) == 1 else f"p.{chunk[0] + 1}-{chunk[-1] + 1}"
                    sheet_labels.append(pg_tag)
                    sheet_groups.append((batch_id, batch_title))

                    gap = 8.0 if nup > 1 else 0.0
                    avail_w = max(36.0, pw - ml_pt - mr_pt - gap * (cols - 1))
                    avail_h = max(36.0, ph - mt_pt - mb_pt - top_extra - bot_extra - gap * (rows - 1))
                    cell_w = avail_w / cols
                    cell_h = avail_h / rows

                    for slot_i, src_idx in enumerate(chunk):
                        r, c = divmod(slot_i, cols)
                        x0 = ml_pt + c * (cell_w + gap)
                        y0 = mt_pt + top_extra + r * (cell_h + gap)
                        cell = fitz.Rect(x0, y0, x0 + cell_w, y0 + cell_h)
                        _place_page_in_cell(dst_pg, src_doc, src_idx, cell, settings.scale_mode)
                        if nup > 1 and settings.nup_border:
                            dst_pg.draw_rect(cell, color=(0.72, 0.75, 0.80), width=0.5)
                if cp_i == 0:
                    preview_idx = start_preview_idx + local_i
        finally:
            src_doc.close()

    unfiltered_sheets = preview_idx - sheet_offset
    if len(out_doc) == 0:
        out_doc.close()
        if allow_empty:
            return ComposedPrint(
                pdf_path=out_pdf,
                total_sheets=0,
                source_pages=total_source_pages,
                file_pages=file_pages,
                skipped=skipped,
                sheet_labels=[],
                sheet_groups=[],
                unfiltered_sheets=unfiltered_sheets,
            )
        if excluded_sheets and not skipped:
            raise PrintError("All pages were unticked — nothing left to print.")
        detail = "; ".join(f"{n}: {e}" for n, e in skipped[:3])
        raise PrintError(f"Nothing could be composed for printing. {detail}".strip())

    # Apply header & footer stamps across content sheets
    total_sheets = len(out_doc)
    if header_tpl or footer_tpl:
        for i in range(total_sheets):
            lbl, fn = sheet_meta[i]
            if not (lbl or fn):
                continue
            pg = out_doc[i]
            pw, ph = pg.rect.width, pg.rect.height
            stamp_l = max(14.0, ml_pt)
            stamp_r = pw - max(14.0, mr_pt)
            if header_tpl:
                txt = _format_stamp(header_tpl, lbl, fn, i + 1, total_sheets, date_str)
                arabic.draw_text(pg, stamp_l, max(14.0, mt_pt + 10), txt, size=8.5,
                                 color=(0.35, 0.35, 0.40), right=stamp_r)
            if footer_tpl:
                txt = _format_stamp(footer_tpl, lbl, fn, i + 1, total_sheets, date_str)
                arabic.draw_text(pg, stamp_l, ph - max(10.0, mb_pt), txt, size=8.5,
                                 color=(0.35, 0.35, 0.40), right=stamp_r)

    if include_copies and int(settings.copies or 1) > 1:
        n_copies = int(settings.copies)
        base_indices = list(range(len(out_doc)))
        if settings.collate:
            seq = base_indices * n_copies
        else:
            seq = [idx for idx in base_indices for _ in range(n_copies)]
        out_doc.select(seq)
        sheet_labels = [sheet_labels[i] for i in seq]
        sheet_groups = [sheet_groups[i] for i in seq]

    final_doc = out_doc
    if settings.color_mode == "grayscale":
        final_doc = _convert_pdf_grayscale(out_doc, dpi=min(200, max(120, int(settings.dpi or 200))))
        out_doc.close()

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    final_doc.save(str(out_pdf))
    sheet_count = len(final_doc)
    final_doc.close()

    return ComposedPrint(
        pdf_path=out_pdf,
        total_sheets=sheet_count,
        source_pages=total_source_pages,
        file_pages=file_pages,
        skipped=skipped,
        sheet_labels=sheet_labels,
        sheet_groups=sheet_groups,
        unfiltered_sheets=unfiltered_sheets,
    )


# --------------------------------------------------------------------------- Direct GDI & system shell printing
_PAPER_WIN_MAP = {"a4": 9, "letter": 1, "legal": 5, "a5": 11}
_DUPLEX_WIN_MAP = {"simplex": 1, "long": 2, "short": 3}  # DMDUP_SIMPLEX=1, VERTICAL=2, HORIZONTAL=3


def _create_printer_dc(printer_name: str, settings: PrintSettings):
    h = wintypes.HANDLE()
    if not _winspool.OpenPrinterW(printer_name, ctypes.byref(h), None):
        raise PrintError(f"Could not open printer '{printer_name}' (error {ctypes.get_last_error()}).")
    try:
        dm_size = _winspool.DocumentPropertiesW(None, h, printer_name, None, None, 0)
        if dm_size <= 0:
            raise PrintError(f"Could not query driver settings for '{printer_name}'.")
        dm_buf = (ctypes.c_byte * dm_size)()
        if _winspool.DocumentPropertiesW(None, h, printer_name, dm_buf, None, 2) < 0:  # DM_OUT_BUFFER
            raise PrintError(f"Could not read DEVMODE for '{printer_name}'.")

        dm = _DEVMODEW_HEAD.from_buffer(dm_buf)
        fields = dm.dmFields
        if settings.orientation == "portrait":
            dm.dmOrientation = 1
            fields |= 0x00000001
        elif settings.orientation == "landscape":
            dm.dmOrientation = 2
            fields |= 0x00000001
        if settings.paper_size in _PAPER_WIN_MAP:
            dm.dmPaperSize = _PAPER_WIN_MAP[settings.paper_size]
            fields |= 0x00000002
        if settings.duplex in _DUPLEX_WIN_MAP:
            dm.dmDuplex = _DUPLEX_WIN_MAP[settings.duplex]
            fields |= 0x00001000
        dm.dmColor = 1 if settings.color_mode == "grayscale" else 2
        fields |= 0x00000800
        dm.dmCopies = 1  # Copies & collation handled explicitly in page loop
        fields |= 0x00000100
        dm.dmFields = fields

        _winspool.DocumentPropertiesW(None, h, printer_name, dm_buf, dm_buf, 2 | 8)  # DM_OUT_BUFFER | DM_IN_BUFFER
    finally:
        _winspool.ClosePrinter(h)

    hdc = _gdi32.CreateDCW("WINSPOOL", printer_name, None, dm_buf)
    if not hdc:
        raise PrintError(f"Could not create printer device context for '{printer_name}' (error {ctypes.get_last_error()}).")
    return hdc


def _blit_pil_to_dc(hdc, img: Image.Image, target_w: int, target_h: int) -> None:
    iw, ih = img.size
    stride = (iw * 3 + 3) & ~3
    raw_bgr = img.convert("RGB").tobytes("raw", "BGR", stride, 0)
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = iw
    bmi.biHeight = -ih  # top-down DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 24
    bmi.biCompression = 0  # BI_RGB
    bmi.biSizeImage = len(raw_bgr)

    scale = min(target_w / max(1, iw), target_h / max(1, ih))
    dw = max(1, int(round(iw * scale)))
    dh = max(1, int(round(ih * scale)))
    dx = max(0, (target_w - dw) // 2)
    dy = max(0, (target_h - dh) // 2)

    _gdi32.SetStretchBltMode(hdc, 4)  # HALFTONE
    _gdi32.SetBrushOrgEx(hdc, 0, 0, None)
    res = _gdi32.StretchDIBits(
        hdc, dx, dy, dw, dh,
        0, 0, iw, ih,
        raw_bgr, ctypes.byref(bmi), 0, 0x00CC0020,  # DIB_RGB_COLORS, SRCCOPY
    )
    if res == 0:
        raise PrintError(f"StretchDIBits failed while printing page (error {ctypes.get_last_error()}).")


def _sleep_with_cancel(
    seconds: float,
    cancelled: Callable[[], bool] | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
    done_n: int = 0,
    total_n: int = 0,
    label: str = "",
) -> bool:
    """Sleep for `seconds` in small steps, reporting countdown and returning False if cancelled."""
    import time

    total = max(0.0, float(seconds or 0.0))
    if total <= 0:
        return not (cancelled and cancelled())
    deadline = time.monotonic() + total
    last_reported = -1.0
    while True:
        if cancelled and cancelled():
            return False
        rem = deadline - time.monotonic()
        if rem <= 0:
            return True
        if progress_cb and (last_reported < 0 or (last_reported - rem) >= 0.4 or rem <= 0.2):
            last_reported = rem
            target = f" before {label}" if label else ""
            progress_cb(done_n, total_n, f"Waiting {rem:.1f}s{target}…")
        time.sleep(min(0.1, rem))


def _wait_for_spooler_job(
    printer: str,
    job_id: int,
    cancelled: Callable[[], bool] | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
    done_n: int = 0,
    total_n: int = 0,
    timeout: float = 120.0,
) -> bool:
    """Wait until `job_id` leaves the printer's spooler queue (or timeout/cancel)."""
    import time

    if not IS_WINDOWS or job_id <= 0 or not printer:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled and cancelled():
            return False
        jobs = list_print_jobs(printer)
        if not any(j.job_id == job_id for j in jobs):
            return True
        if progress_cb:
            progress_cb(done_n, total_n, f"Waiting for printer (job #{job_id})…")
        time.sleep(0.4)
    return True


def print_pdf_direct(
    pdf_path: Path,
    settings: PrintSettings,
    job_name: str = "Utils Hub Print",
    copies: int = 1,
    collate: bool = True,
    progress_cb: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    output_file: str | None = None,
) -> int:
    """Send a composed PDF directly to the printer (Win32 GDI on Windows, `lp` on POSIX). Returns pages sent."""
    printer = (settings.printer or "").strip() or default_printer()
    if not printer:
        raise PrintError("No printer is selected and no system default printer is configured.")

    if not IS_WINDOWS:
        cmd = ["lp", "-d", printer, "-n", str(max(1, copies)), "-t", job_name]
        if settings.duplex == "long":
            cmd += ["-o", "sides=two-sided-long-edge"]
        elif settings.duplex == "short":
            cmd += ["-o", "sides=two-sided-short-edge"]
        else:
            cmd += ["-o", "sides=one-sided"]
        if settings.paper_size in ("a4", "letter", "legal", "a5"):
            cmd += ["-o", f"media={settings.paper_size}"]
        if not collate:
            cmd += ["-o", "Collate=False"]
        cmd.append(str(pdf_path))
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise PrintError(res.stderr.strip() or "lp failed.")
        with fitz.open(pdf_path) as d:
            return len(d) * max(1, copies)

    with fitz.open(pdf_path) as doc:
        n_pages = len(doc)
        if n_pages == 0:
            return 0
        n_copies = max(1, int(copies or 1))
        if collate:
            page_seq = list(range(n_pages)) * n_copies
        else:
            page_seq = [i for i in range(n_pages) for _ in range(n_copies)]

        hdc = _create_printer_dc(printer, settings)
        doc_started = False
        aborted = False
        completed = False
        job_id = 0
        sent = 0
        try:
            horz = _gdi32.GetDeviceCaps(hdc, 8)   # HORZRES
            vert = _gdi32.GetDeviceCaps(hdc, 10)  # VERTRES
            dpix = _gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            dpiy = _gdi32.GetDeviceCaps(hdc, 90)  # LOGPIXELSY
            render_dpi = max(72, min(int(settings.dpi or 200), max(dpix, dpiy, 150)))
            zoom = render_dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            cs = fitz.csGRAY if settings.color_mode == "grayscale" else fitz.csRGB

            di = _DOCINFOW()
            di.cbSize = ctypes.sizeof(_DOCINFOW)
            di.lpszDocName = job_name[:200]
            di.lpszOutput = output_file
            di.lpszDatatype = None
            di.fwType = 0

            job_id = _gdi32.StartDocW(hdc, ctypes.byref(di))
            if job_id <= 0:
                raise PrintError(f"StartDocW failed for '{printer}' (error {ctypes.get_last_error()}).")
            doc_started = True

            for step, p_idx in enumerate(page_seq, 1):
                if cancelled and cancelled():
                    aborted = True
                    break
                if progress_cb:
                    progress_cb(step, len(page_seq), f"{job_name} (sheet {step}/{len(page_seq)})")
                if _gdi32.StartPage(hdc) <= 0:
                    raise PrintError(f"StartPage failed on sheet {step} (error {ctypes.get_last_error()}).")
                try:
                    pix = doc[p_idx].get_pixmap(matrix=mat, colorspace=cs, alpha=False)
                    mode = "L" if cs == fitz.csGRAY else "RGB"
                    pil_img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                    _blit_pil_to_dc(hdc, pil_img, horz, vert)
                finally:
                    _gdi32.EndPage(hdc)
                sent += 1

            if aborted:
                _gdi32.AbortDoc(hdc)
            else:
                _gdi32.EndDoc(hdc)
                completed = True
        except Exception:
            if doc_started and not aborted:
                try:
                    _gdi32.AbortDoc(hdc)
                except Exception:
                    pass
            raise
        finally:
            _gdi32.DeleteDC(hdc)

        if completed and settings.wait_for_job and job_id > 0 and not output_file:
            _wait_for_spooler_job(printer, job_id, cancelled=cancelled, progress_cb=progress_cb)
        return sent


def print_file_shell(path: Path, printer: str = "") -> None:
    """Ask the OS shell / associated app to print `path`."""
    if not path.is_file():
        raise PrintError(f"File not found: {path}")
    if IS_WINDOWS:
        verb = "printto" if printer else "print"
        params = f'"{printer}"' if printer else None
        rc = _shell32.ShellExecuteW(None, verb, str(path), params, None, 0)
        if rc <= 32:
            raise PrintError(f"No default print handler is registered for '{path.suffix}' (code {rc}).")
    else:
        cmd = ["lp"] + (["-d", printer] if printer else []) + [str(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            raise PrintError(res.stderr.strip() or "lp failed.")


def print_test_page(printer: str) -> None:
    """Generate and print a crisp 1-page diagnostic test page."""
    printer = (printer or "").strip() or default_printer()
    if not printer:
        raise PrintError("No printer selected.")
    with tempfile.TemporaryDirectory(dir=temp_dir(), ignore_cleanup_errors=True) as tmp:
        pdf_path = Path(tmp) / "test-page.pdf"
        doc = fitz.open()
        pw, ph = fitz.paper_size("a4")
        pg = doc.new_page(width=pw, height=ph)
        pg.draw_rect(fitz.Rect(24, 24, pw - 24, ph - 24), color=(0.2, 0.35, 0.7), width=1.5)
        arabic.draw_text(pg, 44, 62, "Utils Hub — Printer Test Page", size=20, bold=True, color=(0.12, 0.25, 0.55))
        arabic.draw_text(pg, 44, 88, f"Printer: {printer}", size=12)
        arabic.draw_text(pg, 44, 108, f"Printed: {datetime.now():%Y-%m-%d %H:%M:%S}", size=10.5, color=(0.35, 0.35, 0.4))
        arabic.draw_text(pg, 44, 140, "Arabic text test: اختبار الطباعة باللغة العربية — ١٢٣٤٥", size=13, right=pw - 44)
        for i in range(6):
            shade = i / 5.0
            x0 = 44 + i * 82
            pg.draw_rect(fitz.Rect(x0, 170, x0 + 74, 200), color=(0.2, 0.2, 0.2), fill=(shade, shade, shade))
        doc.save(str(pdf_path))
        doc.close()
        s = PrintSettings(printer=printer, dpi=200)
        print_pdf_direct(pdf_path, s, job_name="Utils Hub Test Page")


def print_batch(
    items: list[PrintItem],
    settings: PrintSettings,
    progress_cb: Callable[[int, int, str], None] | None = None,
    item_status_cb: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    excluded_sheets: set[int] | None = None,
) -> PrintSummary:
    """Print `items` according to `settings` (combined, per-name, per-file, or shell)."""
    indexed = [(i, it) for i, it in enumerate(items) if it.enabled]
    if not indexed:
        raise PrintError("No files are ticked for printing.")

    printer = (settings.printer or "").strip() or default_printer()
    if not printer:
        raise PrintError("No printer selected and no system default printer is configured.")
    eff_settings = PrintSettings(**{**settings.__dict__, "printer": printer})
    delay = max(0.0, float(eff_settings.delay_seconds or 0.0))

    errors: list[tuple[str, str]] = []
    printed_files = 0
    total_sheets = 0

    if eff_settings.method == "shell":
        n_copies = max(1, int(eff_settings.copies or 1))
        for step, (orig_i, it) in enumerate(indexed, 1):
            if cancelled and cancelled():
                return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
            if item_status_cb:
                item_status_cb(orig_i, "Printing…")
            if progress_cb:
                progress_cb(step, len(indexed), it.path.name)
            try:
                cnt = max(1, int(it.copies)) if it.copies > 0 else 1
                total_runs = cnt * n_copies
                for run_i in range(total_runs):
                    if cancelled and cancelled():
                        return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
                    print_file_shell(it.path, printer)
                    is_last = (step == len(indexed) and run_i == total_runs - 1)
                    if delay > 0 and not is_last:
                        nxt_lbl = it.path.name if run_i + 1 < total_runs else indexed[step][1].path.name
                        if not _sleep_with_cancel(delay, cancelled, progress_cb, step, len(indexed), nxt_lbl):
                            return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
                printed_files += 1
                total_sheets += (it.page_count or 1) * cnt * n_copies
                if item_status_cb:
                    item_status_cb(orig_i, "Sent ✔")
            except Exception as exc:
                errors.append((it.path.name, str(exc)))
                if item_status_cb:
                    item_status_cb(orig_i, "Failed ✘")
        return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors)

    # When delay > 0 in combined mode with multiple files, split per-name / per-file so each job pauses in between
    eff_mode = eff_settings.job_mode
    if eff_mode == "combined" and delay > 0 and len(indexed) > 1:
        eff_mode = "per_name" if eff_settings.separator == "per_name" else "per_file"

    with tempfile.TemporaryDirectory(dir=temp_dir(), ignore_cleanup_errors=True) as tmp:
        tmp_dir = Path(tmp)
        if eff_mode == "combined":
            for orig_i, _it in indexed:
                if item_status_cb:
                    item_status_cb(orig_i, "Composing…")
            out_pdf = tmp_dir / "combined.pdf"
            comp = compose_print_pdf(
                [it for _, it in indexed],
                eff_settings,
                out_pdf,
                progress_cb=progress_cb,
                cancelled=cancelled,
                include_copies=False,
                include_item_copies=True,
                excluded_sheets=excluded_sheets,
            )
            skipped_names = {name: msg for name, msg in comp.skipped}
            for orig_i, it in indexed:
                if it.path.name in skipped_names:
                    errors.append((it.path.name, skipped_names[it.path.name]))
                    if item_status_cb:
                        item_status_cb(orig_i, "Failed ✘")
                elif item_status_cb:
                    item_status_cb(orig_i, "Printing…")

            if cancelled and cancelled():
                return PrintSummary(len(indexed), 0, len(errors), 0, errors, cancelled=True)

            n_copies = max(1, int(eff_settings.copies or 1))
            if delay > 0 and n_copies > 1:
                for cp_i in range(1, n_copies + 1):
                    if cancelled and cancelled():
                        break
                    sent = print_pdf_direct(
                        comp.pdf_path,
                        eff_settings,
                        job_name=f"Utils Hub - {indexed[0][1].path.name} ({cp_i}/{n_copies})",
                        copies=1,
                        collate=eff_settings.collate,
                        progress_cb=progress_cb,
                        cancelled=cancelled,
                    )
                    total_sheets += sent
                    if cp_i < n_copies:
                        if not _sleep_with_cancel(delay, cancelled, progress_cb, cp_i, n_copies, f"copy {cp_i + 1}/{n_copies}"):
                            break
            else:
                sent = print_pdf_direct(
                    comp.pdf_path,
                    eff_settings,
                    job_name=f"Utils Hub - {len(indexed) - len(comp.skipped)} file(s)",
                    copies=eff_settings.copies,
                    collate=eff_settings.collate,
                    progress_cb=progress_cb,
                    cancelled=cancelled,
                )
                total_sheets += sent

            was_cancelled = bool(cancelled and cancelled())
            for orig_i, it in indexed:
                if it.path.name not in skipped_names:
                    if was_cancelled:
                        if item_status_cb:
                            item_status_cb(orig_i, "Cancelled")
                    else:
                        printed_files += 1
                        if item_status_cb:
                            item_status_cb(orig_i, "Sent ✔")
            return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=was_cancelled)

        if eff_mode == "per_name":
            groups: dict[str, list[tuple[int, PrintItem]]] = {}
            for orig_i, it in indexed:
                groups.setdefault(it.label or it.path.stem, []).append((orig_i, it))
            group_items = list(groups.items())
            offset = 0
            for g_idx, (lbl, grp) in enumerate(group_items, 1):
                if cancelled and cancelled():
                    return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
                for orig_i, _ in grp:
                    if item_status_cb:
                        item_status_cb(orig_i, "Printing…")
                if progress_cb:
                    progress_cb(g_idx, len(group_items), lbl)
                did_print = False
                try:
                    out_pdf = tmp_dir / f"grp-{g_idx}.pdf"
                    comp = compose_print_pdf(
                        [it for _, it in grp], eff_settings, out_pdf,
                        include_copies=False, include_item_copies=True,
                        excluded_sheets=excluded_sheets, sheet_offset=offset, allow_empty=True,
                    )
                    offset += comp.unfiltered_sheets
                    if comp.total_sheets == 0:
                        for orig_i, _ in grp:
                            if item_status_cb:
                                item_status_cb(orig_i, "Skipped (0 pages)")
                        continue
                    skipped_names = {n: m for n, m in comp.skipped}
                    sent = print_pdf_direct(
                        comp.pdf_path,
                        eff_settings,
                        job_name=f"Utils Hub - {lbl}",
                        copies=eff_settings.copies,
                        collate=eff_settings.collate,
                        progress_cb=progress_cb,
                        cancelled=cancelled,
                    )
                    total_sheets += sent
                    did_print = True
                    for orig_i, it in grp:
                        if it.path.name in skipped_names:
                            errors.append((it.path.name, skipped_names[it.path.name]))
                            if item_status_cb:
                                item_status_cb(orig_i, "Failed ✘")
                        else:
                            printed_files += 1
                            if item_status_cb:
                                item_status_cb(orig_i, "Sent ✔")
                except Exception as exc:
                    for orig_i, it in grp:
                        errors.append((it.path.name, str(exc)))
                        if item_status_cb:
                            item_status_cb(orig_i, "Failed ✘")
                if did_print and delay > 0 and g_idx < len(group_items):
                    nxt_lbl = group_items[g_idx][0]
                    if not _sleep_with_cancel(delay, cancelled, progress_cb, g_idx, len(group_items), nxt_lbl):
                        return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
            return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors)

        # eff_mode == "per_file"
        offset = 0
        for step, (orig_i, it) in enumerate(indexed, 1):
            if cancelled and cancelled():
                return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
            if item_status_cb:
                item_status_cb(orig_i, "Printing…")
            if progress_cb:
                progress_cb(step, len(indexed), it.path.name)
            did_print = False
            try:
                out_pdf = tmp_dir / f"file-{step}.pdf"
                comp = compose_print_pdf(
                    [it], eff_settings, out_pdf,
                    include_copies=False,
                    excluded_sheets=excluded_sheets,
                    sheet_offset=offset,
                    allow_empty=True,
                )
                offset += comp.unfiltered_sheets
                if comp.total_sheets == 0:
                    if item_status_cb:
                        item_status_cb(orig_i, "Skipped (0 pages)")
                    continue
                item_copies = (max(1, int(it.copies)) if it.copies > 0 else 1) * max(1, int(eff_settings.copies or 1))
                if delay > 0 and item_copies > 1:
                    for cp_i in range(1, item_copies + 1):
                        if cancelled and cancelled():
                            return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
                        sent = print_pdf_direct(
                            comp.pdf_path, eff_settings,
                            job_name=f"{it.path.name} ({cp_i}/{item_copies})",
                            copies=1, collate=eff_settings.collate, cancelled=cancelled,
                        )
                        total_sheets += sent
                        if cp_i < item_copies:
                            if not _sleep_with_cancel(delay, cancelled, progress_cb, step, len(indexed), f"{it.path.name} copy {cp_i + 1}"):
                                return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
                else:
                    sent = print_pdf_direct(
                        comp.pdf_path,
                        eff_settings,
                        job_name=it.path.name,
                        copies=item_copies,
                        collate=eff_settings.collate,
                        cancelled=cancelled,
                    )
                    total_sheets += sent
                printed_files += 1
                did_print = True
                if item_status_cb:
                    item_status_cb(orig_i, "Sent ✔")
            except PrintError as exc:
                # Fall back to shell print if extension isn't supported by direct composer
                if "Unsupported file type" in str(exc):
                    try:
                        print_file_shell(it.path, printer)
                        printed_files += 1
                        total_sheets += 1
                        did_print = True
                        if item_status_cb:
                            item_status_cb(orig_i, "Sent (shell) ✔")
                    except Exception as sexc:
                        errors.append((it.path.name, str(sexc)))
                        if item_status_cb:
                            item_status_cb(orig_i, "Failed ✘")
                else:
                    errors.append((it.path.name, str(exc)))
                    if item_status_cb:
                        item_status_cb(orig_i, "Failed ✘")
            except Exception as exc:
                errors.append((it.path.name, str(exc)))
                if item_status_cb:
                    item_status_cb(orig_i, "Failed ✘")
            if did_print and delay > 0 and step < len(indexed):
                nxt_lbl = indexed[step][1].path.name
                if not _sleep_with_cancel(delay, cancelled, progress_cb, step, len(indexed), nxt_lbl):
                    return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors, cancelled=True)
        return PrintSummary(len(indexed), printed_files, len(errors), total_sheets, errors)
