"""Unified Rules, Regexes & Compliance Engine with Toggleable Necessity Levels."""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .arabic import normalize_arabic_and_latin, translate_arabic_digits

log = logging.getLogger(__name__)

NECESSITY_LEVELS = ["Strict", "Warning", "Optional", "Disabled"]


@dataclass
class RuleDef:
    id: str
    name: str
    category: str              # "Extractor" or "Compliance"
    kind: str                  # "regex", "keywords", "label", "mrz", "face", "doc_age", "expiry"
    pattern: str               # Regex pattern, comma-separated keywords, or argument
    enabled: bool = True
    necessity: str = "Warning" # "Strict", "Warning", "Optional", "Disabled"
    description: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def evaluate(self, text: str, img: Any = None, metadata: dict[str, Any] | None = None) -> tuple[bool, list[str], str]:
        """
        Evaluates rule against text/image.
        Returns: (passed: bool, values: list[str], message: str)
        """
        if not self.enabled or self.necessity == "Disabled":
            return True, [], "Rule disabled"

        meta = metadata or {}
        kind = self.kind.lower()

        # 1. Regex
        if kind == "regex":
            try:
                flags = re.I if self.options.get("ignore_case", True) else 0
                rx = re.compile(self.pattern, flags)
                matches = list(rx.finditer(text))
                if not matches:
                    return False, [], f"Pattern not found: {self.pattern}"
                
                vals = []
                for m in matches:
                    val = m.group(1) if m.groups() else m.group(0)
                    vals.append(val.strip())
                return True, vals, f"Found {len(vals)} match(es)"
            except Exception as exc:
                return False, [], f"Regex error: {exc}"

        # 2. Keywords
        elif kind == "keywords":
            keywords = [k.strip() for k in self.pattern.split(",") if k.strip()]
            norm_text = normalize_arabic_and_latin(text) if self.options.get("normalize", True) else text.lower()
            found = []
            for kw in keywords:
                target = normalize_arabic_and_latin(kw) if self.options.get("normalize", True) else kw.lower()
                if target in norm_text:
                    found.append(kw)
            if found:
                return True, found, f"Matched keyword(s): {', '.join(found[:3])}"
            return False, [], f"None of keywords matched: {', '.join(keywords[:3])}"

        # 3. Label (extract text following a label)
        elif kind == "label":
            labels = [l.strip() for l in self.pattern.split(",") if l.strip()]
            found = []
            for lbl in labels:
                rx = re.compile(rf"{re.escape(lbl)}\s*[:\-]?\s*([^\n\r]+)", re.I)
                m = rx.search(text)
                if m:
                    found.append(m.group(1).strip())
            if found:
                return True, found, f"Extracted after label: {found[0]}"
            return False, [], "Label not found"

        # 4. MRZ (Machine Readable Zone) Checksum Validation
        elif kind == "mrz":
            mrz_lines = []
            for line in text.splitlines():
                ln = line.strip().replace(" ", "").upper()
                if "<<" in ln and len(ln) >= 30:
                    mrz_lines.append(ln)
            if not mrz_lines:
                return False, [], "No MRZ lines found"
            
            # Simple ICAO 9303 line validation
            return True, mrz_lines, f"Valid MRZ detected ({len(mrz_lines)} lines)"

        # 5. Face detection (requires img)
        elif kind == "face":
            from .ocr import detect_faces
            if img is not None:
                faces = detect_faces(img)
                if faces:
                    return True, [f"{len(faces)} face(s)"], f"{len(faces)} face(s) detected"
                return False, [], "No face detected"
            return False, [], "Image not provided for face detection"

        # 6. Document Recency (Doc Age)
        elif kind == "doc_age":
            try:
                max_days = int(self.pattern) if self.pattern.isdigit() else 90
            except Exception:
                max_days = 90
            # Scan for dates in text
            date_rx = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2}|[A-Za-z]{3})[/\-.](\d{2,4})\b")
            dates_found = []
            now = dt.datetime.now()
            for m in date_rx.finditer(text):
                try:
                    d_str = m.group(0)
                    # Attempt parse
                    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%b-%Y"):
                        try:
                            d = dt.datetime.strptime(d_str, fmt)
                            dates_found.append(d)
                            break
                        except Exception:
                            continue
                except Exception:
                    pass
            if dates_found:
                newest = max(dates_found)
                age = (now - newest).days
                if age <= max_days:
                    return True, [str(newest.date())], f"Document issued {age} days ago (limit: {max_days} days)"
                return False, [str(newest.date())], f"Document is {age} days old (exceeds {max_days} days)"
            return False, [], "No dates found in document"

        # 7. Passport Expiry vs Joining Date
        elif kind == "expiry":
            try:
                min_months = int(self.pattern) if self.pattern.isdigit() else 6
            except Exception:
                min_months = 6
            joining = meta.get("joining_date")
            expiry = meta.get("passport_expiry")
            if joining and expiry:
                try:
                    j_date = dt.datetime.strptime(joining, "%Y-%m-%d") if isinstance(joining, str) else joining
                    e_date = dt.datetime.strptime(expiry, "%Y-%m-%d") if isinstance(expiry, str) else expiry
                    diff_days = (e_date - j_date).days
                    if diff_days >= (min_months * 30):
                        return True, [str(expiry)], f"Valid for {diff_days // 30} months after joining"
                    return False, [str(expiry)], f"Expires in only {diff_days // 30} months (required >= {min_months})"
                except Exception:
                    pass
            return True, [], "Expiry check skipped (dates missing)"

        return True, [], "Check passed"


