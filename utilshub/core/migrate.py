"""Import settings from the old separate apps (best effort, never raises)."""
from __future__ import annotations

import json
from pathlib import Path

CANDIDATES = {
    "cvsorter": ["config.json", "cvsorter/config.json"],
    "docpacker": ["docpacker_config.json"],
    "file_picker": [".file_picker_settings.json"],
    "screenshot": ["extractor_settings.json"],
}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def import_old_settings(settings, folder: str | Path) -> list[str]:
    """Copy what maps cleanly into the new settings; returns human-readable lines of what was imported."""
    folder, done = Path(folder), []
    search = [folder, Path.home()]
    for app, names in CANDIDATES.items():
        for base in search:
            for n in names:
                p = base / n
                data = _load(p) if p.is_file() else None
                if not isinstance(data, dict):
                    continue
                count = 0
                for key, value in data.items():
                    full = f"{app}.{key}"
                    if settings.opt(full) is not None:
                        settings.set(full, value)
                        count += 1
                    elif settings.opt(f"match.{key}") is not None:
                        settings.set(f"match.{key}", value)
                        count += 1
                    elif settings.opt(f"ocr.{key}") is not None:
                        settings.set(f"ocr.{key}", value)
                        count += 1
                done.append(f"{app}: {count} setting(s) from {p.name}")
                break
            else:
                continue
            break
    return done
