"""Visa Tracker core (no Tk): people, visa deadlines, the follow-up rhythm, warnings and safe storage.

    store = Store(folder)                       # people.json + files/ + backups/ inside `folder`
    p = store.add(Person(name="Ali Khan", visa_issue="2026-11-01"))
    pol = Policy()                              # 90 days, warn at 30 / 14 / 7 ...
    days_left(p, today, pol)                    # 76
    next_due(p, today, pol)                     # date the next follow-up is due
    todo(p, today, pol)                         # ["No contact logged yet"] -> needs action today
    store.log_followup(p.id, "whatsapp", "sent", "Sent the reminder")

Everything that depends on "today" takes it as an argument, so it is easy to test.  Dates are stored as
ISO strings ("2026-11-15"), times of contacts as ISO date-times, so the file is easy to read and edit by hand.
Unknown keys found in the file are kept and written back (a newer version can add fields safely).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from .dates import add_months, find_dates

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
UNDO_LIMIT = 40

# value, label - the lists the UI offers (add a line here and the whole tool understands it)
STATUSES = [("active", "Waiting to arrive"), ("arrived", "Arrived"), ("cancelled", "Cancelled"),
            ("missed", "Visa missed / expired")]
TICKET_STATES = [("none", "No ticket yet"), ("requested", "Ticket requested"), ("booked", "Ticket booked"),
                 ("sent", "Ticket sent to the person")]
CHANNELS = [("call", "📞 Call"), ("whatsapp", "💬 WhatsApp"), ("email", "✉ Email"), ("sms", "📱 SMS"),
            ("meeting", "🤝 In person"), ("note", "📝 Note (not a contact)")]
OUTCOMES = [("reached", "Spoke / confirmed"), ("sent", "Message sent"), ("replied", "Replied"),
            ("no_answer", "No answer"), ("other", "Other")]
DOC_KINDS = [("visa", "Visa"), ("ticket", "Ticket"), ("passport", "Passport"), ("other", "Other")]

LEVEL_ORDER = {"overdue": 0, "critical": 1, "urgent": 2, "nodate": 3, "soon": 4, "ok": 5,
               "arrived": 8, "cancelled": 9, "missed": 9}
LEVEL_LABEL = {"overdue": "Overdue", "critical": "Critical", "urgent": "Urgent", "soon": "Soon", "ok": "OK",
               "nodate": "No visa date", "arrived": "Arrived", "cancelled": "Cancelled", "missed": "Missed"}
LEVEL_ICON = {"overdue": "🔴", "critical": "🔴", "urgent": "🟠", "soon": "🟡", "ok": "🟢", "nodate": "⚪",
              "arrived": "✅", "cancelled": "⛔", "missed": "❌"}

DEFAULT_CADENCE = [(60, 14), (30, 7), (14, 3), (0, 1)]


class VisaError(Exception):
    """A user-facing problem (message is friendly)."""


# --------------------------------------------------------------------------- dates
def to_date(value: Any) -> dt.date | None:
    """ISO text / date / datetime -> date (None for anything else)."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def iso(d: dt.date | None) -> str:
    return d.isoformat() if d else ""


def parse_date_input(text: str, today: dt.date | None = None, order: str = "dmy") -> dt.date | None:
    """What a person types into a date box.

    2026-11-15 | 15/11/2026 | 15 Nov 2026 | Nov 15, 2026 | today | tomorrow | yesterday | +30 (days from today) |
    -5 | +2w | +1m.   `order` ("dmy" / "mdy") decides 03/04/2026.
    """
    s = (text or "").strip().lower()
    today = today or dt.date.today()
    if not s:
        return None
    if s in ("today", "now", "t"):
        return today
    if s in ("tomorrow", "tmrw"):
        return today + dt.timedelta(days=1)
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    m = re.fullmatch(r"([+-])\s*(\d+)\s*([dwm]?)", s)
    if m:
        n = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
        unit = m.group(3) or "d"
        return add_months(today, n) if unit == "m" else today + dt.timedelta(days=n * (7 if unit == "w" else 1))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + (2000 if y < 70 else 1900) if y < 100 else y
        day, mon = (b, a) if order == "mdy" else (a, b)
        if mon > 12 >= day:                                  # impossible in the chosen order: swap
            day, mon = mon, day
        try:
            return dt.date(y, mon, day)
        except ValueError:
            return None
    found = find_dates(text, allow2=True)
    return found[0] if found else None


