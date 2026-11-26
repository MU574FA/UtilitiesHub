"""Autocomplete engine (no Tk): per-field keywords, shortcuts and an on/off switch for every field.

    ac = Autocomplete(settings)                       # the App owns one: app.autocomplete
    ac.is_enabled("visa.position")                    # master switch AND this field's own switch
    ac.suggest("visa.position", "wel")                # -> Result(start, end, items=[Suggestion("Welder", ...)])
    ac.accept(text, cursor, result, suggestion)       # -> (new_text, new_cursor)
    ac.expansion("visa.log_note", "called nr", 9)     # shortcut typed right before the caret -> Expansion or None

What a *field* is
    A `FieldDef` is something the program knows a text box for (`visa.nationality`, `field.job_title` ...).  Tools
    register their own; fields that depend on the user's settings (CV fields, custom fields) come from *sources*
    (`default_sources`).  Unknown ids still work - they just have no defaults.

What the user can decide, per field (stored in `autocomplete.fields`, only what differs from the defaults)
    enabled    suggestions + shortcuts for this field on / off
    keywords   words and phrases offered while typing
    shortcuts  abbreviation -> text   ("nr" -> "No response").  `{today}` `{time}` `{cursor}` work inside the text;
               an abbreviation that starts with a symbol (";sig", "@g") also works in the middle of a word.
    learn      remember what was typed in this field and offer it again (never for sensitive fields)

Where the suggestions come from (merged, no duplicates, best match first)
    shortcuts  >  keywords (+ the field's linked word lists + dynamic values such as existing groups)  >  learned values
    >  the drop-down values of the box itself.

Matching (`autocomplete.match`): prefix | words (any word starts with it) | contains | smart (also initials "hro" ->
Human Resources Officer, and one typo).  Arabic spelling variants and accents are ignored on both sides.

Modes of a field: `value` (the whole box is one value), `list` (values separated by , or ; - the one under the caret is
completed) and `text` (free text: the last 1-3 words are completed; used for notes).
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .schema import Opt
from .textutil import arabic_fold, fold_accents, slugify

log = logging.getLogger(__name__)

MODES = ("value", "list", "text")
MATCH_MODES = [("prefix", "Start of the text (Wel → Welder)"),
               ("words", "Start of any word (Eng → Software Engineer)"),
               ("contains", "Anywhere inside"),
               ("smart", "Smart: also initials (hro) and small typos")]
MATCH_ORDER = [m for m, _ in MATCH_MODES]
LEARN_MAX = 150                     # remembered values per field
MAX_KEYWORD_LEN = 120
_WORD_CHARS = re.compile(r"\w", re.UNICODE)


# --------------------------------------------------------------------------- text helpers
@lru_cache(maxsize=16384)
def fold(text: str) -> str:
    """Comparable form: accents, Arabic spelling variants and case ignored, spaces collapsed."""
    return " ".join(arabic_fold(fold_accents(text or "")).casefold().split())


def title_key(text: str) -> str:
    return fold(text)


def parse_keywords(text: str) -> list[str]:
    """One keyword per line.  A single line with commas / semicolons is split on them (so a pasted list works)."""
    text = (text or "").replace("\r", "")
    parts = text.split("\n") if "\n" in text.strip("\n") else re.split(r"[;,]", text)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = " ".join(p.split())[:MAX_KEYWORD_LEN]
        if p and fold(p) not in seen:
            seen.add(fold(p))
            out.append(p)
    return out


def format_keywords(keywords: Iterable[str]) -> str:
    return "\n".join(keywords)


def validate_shortcut(abbr: str, expansion: str, existing: Iterable[str] = (), replacing: str = "") -> str:
    """'' when fine, otherwise what is wrong (shown to the user)."""
    abbr = (abbr or "").strip()
    if not abbr:
        return "The shortcut is empty."
    if not (expansion or "").strip():
        return "The text it should expand to is empty."
    if len(abbr) > 30:
        return "A shortcut can have at most 30 characters."
    if any(c.isspace() for c in abbr):
        return "A shortcut cannot contain spaces."
    if fold(abbr) == fold(expansion):
        return "The shortcut and its text are the same."
    others = {fold(a) for a in existing if fold(a) != fold(replacing)}
    if fold(abbr) in others:
        return f"'{abbr}' is already a shortcut in this field."
    return ""


def parse_shortcuts(text: str) -> tuple[dict[str, str], list[str]]:
    """Lines like  nr = No response   /   nr<TAB>No response   /   nr,No response.  Returns (shortcuts, problems)."""
    out: dict[str, str] = {}
    problems: list[str] = []
    for n, raw in enumerate((text or "").replace("\r", "").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+?)\s*(?:=>|->|=|\t|,|;)\s*(.+)$", line) or re.match(r"^(\S+)\s+(.+)$", line)
        if not m:
            problems.append(f"Line {n}: expected  shortcut = text")
            continue
        abbr, exp = m.group(1).strip(), m.group(2).strip().strip('"')
        exp = exp.replace("\\n", "\n")
        err = validate_shortcut(abbr, exp, out)
        if err:
            problems.append(f"Line {n}: {err}")
            continue
        out[abbr] = exp
    return out, problems


def format_shortcuts(shortcuts: dict[str, str]) -> str:
    return "\n".join(f"{a} = {e.replace(chr(10), chr(92) + 'n')}" for a, e in shortcuts.items())


def render_expansion(text: str, multiline: bool = False, now: datetime | None = None) -> tuple[str, int | None]:
    """Fill {today} {date} {time} {now} {year}; `{cursor}` says where the caret goes.  -> (text, caret position or None)."""
    now = now or datetime.now()
    repl = {"today": f"{now:%Y-%m-%d}", "date": f"{now:%Y-%m-%d}", "time": f"{now:%H:%M}",
            "now": f"{now:%Y-%m-%d %H:%M}", "year": f"{now:%Y}"}
    out = re.sub(r"\{(today|date|time|now|year)\}", lambda m: repl[m.group(1)], text)
    if not multiline:
        out = out.replace("\n", " ")
    caret = None
    if "{cursor}" in out:
        caret = out.index("{cursor}")
        out = out.replace("{cursor}", "", 1).replace("{cursor}", "")
    return out, caret


# --------------------------------------------------------------------------- data
@dataclass
class FieldDef:
    """What the program knows about one text box."""
    id: str
    label: str
    group: str = "General"
    mode: str = "value"                                  # value | list | text
    keywords: list[str] = field(default_factory=list)    # defaults (the user may change them)
    shortcuts: dict[str, str] = field(default_factory=dict)
    linked: list[str] = field(default_factory=list)      # settings keys of word lists that are always offered too
    separators: str = ",;"                               # list mode
    sensitive: bool = False                              # never learned / remembered (passport and phone numbers ...)
    default_enabled: bool = True
    description: str = ""


@dataclass
class FieldConfig:
    """The effective setting of one field (defaults + what the user changed)."""
    enabled: bool
    keywords: list[str]
    shortcuts: dict[str, str]
    learn: bool


@dataclass
class Options:
    enabled: bool = True
    auto_popup: bool = True
    min_chars: int = 1
    max_items: int = 8
    match: str = "smart"
    expand_on_space: bool = True
    tab_accepts: bool = True
    learn: bool = True


@dataclass
class Suggestion:
    text: str                  # what is inserted (templates already filled)
    kind: str                  # shortcut | keyword | learned | box
    abbr: str = ""             # shortcut: what the user typed
    score: float = 0.0
    caret: int | None = None   # caret offset inside `text` after inserting (from {cursor})

    @property
    def label(self) -> str:
        first = self.text.split("\n")[0]
        if len(first) < len(self.text) or len(first) > 70:
            first = first[:67] + "…"
        return f"{self.abbr}  →  {first}" if self.kind == "shortcut" else first


@dataclass
class Result:
    start: int                 # the token to replace is text[start:end]
    end: int
    token: str
    items: list[Suggestion]


@dataclass
class Expansion:
    start: int
    end: int
    abbr: str
    text: str
    caret: int | None = None


# --------------------------------------------------------------------------- token under the caret
def locate(text: str, cursor: int, mode: str = "value", separators: str = ",;") -> list[tuple[int, str]]:
    """Candidate tokens ending at the caret, longest first: [(start, token), ...]."""
    cursor = max(0, min(cursor, len(text)))
    before = text[:cursor]
    if mode == "list":
        cut = max((before.rfind(s) for s in separators), default=-1)
        start = cut + 1
        token = before[start:]
        stripped = token.lstrip()
        start += len(token) - len(stripped)
        return [(start, stripped)] if stripped else []
    if mode == "text":
        words = [(m.start(), m.group()) for m in re.finditer(r"\S+", before)]
        if not words or before[-1:].isspace():
            return []
        out = []
        for n in (3, 2, 1):
            if len(words) >= n:
                start = words[-n][0]
                out.append((start, before[start:]))
        return out
    stripped = before.lstrip()
    return [(len(before) - len(stripped), stripped)] if stripped else []


# --------------------------------------------------------------------------- matching
def score_match(q: str, cand: str, mode: str = "smart") -> float:
    """0 = no match, higher = better.  `q` and `cand` must already be folded."""
    if not q or not cand:
        return 0.0
    if cand.startswith(q):
        return 100.0 + 10.0 * len(q) / len(cand)
    if mode == "prefix":
        return 0.0
    words = cand.split(" ")
    if any(w.startswith(q) for w in words[1:]) or (" " in q and (" " + q) in (" " + cand)):
        return 80.0 + 10.0 * len(q) / len(cand)
    if mode == "words":
        return 0.0
    if q in cand:
        return 60.0 + 10.0 * len(q) / len(cand)
    if mode == "contains":
        return 0.0
    if len(q) >= 2 and " " not in q:                    # smart: initials  hro -> Human Resources Officer
        initials = "".join(w[0] for w in words if w)
        if initials.startswith(q):
            return 70.0 + 5.0 * len(q) / max(1, len(initials))
    if len(q) >= 3 and " " not in q:                   # smart: one typo (compared with the start of the candidate)
        head = cand[:len(q) + 1]
        sm = difflib.SequenceMatcher(None, q, head)
        if sm.real_quick_ratio() >= 0.75 and sm.ratio() >= 0.8:
            return 30.0 + 10.0 * sm.ratio()
    return 0.0


# --------------------------------------------------------------------------- learned values
class LearnedStore:
    """What the user typed before, per field (most recent first).  Kept in its own file - never exported with settings."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = {k: [str(x) for x in v if str(x).strip()] for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError) as exc:
            log.error("Could not read %s (%s) - starting with an empty memory", self.path, exc)

    def get(self, field_id: str) -> list[str]:
        with self._lock:
            return list(self.data.get(field_id, []))

    def add(self, field_id: str, value: str) -> bool:
        value = " ".join(value.split())
        key = fold(value)
        with self._lock:
            items = self.data.setdefault(field_id, [])
            if items and fold(items[0]) == key and items[0] == value:
                return False
            items[:] = [value] + [x for x in items if fold(x) != key]
            del items[LEARN_MAX:]
        self._schedule()
        return True

    def clear(self, field_id: str | None = None) -> None:
        with self._lock:
            if field_id is None:
                self.data.clear()
            else:
                self.data.pop(field_id, None)
        self._schedule()

    def count(self, field_id: str) -> int:
        with self._lock:
            return len(self.data.get(field_id, []))

    def _schedule(self) -> None:
        if not self.path:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(1.0, self.save)
            self._timer.daemon = True
            self._timer.start()

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            payload = json.dumps({k: v for k, v in sorted(self.data.items()) if v}, ensure_ascii=False, indent=1)
            tmp = self.path.with_suffix(".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                log.exception("Could not save the autocomplete memory")

    def flush(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self.save()


# --------------------------------------------------------------------------- the service
SOURCE = Callable[["Autocomplete"], list[FieldDef]]


class Autocomplete:
    def __init__(self, settings, learned: LearnedStore | None = None) -> None:
        self.settings = settings
        self.learned = learned if learned is not None else LearnedStore()
        self._defs: dict[str, FieldDef] = {}
        self._sources: list[SOURCE] = []
        self._listeners: list[Callable[[], None]] = []
        self._select_listeners: list[Callable[[str], None]] = []
        self.pending_select: str = ""
        self._cache: dict[str, FieldDef] = {}
        settings.subscribe("autocomplete.", lambda k, v: self._changed())

    # ------------------------------------------------------------------ registry
    def register(self, *defs: FieldDef) -> None:
        for d in defs:
            self._defs[d.id] = d
        self._changed(refresh_only=True)

    def add_source(self, source: SOURCE) -> None:
        self._sources.append(source)
        self._changed(refresh_only=True)

    def _all(self) -> dict[str, FieldDef]:
        out = dict(self._defs)
        for src in self._sources:
            try:
                for d in src(self):
                    out.setdefault(d.id, d)
            except Exception:
                log.exception("An autocomplete field source failed")
        return out

    def fields(self) -> list[FieldDef]:
        """Every known field, grouped and sorted for the settings page."""
        return sorted(self._all().values(), key=lambda d: (d.group.lower(), d.label.lower(), d.id))

    def definition(self, field_id: str) -> FieldDef:
        d = self._all().get(field_id)
        if d is None:
            d = FieldDef(field_id, field_id.split(".")[-1].replace("_", " ").title())
        return d

    # ------------------------------------------------------------------ change notification
    def on_change(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _changed(self, refresh_only: bool = False) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("Autocomplete listener failed")

    def on_select(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._select_listeners.append(callback)
        return lambda: self._select_listeners.remove(callback) if callback in self._select_listeners else None

    def select_field(self, field_id: str) -> None:
        """Ask the settings page to show this field (used by the right-click menu of a box)."""
        self.pending_select = field_id
        for cb in list(self._select_listeners):
            try:
                cb(field_id)
            except Exception:
                log.exception("Autocomplete select listener failed")

    # ------------------------------------------------------------------ options
    def options(self) -> Options:
        s = self.settings
        match = str(s.get("autocomplete.match", "smart"))
        return Options(
            enabled=s.get_bool("autocomplete.enabled", True), auto_popup=s.get_bool("autocomplete.auto_popup", True),
            min_chars=max(0, s.get_int("autocomplete.min_chars", 1)), max_items=max(1, s.get_int("autocomplete.max_items", 8)),
            match=match if match in MATCH_ORDER else "smart", expand_on_space=s.get_bool("autocomplete.expand_on_space", True),
            tab_accepts=s.get_bool("autocomplete.tab_accepts", True), learn=s.get_bool("autocomplete.learn", True))

    def master(self) -> bool:
        return self.settings.get_bool("autocomplete.enabled", True)

    def set_master(self, on: bool) -> None:
        self.settings.set("autocomplete.enabled", bool(on))

    # ------------------------------------------------------------------ per-field configuration
    def _overrides(self) -> dict[str, dict]:
        data = self.settings.get("autocomplete.fields", {})
        return data if isinstance(data, dict) else {}

    def _override(self, field_id: str) -> dict:
        o = self._overrides().get(field_id)
        return o if isinstance(o, dict) else {}

    def config(self, field_id: str) -> FieldConfig:
        d, o = self.definition(field_id), self._override(field_id)
        keywords = o["keywords"] if isinstance(o.get("keywords"), list) else d.keywords
        shortcuts = o["shortcuts"] if isinstance(o.get("shortcuts"), dict) else d.shortcuts
        return FieldConfig(
            enabled=bool(o["enabled"]) if "enabled" in o else d.default_enabled,
            keywords=[str(k) for k in keywords], shortcuts={str(a): str(e) for a, e in shortcuts.items()},
            learn=False if d.sensitive else (bool(o["learn"]) if "learn" in o else True))

    def is_enabled(self, field_id: str) -> bool:
        return self.master() and self.config(field_id).enabled

    def is_customised(self, field_id: str) -> bool:
        return bool(self._override(field_id))

    def _store(self, field_id: str, **changes: Any) -> None:
        data = self._overrides()
        data = {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
        cur = data.setdefault(field_id, {})
        d = self.definition(field_id)
        for k, v in changes.items():
            default = {"enabled": d.default_enabled, "keywords": d.keywords, "shortcuts": d.shortcuts, "learn": True}[k]
            if v == default:
                cur.pop(k, None)                         # equal to the default: do not keep a copy that would hide updates
            else:
                cur[k] = v
        if not cur:
            data.pop(field_id, None)
        self.settings.set("autocomplete.fields", data)

    def set_enabled(self, field_id: str, on: bool) -> None:
        self._store(field_id, enabled=bool(on))

    def set_many_enabled(self, field_ids: Iterable[str], on: bool) -> None:
        data = {k: dict(v) for k, v in self._overrides().items() if isinstance(v, dict)}
        for fid in field_ids:
            d = self.definition(fid)
            cur = data.setdefault(fid, {})
            if bool(on) == d.default_enabled:
                cur.pop("enabled", None)
            else:
                cur["enabled"] = bool(on)
            if not cur:
                data.pop(fid, None)
        self.settings.set("autocomplete.fields", data)

    def set_learn(self, field_id: str, on: bool) -> None:
        self._store(field_id, learn=bool(on))

    def set_keywords(self, field_id: str, keywords: Iterable[str]) -> None:
        self._store(field_id, keywords=parse_keywords("\n".join(keywords)))

    def add_keyword(self, field_id: str, keyword: str) -> bool:
        keyword = " ".join((keyword or "").split())[:MAX_KEYWORD_LEN]
        cfg = self.config(field_id)
        if not keyword or fold(keyword) in {fold(k) for k in cfg.keywords}:
            return False
        self.set_keywords(field_id, cfg.keywords + [keyword])
        return True

    def set_shortcuts(self, field_id: str, shortcuts: dict[str, str]) -> None:
        self._store(field_id, shortcuts=dict(shortcuts))

    def add_shortcut(self, field_id: str, abbr: str, expansion: str) -> str:
        """'' on success, otherwise the reason."""
        cur = self.config(field_id).shortcuts
        err = validate_shortcut(abbr, expansion, cur)
        if err:
            return err
        self.set_shortcuts(field_id, {**cur, abbr.strip(): expansion.strip()})
        return ""

    def reset_field(self, field_id: str) -> None:
        data = {k: dict(v) for k, v in self._overrides().items() if isinstance(v, dict) and k != field_id}
        self.settings.set("autocomplete.fields", data)

    def reset_all(self) -> None:
        self.settings.set("autocomplete.fields", {})

    # ------------------------------------------------------------------ candidates
    def keywords_for(self, field_id: str, extra: Iterable[str] = ()) -> list[str]:
        """Keywords of the field, its linked word lists and `extra` (values the box itself knows), no duplicates."""
        d, cfg = self.definition(field_id), self.config(field_id)
        out, seen = [], set()
        sources = [cfg.keywords]
        for key in d.linked:
            val = self.settings.get(key, [])
            sources.append(val if isinstance(val, list) else [])
        sources.append(list(extra))
        for src in sources:
            for k in src:
                k = " ".join(str(k).split())
                if k and fold(k) not in seen:
                    seen.add(fold(k))
                    out.append(k)
        return out

    # ------------------------------------------------------------------ suggestions
    def suggest(self, field_id: str, text: str, cursor: int | None = None, extra: Iterable[str] = (),
                box_values: Iterable[str] = (), force: bool = False, limit: int | None = None,
                multiline: bool = False, now: datetime | None = None) -> Result | None:
        """The suggestions for the token before the caret, or None.  `force` (Ctrl+Space) lists everything even when the
        token is empty / shorter than the minimum."""
        if not self.is_enabled(field_id):
            return None
        opts, d, cfg = self.options(), self.definition(field_id), self.config(field_id)
        cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
        limit = limit or opts.max_items
        extra, box_values = list(extra), list(box_values)

        hit = self._shortcut_at(cfg, d, text, cursor)
        pool = self._pool(field_id, cfg, extra, box_values)
        windows = locate(text, cursor, d.mode, d.separators)
        if force and not windows:
            windows = [(cursor, "")]
        for start, token in windows:
            q = fold(token)
            if not force and len(q) < opts.min_chars and not hit:
                continue
            items: list[Suggestion] = []
            if hit and hit[0] >= start:
                items.append(self._shortcut_item(hit, multiline, now))
            items += self._rank(pool, q, token, opts.match, force and not q, multiline, now)
            seen: set[str] = set()
            uniq = []
            for it in items:
                key = (it.kind == "shortcut", fold(it.text))
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(it)
            if uniq:
                return Result(start, cursor, token, uniq[:limit])
        if hit and not windows:
            return Result(hit[0], cursor, text[hit[0]:cursor], [self._shortcut_item(hit, multiline, now)])
        return None

    def _pool(self, field_id: str, cfg: FieldConfig, extra: list[str], box_values: list[str]) -> list[tuple[str, str, float]]:
        """(text, kind, bonus) for every non-shortcut candidate; the same text appears once."""
        opts = self.options()
        learned = self.learned.get(field_id) if (opts.learn and cfg.learn) else []
        bonus = {fold(v): 15.0 * (1 - i / max(1, len(learned))) for i, v in enumerate(learned)}
        pool: dict[str, tuple[str, str, float]] = {}

        def put(text: str, kind: str) -> None:
            text = " ".join(str(text).split())
            key = fold(text)
            if text and key not in pool:
                pool[key] = (text, kind, bonus.get(key, 0.0))

        for k in self.keywords_for(field_id, extra):
            put(k, "keyword")
        for e in cfg.shortcuts.values():                     # the text a shortcut stands for is a keyword too
            if "\n" not in e and "{" not in e and len(e) <= 60:
                put(e, "keyword")
        for v in learned:
            put(v, "learned")
        for v in box_values:
            put(v, "box")
        return list(pool.values())

    def _rank(self, pool, q: str, token: str, match: str, everything: bool, multiline: bool, now) -> list[Suggestion]:
        scored = []
        for order, (text, kind, bonus) in enumerate(pool):
            folded = fold(text)
            if everything:
                s = 1.0 + bonus
            else:
                if folded == q:
                    continue                                   # already typed in full: nothing to complete
                s = score_match(q, folded, match)
                if s <= 0:
                    continue
                s += bonus
            scored.append((-s, order, Suggestion(text, kind, score=s)))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [s for _, _, s in scored]

    def _shortcut_at(self, cfg: FieldConfig, d: FieldDef, text: str, cursor: int) -> tuple[int, str, str] | None:
        """(start, abbr, expansion) when a shortcut is typed right before the caret.  Longest abbreviation wins."""
        if not cfg.shortcuts:
            return None
        before = text[:cursor]
        low = fold(before)
        best: tuple[int, str, str] | None = None
        by_fold = {fold(a): (a, e) for a, e in cfg.shortcuts.items()}
        for start, token in locate(text, cursor, d.mode, d.separators):
            if token[-1:].isspace():
                continue                                     # "se " is not the shortcut "se" (it was already typed past)
            key = fold(token)
            if key in by_fold and (best is None or len(token) > cursor - best[0]):
                a, e = by_fold[key]
                best = (start, a, e)
        for key, (a, e) in by_fold.items():                  # ";sig" / "@g": symbol shortcuts work mid-word
            if key and not _WORD_CHARS.match(key[0]) and len(before) >= len(a) and low.endswith(key):
                start = len(before) - len(a)
                if before[start:].casefold() == a.casefold() and (best is None or len(a) > cursor - best[0]):
                    best = (start, a, e)
        return best

    def _shortcut_item(self, hit: tuple[int, str, str], multiline: bool, now) -> Suggestion:
        _, abbr, exp = hit
        text, caret = render_expansion(exp, multiline, now)
        return Suggestion(text, "shortcut", abbr=abbr, score=1000.0, caret=caret)

    # ------------------------------------------------------------------ expansion / accepting
    def expansion(self, field_id: str, text: str, cursor: int, multiline: bool = False,
                  now: datetime | None = None) -> Expansion | None:
        """A shortcut typed right before the caret (called when the user presses Space)."""
        if not self.is_enabled(field_id):
            return None
        d, cfg = self.definition(field_id), self.config(field_id)
        hit = self._shortcut_at(cfg, d, text, cursor)
        if not hit:
            return None
        start, abbr, exp = hit
        out, caret = render_expansion(exp, multiline, now)
        return Expansion(start, cursor, abbr, out, caret)

    @staticmethod
    def accept(text: str, cursor: int, result: Result, suggestion: Suggestion) -> tuple[str, int]:
        """Put `suggestion` in place of the token.  -> (new text, new caret position)."""
        cursor = max(0, min(cursor, len(text)))
        new = text[:result.start] + suggestion.text + text[cursor:]
        caret = result.start + (suggestion.caret if suggestion.caret is not None else len(suggestion.text))
        return new, caret

    @staticmethod
    def apply_expansion(text: str, exp: Expansion) -> tuple[str, int]:
        new = text[:exp.start] + exp.text + text[exp.end:]
        return new, exp.start + (exp.caret if exp.caret is not None else len(exp.text))

    # ------------------------------------------------------------------ learning
    def learn(self, field_id: str, value: str) -> int:
        """Remember what was typed in a finished box.  Returns how many values were stored."""
        if not self.master() or not self.options().learn:
            return 0
        d, cfg = self.definition(field_id), self.config(field_id)
        if not cfg.enabled or not cfg.learn or d.sensitive or d.mode == "text":
            return 0
        parts = [p.strip() for p in re.split("[" + re.escape(d.separators) + "]", value)] if d.mode == "list" else [value.strip()]
        n = 0
        for p in parts:
            p = " ".join(p.split())
            if 2 <= len(p) <= 80 and any(c.isalpha() for c in p) and p.casefold() not in {a.casefold() for a in cfg.shortcuts}:
                n += bool(self.learned.add(field_id, p))
        return n

    def learned_count(self, field_id: str) -> int:
        return self.learned.count(field_id)

    def clear_learned(self, field_id: str | None = None) -> None:
        self.learned.clear(field_id)
        self._changed()

    def flush(self) -> None:
        self.learned.flush()


# --------------------------------------------------------------------------- settings declarations
AUTOCOMPLETE_OPTS = [
    Opt("autocomplete.enabled", True, "Autocomplete in text boxes", "bool", section="Master switch",
        help="Off = no suggestions and no shortcut expansion anywhere (your keywords and shortcuts are kept). "
             "Below you choose, box by box, where it works."),
    Opt("autocomplete.auto_popup", True, "Show suggestions while I type", "bool", section="Behaviour",
        help="Off = suggestions only appear when you press Ctrl+Space (shortcuts still expand)."),
    Opt("autocomplete.min_chars", 1, "Start suggesting after this many characters", "int", min=0, max=6, section="Behaviour"),
    Opt("autocomplete.max_items", 8, "Suggestions shown at most", "int", min=3, max=20, section="Behaviour"),
    Opt("autocomplete.match", "smart", "How to match what you type", "choice", choices=MATCH_MODES, section="Behaviour"),
    Opt("autocomplete.tab_accepts", True, "Tab accepts the highlighted suggestion", "bool", section="Keys",
        help="Always: Up / Down choose, Enter accepts a suggestion you chose with the arrows (so Enter still submits a "
             "form when you did not), Esc closes, Ctrl+Space opens the list."),
    Opt("autocomplete.expand_on_space", True, "Pressing Space right after a shortcut expands it", "bool", section="Keys",
        help="Backspace straight after an expansion puts the shortcut back."),
    Opt("autocomplete.learn", True, "Remember what I type and offer it again", "bool", section="Memory",
        help="Names, groups, companies ... typed in a box are suggested next time. Stored only on this computer "
             "(data/autocomplete_learned.json). Boxes holding passport, phone or visa numbers are never remembered."),
]
AUTOCOMPLETE_STATE = [
    Opt("autocomplete.fields", {}, "Per-field choices (keywords, shortcuts, on/off)", "custom"),
]

# --------------------------------------------------------------------------- built-in fields
JOB_TITLES = [
    "Accountant", "Administrator", "Architect", "Assistant Manager", "Barista", "Bricklayer", "Business Analyst", "Cashier",
    "Chef", "Cleaner", "Cook", "Customer Service Agent", "Data Analyst", "Designer", "Doctor", "Driver", "Electrician",
    "Engineer", "Foreman", "General Worker", "HR Officer", "Heavy Equipment Operator", "Housekeeper", "Interpreter",
    "IT Support", "Labourer", "Mechanic", "Nurse", "Office Manager", "Operator", "Painter", "Plumber", "Project Manager",
    "Receptionist", "Sales Representative", "Security Guard", "Site Engineer", "Software Developer", "Storekeeper",
    "Supervisor", "Technician", "Translator", "Warehouse Operator", "Waiter", "Welder"]
NATIONALITIES = [
    "Afghan", "American", "Bangladeshi", "British", "Chinese", "Egyptian", "Ethiopian", "Filipino", "Ghanaian", "Indian",
    "Indonesian", "Iranian", "Iraqi", "Jordanian", "Kenyan", "Lebanese", "Malaysian", "Moroccan", "Nepalese", "Nigerian",
    "Pakistani", "Palestinian", "Russian", "Saudi", "Sri Lankan", "Sudanese", "Syrian", "Tunisian", "Turkish", "Ugandan",
    "Ukrainian", "Vietnamese", "Yemeni"]
EMAIL_SHORTCUTS = {"@g": "@gmail.com", "@o": "@outlook.com", "@h": "@hotmail.com", "@y": "@yahoo.com", "@i": "@icloud.com"}
NOTE_SHORTCUTS = {"nr": "No response", "na": "No answer, will try again tomorrow", "cb": "Asked to call back later",
                  "wa": "Sent a WhatsApp message", "ok": "Confirmed, no problems", "vm": "Left a voicemail",
                  ";d": "{today}", ";t": "{time}"}


def builtin_fields() -> list[FieldDef]:
    visa = "Visa Tracker"
    dp = "DocPacker"
    ext = "Extract"
    return [
        FieldDef("visa.name", "Person: name", visa, description="Full name of the person."),
        FieldDef("visa.passport_no", "Person: passport number", visa, sensitive=True, default_enabled=False),
        FieldDef("visa.nationality", "Person: nationality", visa, keywords=list(NATIONALITIES)),
        FieldDef("visa.phone", "Person: phone / WhatsApp", visa, sensitive=True, default_enabled=False),
        FieldDef("visa.email", "Person: e-mail", visa, sensitive=True, shortcuts=dict(EMAIL_SHORTCUTS),
                 description="Type  john@g  and press Space or Tab to get john@gmail.com."),
        FieldDef("visa.position", "Person: position", visa, keywords=list(JOB_TITLES), linked=["match.job_title_keywords"]),
        FieldDef("visa.group", "Person: group / batch", visa, description="Groups already in your list are offered too."),
        FieldDef("visa.visa_no", "Visa: number", visa, sensitive=True, default_enabled=False),
        FieldDef("visa.visa_type", "Visa: type", visa, linked=["visa.visa_types"]),
        FieldDef("visa.flight_no", "Ticket: flight number", visa, sensitive=True, default_enabled=False),
        FieldDef("visa.notes", "Person: notes", visa, mode="text", shortcuts=dict(NOTE_SHORTCUTS),
                 description="Free text: shortcuts such as nr → No response."),
        FieldDef("visa.log_note", "Contact log: note", visa, mode="text", shortcuts=dict(NOTE_SHORTCUTS)),
        FieldDef("docpacker.name", "Cover page: full name", dp),
        FieldDef("docpacker.phone", "Cover page: phone / WhatsApp", dp, sensitive=True, default_enabled=False),
        FieldDef("workflows.name", "Workflow: name", "Workflows"),
        FieldDef("workflows.description", "Workflow: what it is for", "Workflows", mode="text"),
        FieldDef("workflows.param", "Workflow: text settings of a brick (rename pattern, message ...)", "Workflows",
                 shortcuts={";d": "{date}", ";n": "{n}"}),
        FieldDef("whatsapp.search", "WhatsApp import: search box", "WhatsApp import"),
        FieldDef("match.search", "Name matcher: search for a name", "Match & Sync"),
        FieldDef("ocr_match.patterns", "Find text: patterns / names", ext, mode="list", separators="\n",
                 default_enabled=False, description="One regex pattern or name per line to search for."),
        FieldDef("passport.mrz", "Passports: MRZ text", ext, mode="text", sensitive=True,
                 default_enabled=False, description="Pasted 2-line MRZ text to validate."),
        FieldDef("similarity_search.query", "Similarity Search: search query", ext, mode="list", separators="\n",
                 default_enabled=False, description="Text, names, or phrases to search for (one per line)."),
        FieldDef("similarity_search.exclude", "Similarity Search: exclude terms", ext, mode="list", separators=",; ",
                 default_enabled=False, description="Words or terms to exclude from search results."),
        FieldDef("similarity_search.extensions", "Similarity Search: file extensions", ext, mode="list", separators=",; ",
                 keywords=["pdf", "docx", "doc", "xlsx", "jpg", "jpeg", "png", "txt"], default_enabled=False,
                 description="Optional file extension filter (e.g. pdf, png)."),
        FieldDef("similarity_search.pasted_title", "Similarity Search: pasted text title", ext,
                 default_enabled=False, description="Title for a pasted text snippet added to the search corpus."),
        FieldDef("similarity_search.pasted_text", "Similarity Search: pasted text body", ext, mode="text",
                 default_enabled=False, description="Pasted text snippet content added to the search corpus."),
        FieldDef("badge_extractor.ar_name", "Badge Extractor: Arabic name", ext,
                 default_enabled=False, description="Arabic name on the ID badge."),
        FieldDef("badge_extractor.en_name", "Badge Extractor: English name", ext,
                 default_enabled=False, description="English / transliterated name on the ID badge."),
        FieldDef("badge_extractor.job_title", "Badge Extractor: job title", ext, keywords=list(JOB_TITLES),
                 linked=["match.job_title_keywords"], default_enabled=False, description="Job title on the ID badge."),
        FieldDef("badge_extractor.badge_no", "Badge Extractor: badge number", ext, sensitive=True,
                 default_enabled=False, description="ID badge number."),
        FieldDef("badge_extractor.filename_template", "Badge Extractor: filename template", ext,
                 default_enabled=False,
                 description="Output filename pattern (e.g. {ar_name}-{en_name}-{badge_no}-{job_title})."),
    ]


def _spec_defaults(spec: dict, prefix: str = "field", group: str = "CV Sorter fields",
                   label_prefix: str = "", default_enabled: bool | None = None) -> FieldDef:
    kind, key = str(spec.get("kind", "")), str(spec.get("key", ""))
    label = str(spec.get("label") or key.replace("_", " ").title())
    d = FieldDef(f"{prefix}.{key}", f"{label_prefix}{label}", group)
    if kind == "job_title" or key in ("job_title", "title", "position"):
        d.keywords, d.linked = list(JOB_TITLES), ["match.job_title_keywords"]
    elif kind == "company":
        d.linked = ["match.known_companies"]
    elif kind == "email":
        d.shortcuts, d.sensitive = dict(EMAIL_SHORTCUTS), True
    elif kind == "phone":
        d.sensitive, d.default_enabled = True, False
    elif kind == "keywords":
        d.mode = "list"
        d.keywords = [k.strip() for k in re.split(r"[,;\n]+", str(spec.get("pattern", ""))) if k.strip()]
    elif kind in ("today", "regex", "label", "builtin"):
        d.default_enabled = kind not in ("today", "regex")
    if default_enabled is not None:
        d.default_enabled = default_enabled
    return d


def default_sources() -> list[SOURCE]:
    """Fields that depend on the user's own settings: extraction fields and custom fields."""
    def cv_fields(ac: "Autocomplete") -> list[FieldDef]:
        out: dict[str, FieldDef] = {}
        specs = ac.settings.get("cv_sorter.fields", [])
        for spec in specs if isinstance(specs, list) else []:
            if isinstance(spec, dict) and spec.get("key") and f"field.{spec['key']}" not in out:
                out[f"field.{spec['key']}"] = _spec_defaults(spec)
        return list(out.values())

    def screenshot_fields(ac: "Autocomplete") -> list[FieldDef]:
        out: dict[str, FieldDef] = {}
        specs = ac.settings.get("screenshot.fields", [])
        for spec in specs if isinstance(specs, list) else []:
            if isinstance(spec, dict) and spec.get("key") and f"screenshot.{spec['key']}" not in out:
                out[f"screenshot.{spec['key']}"] = _spec_defaults(
                    spec, prefix="screenshot", group="Extract", label_prefix="Screenshots: ", default_enabled=False)
        return list(out.values())

    def custom(setting: str, prefix: str, group: str) -> SOURCE:
        def source(ac: "Autocomplete") -> list[FieldDef]:
            labels = ac.settings.get(setting, [])
            return [FieldDef(f"{prefix}.{slugify(str(l))}", f"Custom: {l}", group)
                    for l in (labels if isinstance(labels, list) else []) if str(l).strip()]
        return source

    return [cv_fields, screenshot_fields, custom("visa.custom_fields", "visa.custom", "Visa Tracker"),
            custom("docpacker.custom_fields", "docpacker.custom", "DocPacker")]


def create(settings, learned_path: Path | None = None) -> Autocomplete:
    """The service the App uses: built-in fields + the sources that follow the user's settings."""
    ac = Autocomplete(settings, LearnedStore(learned_path))
    ac.register(*builtin_fields())
    for src in default_sources():
        ac.add_source(src)
    return ac
