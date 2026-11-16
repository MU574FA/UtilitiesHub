"""Visa Tracker input / output (no Tk): spreadsheet import, exports, calendar file, message templates, links.

    mapping = guess_mapping(table.headers)                      # {"name": "Full name", "visa_issue": "Issued", ...}
    people, problems = table_to_people(table.records(), mapping, today)
    rows = people_table(people, today, pol)                     # header + rows -> sheets.write_xlsx / write_csv
    open(path, "w").write(make_ics(people, today, pol))         # deadlines + follow-ups + flights for Outlook / Google
    text = render_template(template["text"], person, today, pol, my_name="Sara")
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import quote

from .textutil import slugify
from .visa import (CHANNELS, LEVEL_LABEL, OUTCOMES, STATUSES, TICKET_STATES, Person, Policy, days_left, deadline_of,
                   human_days, iso, last_contact, last_contact_entry, level_of, next_due, parse_date_input, priority,
                   to_date, todo)

# --------------------------------------------------------------------------- display helpers
DATE_FORMATS = [("", "15-Nov-2026"), ("%Y-%m-%d", "2026-11-15"), ("%d/%m/%Y", "15/11/2026"), ("%m/%d/%Y", "11/15/2026")]


def fmt(d: dt.date | None, date_format: str = "") -> str:
    if not d:
        return ""
    return d.strftime(date_format) if date_format else f"{d.day:02d}-{d.strftime('%b')}-{d.year}"


def plain(label: str) -> str:
    """'📞 Call' -> 'Call'  (labels without the emoji, for files)."""
    return re.sub(r"^[^\w(]+\s*", "", label).strip()


def label_of(pairs: list[tuple[str, str]], value: str) -> str:
    return plain(dict(pairs).get(value, value or ""))


# --------------------------------------------------------------------------- import
# (field, label shown in the import window, header pattern) - the most specific patterns first
IMPORT_FIELDS: list[tuple[str, str, str]] = [
    ("passport_expiry", "Passport expiry", r"passport.*(exp|valid)|(exp|valid).*passport"),
    ("passport_no", "Passport number", r"passport\s*(no\b|num|#|id)|^\s*passport\s*$|رقم الجواز|^جواز"),
    ("visa_type", "Visa type", r"visa.*type|type.*visa"),
    ("visa_no", "Visa number", r"visa\s*(no\b|num|ref|#)|^\s*visa\s*$"),
    ("visa_issue", "Visa issue date", r"issue|issued|اصدار|إصدار"),
    ("visa_deadline", "Visa expiry / last entry date", r"expir|valid.*(until|to|till)|deadline|last.*entry|انتهاء"),
    ("visa_days", "Validity (days)", r"validity|valid.*days|^days$"),
    ("flight_date", "Flight / travel date", r"flight.*date|travel.*date|departure|date.*(travel|flight)"),
    ("flight_no", "Flight number", r"flight|airline"),
    ("ticket_status", "Ticket status", r"ticket"),
    ("status", "Status", r"^status$|حالة"),
    ("email", "E-mail", r"e-?mail|بريد"),
    ("phone", "Phone / WhatsApp", r"phone|mobile|whats|tel\b|contact|هاتف|جوال|موبايل"),
    ("nationality", "Nationality", r"national|citizen|country|الجنسية"),
    ("position", "Position / job", r"position|job|title|role|profession|trade|الوظيفة|المهنة"),
    ("group", "Group / batch / project", r"group|batch|project|agency|company|lot"),
    ("notes", "Notes", r"note|remark|comment|ملاحظ"),
    ("name", "Name", r"name|candidate|employee|worker|الاسم|اسم"),
]
FIELD_LABELS = {f: lbl for f, lbl, _ in IMPORT_FIELDS}


_STRONG = {"visa_issue": r"visa.*(issu)", "visa_deadline": r"visa.*(expir|valid|deadline|last)",
           "flight_date": r"(flight|travel|departure).*date"}


def guess_mapping(headers: list[str]) -> dict[str, str]:
    """Which column of a sheet holds which field (each column is used once; '' = not found).  A column that talks
    about the passport is never taken for a visa / flight field."""
    used: set[str] = set()
    out: dict[str, str] = {}
    for field_name, _label, pattern in IMPORT_FIELDS:
        hit = ""
        tries = ([re.compile(_STRONG[field_name], re.I)] if field_name in _STRONG else []) + [re.compile(pattern, re.I)]
        for rx in tries:
            hit = next((h for h in headers if h not in used and rx.search(h)
                        and (field_name.startswith("passport") or "passport" not in h.lower())), "")
            if hit:
                break
        if hit:
            used.add(hit)
        out[field_name] = hit
    return out


def cell_date(value: Any, today: dt.date, order: str = "dmy") -> dt.date | None:
    """A date from an Excel cell: real date, Excel serial number or text."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    return parse_date_input(str(value), today, order)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date)):
        return to_date(value).isoformat()
    return str(value).strip()