def human_days(n: int | None) -> str:
    """12 -> '12 days', 1 -> '1 day', 0 -> 'today', -3 -> '3 days ago'."""
    if n is None:
        return "-"
    if n == 0:
        return "today"
    unit = "day" if abs(n) == 1 else "days"
    return f"{n} {unit}" if n > 0 else f"{-n} {unit} ago"


# --------------------------------------------------------------------------- model
@dataclass
class Person:
    id: str = ""
    name: str = ""
    passport_no: str = ""
    passport_expiry: str = ""
    nationality: str = ""
    phone: str = ""
    email: str = ""
    position: str = ""
    group: str = ""                  # batch / project / agency - to filter and report by
    visa_no: str = ""
    visa_type: str = ""
    visa_issue: str = ""             # date the visa was issued
    visa_days: int = 0               # entry window in days (0 = use the default of the settings)
    visa_deadline: str = ""          # optional fixed last entry date (wins over issue + days)
    ticket_status: str = "none"
    flight_date: str = ""
    flight_no: str = ""
    status: str = "active"           # see STATUSES
    arrived_on: str = ""
    next_followup: str = ""          # explicit date chosen by the user ("remind me on ...")
    notes: str = ""
    extra: dict = field(default_factory=dict)         # user-defined fields (Settings > Visa Tracker > Extra fields)
    docs: list = field(default_factory=list)          # stored files: visa page, ticket ...
    followups: list = field(default_factory=list)     # the log: who was contacted, when, how, what was said
    added_on: str = ""
    updated_on: str = ""
    unknown: dict = field(default_factory=dict, repr=False)   # keys of newer versions, kept as they are

    @classmethod
    def from_dict(cls, data: dict) -> "Person":
        known = {f.name for f in fields(cls)} - {"unknown"}
        kw = {k: copy.deepcopy(v) for k, v in data.items() if k in known}
        p = cls(**kw)
        p.unknown = {k: copy.deepcopy(v) for k, v in data.items() if k not in known}
        try:
            p.visa_days = max(0, int(p.visa_days or 0))
        except (TypeError, ValueError):
            p.visa_days = 0
        for attr in ("extra",):
            if not isinstance(getattr(p, attr), dict):
                setattr(p, attr, {})
        for attr in ("docs", "followups"):
            if not isinstance(getattr(p, attr), list):
                setattr(p, attr, [])
        return p

    def to_dict(self) -> dict:
        out = {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self) if f.name != "unknown"}
        out.update(copy.deepcopy(self.unknown))
        return out

    @property
    def first_name(self) -> str:
        return (self.name.split() or [""])[0]

    def doc(self, kind: str) -> dict | None:
        """The newest stored file of `kind`."""
        found = [d for d in self.docs if d.get("kind") == kind]
        return found[-1] if found else None


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def make_followup(channel: str, outcome: str = "", note: str = "", by: str = "", when: dt.datetime | None = None) -> dict:
    return {"id": new_id(), "when": (when or dt.datetime.now()).isoformat(timespec="seconds"), "channel": channel,
            "outcome": outcome, "note": note, "by": by}


