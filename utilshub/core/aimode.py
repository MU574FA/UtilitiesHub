"""How a feature combines the classic way ("Rules": regex, keywords, Tesseract) with the AI (local Ollama).

Every AI-capable feature has one of four modes:

    rules     Rules only            exactly what the program did before AI existed
    ai        AI only               the model decides; if it fails the result is empty (no Rules behind it)
    rules_ai  Rules, then AI        AI is asked only when the Rules found nothing / failed
    ai_rules  AI, then Rules        Rules are used only when the AI found nothing / failed

The master switch `ai.enabled` overrides everything: while it is off every feature behaves as "rules".

    mode = mode_for(settings, "ocr")                       # reads ai.ocr_mode + ai.enabled
    out  = run(mode, rules_fn, ai_fn)                      # -> Outcome(value, source, ...)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LlmError

log = logging.getLogger(__name__)

RULES, AI, RULES_AI, AI_RULES = "rules", "ai", "rules_ai", "ai_rules"
MODES = (RULES, AI, RULES_AI, AI_RULES)
INHERIT = "inherit"

MODE_CHOICES = [(RULES, "Rules only (regex / keywords / Tesseract)"), (AI, "AI only"),
                (RULES_AI, "Rules first, AI if they find nothing"), (AI_RULES, "AI first, Rules if it finds nothing")]
MODE_CHOICES_INHERIT = [(INHERIT, "Use the default of this feature")] + MODE_CHOICES

# feature id -> label (each has its own `ai.<id>_mode` setting)
FEATURES = {"ocr": "Reading scans (OCR)", "fields": "Filling fields (CV Sorter, Screenshot ...)",
            "doctype": "Recognising documents (DocPacker)", "checks": "AI questions in DocPacker checklists",
            "cover": "Cover-page values (DocPacker)", "names": "Comparing names (DocPacker)"}


class AiPending(Exception):
    """Raised by an AI step whose answer is not ready yet (computed in the background so the window stays responsive)."""


def normalise(mode: Any) -> str:
    mode = str(mode or "").strip().lower()
    return mode if mode in MODES else RULES


def uses_ai(mode: str) -> bool:
    return normalise(mode) != RULES


def mode_for(settings, feature: str, override: str = INHERIT) -> str:
    """Effective mode of a feature (`override` = a per-item mode such as a field's own; 'inherit' = the feature's)."""
    if not settings.get_bool("ai.enabled", False):
        return RULES
    ov = str(override or INHERIT).strip().lower()
    if ov in MODES:
        return ov
    return normalise(settings.get(f"ai.{feature}_mode", RULES))


def order(mode: str) -> list[str]:
    return {RULES: [RULES], AI: [AI], RULES_AI: [RULES, AI], AI_RULES: [AI, RULES]}[normalise(mode)]


@dataclass
class Outcome:
    value: Any = None
    source: str = ""                  # "rules" | "ai" | "" (nothing produced a result)
    error: str = ""                   # why the AI (or the Rules) failed, for the log / UI
    conflict: Any = None              # cross-check: what the other method said when it disagreed
    conflict_source: str = ""
    exc: Exception | None = None      # the last exception raised by the Rules step (so callers can re-raise it)
    tried: list[str] = field(default_factory=list)
    pending: bool = False             # an AI answer is still being computed in the background (see AiPending)

    @property
    def from_ai(self) -> bool:
        """True when the AI produced the (non-empty) value."""
        return self.source == AI and _default_ok(self.value)


def _default_ok(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _norm_for_compare(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return sorted(str(v).strip().lower() for v in value)
    if isinstance(value, str):
        return " ".join(value.lower().split())
    return value


def run(mode: str, rules_fn: Callable[[], Any], ai_fn: Callable[[], Any] | None, *, empty: Any = None,
        ok: "Callable[[Any], bool] | dict[str, Callable[[Any], bool]]" = _default_ok, cross_check: bool = False,
        same: Callable[[Any, Any], bool] | None = None) -> Outcome:
    """Run the methods of `mode` in order; the first result that is `ok` wins.

    * mode "rules": the Rules function runs alone and its exceptions propagate (old behaviour preserved).
    * AI errors (`LlmError`, anything raised by the AI step) never propagate: they are recorded in `.error`.
    * If nothing is ok, the best non-ok value that was produced is returned (e.g. very short OCR text), else `empty`.
    * `ok` may be one function or {"rules": f, "ai": g} when the two methods need different "success" tests.
    * `AiPending` from the AI step means "answer comes later": the other method (if any) is used meanwhile and
      `.pending` is set so the UI can show a provisional result and refresh when the answer arrives.
    * cross_check: in the two combined modes the second method also runs when the first succeeded; a different
      answer is reported in `.conflict` (the first method's answer still wins).
    """
    mode = normalise(mode)
    if mode == RULES or ai_fn is None:
        return Outcome(rules_fn(), RULES, tried=[RULES])
    out = Outcome(value=empty)
    best: tuple[Any, str] | None = None
    steps = order(mode)

    def ok_of(step: str) -> Callable[[Any], bool]:
        return ok.get(step, _default_ok) if isinstance(ok, dict) else ok

    for i, step in enumerate(steps):
        fn = rules_fn if step == RULES else ai_fn
        out.tried.append(step)
        try:
            value = fn()
        except AiPending:
            out.pending = True
            continue
        except LlmError as exc:
            out.error = f"AI: {exc}"
            continue
        except Exception as exc:                      # a broken AI step or Rules step must not stop the other one
            if step == AI:
                log.warning("AI step failed: %s", exc, exc_info=True)
                out.error = f"AI: {exc}"
            else:
                out.exc = exc
                out.error = out.error or f"Rules: {exc}"
            continue
        if ok_of(step)(value):
            out.value, out.source = value, step
            if cross_check and len(steps) == 2 and i == 0:
                other = steps[1]
                try:
                    second = (rules_fn if other == RULES else ai_fn)()
                    if ok_of(other)(second) and not (same or (lambda a, b: _norm_for_compare(a) == _norm_for_compare(b)))(value, second):
                        out.conflict, out.conflict_source = second, other
                except Exception as exc:               # the cross-check is optional
                    log.info("cross-check (%s) skipped: %s", other, exc)
            return out
        if best is None:
            best = (value, step)
    if best is not None:
        out.value, out.source = best
    return out