def _ticket_state(text: str) -> str:
    t = text.strip().lower()
    if not t or t in ("no", "none", "n", "false", "0", "-"):
        return "none"
    if "sent" in t or "deliver" in t or "given" in t:
        return "sent"
    if "request" in t or "pending" in t or "wait" in t:
        return "requested"
    return "booked"


def _status(text: str) -> str:
    t = text.strip().lower()
    if re.search(r"arriv|entered|joined|landed", t):
        return "arrived"
    if re.search(r"cancel|withdr|declin|refus", t):
        return "cancelled"
    if re.search(r"miss|expir|lost|overdue", t):
        return "missed"
    return "active"


def table_to_people(records: list[dict], mapping: dict[str, str], today: dt.date | None = None, order: str = "dmy",
                    custom_fields: list[str] | None = None) -> tuple[list[Person], list[str]]:
    """Rows of a sheet -> people.  `problems` lists rows that were skipped or had dates that could not be read."""
    today = today or dt.date.today()
    people: list[Person] = []
    problems: list[str] = []
    date_fields = ("visa_issue", "visa_deadline", "passport_expiry", "flight_date")
    for n, rec in enumerate(records, start=2):
        def get(name: str) -> Any:
            head = mapping.get(name, "")
            return rec.get(head) if head else None

        name = _text(get("name"))
        if not name:
            if any(_text(v) for v in rec.values()):
                problems.append(f"Row {n}: no name - skipped")
            continue
        p = Person(name=name)
        for f in ("passport_no", "nationality", "phone", "email", "position", "group", "visa_no", "visa_type",
                  "flight_no", "notes"):
            setattr(p, f, _text(get(f)))
        for f in date_fields:
            raw = get(f)
            d = cell_date(raw, today, order)
            if raw not in (None, "") and d is None:
                problems.append(f"Row {n} ({name}): cannot read the date '{_text(raw)}' for {FIELD_LABELS[f]}")
            setattr(p, f, iso(d))
        digits = re.search(r"\d+", _text(get("visa_days")))
        p.visa_days = int(digits.group()) if digits else 0
        p.ticket_status = _ticket_state(_text(get("ticket_status")))
        if p.flight_date and p.ticket_status == "none":
            p.ticket_status = "booked"
        p.status = _status(_text(get("status")))
        for label in custom_fields or []:
            v = _text(rec.get(label))
            if v:
                p.extra[label] = v
        people.append(p)
    return people, problems


# --------------------------------------------------------------------------- export
PEOPLE_HEADERS = ["Name", "Passport number", "Passport expiry", "Nationality", "Phone", "E-mail", "Position", "Group",
                  "Visa number", "Visa type", "Visa issued", "Visa deadline", "Days left", "Level", "Status", "Ticket",
                  "Flight date", "Flight number", "Last contact", "Last contact by", "Next follow-up", "Visa file",
                  "Ticket file", "Notes"]


def people_table(people: list[Person], today: dt.date, pol: Policy, date_format: str = "",
                 custom_fields: list[str] | None = None, store=None) -> list[list]:
    """Header + one row per person (opens in Excel; can be imported again)."""
    rows: list[list] = [PEOPLE_HEADERS + list(custom_fields or [])]
    for p in sorted(people, key=lambda q: priority(q, today, pol)):
        last = last_contact(p)
        entry = last_contact_entry(p) or {}
        visa, ticket = p.doc("visa"), p.doc("ticket")
        n = days_left(p, today, pol)
        due = next_due(p, today, pol)
        path = (lambda d: str(store.doc_path(d)) if store is not None and d else (d or {}).get("name", ""))
        rows.append([p.name, p.passport_no, fmt(to_date(p.passport_expiry), date_format), p.nationality, p.phone, p.email,
                     p.position, p.group, p.visa_no, p.visa_type, fmt(to_date(p.visa_issue), date_format),
                     fmt(deadline_of(p, pol), date_format), "" if n is None else n, LEVEL_LABEL.get(level_of(p, today, pol), ""),
                     label_of(STATUSES, p.status), label_of(TICKET_STATES, p.ticket_status),
                     fmt(to_date(p.flight_date), date_format), p.flight_no,
                     last.strftime("%Y-%m-%d %H:%M") if last else "", entry.get("by", ""), fmt(due, date_format),
                     path(visa), path(ticket), p.notes] + [p.extra.get(c, "") for c in custom_fields or []])
    return rows