# --------------------------------------------------------------------------- rules (settings -> numbers)
def parse_cadence(text: str) -> list[tuple[int, int]]:
    """'60:14, 30:7, 14:3, 0:1' -> [(60, 14), (30, 7), (14, 3), (0, 1)]  (from N days left on: contact every K days)."""
    pairs: list[tuple[int, int]] = []
    for part in re.split(r"[,;\n]+", text or ""):
        m = re.fullmatch(r"\s*(-?\d+)\s*[:=]\s*(\d+)\s*", part)
        if m and int(m.group(2)) >= 1:
            pairs.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(pairs), reverse=True) or list(DEFAULT_CADENCE)


@dataclass
class Policy:
    default_days: int = 90            # entry window of a visa
    inclusive: bool = False           # the issue date counts as day 1
    critical: int = 7                 # red from this many days left
    urgent: int = 14                  # orange
    warn: int = 30                    # yellow
    cadence: list = field(default_factory=lambda: list(DEFAULT_CADENCE))
    retry_days: int = 1               # after "no answer"
    ticket_warn_days: int = 21        # warn when there is still no ticket this close to the deadline
    min_passport_months: int = 6

    @classmethod
    def from_settings(cls, s) -> "Policy":
        return cls(default_days=max(1, s.get_int("visa.default_days", 90)), inclusive=s.get_bool("visa.inclusive"),
                   critical=s.get_int("visa.critical_days", 7), urgent=s.get_int("visa.urgent_days", 14),
                   warn=s.get_int("visa.warn_days", 30), cadence=parse_cadence(str(s.get("visa.cadence", ""))),
                   retry_days=max(1, s.get_int("visa.retry_days", 1)),
                   ticket_warn_days=s.get_int("visa.ticket_warn_days", 21),
                   min_passport_months=s.get_int("passport.min_months", 6))

    def interval_for(self, days: int | None) -> int:
        """How often (in days) this person should be contacted when `days` are left."""
        rules = sorted(self.cadence or DEFAULT_CADENCE, reverse=True)
        if days is None:
            return rules[0][1]
        for threshold, every in rules:
            if days >= threshold:
                return every
        return rules[-1][1]


# --------------------------------------------------------------------------- derived facts
def deadline_of(p: Person, pol: Policy) -> dt.date | None:
    fixed = to_date(p.visa_deadline)
    if fixed:
        return fixed
    issued = to_date(p.visa_issue)
    if not issued:
        return None
    n = p.visa_days or pol.default_days
    return issued + dt.timedelta(days=n - (1 if pol.inclusive else 0))


def days_left(p: Person, today: dt.date, pol: Policy) -> int | None:
    d = deadline_of(p, pol)
    return (d - today).days if d else None


def level_of(p: Person, today: dt.date, pol: Policy) -> str:
    """overdue | critical | urgent | soon | ok | nodate  - or the closed status (arrived / cancelled / missed)."""
    if p.status != "active":
        return p.status if p.status in LEVEL_ORDER else "cancelled"
    n = days_left(p, today, pol)
    if n is None:
        return "nodate"
    if n < 0:
        return "overdue"
    if n <= pol.critical:
        return "critical"
    if n <= pol.urgent:
        return "urgent"
    if n <= pol.warn:
        return "soon"
    return "ok"


def contacts(p: Person) -> list[dict]:
    """Log entries that count as an attempt to reach the person (not notes, not automatic entries)."""
    return [f for f in p.followups if f.get("channel") not in ("note", "system", "") and f.get("when")]


def _when(f: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(f.get("when")))
    except ValueError:
        return None


def last_contact(p: Person) -> dt.datetime | None:
    stamps = [w for w in (_when(f) for f in contacts(p)) if w]
    return max(stamps) if stamps else None


def last_contact_entry(p: Person) -> dict | None:
    stamped = [(w, f) for f in contacts(p) if (w := _when(f))]
    return max(stamped, key=lambda t: t[0])[1] if stamped else None


