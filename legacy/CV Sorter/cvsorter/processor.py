"""The pipeline glue: read -> extract (analyze), then rename + move (finalize)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import history
from .config import Settings
from .extractor import CVExtractor
from .fileops import duplicates_dir, render_filename, transfer
from .links import build_links
from .readers import read_document
from .registry import NameRegistry

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    path: Path
    text: str = ""
    fields: dict[str, list[str]] = field(default_factory=dict)
    used_ocr: bool = False
    error: str | None = None


@dataclass
class Decision:
    """What the user chose in the verification pop-up."""
    action: str                        # "save" | "duplicate" | "skip"
    fields: dict[str, list[str]] = field(default_factory=dict)
    clip_name: str | None = None       # clipboard format to copy after saving
    move: bool = True                  # False: record the data but leave the file where it is


@dataclass
class Outcome:
    status: str                        # saved | recorded | duplicate | exists
    dest: Path | None                  # where the file ended up (None if it was not moved)
    filename: str                      # final (or planned) file name incl. extension
    links: dict[str, list[str]] = field(default_factory=dict)
    name_added: bool = False           # the name was appended to the names list


def analyze(path: Path, settings: Settings) -> AnalysisResult:
    """Never raises: failures are reported through `AnalysisResult.error`."""
    try:
        read = read_document(path, settings)
        fields = CVExtractor(settings).extract(read.text, path)
        return AnalysisResult(path, read.text, fields, read.used_ocr)
    except Exception as exc:
        log.exception("Failed to analyze %s", path)
        return AnalysisResult(path, error=str(exc) or exc.__class__.__name__)


def finalize(result: AnalysisResult, decision: Decision, settings: Settings,
             registry: NameRegistry | None = None) -> Outcome:
    """Carry out a "save" or "duplicate" decision."""
    fields = decision.fields
    stem = render_filename(settings.filename_template, fields, result.path)
    suffix = result.path.suffix.lower()
    dest: Path | None = None

    if decision.action == "duplicate":
        dup_stem = stem if settings.duplicates_rename else result.path.stem
        dest = transfer(result.path, duplicates_dir(settings), dup_stem, "move", "number")
        status = "duplicate"
    elif not decision.move:
        status = "recorded"
    else:
        dest = transfer(result.path, Path(settings.output_folder), stem,
                        settings.file_action, settings.duplicate_policy)
        status = "saved" if dest else "exists"

    filename = dest.name if dest else f"{stem}{suffix}"
    links = build_links(settings, fields, result.path, filename)

    name_added = False
    if status in ("saved", "recorded") and registry is not None and settings.names_auto_add:
        name = (fields.get("name") or [""])[0]
        name_added = registry.add(name) if name else False

    if settings.keep_history and status != "exists":
        try:
            history.append(settings, result.path, dest, {**fields, **links}, status)
        except OSError:
            log.exception("Could not write history")
    return Outcome(status, dest, filename, links, name_added)