def followup_table(people: list[Person], date_format: str = "") -> list[list]:
    """The complete follow-up log of everybody (the proof that people were reminded)."""
    rows: list[list] = [["Name", "Passport number", "Date", "Time", "Channel", "Result", "By", "Note"]]
    for p in sorted(people, key=lambda q: q.name.lower()):
        for f in sorted(p.followups, key=lambda x: str(x.get("when"))):
            try:
                when = dt.datetime.fromisoformat(str(f.get("when")))
            except ValueError:
                continue
            channel = "System" if f.get("channel") == "system" else label_of(CHANNELS, f.get("channel", ""))
            rows.append([p.name, p.passport_no, fmt(when.date(), date_format), when.strftime("%H:%M"), channel,
                         label_of(OUTCOMES, f.get("outcome", "")), f.get("by", ""), f.get("note", "")])
    return rows


def summary_text(people: list[Person], today: dt.date, pol: Policy, date_format: str = "", limit: int = 40) -> str:
    """A short status report to paste into an e-mail or chat."""
    from .visa import counts
    c = counts(people, today, pol)
    lines = [f"Visa status - {fmt(today, date_format)}",
             f"Waiting to arrive: {c['active']}   Overdue: {c['overdue']}   Critical: {c['d7']}   Urgent: {c['d14']}   "
             f"Soon: {c['d30']}   Follow-ups due today: {c['todo']}   Without ticket: {c['noticket']}", ""]
    active = sorted((p for p in people if p.status == "active"), key=lambda q: priority(q, today, pol))
    shown = [p for p in active if level_of(p, today, pol) in ("overdue", "critical", "urgent", "nodate") or todo(p, today, pol)]
    for p in shown[:limit]:
        n, dl, last = days_left(p, today, pol), deadline_of(p, pol), last_contact(p)
        bits = [p.name, "no visa date" if n is None else f"EXPIRED {human_days(n)}" if n < 0 else f"{human_days(n)} left"]
        if dl:
            bits.append(f"deadline {fmt(dl, date_format)}")
        bits.append(f"last contact {(today - last.date()).days} day(s) ago" if last else "never contacted")
        bits.append(label_of(TICKET_STATES, p.ticket_status).lower())
        lines.append("- " + " | ".join(bits))
    if len(shown) > limit:
        lines.append(f"... and {len(shown) - limit} more")
    if not shown:
        lines.append("Nothing urgent today.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- calendar (.ics)
def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines longer than 75 bytes are continued on the next line with a leading space."""
    raw, out, cur, size = line, [], "", 0
    for ch in raw:
        b = len(ch.encode("utf-8"))
        if size + b > 74 and cur:
            out.append(cur)
            cur, size = " " + ch, 1 + b
        else:
            cur, size = cur + ch, size + b
    out.append(cur)
    return out


def make_ics(people: list[Person], today: dt.date, pol: Policy, alarms: tuple[int, ...] = (14, 7, 3, 1),
             include: tuple[str, ...] = ("deadline", "followup", "flight"), name: str = "Visa Tracker") -> str:
    """All-day calendar events for the visa deadlines (with alarms), next follow-ups and flights of waiting people."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//UtilsHub//Visa Tracker//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_esc(name)}"]

    def event(uid: str, day: dt.date, title: str, desc: str, alarm_days: tuple[int, ...] = ()) -> None:
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@utilshub", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}", f"SUMMARY:{_esc(title)}",
                      f"DESCRIPTION:{_esc(desc)}"])
        for n in alarm_days:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(title)}", f"TRIGGER:-P{n}D", "END:VALARM"])
        lines.append("END:VEVENT")

    for p in people:
        if p.status != "active":
            continue
        info = " | ".join(x for x in (p.passport_no and f"Passport {p.passport_no}", p.phone, p.position) if x)
        dl = deadline_of(p, pol)
        if "deadline" in include and dl:
            event(f"{p.id}-deadline", dl, f"VISA DEADLINE: {p.name}", f"Last day to enter the country. {info}", alarms)
        due = to_date(p.next_followup)
        if "followup" in include and due:
            event(f"{p.id}-followup", due, f"Follow up: {p.name}", f"Contact about the visa / ticket. {info}")
        fd = to_date(p.flight_date)
        if "flight" in include and fd:
            event(f"{p.id}-flight", fd, f"Flight: {p.name} {p.flight_no}".strip(), f"{info}")
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _fold(line)) + "\r\n"