def next_due(p: Person, today: dt.date, pol: Policy) -> dt.date | None:
    """The day the next follow-up is due (None for people who are no longer waiting)."""
    if p.status != "active":
        return None
    explicit = to_date(p.next_followup)
    if explicit:
        return explicit
    last = last_contact(p)
    if last is None:
        return min(to_date(p.added_on) or today, today)          # never contacted: due since they were added
    entry = last_contact_entry(p) or {}
    every = pol.retry_days if entry.get("outcome") == "no_answer" else pol.interval_for(days_left(p, today, pol))
    return last.date() + dt.timedelta(days=every)


def todo(p: Person, today: dt.date, pol: Policy) -> list[str]:
    """What needs doing today (empty = nothing).  This is what the badge and the 'To do today' filter count."""
    if p.status != "active":
        return []
    out: list[str] = []
    due = next_due(p, today, pol)
    if due and due <= today:
        if last_contact(p) is None:
            out.append("No contact logged yet")
        else:
            late = (today - due).days
            out.append("Follow-up due today" if late == 0 else f"Follow-up overdue by {late} day{'s' if late != 1 else ''}")
    n = days_left(p, today, pol)
    if n is not None and n < 0:
        out.append("Visa deadline has passed - mark as arrived or missed")
    fd = to_date(p.flight_date)
    if fd and fd < today:
        out.append("Flight date has passed - confirm that they arrived")
    return out


@dataclass
class Issue:
    level: str                       # err | warn | info
    text: str


def issues(p: Person, today: dt.date, pol: Policy) -> list[Issue]:
    """Everything worth a look on one person (shown as alerts in the detail panel)."""
    if p.status != "active":
        return []
    out: list[Issue] = []
    n, dl = days_left(p, today, pol), deadline_of(p, pol)
    if dl is None:
        out.append(Issue("warn", "No visa date yet - enter the issue date (or the last entry date)."))
    elif n < 0:
        out.append(Issue("err", f"The visa window ended {-n} day{'s' if n != -1 else ''} ago."))
    elif n <= pol.critical:
        out.append(Issue("err", f"Only {human_days(n)} left to enter the country."))
    fd = to_date(p.flight_date)
    if fd and dl and fd > dl:
        out.append(Issue("err", f"The flight ({fd:%d-%b-%Y}) is AFTER the visa deadline ({dl:%d-%b-%Y})."))
    if fd and fd < today:
        out.append(Issue("warn", f"The flight date ({fd:%d-%b-%Y}) has passed - confirm arrival."))
    if p.ticket_status in ("none", "requested") and not fd and n is not None and 0 <= n <= pol.ticket_warn_days:
        out.append(Issue("warn", f"No ticket booked yet and only {human_days(n)} left."))
    if not (p.phone.strip() or p.email.strip()):
        out.append(Issue("warn", "No phone number or e-mail - this person cannot be reached."))
    last = last_contact(p)
    if last is not None and (today - last.date()).days > 2 * pol.interval_for(n):
        out.append(Issue("warn", f"Last contact was {(today - last.date()).days} days ago."))
    pe = to_date(p.passport_expiry)
    ref = fd or dl
    if pe and ref and pe < add_months(ref, pol.min_passport_months):
        out.append(Issue("warn", f"The passport is valid for less than {pol.min_passport_months} months at travel "
                                 f"(expires {pe:%d-%b-%Y})."))
    due = to_date(p.next_followup)
    if due and dl and due > dl:
        out.append(Issue("warn", "The next reminder is set AFTER the visa deadline."))
    if p.doc("visa") is None:
        out.append(Issue("info", "The visa file is not stored yet (Documents tab)."))
    if p.ticket_status in ("booked", "sent") and p.doc("ticket") is None:
        out.append(Issue("info", "The ticket file is not stored yet (Documents tab)."))
    return out


def priority(p: Person, today: dt.date, pol: Policy) -> tuple:
    """Sort key: the most urgent first, closed people last."""
    n = days_left(p, today, pol)
    return (LEVEL_ORDER.get(level_of(p, today, pol), 9), n if n is not None else 10 ** 6, p.name.lower())