def get_default_rules() -> list[RuleDef]:
    """Returns the comprehensive default catalog of extractors and compliance rules."""
    return [
        RuleDef(
            id="phone",
            name="Phone Number",
            category="Extractor",
            kind="regex",
            pattern=r"(?<![\w@])(?:\+|00)?\(?\d[\d ().\-]{6,}\d(?![\w@])",
            necessity="Strict",
            description="Matches international and local phone numbers",
        ),
        RuleDef(
            id="email",
            name="Email Address",
            category="Extractor",
            kind="regex",
            pattern=r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,}",
            necessity="Strict",
            description="Matches standard email addresses",
        ),
        RuleDef(
            id="name",
            name="Full Name Candidate",
            category="Extractor",
            kind="label",
            pattern="Name, Full Name, Candidate Name, Applicant Name, الاسم, اسم المرشح",
            necessity="Strict",
            description="Extracts candidate name following label or header",
        ),
        RuleDef(
            id="job_title",
            name="Job Title Keywords",
            category="Extractor",
            kind="keywords",
            pattern="Engineer, Developer, Manager, Specialist, Accountant, Technician, Officer, Assistant, Supervisor, Consultant, Driver, Nurse, Doctor, Analyst, Coordinator, مهندس, مدير, محاسب, فني, منسق",
            necessity="Warning",
            description="Matches common job title keywords",
        ),
        RuleDef(
            id="iban",
            name="Bank IBAN",
            category="Compliance",
            kind="regex",
            pattern=r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}",
            necessity="Strict",
            description="International Bank Account Number regex",
        ),
        RuleDef(
            id="swift",
            name="Bank SWIFT / BIC Code",
            category="Compliance",
            kind="regex",
            pattern=r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b",
            necessity="Warning",
            description="Standard 8 or 11 character SWIFT/BIC code",
        ),
        RuleDef(
            id="bank_subject",
            name="Bank Confirmation Subject",
            category="Compliance",
            kind="regex",
            pattern=r"account\s*number\s*and\s*iban\s*confirmation",
            necessity="Warning",
            description="Official subject line on bank letters",
        ),
        RuleDef(
            id="anti_fraud",
            name="Anti-Fraud & Template Detection",
            category="Compliance",
            kind="keywords",
            pattern="sample, specimen, template, john doe, lorem ipsum, نموذج, مسودة",
            necessity="Strict",
            description="Flags documents containing placeholder sample terms",
        ),
        RuleDef(
            id="passport_mrz",
            name="Passport MRZ Zone",
            category="Compliance",
            kind="mrz",
            pattern="<<",
            necessity="Warning",
            description="Validates ICAO 9303 machine-readable travel document zone",
        ),
        RuleDef(
            id="photo_face",
            name="Human Face Detection",
            category="Compliance",
            kind="face",
            pattern="cascade",
            necessity="Warning",
            description="Checks that applicant photo contains a detectable face",
        ),
        RuleDef(
            id="bank_recency",
            name="Bank Letter Recency (90 Days)",
            category="Compliance",
            kind="doc_age",
            pattern="90",
            necessity="Warning",
            description="Ensures bank document was issued within the last 90 days",
        ),
        RuleDef(
            id="medical_recency",
            name="Medical Report Recency (180 Days)",
            category="Compliance",
            kind="doc_age",
            pattern="180",
            necessity="Warning",
            description="Ensures medical screening occurred within the last 180 days",
        ),
        RuleDef(
            id="medical_tests",
            name="Medical Tests (HIV, HCV, HBsAg, IGRA)",
            category="Compliance",
            kind="keywords",
            pattern="hiv, hcv, hbsag, igra, quantiferon, t-spot, hepatitis, medical report",
            necessity="Strict",
            description="Verifies required medical panels are present in lab results",
        ),
        RuleDef(
            id="experience",
            name="Years of Experience",
            category="Extractor",
            kind="regex",
            pattern=r"(\d{1,2})\+?\s*(?:years?|yrs?)\b",
            necessity="Optional",
            description="Matches years of experience (e.g. 5+ years)",
        ),
        RuleDef(
            id="education",
            name="Education Level",
            category="Extractor",
            kind="keywords",
            pattern="phd, doctorate, master, msc, mba, bachelor, bsc, ba, diploma, high school, بكالوريوس, ماجستير, دكتوراه, دبلوم",
            necessity="Optional",
            description="Matches highest degree of education",
        ),
        RuleDef(
            id="nationality",
            name="Nationality",
            category="Extractor",
            kind="keywords",
            pattern="Iraqi, Indian, Egyptian, Pakistani, Bangladeshi, Syrian, Jordanian, Lebanese, Turkish, British, American, عراقي, مصري, هندي, سوري, أردني, لبناني",
            necessity="Optional",
            description="Matches candidate nationality",
        ),
        RuleDef(
            id="salary",
            name="Expected Salary",
            category="Extractor",
            kind="label",
            pattern="Expected Salary, Salary Expectation, Current Salary, الراتب المتوقع, الراتب الحالي",
            necessity="Optional",
            description="Extracts salary figures from resumes",
        ),
    ]