# --------------------------------------------------------------------------- messages
DEFAULT_TEMPLATES = [
    {"name": "Friendly check-in", "auto": "check",
     "text": "Hello {first_name}, this is {my_name} from {company}. Your visa is valid until {deadline} "
             "({days_text} left). Please confirm that you are preparing to travel and tell me your expected "
             "travel date. Thank you!"},
    {"name": "Ask for travel date", "auto": "",
     "text": "Hello {first_name}, could you please tell me which date you would like to travel? "
             "Your visa must be used before {deadline}. - {my_name}, {company}"},
    {"name": "Ticket details", "auto": "ticket",
     "text": "Hello {first_name}, your ticket is ready: flight {flight_no} on {flight_date}. "
             "Your visa must be used before {deadline}. Please confirm that you received it and that you can travel. "
             "- {my_name}, {company}"},
    {"name": "Urgent reminder", "auto": "urgent",
     "text": "URGENT {first_name}: your visa expires on {deadline} - only {days_text} left. You must enter the country "
             "before that date, otherwise the visa is lost. Please reply TODAY with your travel plan. "
             "- {my_name}, {company}"},
]


def template_vars(p: Person, today: dt.date, pol: Policy, date_format: str = "", my_name: str = "",
                  company: str = "") -> dict[str, str]:
    n, dl = days_left(p, today, pol), deadline_of(p, pol)
    out = {"name": p.name, "first_name": p.first_name, "passport_no": p.passport_no, "position": p.position,
           "group": p.group, "visa_no": p.visa_no, "visa_type": p.visa_type, "deadline": fmt(dl, date_format),
           "issue_date": fmt(to_date(p.visa_issue), date_format), "days_left": "" if n is None else str(n),
           "days_text": human_days(n) if n is not None else "", "flight_date": fmt(to_date(p.flight_date), date_format),
           "flight_no": p.flight_no, "today": fmt(today, date_format), "my_name": my_name, "company": company,
           "nationality": p.nationality}
    for label, value in p.extra.items():
        out.setdefault(slugify(label), str(value))
    return out


def render_template(text: str, p: Person, today: dt.date, pol: Policy, date_format: str = "", my_name: str = "",
                    company: str = "") -> str:
    """Fill {placeholders}.  Unknown ones stay as typed so a typo is easy to spot."""
    values = template_vars(p, today, pol, date_format, my_name, company)
    out = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text or "")
    return re.sub(r"[ \t]{2,}", " ", out).replace(" ,", ",").replace(" .", ".").strip()


def pick_template(templates: list[dict], p: Person, today: dt.date, pol: Policy) -> dict:
    """The template that fits the situation: urgent when time is short, ticket details once a flight is set."""
    templates = templates or DEFAULT_TEMPLATES
    lvl = level_of(p, today, pol)
    want = "urgent" if lvl in ("overdue", "critical") else "ticket" if (p.flight_date and p.ticket_status in ("booked", "sent")) else "check"
    return next((t for t in templates if t.get("auto") == want), templates[0])


def clean_phone(raw: str, default_cc: str = "") -> str:
    """Digits for a wa.me link: '+964 750 123 4567' -> '9647501234567'; '' when it cannot be a phone number."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and default_cc:
        digits = re.sub(r"\D", "", default_cc) + digits.lstrip("0")
    return digits if 8 <= len(digits) <= 15 else ""


def whatsapp_url(phone: str, text: str, default_cc: str = "") -> str:
    number = clean_phone(phone, default_cc)
    return f"https://wa.me/{number}?text={quote(text, safe='')}" if number else ""


def mailto_url(email: str, subject: str, body: str) -> str:
    return f"mailto:{email.strip()}?subject={quote(subject, safe='')}&body={quote(body, safe='')}" if email.strip() else ""
