"""Reading a WhatsApp "Export chat" ZIP: the attachments, the chat text, who sent what and who the person is.

No UI in here.  The pieces:

  parse_chat(text, names)      chat text (iPhone or Android, any language we can recognise) -> `Chat`
  WaPackage(zip_path)          the ZIP: lists the files, reads the chat, extracts ONE file at a time (when you look at it)
  guess_person / person_info   who the documents are about, and the name / phone that follow from it
  clean_phone / find_phone     phone numbers (as WhatsApp shows them, minus invisible characters)

Safety: members are never extracted by their stored path (no "../" tricks) but under a flat, sanitised name in a
private work folder, with a size limit that is enforced on the bytes actually read.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import time
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 600 * 1024 * 1024          # one extracted file
SMALL_IMAGE_BYTES = 20 * 1024               # below this an image is "small" (icons, link previews, stickers)

DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".rtf", ".csv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".opus", ".ogg", ".m4a", ".aac", ".mp3", ".amr", ".wav"}
VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".avi", ".webm"}


class WaError(Exception):
    """The ZIP cannot be used (message is user-friendly)."""


# --------------------------------------------------------------------------- text helpers
_INVISIBLE = dict.fromkeys(list(range(0x200B, 0x2010)) + list(range(0x202A, 0x202F)) + [0xFEFF, 0x2066, 0x2067, 0x2068, 0x2069])
_SPACES = {0x00A0: " ", 0x202F: " ", 0x2009: " ", 0x2007: " "}
_DIGITS = {**{0x0660 + i: str(i) for i in range(10)}, **{0x06F0 + i: str(i) for i in range(10)}}   # Arabic-Indic digits


def clean(text: str) -> str:
    """Invisible direction marks removed, odd spaces and Arabic digits made plain (what the parser works on)."""
    return (text or "").translate({**_INVISIBLE, **_SPACES, **_DIGITS}).replace("\u060c", ",")


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def norm_name(s: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", clean(s))).casefold()


# --------------------------------------------------------------------------- chat model
@dataclass
class Message:
    n: int                                   # position in the chat (0-based)
    when: dt.datetime | None
    sender: str                              # "" = a system line ("Messages are end-to-end encrypted", "X joined" ...)
    text: str
    files: list[str] = field(default_factory=list)      # ZIP member names this message carried
    caption: str = ""                        # the text without the file name / "(file attached)" marker
    omitted: bool = False                    # the export left the media out ("<Media omitted>")

    @property
    def system(self) -> bool:
        return not self.sender


@dataclass
class Chat:
    source: str = ""                         # name of the chat file inside the ZIP
    title: str = ""
    messages: list[Message] = field(default_factory=list)
    date_order: str = "dmy"                  # dmy | mdy | ymd
    ambiguous: bool = False                  # every date was ≤ 12, so day/month could be either way round

    def participants(self) -> list[tuple[str, int]]:
        """(sender, message count), busiest first.  System lines are not people."""
        count = Counter(m.sender for m in self.messages if m.sender)
        return sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))

    def attachments_by(self) -> Counter:
        c: Counter = Counter()
        for m in self.messages:
            if m.sender and (m.files or m.omitted):
                c[m.sender] += max(1, len(m.files))
        return c


_DATE = r"(?P<a>\d{1,4})[/.\-](?P<b>\d{1,2})[/.\-](?P<c>\d{1,4})"
_TIME = (r"(?P<h>\d{1,2})[:.](?P<mi>\d{2})(?:[:.](?P<s>\d{2}))?\s*"
         r"(?P<ap>[AaPp]\.?\s?[Mm]\.?|[\u0635\u0645]\w*)?")
_ANDROID = re.compile(rf"^{_DATE},?\s+{_TIME}\s*[-\u2013\u2014]\s+(?P<rest>.*)$")
_IOS = re.compile(rf"^\[{_DATE},?\s+{_TIME}\]\s*(?P<rest>.*)$")
_OMITTED = re.compile(r"(?i)(<\s*media omitted\s*>|\b(image|video|audio|sticker|document|gif|contact card|voice message)\s+omitted\b"
                      r"|<\s*attachment omitted\s*>|\u0627\u0633\u062a\u0628\u0639\u0627\u062f|\u062a\u0645 \u062d\u0630\u0641)")


def _header(line: str):
    m = _IOS.match(line) or _ANDROID.match(line)
    return m


def _split_sender(rest: str) -> tuple[str, str]:
    """'Ahmed Ali: hello' -> ('Ahmed Ali', 'hello'); a system line -> ('', rest)."""
    idx = rest.find(": ")
    if idx <= 0 or idx > 80:
        return "", rest
    sender, text = rest[:idx].strip(), rest[idx + 2:]
    if sender.count('"') % 2 or sender.count("\u201c") != sender.count("\u201d"):     # 'X changed the subject to "a: b"'
        return "", rest
    return sender, text


def _when(h: dict, order: str) -> dt.datetime | None:
    a, b, c = int(h["a"]), int(h["b"]), int(h["c"])
    try:
        if len(h["a"]) == 4:
            y, mo, d = a, b, c
        else:
            y = c + 2000 if len(h["c"]) <= 2 else c
            d, mo = (a, b) if order != "mdy" else (b, a)
        hour, minute, sec = int(h["h"]), int(h["mi"]), int(h["s"] or 0)
        ap = (h["ap"] or "").lower().replace(".", "").replace(" ", "")
        if ap:
            pm = ap.startswith("p") or ap.startswith("\u0645")
            hour = hour % 12 + (12 if pm else 0)
        return dt.datetime(y, mo, d, hour, minute, sec)
    except ValueError:
        return None


def detect_order(headers: list[dict]) -> tuple[str, bool]:
    """dmy / mdy / ymd from the data: a first number above 12 can only be a day, a second one only a day the other way."""
    if headers and all(len(h["a"]) == 4 for h in headers):
        return "ymd", False
    first = any(int(h["a"]) > 12 for h in headers if len(h["a"]) != 4)
    second = any(int(h["b"]) > 12 for h in headers if len(h["a"]) != 4)
    if first and not second:
        return "dmy", False
    if second and not first:
        return "mdy", False
    return "dmy", not (first or second)


def _name_matcher(names: list[str]):
    if not names:
        return None, {}
    by_norm = {unicodedata.normalize("NFC", n): n for n in names}
    ordered = sorted(by_norm, key=len, reverse=True)
    rx = re.compile(r"(?<![\w.\-])(?:" + "|".join(re.escape(n) for n in ordered) + r")(?![\w])")
    return rx, by_norm


_MARKER_LINE = re.compile(r"^\s*[\(\[<][^)\]>\n]{1,40}[)\]>]\s*$")


def _strip_attachments(text: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for a, b in spans:
        out.append(text[last:a])
        last = b
    out.append(text[last:])
    t = re.sub(r"<\s*[^<>:\n]{1,20}:\s*>", "", "".join(out))                 # an emptied "<attached: >"
    lines = t.split("\n")
    if lines and _MARKER_LINE.match(lines[0]):                                  # "(file attached)" in any language
        lines = lines[1:]
    return "\n".join(l for l in lines).strip().strip("<>").strip()


def parse_chat(text: str, names: list[str] | tuple[str, ...] = (), date_order: str = "auto", title: str = "",
               source: str = "") -> Chat:
    """Chat text -> messages.  `names` are the ZIP's file names: a message that mentions one carries that file."""
    raw: list[dict] = []                                                        # header groups + collected lines
    for line in clean(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _header(line)
        if m:
            h = m.groupdict()
            h["lines"] = [h.pop("rest")]
            raw.append(h)
        elif raw:
            raw[-1]["lines"].append(line)
    order, ambiguous = detect_order(raw) if date_order == "auto" else (date_order, False)
    rx, by_norm = _name_matcher(list(names))
    chat = Chat(source=source, title=title, date_order=order, ambiguous=ambiguous)
    for n, h in enumerate(raw):
        sender, body = _split_sender("\n".join(h["lines"]).rstrip())
        msg = Message(n, _when(h, order), sender, body)
        if rx is not None and "." in body:
            spans = []
            for mt in rx.finditer(unicodedata.normalize("NFC", body)):
                real = by_norm.get(mt.group(0))
                if real and real not in msg.files:
                    msg.files.append(real)
                spans.append(mt.span())
            if spans:
                msg.caption = _strip_attachments(unicodedata.normalize("NFC", body), spans)
        elif sender:
            msg.caption = ""
        msg.omitted = bool(sender and not msg.files and _OMITTED.search(body))
        chat.messages.append(msg)
    return chat


# --------------------------------------------------------------------------- phone numbers & people
_PHONE_WORDS = re.compile(r"(?i)(phone|mobile|whats?app|tel\b|call|contact|number|\bno\b|\u0631\u0642\u0645|\u0647\u0627\u062a\u0641|"
                          r"\u062c\u0648\u0627\u0644|\u0645\u0648\u0628\u0627\u064a\u0644|\u0648\u0627\u062a\u0633)")
_PHONE_RX = re.compile(r"(?<![\w+])(\+?\d[\d\s().\-]{6,20}\d)(?!\w)")
_DATE_LIKE = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$")


def clean_phone(text: str) -> str:
    """A number as WhatsApp shows it, without invisible marks and stray spaces ('+971 50 123 4567')."""
    return re.sub(r"\s+", " ", clean(text)).strip(" .-")


def looks_like_phone(text: str) -> bool:
    t = clean_phone(text)
    digits = re.sub(r"\D", "", t)
    return bool(re.fullmatch(r"\+?[\d\s().\-]+", t)) and 7 <= len(digits) <= 15


def find_phone(texts: list[str]) -> str:
    """A phone number written inside messages.  Conservative: '+' or '00' numbers, or a number beside a phone word."""
    best = ""
    for text in texts:
        t = clean(text)
        for m in _PHONE_RX.finditer(t):
            cand = m.group(1).strip()
            digits = re.sub(r"\D", "", cand)
            if not 8 <= len(digits) <= 15 or _DATE_LIKE.match(cand):
                continue
            strong = cand.startswith("+") or cand.startswith("00")
            if strong:
                return clean_phone(cand)
            if not best and _PHONE_WORDS.search(t) and len(digits) >= 9:
                best = clean_phone(cand)
    return best


_ORDINAL = re.compile(r"(?i)\b(\d{1,2})\s*(?:st|nd|rd|th)\b")
_DAY_OF = re.compile(r"(?i)\b(\d{1,2})\s+of\s+(?=[a-z])")
_THE = re.compile(r"(?i)\bthe\s+(?=\d)")


def date_from_text(text: str) -> dt.date | None:
    """A date written in a message ('I join on the 1st of March 2026', 'March 1st, 26') -> date.
    DocPacker's date reader does not know ordinals, which people use all the time in chats."""
    from .dates import parse_any_date
    t = _THE.sub("", _DAY_OF.sub(r"\1 ", _ORDINAL.sub(r"\1", clean(text))))
    return parse_any_date(t)


@dataclass
class PersonInfo:
    sender: str = ""
    name: str = ""
    phone: str = ""
    phone_from: str = ""                     # "sender" | "message" | ""


def person_info(chat: Chat, sender: str) -> PersonInfo:
    """Name and phone of one participant.  An unsaved contact shows as a number: that is the phone, the name stays empty."""
    info = PersonInfo(sender=sender)
    if not sender:
        return info
    if looks_like_phone(sender):
        info.phone, info.phone_from = clean_phone(sender), "sender"
        return info
    info.name = re.sub(r"\s+", " ", clean(sender)).strip()
    info.phone = find_phone([m.text for m in chat.messages if m.sender == sender])
    info.phone_from = "message" if info.phone else ""
    return info


def title_from_name(name: str) -> str:
    """'WhatsApp Chat with Ahmed Ali.zip' / 'WhatsApp Chat - Ahmed' / '_chat.txt' -> 'Ahmed Ali' (or '')."""
    stem = Path(name).stem
    m = re.match(r"(?i)^whatsapp chat\s*(?:with|-|\u2013)?\s*(.+)$", stem.strip())
    if m:
        return m.group(1).strip()
    m = re.match(r"^\u0645\u062d\u0627\u062f\u062b\u0629 \u0648\u0627\u062a\u0633\u0627\u0628(?: \u0645\u0639)?\s*(.+)$", stem.strip())
    if m:
        return m.group(1).strip()
    return "" if stem.lower() in ("_chat", "chat") else stem


def chat_key(title: str, fallback: str = "") -> str:
    return norm_name(title) or norm_name(fallback)


def guess_person(chat: Chat, title: str = "") -> str:
    """The participant the documents are about: the chat's other side in a 1:1 chat, else whoever sends the files."""
    parts = [p for p, _n in chat.participants()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    t = norm_name(title)
    digits = re.sub(r"\D", "", title)
    if len(parts) == 2 and (t or digits):
        for p in parts:
            if (t and norm_name(p) == t) or (len(digits) >= 7 and re.sub(r"\D", "", p) == digits):
                return p
    sent = chat.attachments_by()
    return max(parts, key=lambda p: (sent.get(p, 0), -parts.index(p)))


# --------------------------------------------------------------------------- the package
@dataclass
class WaFile:
    name: str                                # safe, flat file name (what you see and what is extracted)
    member: str                              # the name inside the ZIP
    size: int
    kind: str                                # document | image | sticker | voice | audio | video | contact | other
    crc: int = 0
    message: int | None = None               # Message.n that carried it
    sender: str = ""
    when: dt.datetime | None = None
    caption: str = ""
    small: bool = False
    dup_of: str = ""                         # name of the identical file seen first
    source: int = 0                          # which ZIP of an import (0-based)

    @property
    def noise(self) -> bool:
        """Stickers, voice notes, videos, tiny images and the like: hidden unless you ask to see everything."""
        return self.kind in ("sticker", "voice", "audio", "video", "contact", "other") or self.small


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    up = name.upper()
    if ext in DOC_EXT:
        return "document"
    if ext in IMAGE_EXT:
        return "sticker" if up.startswith("STK-") or "-STICKER-" in up else "image"
    if ext in AUDIO_EXT:
        return "voice" if up.startswith("PTT-") or "-PTT-" in up else "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext == ".vcf":
        return "contact"
    return "other"


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return base or "file"


def work_root() -> Path:
    from .settings import temp_dir
    root = temp_dir() / "whatsapp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def purge_old(days: int = 3, root: Path | None = None) -> int:
    """Remove import folders older than `days` (chats are private: they should not pile up).  Returns how many."""
    root = root or work_root()
    cutoff, removed = time.time() - days * 86400, 0
    for d in root.iterdir() if root.exists() else []:
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


class WaPackage:
    """One WhatsApp export ZIP."""

    def __init__(self, zip_path: str | Path, index: int = 0, root: Path | None = None) -> None:
        self.path = Path(zip_path)
        self.index = index
        self.root = root
        self.workdir: Path | None = None
        self.files: list[WaFile] = []
        self.chat: Chat | None = None
        self.title = ""
        self.chat_names: list[str] = []          # every .txt that might be a chat
        self._members: dict[str, str] = {}       # safe name -> member name
        self._text_cache: dict[str, str] = {}
        self._read()

    # ------------------------------------------------------------------ reading
    def _open(self) -> zipfile.ZipFile:
        try:
            return zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise WaError(f"{self.path.name} is not a readable ZIP file ({exc.__class__.__name__}).") from exc

    def _read(self) -> None:
        with self._open() as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if any(i.flag_bits & 0x1 for i in infos):
                raise WaError(f"{self.path.name} is password-protected; unzip it with the password first.")
            seen_crc: dict[tuple[int, int], str] = {}
            used: set[str] = set()
            texts: list[tuple[str, zipfile.ZipInfo]] = []
            for info in infos:
                base = safe_name(info.filename)
                if base.startswith("__MACOSX") or base.startswith("._") or "__MACOSX/" in info.filename:
                    continue
                name, n = base, 2
                while name in used:
                    name = f"{Path(base).stem} ({n}){Path(base).suffix}"
                    n += 1
                used.add(name)
                self._members[name] = info.filename
                if Path(name).suffix.lower() == ".txt":
                    texts.append((name, info))
                    continue
                f = WaFile(name, info.filename, info.file_size, kind_of(name), info.CRC, source=self.index)
                f.small = f.kind == "image" and info.file_size < SMALL_IMAGE_BYTES
                key = (info.CRC, info.file_size)
                if info.file_size and key in seen_crc:
                    f.dup_of = seen_crc[key]
                else:
                    seen_crc[key] = name
                self.files.append(f)
            self.chat_names = [n for n, _i in texts]
            self.title = title_from_name(self.path.name)
            names = [f.name for f in self.files]
            best: Chat | None = None
            for name, info in sorted(texts, key=lambda t: -t[1].file_size)[:6]:
                if info.file_size > 200 * 1024 * 1024:
                    continue
                chat = parse_chat(decode(zf.read(info)), names, title=self.title or title_from_name(name), source=name)
                if chat.messages and (best is None or len(chat.messages) > len(best.messages)):
                    best = chat
            self.chat = best
            if best is not None and not self.title:
                self.title = best.title
        self.link()

    def reparse(self, date_order: str) -> None:
        """Read the chat again with a different date order (when day/month could not be told apart)."""
        if self.chat is None:
            return
        with self._open() as zf:
            member = self._members[self.chat.source]
            self.chat = parse_chat(decode(zf.read(member)), [f.name for f in self.files], date_order,
                                   self.chat.title, self.chat.source)
        self.link()

    def link(self) -> None:
        """Give every file the message that carried it: who sent it, when, with which words."""
        for f in self.files:
            f.message, f.sender, f.when, f.caption = None, "", None, ""
        if self.chat is None:
            return
        by_name = {f.name: f for f in self.files}
        for m in self.chat.messages:
            for name in m.files:
                f = by_name.get(name)
                if f is not None and f.message is None:
                    f.message, f.sender, f.when, f.caption = m.n, m.sender, m.when, m.caption
        order = {f.name: (f.message if f.message is not None else 10 ** 9, i) for i, f in enumerate(self.files)}
        self.files.sort(key=lambda f: order[f.name])

    # ------------------------------------------------------------------ extracting
    def _dir(self) -> Path:
        if self.workdir is None:
            root = self.root or work_root()
            stem = re.sub(r"[^\w.-]+", "_", self.path.stem)[:40]
            self.workdir = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.index}-{stem}"
            self.workdir.mkdir(parents=True, exist_ok=True)
        return self.workdir

    def extract(self, name: str) -> Path:
        """Unzip ONE file (once) and return where it is."""
        folder = self._dir()
        folder.mkdir(parents=True, exist_ok=True)                 # it may have been cleaned up meanwhile
        dest = folder / safe_name(name)
        if dest.exists():
            return dest
        member = self._members.get(name)
        if member is None:
            raise WaError(f"{name} is not in {self.path.name}.")
        with self._open() as zf:
            info = zf.getinfo(member)
            if info.file_size > MAX_FILE_BYTES:
                raise WaError(f"{name} is too large to open here ({info.file_size // 1024 // 1024} MB).")
            tmp = dest.with_name(dest.name + ".part")
            limit, got = max(info.file_size, 1) + 1024 * 1024, 0
            try:
                with zf.open(info) as src, open(tmp, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        got += len(chunk)
                        if got > limit:
                            raise WaError(f"{name} is larger than the ZIP says; not extracted.")
                        out.write(chunk)
                tmp.replace(dest)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        return dest

    def merge(self, names: list[str], out_name: str = "", page_size: str = "a4") -> tuple[WaFile, list[str]]:
        """Join several files (photos, PDFs, Office files) into ONE PDF, in the order given.

        The result lives in the package's work folder and is listed like any other file (so it can be merged again,
        previewed and extracted).  Returns (the new file, files that could not be merged).  The originals stay as they are.
        """
        from .pdfops import AssembleOptions, Piece, assemble
        by_name = {f.name: f for f in self.files}
        files = [by_name[n] for n in dict.fromkeys(names) if n in by_name]
        if len(files) < 2:
            raise WaError("Choose at least two files to merge.")
        stem = Path(safe_name(out_name or "")).stem if out_name.strip() else ""
        stem = stem or f"Merged {len(files)} files"
        name, n = f"{stem}.pdf", 2
        taken = set(by_name) | set(self._members)
        while name in taken:
            name, n = f"{stem} ({n}).pdf", n + 1
        dest = self._dir() / name
        pieces = [Piece(str(self.extract(f.name))) for f in files]
        try:
            res = assemble(pieces, dest, AssembleOptions(page_size=page_size, bookmarks=False))
        except WaError:
            raise
        except Exception as exc:                                  # nothing could be placed, disk full, ...
            dest.unlink(missing_ok=True)
            raise WaError(f"The files could not be merged ({exc}).") from exc
        if not res.pages:
            dest.unlink(missing_ok=True)
            raise WaError("None of the files could be merged: " + "; ".join(res.skipped[:3]))
        senders = {f.sender for f in files}
        whens = [f.when for f in files if f.when]
        captions = list(dict.fromkeys(f.caption for f in files if f.caption))
        merged = WaFile(name, "", res.size, "document", sender=senders.pop() if len(senders) == 1 else "",
                        when=min(whens) if whens else None, caption=" / ".join(captions)[:300], source=self.index)
        self.files.append(merged)
        return merged, list(res.skipped)

    def cleanup(self) -> None:
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


# --------------------------------------------------------------------------- suggestions
def suggest_type(types, f: WaFile, threshold: float = 0.8, min_score: float = 0.4) -> str:
    """The document type that the file name and the words sent with it point to ('' when nothing does)."""
    from .doctypes import score_doctype
    from .readers import DocInfo
    kind = "pdf" if f.name.lower().endswith(".pdf") else "image" if f.kind == "image" else "office" if f.kind == "document" else "other"
    info = DocInfo(f.name, kind=kind, text=f.caption)
    best, best_score = "", min_score
    for t in types:
        if not getattr(t, "enabled", True):
            continue
        sc = score_doctype(t, info, f.name, threshold)
        if sc > best_score:
            best, best_score = t.id, sc
    return best