# --------------------------------------------------------------------------- filters / counts
FILTERS = [("active", "Active"), ("todo", "To do today"), ("overdue", "Overdue"), ("d7", "Critical"),
           ("d14", "Urgent"), ("d30", "Soon"), ("noticket", "No ticket"), ("closed", "Closed"), ("all", "Everyone")]


def matches_filter(p: Person, key: str, today: dt.date, pol: Policy) -> bool:
    lvl = level_of(p, today, pol)
    active = p.status == "active"
    if key == "all":
        return True
    if key == "closed":
        return not active
    if not active:
        return False
    if key == "active":
        return True
    if key == "todo":
        return bool(todo(p, today, pol))
    if key == "overdue":
        return lvl == "overdue"
    if key == "d7":
        return lvl == "critical"
    if key == "d14":
        return lvl == "urgent"
    if key == "d30":
        return lvl == "soon"
    if key == "noticket":
        return p.ticket_status in ("none", "requested")
    return True


def counts(people: list[Person], today: dt.date, pol: Policy) -> dict[str, int]:
    return {key: sum(1 for p in people if matches_filter(p, key, today, pol)) for key, _ in FILTERS}


# --------------------------------------------------------------------------- storage
class Store:
    """people.json + files/ + backups/ in one folder.  Every change is saved at once, atomically; the first save of
    a day also keeps a dated backup.  Changes can be undone (Ctrl+Z) for as long as the program is open."""

    def __init__(self, folder: str | Path, keep_backups: int = 14) -> None:
        self.folder = Path(folder)
        self.keep_backups = max(1, keep_backups)
        self.path = self.folder / "people.json"
        self.files_dir = self.folder / "files"
        self.backup_dir = self.folder / "backups"
        self.people: dict[str, Person] = {}
        self.meta: dict[str, Any] = {}
        self.warning = ""                           # set when the file was damaged and a backup was used
        self._stamp: tuple | None = None
        self._undo: list[tuple[str, str]] = []
        self._redo: list[tuple[str, str]] = []
        self._listeners: list[Callable[[], None]] = []
        self.folder.mkdir(parents=True, exist_ok=True)
        self.load()

    # ------------------------------------------------------------------ listeners
    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _changed(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("Visa store listener failed")

    # ------------------------------------------------------------------ load / save
    def _file_stamp(self) -> tuple | None:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def load(self) -> None:
        self.warning = ""
        if not self.path.exists():
            self.people, self.meta, self._stamp = {}, {}, None
            return
        try:
            self._read(self.path)
        except (OSError, ValueError) as exc:
            log.error("Could not read %s: %s", self.path, exc)
            bad = self.path.with_suffix(".bad")
            try:
                shutil.copy2(self.path, bad)
            except OSError:
                pass
            for b in self.backups():
                try:
                    self._read(b)
                    self.warning = (f"people.json could not be read ({exc}). The newest backup ({b.name}) was loaded; "
                                    f"the damaged file was kept as {bad.name}.")
                    return
                except (OSError, ValueError):
                    continue
            self.people, self.meta = {}, {}
            self.warning = f"people.json could not be read ({exc}) and no usable backup exists. A copy is kept as {bad.name}."
        self._stamp = self._file_stamp()

    def _read(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("unexpected content")
        people = {}
        for raw in data.get("people", []):
            if isinstance(raw, dict):
                p = Person.from_dict(raw)
                p.id = p.id or new_id()
                people[p.id] = p
        self.people = people
        self.meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
        self._stamp = self._file_stamp()

    def refresh(self) -> bool:
        """Re-read the file when somebody else (another PC, a restored backup) changed it.  True = data reloaded."""
        if self._file_stamp() != self._stamp:
            self.load()
            self._changed()
            return True
        return False

    def _dump(self) -> str:
        return json.dumps({"version": SCHEMA_VERSION, "saved": now_iso(), "meta": self.meta,
                           "people": [p.to_dict() for p in self.people.values()]}, indent=1, ensure_ascii=False)

    def save(self) -> None:
        try:
            self._daily_backup()
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(self._dump(), encoding="utf-8")
            tmp.replace(self.path)
        except PermissionError as exc:
            raise VisaError(f"Cannot save {self.path.name}: the file is locked or read-only. Close it and try again.") from exc
        except OSError as exc:
            raise VisaError(f"Cannot save {self.path.name}: {exc}") from exc
        self._stamp = self._file_stamp()

    def _daily_backup(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            json.loads(self.path.read_text(encoding="utf-8"))      # never keep a damaged file as "the backup"
        except (OSError, ValueError):
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"people-{dt.date.today():%Y%m%d}.json"
        if not target.exists():
            shutil.copy2(self.path, target)
            for old in self.backups()[self.keep_backups:]:
                try:
                    old.unlink()
                except OSError:
                    pass

    def backups(self) -> list[Path]:
        """Newest first."""
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("people-*.json"), reverse=True)

    def backup_now(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"people-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        self.save()
        shutil.copy2(self.path, target)
        return target

    def restore(self, path: str | Path) -> int:
        """Replace everything by a backup (the current state can be undone).  Returns the number of people."""
        before = self._snapshot()
        old_people, old_meta = self.people, self.meta
        try:
            self._read(Path(path))
        except (OSError, ValueError) as exc:
            self.people, self.meta = old_people, old_meta
            raise VisaError(f"That file is not a usable backup: {exc}") from exc
        self._push_undo("Restore backup", before)
        self.save()
        self._changed()
        return len(self.people)

    # ------------------------------------------------------------------ undo
    def _snapshot(self) -> str:
        return json.dumps([p.to_dict() for p in self.people.values()], ensure_ascii=False)

    def _push_undo(self, label: str, snapshot: str | None = None) -> None:
        self._undo.append((label, snapshot if snapshot is not None else self._snapshot()))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def _apply_snapshot(self, snap: str) -> None:
        self.people = {}
        for raw in json.loads(snap):
            p = Person.from_dict(raw)
            self.people[p.id] = p

    def can_undo(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def undo(self) -> str:
        """Undo the last change; returns what was undone ('' = nothing)."""
        if not self._undo:
            return ""
        label, snap = self._undo.pop()
        self._redo.append((label, self._snapshot()))
        self._apply_snapshot(snap)
        self.save()
        self._changed()
        return label

    def redo(self) -> str:
        if not self._redo:
            return ""
        label, snap = self._redo.pop()
        self._undo.append((label, self._snapshot()))
        self._apply_snapshot(snap)
        self.save()
        self._changed()
        return label

    def _mutate(self, label: str) -> None:
        """Call BEFORE changing anything: reloads foreign changes, remembers the state for undo."""
        self.refresh()
        self._push_undo(label)

    # ------------------------------------------------------------------ people
    def all(self) -> list[Person]:
        return list(self.people.values())

    def get(self, pid: str) -> Person | None:
        return self.people.get(pid)

    def find_passport(self, number: str) -> Person | None:
        key = re.sub(r"\W+", "", number or "").upper()
        if not key:
            return None
        return next((p for p in self.people.values() if re.sub(r"\W+", "", p.passport_no).upper() == key), None)

    def groups(self) -> list[str]:
        return sorted({p.group for p in self.people.values() if p.group.strip()}, key=str.lower)

    def add(self, person: Person, label: str = "") -> Person:
        self._mutate(label or f"Add {person.name or 'person'}")
        person.id = person.id if person.id and person.id not in self.people else new_id()
        person.added_on = person.added_on or dt.date.today().isoformat()
        person.updated_on = now_iso()
        self.people[person.id] = person
        self.save()
        self._changed()
        return person

    def add_many(self, people: list[Person], label: str = "Import") -> int:
        self._mutate(label)
        for person in people:
            person.id = person.id if person.id and person.id not in self.people else new_id()
            person.added_on = person.added_on or dt.date.today().isoformat()
            person.updated_on = now_iso()
            self.people[person.id] = person
        self.save()
        self._changed()
        return len(people)

    MERGE_FIELDS = ("name", "passport_no", "passport_expiry", "nationality", "phone", "email", "position", "group",
                    "visa_no", "visa_type", "visa_issue", "visa_deadline", "flight_date", "flight_no", "notes")

    def merge_people(self, new: list[Person], mode: str = "skip", label: str = "Import") -> tuple[int, int, int]:
        """Add imported people as ONE undoable step.  A person is a duplicate when the passport number matches (or, if
        neither has one, the name).  mode: 'skip' leave duplicates alone | 'update' fill the existing person with the
        imported values | 'add' add everybody.  Returns (added, updated, skipped)."""
        self._mutate(label)
        added = updated = skipped = 0
        stamp = now_iso()
        for p in new:
            twin = self.find_passport(p.passport_no)
            if twin is None and not p.passport_no.strip():
                twin = next((q for q in self.people.values() if not q.passport_no.strip()
                             and q.name.strip().lower() == p.name.strip().lower()), None)
            if twin is not None and mode == "skip":
                skipped += 1
            elif twin is not None and mode == "update":
                for f in self.MERGE_FIELDS:
                    if getattr(p, f):
                        setattr(twin, f, getattr(p, f))
                if p.visa_days:
                    twin.visa_days = p.visa_days
                if p.ticket_status != "none":
                    twin.ticket_status = p.ticket_status
                if p.status != "active":
                    twin.status = p.status
                twin.extra.update(p.extra)
                twin.updated_on = stamp
                updated += 1
            else:
                p.id = p.id if p.id and p.id not in self.people else new_id()
                p.added_on = p.added_on or dt.date.today().isoformat()
                p.updated_on = stamp
                self.people[p.id] = p
                added += 1
        if added or updated:
            self.save()
            self._changed()
        else:
            self._undo.pop()                           # nothing happened: nothing to undo
        return added, updated, skipped

    def update(self, person: Person, label: str = "") -> None:
        if person.id not in self.people:
            raise VisaError("That person no longer exists.")
        self._mutate(label or f"Edit {person.name}")
        person.updated_on = now_iso()
        self.people[person.id] = person
        self.save()
        self._changed()

    def delete(self, ids: list[str]) -> int:
        ids = [i for i in ids if i in self.people]
        if not ids:
            return 0
        self._mutate(f"Delete {len(ids)} person(s)")
        for i in ids:
            del self.people[i]                         # their files stay on disk - nothing is lost for good
        self.save()
        self._changed()
        return len(ids)

    def edit_many(self, ids: list[str], fn: Callable[[Person], None], label: str) -> int:
        """Apply `fn` to several people as ONE undoable step."""
        if not any(i in self.people for i in ids):
            return 0
        self._mutate(label)                            # may reload foreign changes: resolve the people afterwards
        targets = [self.people[i] for i in ids if i in self.people]
        if not targets:
            return 0
        stamp = now_iso()
        for p in targets:
            fn(p)
            p.updated_on = stamp
        self.save()
        self._changed()
        return len(targets)

    # ------------------------------------------------------------------ follow-ups and status
    def log_followup(self, ids: str | list[str], channel: str, outcome: str = "", note: str = "", by: str = "",
                     when: dt.datetime | None = None, next_followup: dt.date | None = None) -> int:
        """Write a contact into the log of one or several people.  Logging a real contact clears an old snooze; pass
        `next_followup` to set the next reminder date yourself."""
        ids = [ids] if isinstance(ids, str) else list(ids)

        def apply(p: Person) -> None:
            p.followups.append(make_followup(channel, outcome, note, by, when))
            if channel not in ("note", "system"):
                p.next_followup = ""
            if next_followup:
                p.next_followup = iso(next_followup)

        return self.edit_many(ids, apply, "Log contact" if channel not in ("note", "system") else "Add note")

    def snooze(self, ids: list[str], until: dt.date | None) -> int:
        return self.edit_many(ids, lambda p: setattr(p, "next_followup", iso(until)),
                              "Snooze reminder" if until else "Clear reminder date")

    def delete_followup(self, pid: str, fid: str) -> bool:
        p = self.people.get(pid)
        if not p or not any(f.get("id") == fid for f in p.followups):
            return False
        self.edit_many([pid], lambda q: setattr(q, "followups", [f for f in q.followups if f.get("id") != fid]),
                       "Delete log entry")
        return True

    def set_status(self, ids: list[str], status: str, on: dt.date | None = None, note: str = "", by: str = "") -> int:
        label = dict(STATUSES).get(status, status)

        def apply(p: Person) -> None:
            p.status = status
            p.arrived_on = iso(on or dt.date.today()) if status == "arrived" else ""
            p.next_followup = ""
            p.followups.append(make_followup("system", "", f"Status: {label}" + (f" - {note}" if note else ""), by))

        return self.edit_many(ids, apply, f"Mark as {label.lower()}")

    # ------------------------------------------------------------------ files
    def person_dir(self, p: Person) -> Path:
        stem = re.sub(r"[^\w\-]+", "_", p.name).strip("_")[:40] or "person"
        return self.files_dir / f"{stem}_{p.id}"

    def doc_path(self, doc: dict) -> Path:
        return self.folder / str(doc.get("file", ""))

    def new_doc_path(self, p: Person, kind: str, suffix: str = ".pdf") -> Path:
        """A free file name for a document of `kind` (nothing is written)."""
        label = dict(DOC_KINDS).get(kind, kind.title())
        base = re.sub(r"[^\w\- ]+", "_", f"{label} - {p.name or 'person'}").strip()
        folder = self.person_dir(p)
        folder.mkdir(parents=True, exist_ok=True)
        path, n = folder / f"{base}{suffix}", 2
        while path.exists():
            path, n = folder / f"{base} ({n}){suffix}", n + 1
        return path

    def add_doc(self, pid: str, kind: str, file: str | Path, source: str = "", pages: list[int] | None = None,
                replace: bool = True) -> dict:
        """Record a file that already lives in the person's folder (see new_doc_path).  With `replace` the older file
        of the same kind is unlinked but stays on disk - files are never deleted."""
        p = self.people.get(pid)
        if p is None:
            raise VisaError("That person no longer exists.")
        file = Path(file)
        try:
            rel = file.resolve().relative_to(self.folder.resolve()).as_posix()
        except ValueError as exc:
            raise VisaError("The file must be inside the Visa Tracker folder.") from exc
        doc = {"id": new_id(), "kind": kind, "file": rel, "name": file.name, "source": source,
               "pages": [n + 1 for n in (pages or [])], "added": now_iso()}

        def apply(q: Person) -> None:
            if replace:                                # the older file stays in the folder, it just is not linked
                q.docs = [d for d in q.docs if d.get("kind") != kind]
            q.docs.append(doc)

        self.edit_many([pid], apply, f"Store {dict(DOC_KINDS).get(kind, kind).lower()} file")
        return doc

    def remove_doc(self, pid: str, doc_id: str) -> bool:
        p = self.people.get(pid)
        doc = next((d for d in (p.docs if p else []) if d.get("id") == doc_id), None)
        if doc is None:
            return False

        self.edit_many([pid], lambda q: setattr(q, "docs", [d for d in q.docs if d.get("id") != doc_id]), "Remove file")
        return True                                    # the file itself stays in the person's folder
