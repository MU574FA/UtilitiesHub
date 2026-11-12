"""AI for DocPacker: recognise documents, split files, answer checklist questions, compare names, fill the cover.

Each feature has its own mode (Rules / AI / Rules then AI / AI then Rules, see core/aimode.py):

    ai.doctype_mode   recognise()  page_labels()     which document is this file / page?
    ai.checks_mode    run_ai_check()                  "AI question" checks of a document type
    ai.names_mode     name_in_text_check() ...        "name appears in the document" style checks
    ai.cover_mode     fill_cover()                    name / surname / custom fields on the cover form

The model can take seconds, but checks are re-run on the UI thread whenever the user types.  So every AI answer goes
through `ai_call`: it is cached (same document + same question = one model call) and, when `AiContext.background` is
set by the tool, computed in a background job - the check shows "AI is checking ..." and the tool re-checks when the
answer is ready (`AiContext.on_ready`).  Without `background` (tests, scripts) calls simply block.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import aimode, aitasks
from .aimode import AI, RULES, AiPending
from .llm import LlmConfig, LlmError

log = logging.getLogger(__name__)

UNASSIGNED = ""


# --------------------------------------------------------------------------- context
@dataclass
class AiContext:
    """Settings + hooks the DocPacker logic needs for AI.  `AiContext()` = AI off (Rules only)."""
    llm: LlmConfig | None = None
    doctype_mode: str = RULES
    checks_mode: str = RULES
    names_mode: str = RULES
    cover_mode: str = RULES
    min_confidence: float = 0.6
    max_pages: int = 30
    # set by the tool: run `work()` in a background job, then call `done()` on the UI thread
    background: Callable[[Callable[[], None], Callable[[], None]], None] | None = None
    on_ready: Callable[[], None] = lambda: None

    @classmethod
    def from_settings(cls, s) -> "AiContext":
        return cls(LlmConfig.from_settings(s), aimode.mode_for(s, "doctype"), aimode.mode_for(s, "checks"),
                   aimode.mode_for(s, "names"), aimode.mode_for(s, "cover"),
                   min(1.0, max(0.0, s.get_int("ai.min_confidence", 60) / 100)), max(1, s.get_int("ai.max_pages", 30)))

    @property
    def enabled(self) -> bool:
        return self.llm is not None and self.llm.enabled

    def mode(self, feature: str) -> str:
        return aimode.normalise(getattr(self, f"{feature}_mode", RULES)) if self.enabled else RULES

    @property
    def cross_check(self) -> bool:
        return bool(self.llm and self.llm.cross_check)


# --------------------------------------------------------------------------- cache + background
_MISS = object()
_ERROR_TTL = 30.0
_lock = threading.Lock()
_cache: "OrderedDict[Any, tuple[str, Any, float]]" = OrderedDict()
_pending: set = set()
_CACHE_MAX = 800


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _pending.clear()


def _get(key):
    with _lock:
        item = _cache.get(key)
        if item is None:
            return _MISS
        kind, value, stamp = item
        if kind == "err" and time.monotonic() - stamp > _ERROR_TTL:
            del _cache[key]
            return _MISS
        _cache.move_to_end(key)
        return item


def _put(key, kind: str, value) -> None:
    with _lock:
        _cache[key] = (kind, value, time.monotonic())
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def ai_call(ai: AiContext, key, compute: Callable[[], Any], blocking: bool | None = None):
    """The answer of `compute()` (cached).  In non-blocking mode a miss raises AiPending and the answer is computed
    in the background; AI errors raise LlmError (remembered for a short time so the UI does not retry in a loop)."""
    hit = _get(key)
    if hit is not _MISS:
        kind, value, _ = hit
        if kind == "err":
            raise LlmError(value)
        return value
    if blocking is None:
        blocking = ai.background is None
    if blocking:
        try:
            value = compute()
        except LlmError as exc:
            _put(key, "err", str(exc))
            raise
        _put(key, "ok", value)
        return value
    with _lock:
        if key in _pending:
            raise AiPending()
        _pending.add(key)

    def work() -> None:
        try:
            _put(key, "ok", compute())
        except LlmError as exc:
            _put(key, "err", str(exc))
        except Exception as exc:                          # keep the app alive whatever the model does
            log.warning("AI job failed: %s", exc, exc_info=True)
            _put(key, "err", str(exc))
        finally:
            with _lock:
                _pending.discard(key)

    ai.background(work, ai.on_ready)
    raise AiPending()


def _digest(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


# --------------------------------------------------------------------------- page picture (for vision questions)
def page_image(info, page: int = 0, dpi: int = 130):
    """PIL picture of one page of a file (images and PDFs), rotated upright; None when it cannot be made."""
    try:
        from .imaging import open_rgb, rotate_cw
        rot = (info.page_rot[page] if info.page_rot and page < len(info.page_rot) else 0) % 360
        img = None
        if info.kind == "image":
            img = open_rgb(info.path)
        elif info.kind == "pdf":
            from .readers import fitz
            if fitz is not None:
                from PIL import Image
                with fitz.open(info.path) as pdf:
                    pix = pdf[page].get_pixmap(dpi=dpi, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if img is not None and rot:
            img = rotate_cw(img, rot)
        return img
    except Exception as exc:
        log.info("no picture for %s: %s", getattr(info, "path", "?"), exc)
        return None


# --------------------------------------------------------------------------- recognising documents
@dataclass
class Recognition:
    dtype: str = UNASSIGNED
    score: float = 0.0
    source: str = ""            # "rules" | "ai" | ""
    reason: str = ""            # the model's short reason (AI only)
    conflict: str = ""          # cross-check: the type the other method chose
    pending: bool = False       # an AI answer is on its way (the tool re-checks when it arrives)
    error: str = ""


def rules_recognise(types, info, filename: str, threshold: float, taken=frozenset()) -> tuple[str, float]:
    """The classic recognition: the best-scoring enabled type (this is what DocPacker always did)."""
    from .doctypes import score_doctype
    best, best_score = UNASSIGNED, 0.0
    for t in types:
        if not t.enabled:
            continue
        s = score_doctype(t, info, filename, threshold)
        if s >= t.min_score and s > best_score + (0 if t.id not in taken else -0.05):
            best, best_score = t.id, s
    return best, best_score


def _catalog(types) -> list[tuple[str, str, str]]:
    return [(t.id, t.name, (getattr(t, "ai_hint", "") or t.note or "").strip()) for t in types]


def _types_sig(types) -> str:
    return _digest(*[(t.id, t.name, getattr(t, "ai_hint", ""), t.note) for t in types])


def recognise(types, info, filename: str, threshold: float, ai: AiContext | None, taken=frozenset(),
              blocking: bool | None = None) -> Recognition:
    """Which document type is `info`?  Honours `ai.doctype_mode`; with no AI this equals `rules_recognise`."""
    mode = ai.mode("doctype") if ai else RULES

    def rules():
        b, s = rules_recognise(types, info, filename, threshold, taken)
        return b, s, ""

    if mode == RULES or ai is None:
        b, s, _ = rules()
        return Recognition(b, s, RULES if b else "")
    cands = [t for t in types if t.enabled and getattr(t, "use_ai", True) and (not t.kinds or info.kind in t.kinds)]

    def ask():
        if not cands:
            return UNASSIGNED, 0.0, ""
        text = info.text or ""
        use_pic = not text.strip() and bool(ai.llm and ai.llm.vision_model)
        key = ("doc", _digest(info.path, info.hash, len(text), _digest(text), filename, info.pages, _types_sig(cands),
                              ai.llm.text_model, ai.llm.vision_model if use_pic else "", ai.min_confidence))

        def compute():
            img = page_image(info) if use_pic else None
            tid, conf, why = aitasks.classify_document(ai.llm, text, _catalog(cands), filename, info.pages, img)
            return (tid or UNASSIGNED), conf, why

        tid, conf, why = ai_call(ai, key, compute, blocking)
        if not tid or conf < ai.min_confidence:
            return UNASSIGNED, 0.0, why
        return tid, conf, why

    out = aimode.run(mode, rules, ask, empty=(UNASSIGNED, 0.0, ""), ok=lambda v: bool(v[0]), cross_check=ai.cross_check,
                     same=lambda a, b: a[0] == b[0])
    tid, score, reason = out.value
    if not tid and mode == AI:                                # types that opted out of AI are always recognised by rules
        own = [t for t in types if not getattr(t, "use_ai", True)]
        if own:
            tid, score = rules_recognise(own, info, filename, threshold, taken)
            if tid:
                return Recognition(tid, score, RULES)
    return Recognition(tid, score, out.source if tid else "", reason, out.conflict[0] if out.conflict else "",
                       out.pending and not tid, out.error)


def page_labels(types, info, threshold: float, ai: AiContext | None, blocking: bool | None = None) -> list[str]:
    """Type id (or '' = unknown) for every page of a file - the basis for splitting one scan into documents."""
    from .doctypes import score_doctype

    def page_part(i: int):
        from dataclasses import replace
        texts = [info.page_texts[i]]
        return replace(info, text=texts[0], pages=1, page_texts=texts, page_meta=[], page_rot=[], hash="")

    def rules() -> list[str]:
        labels = []
        for i in range(len(info.page_texts)):
            one = page_part(i)
            best, best_score = UNASSIGNED, 0.0
            for t in types:
                if t.enabled:
                    s = score_doctype(t, one, "", threshold)
                    if s >= t.min_score and s > best_score:
                        best, best_score = t.id, s
            labels.append(best)
        return labels

    mode = ai.mode("doctype") if ai else RULES
    if mode == RULES or ai is None:
        return rules()
    cands = [t for t in types if t.enabled and getattr(t, "use_ai", True)]

    def ask() -> list[str]:
        key = ("pages", _digest(info.path, info.hash, _digest(*info.page_texts), _types_sig(cands), ai.llm.text_model,
                                ai.max_pages))
        got = ai_call(ai, key, lambda: aitasks.classify_pages(ai.llm, info.page_texts, _catalog(cands), ai.max_pages),
                      blocking)
        return [x or UNASSIGNED for x in got]

    out = aimode.run(mode, rules, ask, empty=[UNASSIGNED] * len(info.page_texts), ok=lambda labels: any(labels))
    return list(out.value)


def detect_groups(types, info, threshold: float = 0.8, ai: AiContext | None = None,
                  blocking: bool | None = None) -> list[tuple[str, list[int]]]:
    """Recognise every page on its own and join neighbours of one type: [(type id, pages)].

    A page nobody recognises continues the previous document.  [] when the file is only one document.
    With `ai` the pages are recognised by the Rules, the AI or both, according to `ai.doctype_mode`.
    """
    if info.error or len(info.page_texts) < 2:
        return []
    labels: list[str] = []
    for lab in page_labels(types, info, threshold, ai, blocking):
        labels.append(lab or (labels[-1] if labels else UNASSIGNED))
    groups: list[tuple[str, list[int]]] = []
    for i, label in enumerate(labels):
        if groups and groups[-1][0] == label:
            groups[-1][1].append(i)
        else:
            groups.append((label, [i]))
    return groups if len({g[0] for g in groups}) > 1 else []


# --------------------------------------------------------------------------- checks
def _facts(ctx) -> dict[str, str]:
    return {"applicant name": ctx.name, "applicant surname": ctx.surname,
            "joining date": ctx.joining.isoformat() if ctx.joining else ""}


def run_ai_check(ch, info, ctx) -> tuple[str, str]:
    """The 'AI question' check -> (status, detail).  Modes (ai.checks_mode):

    rules     the AI is not used: you confirm the question yourself (a manual check)
    ai        the AI answers yes -> ok, no -> the check's failure level, unsure / error -> warning
    ai_rules  like 'ai', but when the AI is unsure or fails you are asked to confirm (manual)
    rules_ai  you confirm it yourself, and the AI's opinion is shown next to the question as a hint
    """
    ai: AiContext | None = getattr(ctx, "ai", None)
    question = (ch.arg or "").strip()
    if not question:
        return "skip", "no question set"
    mode = ai.mode("checks") if ai else RULES
    if mode == RULES:
        return "manual", question
    use_pic = bool(getattr(ch, "use_image", False))
    key = ("check", _digest(info.path, info.hash, len(info.text), _digest(info.text), question, use_pic,
                            tuple(sorted(_facts(ctx).items())), ai.llm.text_model, ai.llm.vision_model if use_pic else ""))

    def compute():
        img = page_image(info) if use_pic else None
        return aitasks.check_question(ai.llm, question, info.text, _facts(ctx), info.name if hasattr(info, "name") else "", img)

    try:
        verdict, why = ai_call(ai, key, compute)
    except AiPending:
        return "manual", f"AI is checking ... ({question})"
    except LlmError as exc:
        if mode == aimode.AI:
            return "warn", f"AI could not check: {exc}"
        return "manual", f"AI unavailable - please confirm: {question}"
    if mode == aimode.RULES_AI:
        hint = {True: "AI says yes", False: "AI says no", None: "AI is unsure"}[verdict]
        return "manual", f"{hint}{(' - ' + why) if why else ''}. Please confirm: {question}"
    if verdict is True:
        return "ok", f"AI: {why}" if why else "AI: yes"
    if verdict is False:
        return ch.status_if_failed, f"AI: {why or 'no'}"
    if mode == aimode.AI_RULES:
        return "manual", f"AI is unsure{(' - ' + why) if why else ''}. Please confirm: {question}"
    return "warn", f"AI is unsure{(': ' + why) if why else ''}"


def _name_verdict(ctx, info, name: str, what: str, rules_fn) -> tuple[bool | None, str, str]:
    """(True | False | None, detail, source) for 'does the document name this person' under ai.names_mode."""
    ai: AiContext | None = getattr(ctx, "ai", None)
    mode = ai.mode("names") if ai else RULES
    if mode == RULES or ai is None:
        return rules_fn(), "", RULES

    def ask():
        key = ("name", _digest(info.path, info.hash, _digest(info.text), name, what, ai.llm.text_model))
        return ai_call(ai, key, lambda: aitasks.name_matches_document(ai.llm, name, info.text))

    why_box = {"why": ""}

    def ask_value():
        verdict, why = ask()
        why_box["why"] = why
        return verdict

    out = aimode.run(mode, rules_fn, ask_value, empty=None,
                     ok={RULES: lambda v: v is True, AI: lambda v: v is not None})
    return out.value, (f"AI: {why_box['why']}" if out.source == AI and why_box["why"] else ""), out.source


def name_in_text_check(ch, ctx, info, name: str, what: str, rules_fn, miss_text: str) -> tuple[str, str]:
    """Shared body of the 'name / surname appears in the document' checks (rules_fn() -> True/False/None)."""
    r, detail, source = _name_verdict(ctx, info, name, what, rules_fn)
    if r is None:
        return "skip", f"{what} too short to check"
    tag = " (AI)" if source == AI else ""
    return ("ok", (detail or tag.strip())) if r else (ch.status_if_failed, f"{miss_text}{tag}" + (f" - {detail}" if detail else ""))


def names_same(ctx, a: str, b: str, rules_fn) -> tuple[bool, str]:
    """Do two names denote one person?  rules_fn() -> bool is the classic comparison. -> (same, detail)."""
    ai: AiContext | None = getattr(ctx, "ai", None)
    mode = ai.mode("names") if ai else RULES
    if mode == RULES or ai is None:
        return rules_fn(), ""
    why_box = {"why": ""}

    def ask():
        key = ("same", _digest(a, b, ai.llm.text_model))
        verdict, why = ai_call(ai, key, lambda: aitasks.same_person(ai.llm, a, b))
        why_box["why"] = why
        return verdict

    out = aimode.run(mode, rules_fn, ask, empty=False, ok={RULES: lambda v: v is True, AI: lambda v: v is not None})
    detail = f"AI: {why_box['why']}" if out.source == AI and why_box["why"] else ("AI" if out.source == AI else "")
    return bool(out.value), detail


# --------------------------------------------------------------------------- cover page values
@dataclass
class CoverResult:
    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def fill_cover(ai: AiContext, docs: list[tuple[str, str]], asks: list[aitasks.FieldAsk],
               rules_values: dict[str, str], require_grounding: bool = True) -> CoverResult:
    """Values for empty cover boxes.  `docs` = [(document name, text)], `rules_values` = what the classic logic found
    (e.g. the name from the folder, the surname from the passport MRZ).  Honours `ai.cover_mode`; blocking - call it
    from a background job."""
    from .fields import grounded
    res = CoverResult()
    mode = ai.mode("cover")
    per_doc = max(800, (ai.llm.max_chars if ai.llm else 6000) // max(1, len(docs)))
    blob = "\n\n".join(f"=== {name} ===\n{aitasks.clip(text, per_doc)}" for name, text in docs if text.strip())
    box: dict[str, Any] = {}

    def ask() -> dict[str, list[str]]:
        if "raw" not in box:
            if not blob.strip():
                raise LlmError("no document text to read")
            box["raw"] = aitasks.extract_fields(ai.llm, blob, asks)
        return box["raw"]

    for a in asks:
        def rules(a=a) -> str:
            return (rules_values.get(a.key) or "").strip()

        def ai_value(a=a) -> str:
            for v in ask().get(a.key, []):
                if not require_grounding or grounded(v, blob):
                    return v
            return ""

        out = aimode.run(mode, rules, ai_value if mode != RULES else None, empty="")
        if out.error:
            res.errors.append(out.error)
        if (out.value or "").strip():
            res.values[a.key], res.sources[a.key] = out.value.strip(), out.source
    res.errors = list(dict.fromkeys(res.errors))
    return res
