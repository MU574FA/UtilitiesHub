"""Passport MRZ (ICAO 9303, TD3: two lines of 44 characters) parsing and validation."""
from __future__ import annotations

import datetime as dt
import re

from .dates import mkdate

_DIGITFIX = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}


def check_digit(s: str) -> int | None:
    """ICAO 9303 check digit (weights 7-3-1), or None if `s` has illegal characters."""
    total = 0
    for i, c in enumerate(s):
        v = 0 if c == "<" else int(c) if c.isdigit() else ord(c) - 55 if "A" <= c <= "Z" else None
        if v is None:
            return None
        total += v * (7, 3, 1)[i % 3]
    return total % 10


def parse_mrz(text: str) -> dict | None:
    """Parse a passport MRZ. Returns a dict or None.  Validates all ICAO check digits."""
    flat = re.sub(r"\s+", "", text.upper().replace("«", "<"))
    cands = list(re.finditer(r"P<[A-Z]{3}[A-Z<]{39}", flat)) or list(re.finditer(r"P[A-Z<][A-Z]{3}[A-Z<]{39}", flat))
    if not cands:
        return None
    # the printed word "PASSPORT" can look like a line start: prefer the candidate with a second MRZ line after it
    m = next((c for c in cands if re.search(r"[A-Z0-9<]{44}", flat[c.end():c.end() + 80])), cands[0])
    l1, rest = flat[m.start():m.start() + 44], flat[m.start() + 44:]
    parts = l1[5:].split("<<")
    surname = re.sub(r"\s+", " ", parts[0].replace("<", " ").strip())
    given = re.sub(r"\s+", " ", " ".join(parts[1:]).replace("<", " ")).strip()
    res = dict(surname=surname, given=given, full=(given + " " + surname).strip().title(), country=l1[2:5],
               number="", dob=None, expiry=None, sex="", checks=[], ok=False, has_line2=False)
    m2 = re.search(r"[A-Z0-9<]{44}", rest[:80])
    if not m2:
        return res
    l2 = m2.group(0)

    def num(s: str) -> str:
        return "".join(_DIGITFIX.get(c, c) for c in s)

    dob, exp = num(l2[13:19]), num(l2[21:27])
    if not (dob.isdigit() and exp.isdigit()):
        return res
    res.update(has_line2=True, number=l2[0:9].replace("<", ""), sex=l2[20])
    yy = int(dob[:2])
    res["dob"] = mkdate((2000 if yy <= dt.date.today().year % 100 else 1900) + yy, int(dob[2:4]), int(dob[4:6]))
    res["expiry"] = mkdate(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:6]))

    def chk(body: str, digit: str) -> bool:
        c = check_digit(body)
        return c is not None and num(digit).isdigit() and c == int(num(digit))

    pers_ok = (set(l2[28:42]) == {"<"} and l2[42] in "<0") or chk(l2[28:42], l2[42])
    res["checks"] = [("passport number", chk(l2[0:9], l2[9])), ("birth date", chk(dob, l2[19])),
                     ("expiry date", chk(exp, l2[27])), ("personal number", pers_ok),
                     ("composite", chk(l2[0:10] + l2[13:20] + l2[21:43], l2[43]))]
    res["ok"] = all(ok for _, ok in res["checks"])
    return res


def looks_like_passport(text: str) -> bool:
    return bool(re.search(r"passport|nationality|date of birth|surname|given names", text, re.I)) or text.count("<") >= 5


def extract_surname(text: str) -> tuple[str, str]:
    """Return (surname, source). An empty surname means none could be found / the passport shows none."""
    flat = re.sub(r"\s+", "", text.upper())
    m = re.search(r"P<([A-Z]{3})([A-Z<]{3,})", flat)           # MRZ line 1
    if m:
        rest = m.group(2)
        if rest.startswith("<<"):
            return "", "MRZ shows an EMPTY surname"
        sn = rest.split("<<")[0].replace("<", " ").strip()
        if len(sn) > 1:
            return sn, "MRZ"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]   # printed field
    for i, ln in enumerate(lines):
        if re.search(r"surname|family name|apellidos?|\bnom\b", ln, re.I):
            cand = re.sub(r"(?i)surname|family name|apellidos?|\bnom\b|[/:]", " ", ln)
            words = re.findall(r"[A-Z][A-Z'\-]+", cand)
            if not words and i + 1 < len(lines):
                words = re.findall(r"[A-Z][A-Z'\-]+", lines[i + 1])
            if words:
                return " ".join(words), "printed field"
    return "", "no surname field detected"


def passport_expiry_date(text: str) -> dt.date | None:
    from .dates import find_dates
    mz = parse_mrz(text)
    if mz and mz["expiry"]:
        return mz["expiry"]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.search(r"expir|date of exp|valid until", ln, re.I):
            for chunk in (ln, lines[i + 1] if i + 1 < len(lines) else ""):
                ds = find_dates(chunk, allow2=True)
                if ds:
                    return max(ds)
    return None


def _add_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = (dt.date(year + (month + 1) // 12, (month + 1) % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month + 1, min(day.day, last))


def passport_summary(text: str, min_months: int = 6, today: dt.date | None = None) -> dict:
    """Everything a passport page tells us, flat and ready for a table / CSV.

    `status` is 'valid', 'expires soon' (inside `min_months`), 'expired' or '' (no expiry found);
    `issues` lists what to look at (unreadable MRZ, failed check digits, short validity ...).
    """
    today = today or dt.date.today()
    mz = parse_mrz(text)
    out = dict(surname="", given="", full="", number="", country="", dob="", expiry="", sex="", mrz="not found",
               status="", days_left="", issues=[])
    expiry = None
    if mz:
        out.update(surname=mz["surname"], given=mz["given"], full=mz["full"], number=mz["number"], country=mz["country"],
                   sex=mz["sex"].replace("<", ""), dob=mz["dob"].isoformat() if mz["dob"] else "")
        expiry = mz["expiry"]
        if not mz["has_line2"]:
            out["mrz"] = "line 2 unreadable"
            out["issues"].append("MRZ second line could not be read")
        elif mz["ok"]:
            out["mrz"] = "valid"
        else:
            bad = [name for name, ok in mz["checks"] if not ok]
            out["mrz"] = "check digits failed"
            out["issues"].append("failed: " + ", ".join(bad))
    else:
        out["surname"] = extract_surname(text)[0]
        out["issues"].append("no MRZ found")
        expiry = passport_expiry_date(text)
    if expiry:
        out["expiry"] = expiry.isoformat()
        days = (expiry - today).days
        out["days_left"] = days
        if days < 0:
            out["status"] = "expired"
            out["issues"].append(f"expired {-days} day(s) ago")
        elif expiry < _add_months(today, min_months):
            out["status"] = "expires soon"
            out["issues"].append(f"valid for less than {min_months} month(s)")
        else:
            out["status"] = "valid"
    return out
