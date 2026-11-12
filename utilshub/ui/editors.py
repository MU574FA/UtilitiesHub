"""Concrete RecordEditors: extraction fields, document types, checks, clipboard formats, link templates."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core.doctypes import CHECK_TYPES, Check, DocType
from ..core.aimode import INHERIT, MODE_CHOICES, MODE_CHOICES_INHERIT
from ..core.fields import ALL_KINDS, FieldSpec, map_error
from ..core.rules import LEVEL_LABELS, MATCH_MODES, RULE_LIBRARY, Rule
from ..core.templates import FILTERS
from ..core.textutil import slugify
from .records import Col, Prop, RecordEditor, RuleTester, rules_editor

EDITORS: dict[str, callable] = {}


def editor(name: str):
    def deco(fn):
        EDITORS[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- fields
def _field_library():
    out = []
    kind_map = {"keyword": "keywords"}
    for group, rule in RULE_LIBRARY:
        spec = FieldSpec(key=slugify(rule.label), label=rule.label, kind=kind_map.get(rule.kind, rule.kind),
                         pattern=rule.pattern, ignore_case=rule.ignore_case, multiple=rule.multiple)
        out.append((group, rule.label, spec.to_dict()))
    out.append(("Built-in smart", "Full name", FieldSpec("name", "Full name", "name", level="required").to_dict()))
    out.append(("Built-in smart", "Job title", FieldSpec("job_title", "Job title", "job_title").to_dict()))
    out.append(("Built-in smart", "Company", FieldSpec("company", "Company", "company").to_dict()))
    out.append(("AI (needs Ollama, Settings > AI)", "Described in plain words",
                FieldSpec("custom_value", "My value", "ai", ai_mode="ai", ai_hint="describe it here").to_dict()))
    out.append(("Other", "Mapped from job title (e.g. Interviewer)",
                FieldSpec("interviewer", "Interviewer", "map", source="job_title", fuzzy=True, multiple=False,
                          pattern="Muhasan = general worker, labourer").to_dict()))
    out.append(("Other", "Today's date", FieldSpec("date", "Date", "today").to_dict()))
    out.append(("Other", "Manual note (typed)", FieldSpec("notes", "Notes", "manual").to_dict()))
    return out


def _map_probe(d: dict) -> str:
    """Regex that lights up the words a mapped field would react to (for the 'Try it' box)."""
    import re as _re
    from ..core.fields import parse_map_rules
    words = [v[3:] if v.lower().startswith("re:") else _re.escape(v)
             for _, vs in parse_map_rules(d.get("pattern", "")) for v in vs]
    return "|".join(words) or "(?!)"


@editor("fields")
def fields_editor(parent, app, value, on_change, height: int = 9):
    kinds = [(k, v) for k, v in ALL_KINDS.items()]
    rule_kinds = ("keywords", "regex", "label", "builtin")
    props = [
        Prop("label", "Label", help="Shown in the pop-up and used as the column title"),
        Prop("key", "Placeholder key", help="Used in {placeholders}; leave empty to build it from the label"),
        Prop("kind", "Detector", "choice", kinds),
        Prop("source", "Read from field (key)", "str", visible_if=lambda d: d.get("kind") == "map",
             help="Key of the field whose value decides the result, e.g. job_title (default)"),
        Prop("pattern", "Keywords / pattern", "text", rows=4, width=52,
             visible_if=lambda d: d.get("kind") in rule_kinds + ("map",),
             help="keywords: list ('Canonical = a, b' allowed) • regex: pattern with an optional (group) • "
                  "label: label words • builtin: email, phone, url, iban, linkedin, date • "
                  "mapped: one line per result, 'Muhasan = general worker, labourer, re:^gen.*worker' (first match wins)"),
        Prop("level", "Necessity", "level", help="Required: an empty result blocks auto-save and is flagged red. "
             "Recommended: flagged orange. Optional: quiet."),
        Prop("match", "Keyword matching", "choice", list(MATCH_MODES), visible_if=lambda d: d.get("kind") == "keywords"),
        Prop("fuzzy", "Tolerate typos (fuzzy)", "bool", visible_if=lambda d: d.get("kind") in ("keywords", "map")),
        Prop("ignore_case", "Ignore upper/lower case", "bool", visible_if=lambda d: d.get("kind") in rule_kinds + ("map",)),
        Prop("multiple", "Keep every match", "bool", visible_if=lambda d: d.get("kind") in rule_kinds),
        Prop("join", "Join several values with", "str", visible_if=lambda d: d.get("kind") in rule_kinds),
        Prop("max_len", "Max length of value", "int", visible_if=lambda d: d.get("kind") == "label"),
        Prop("normalize", "Phone format", "choice", [("", "As written"), ("digits", "Digits only"), ("intl", "+ digits")],
             visible_if=lambda d: d.get("kind") == "phone"),
        Prop("country_code", "Country code for numbers starting 0", "str", visible_if=lambda d: d.get("kind") == "phone"),
        Prop("format", "Date format", "str", visible_if=lambda d: d.get("kind") == "today",
             help="e.g. %d-%b-%Y → 10-Oct-2026"),
        Prop("ai_mode", "AI for this field", "choice", MODE_CHOICES_INHERIT,
             visible_if=lambda d: d.get("kind") not in ("today", "manual", "map"),
             help="Needs Settings > AI (Ollama) switched on. 'Use the default' follows Settings > AI > Filling fields. "
                  "Rules only = AI off for this field. Kind 'AI-described' has no rules, so it needs an AI mode."),
        Prop("ai_hint", "Tell the AI what to look for", "text", rows=2, width=52,
             visible_if=lambda d: d.get("kind") not in ("today", "manual", "map"),
             help="Plain words, e.g. 'current employer, not past ones' or 'years of experience as a number'."),
        Prop("note", "Note"),
    ]

    def validate(d):
        if not d.get("label", "").strip():
            return "Give the field a label."
        if d.get("kind") == "ai" and d.get("ai_mode") in ("rules", ""):
            return "An AI-described field needs an AI mode (AI only, or AI first)."
        if d.get("kind") == "map":
            return map_error(d.get("pattern", ""))
        if d.get("kind") in rule_kinds:
            return FieldSpec.from_dict(d).rule().error()
        return ""

    def fill_key(recs):
        for r in recs:
            r["key"] = r.get("key") or slugify(r.get("label", ""))
        return recs

    def extra(holder, working, collect, extra_get, vars_):
        def tester_rule():
            d = collect()
            if d.get("kind") == "map":      # tester shows what the mapping would give for the pasted job title
                return Rule(label=d.get("label", ""), kind="regex", pattern=_map_probe(d))
            return FieldSpec.from_dict(d).rule()
        tester = RuleTester(holder, app, tester_rule, height=4)
        tester.pack(fill="both", expand=True)
        for v in vars_.values():
            v.trace_add("write", tester.schedule)

    return RecordEditor(parent, app, [Col("label", "Label", 140), Col("kind", "Detector", 100, lambda r: ALL_KINDS.get(r.get("kind"), "")),
                                      Col("level", "Necessity", 90, lambda r: LEVEL_LABELS.get(r.get("level"), "")),
                                      Col("ai_mode", "AI", 90, lambda r: dict(MODE_CHOICES_INHERIT).get(r.get("ai_mode", INHERIT), "").split(" ")[0]
                                          if r.get("ai_mode", INHERIT) != INHERIT else "default"),
                                      Col("pattern", "Pattern / keywords", 220)],
                        props, lambda: FieldSpec(label="", kind="regex").to_dict(), value,
                        (lambda recs: on_change(fill_key(recs))) if on_change else None, library=_field_library(),
                        dialog_extra=extra, validate=validate, noun="field", height=height, dialog_width=600)


# --------------------------------------------------------------------------- checks
@editor("checks")
def checks_editor(parent, app, value, on_change, height: int = 7):
    types = [(k, v.label) for k, v in CHECK_TYPES.items()]
    props = [
        Prop("label", "What is checked", help="Shown in the checklist"),
        Prop("type", "Kind of check", "choice", types),
        Prop("arg", "Setting", "str", width=52, help="Regex / words / number depending on the kind of check"),
        Prop("level", "If it fails", "level", help="Required = failure • Recommended = warning • Optional = information only"),
        Prop("compact", "Ignore spaces in the text", "bool", visible_if=lambda d: d.get("type") in ("regex", "keywords")),
        Prop("ignore_case", "Ignore upper/lower case", "bool", visible_if=lambda d: d.get("type") == "regex"),
        Prop("use_image", "Let the AI look at the picture of the first page (signatures, stamps, logos)", "bool",
             visible_if=lambda d: d.get("type") == "ai_question", help="Needs a vision model (Settings > AI)."),
    ]

    def extra(holder, working, collect, extra_get, vars_):
        lbl = ttk.Label(holder, style="Muted.TLabel", wraplength=520, justify="left")
        lbl.pack(anchor="w")

        def upd(*_):
            ct = CHECK_TYPES.get(collect().get("type"))
            lbl.configure(text=(f"{ct.label}. {ct.help}" + (f"\nSetting = {ct.arg_label} (e.g. {ct.arg_default})" if ct.arg_label else "")) if ct else "")
        vars_["type"].trace_add("write", upd)
        upd()

    def validate(d):
        if d.get("type") == "ai_question" and not d.get("arg", "").strip():
            return "Write the yes/no question for the AI in the Setting box."
        if d.get("type") == "regex":
            import re
            try:
                re.compile(d.get("arg", ""))
            except re.error as exc:
                return f"Invalid regex: {exc}"
        return ""

    return RecordEditor(parent, app, [Col("label", "Check", 250), Col("type", "Kind", 150, lambda r: CHECK_TYPES[r["type"]].label if r.get("type") in CHECK_TYPES else r.get("type", "")),
                                      Col("level", "If it fails", 90, lambda r: LEVEL_LABELS.get(r.get("level"), ""))],
                        props, lambda: Check("New check", "regex").to_dict(), value, on_change, dialog_extra=extra,
                        validate=validate, noun="check", height=height, dialog_width=600)


# --------------------------------------------------------------------------- document types
@editor("doctypes")
def doctypes_editor(parent, app, value, on_change, height: int = 8):
    props = [
        Prop("name", "Document name"),
        Prop("required", "Package needs this document", "bool", help="Missing required documents are reported as MISSING"),
        Prop("kinds", "File kinds", "list", help="pdf, image, office (comma separated)"),
        Prop("min_score", "Strictness (0.1 loose … 1 strict)", "float",
             help="How confident the recognition must be before a file is assigned automatically"),
        Prop("min_signals", "Signals needed", "float", help="Total signal weight that counts as 'fully recognised'"),
        Prop("low_text_bonus", "Photo-like (almost no text) bonus", "bool"),
        Prop("use_ai", "The AI may assign files to this document type", "bool",
             help="Only matters when Settings > AI > Recognising documents is not 'Rules only'."),
        Prop("ai_hint", "Describe it for the AI", "str", width=52,
             help="e.g. 'Letter from a bank confirming the account number and IBAN'. Empty = use the name and note."),
        Prop("note", "Note"),
    ]

    def extra(holder, working, collect, extra_get, vars_):
        nb = ttk.Notebook(holder)
        nb.pack(fill="both", expand=True)
        sig = ttk.Frame(nb, padding=6)
        chk = ttk.Frame(nb, padding=6)
        nb.add(sig, text="Recognition rules")
        nb.add(chk, text="Checks")
        se = rules_editor(sig, app, working.get("signals", []), None, noun="signal", height=6,
                          help_text="Rules that recognise this document. Required rules must all hit; recommended/optional add weight.")
        se.pack(fill="both", expand=True)
        ce = checks_editor(chk, app, working.get("checks", []), None, height=7)
        ce.pack(fill="both", expand=True)
        extra_get["signals"] = lambda: [{k: v for k, v in r.items() if not k.startswith("_")} for r in se.get()]
        extra_get["checks"] = ce.get

    def validate(d):
        return "" if d.get("name", "").strip() else "Give the document a name."

    def fill_id(recs):
        for r in recs:
            r["id"] = r.get("id") or slugify(r.get("name", "doc"))
        return recs

    return RecordEditor(parent, app, [Col("name", "Document", 250), Col("required", "Needed", 60, lambda r: "yes" if r.get("required", True) else "optional"),
                                      Col("signals", "Rules", 60, lambda r: len(r.get("signals", []))),
                                      Col("checks", "Checks", 60, lambda r: len(r.get("checks", []))),
                                      Col("min_score", "Strictness", 80)],
                        props, lambda: DocType(id="", name="New document", signals=[Rule("Keywords", "keyword", "", level="recommended")]).to_dict(),
                        value, (lambda recs: on_change(fill_id(recs))) if on_change else None, dialog_extra=extra,
                        validate=validate, noun="document type", height=height, dialog_width=720)


# --------------------------------------------------------------------------- clipboard / links
def _placeholder_help(holder, app, *a):
    txt = ("Placeholders: {name} {email} … any field key, {key_all}, {date} {filename} {original}.  "
           "Filters: {name|upper} {name|initials} {name|first} …  (" + " ".join(FILTERS) + ")\n"
           "Clipboard: \\t = tab (next Excel cell), \\n = new line.")
    ttk.Label(holder, text=txt, style="Muted.TLabel", wraplength=500, justify="left").pack(anchor="w")


def _csv_library():
    return [
        ("Candidate details", "Candidate name", {"header": "Candidate Name", "value": "{name}", "enabled": True}),
        ("Candidate details", "Phone", {"header": "Phone", "value": "{phone}", "enabled": True}),
        ("Candidate details", "Email", {"header": "Email", "value": "{email}", "enabled": True}),
        ("Candidate details", "Job title", {"header": "Job Title", "value": "{job_title}", "enabled": True}),
        ("Candidate details", "Company", {"header": "Company", "value": "{company}", "enabled": True}),
        ("Interview & Review", "Interviewer", {"header": "Interviewer", "value": "{interviewer}", "enabled": True}),
        ("Interview & Review", "Interview date", {"header": "Date", "value": "{date}", "enabled": True}),
        ("Interview & Review", "Review status", {"header": "Status", "value": "{status}", "enabled": True}),
        ("File details", "Saved file name", {"header": "Saved File", "value": "{saved_as}", "enabled": True}),
        ("File details", "Original file name", {"header": "Original File", "value": "{original_file}", "enabled": True}),
        ("File details", "Sub-folder", {"header": "Folder", "value": "{folder}", "enabled": True}),
        ("File details", "Timestamp", {"header": "Timestamp", "value": "{timestamp}", "enabled": True}),
    ]


@editor("csv_columns")
def csv_columns_editor(parent, app, value, on_change, height: int = 7):
    props = [
        Prop("header", "Column header", help="Header text in the first row of the CSV (e.g. Full Name, Email, Interviewer)"),
        Prop("value", "Value / Template", "str", width=52,
             help="Field key or template: e.g. {name}, {phone}, {email}, {interviewer}, {saved_as}, {date}, {name|upper}"),
    ]

    def validate(d):
        if not d.get("header", "").strip():
            return "Give the column a header title."
        if not d.get("value", "").strip():
            return "Specify a value or placeholder (e.g. {name})."
        return ""

    def placeholder_help(holder, app, *a):
        txt = ("Placeholders: {name} {email} {phone} {job_title} {company} {interviewer} … any field key.\n"
               "Files: {original_file} {saved_as} {folder} {date} {time} {year} {month} {timestamp}\n"
               "Filters: {name|upper} {name|title} {name|slug} {name|first} {name|last}")
        ttk.Label(holder, text=txt, style="Muted.TLabel", wraplength=520, justify="left").pack(anchor="w")

    cols = [
        Col("header", "Column Header", 180),
        Col("value", "Value / Template", 300),
    ]

    return RecordEditor(parent, app, cols, props,
                        lambda: {"header": "New Column", "value": "{name}", "enabled": True},
                        value, on_change, toggle_key="enabled", library=_csv_library(),
                        dialog_extra=placeholder_help, validate=validate, noun="column",
                        height=height, dialog_width=580,
                        help_text="Arrange columns using ▲ and ▼. Uncheck to exclude. Click '＋ Add column' or 'From library ▾' to add fields.")


@editor("clipboard")
def clipboard_editor(parent, app, value, on_change, height: int = 5):
    props = [Prop("name", "Name"), Prop("template", "Template", "text", rows=3, width=52)]
    return RecordEditor(parent, app, [Col("name", "Name", 160), Col("template", "Template", 380)], props,
                        lambda: {"name": "New format", "template": r"{name}\t{email}\t{phone}"}, value, on_change,
                        toggle_key=None, dialog_extra=_placeholder_help, noun="format", height=height,
                        validate=lambda d: "" if d.get("name", "").strip() else "Give the format a name.")


@editor("links")
def links_editor(parent, app, value, on_change, height: int = 4):
    props = [Prop("name", "Name"), Prop("template", "URL template", "text", rows=2, width=52)]
    return RecordEditor(parent, app, [Col("name", "Name", 160), Col("template", "Template", 380)], props,
                        lambda: {"name": "Archive link", "template": "https://example.com/Archive/{filename}"}, value,
                        on_change, toggle_key=None, dialog_extra=_placeholder_help, noun="link", height=height,
                        validate=lambda d: "" if d.get("name", "").strip() else "Give the link a name.")


@editor("rules")
def _rules(parent, app, value, on_change, height: int = 7):
    return rules_editor(parent, app, value, on_change, height=height)
