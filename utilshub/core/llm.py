"""Local LLM support through Ollama (https://ollama.com) - everything stays on your own computer.

Only the standard library is used (no extra dependency).  Nothing here runs unless the master switch
`ai.enabled` is on; every function raises `LlmUnavailable` (server down / model missing / switched off) or
`LlmError` (bad answer, timeout) so callers can fall back to the rules-based way.

    cfg = LlmConfig.from_settings(settings)
    ok, message, models = ping(cfg)                       # "Test connection"
    text = ask_text(cfg, "Summarise ...", system="...")
    data = ask_json(cfg, "Extract ...", schema={...})     # parsed dict / list
    text = ocr_image(cfg, pil_image)                      # vision model reads an image
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
SUGGESTED_TEXT_MODELS = ["llama3.2", "qwen2.5", "llama3.1", "mistral", "gemma2"]
SUGGESTED_VISION_MODELS = ["qwen2.5vl", "llama3.2-vision", "minicpm-v", "gemma3", "llava"]


class LlmError(Exception):
    """The model answered badly or too slowly."""


class LlmUnavailable(LlmError):
    """AI is switched off, Ollama is not running, or the model is not installed."""


# --------------------------------------------------------------------------- configuration
@dataclass
class LlmConfig:
    enabled: bool = False                 # the master switch (Settings > AI)
    host: str = DEFAULT_HOST
    text_model: str = "llama3.2"
    vision_model: str = "qwen2.5vl"
    timeout: int = 120                    # seconds per request
    max_chars: int = 12000                # document text sent to the model is cut to this length
    cross_check: bool = False             # in "Rules + AI" modes also run the second method and flag disagreements
    keep_alive: str = "5m"                # how long Ollama keeps the model in memory

    @classmethod
    def from_settings(cls, s) -> "LlmConfig":
        return cls(s.get_bool("ai.enabled", False), s.get("ai.host", DEFAULT_HOST), s.get("ai.text_model", "llama3.2"),
                   s.get("ai.vision_model", "qwen2.5vl"), max(5, s.get_int("ai.timeout", 120)),
                   max(500, s.get_int("ai.max_chars", 12000)), s.get_bool("ai.cross_check", False),
                   s.get("ai.keep_alive", "5m"))

    @property
    def base_url(self) -> str:
        host = (self.host or DEFAULT_HOST).strip().rstrip("/")
        return host if re.match(r"^https?://", host, re.I) else "http://" + host


# --------------------------------------------------------------------------- transport
_DOWN: dict[str, float] = {}              # base url -> time until which we do not try again (server was unreachable)
_DOWN_SECONDS = 20.0
_lock = threading.Lock()


def reset_backoff() -> None:
    with _lock:
        _DOWN.clear()


def _request(cfg: LlmConfig, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
    base = cfg.base_url
    with _lock:
        until = _DOWN.get(base, 0.0)
    if until > time.monotonic():
        raise LlmUnavailable(f"Ollama is not reachable at {base} (will retry in a few seconds).")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            pass
        if exc.code == 404:
            model = (payload or {}).get("model", "")
            raise LlmUnavailable(f"Model '{model}' is not installed in Ollama. Install it with:  ollama pull {model}"
                                 if model else f"Ollama answered 404 ({detail or path}).") from exc
        raise LlmError(f"Ollama error {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise LlmError(f"Ollama took longer than {cfg.timeout} s to answer.") from exc
        with _lock:
            _DOWN[base] = time.monotonic() + _DOWN_SECONDS
        raise LlmUnavailable(f"Ollama is not reachable at {base}. Start it (ollama serve) or check Settings > AI.") from exc
    except TimeoutError as exc:
        raise LlmError(f"Ollama took longer than {cfg.timeout} s to answer.") from exc
    except OSError as exc:
        raise LlmUnavailable(f"Could not talk to Ollama: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise LlmError("Ollama sent an answer that is not JSON.") from exc


def list_models(cfg: LlmConfig) -> list[str]:
    data = _request(cfg, "/api/tags", timeout=min(cfg.timeout, 10))
    return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


def _has_model(models: list[str], wanted: str) -> bool:
    wanted = (wanted or "").strip().lower()
    if not wanted:
        return False
    names = [m.lower() for m in models]
    return wanted in names or any(n.split(":")[0] == wanted.split(":")[0] and ":" not in wanted for n in names)


def ping(cfg: LlmConfig) -> tuple[bool, str, list[str]]:
    """'Test connection': (ok, human message, installed models).  Never raises."""
    reset_backoff()
    try:
        models = list_models(cfg)
    except LlmError as exc:
        return False, str(exc), []
    notes = []
    for label, model in (("text", cfg.text_model), ("vision", cfg.vision_model)):
        notes.append(f"{label} model '{model}': " + ("installed" if _has_model(models, model)
                                                      else f"NOT installed (ollama pull {model})"))
    msg = f"Connected to Ollama at {cfg.base_url} - {len(models)} model(s) installed.\n" + "\n".join(notes)
    return True, msg, models


def chat(cfg: LlmConfig, model: str, messages: list[dict], fmt: Any = None, num_predict: int | None = None) -> str:
    """One non-streaming /api/chat call; returns the assistant's text."""
    if not cfg.enabled:
        raise LlmUnavailable("AI is switched off (Settings > AI).")
    if not (model or "").strip():
        raise LlmUnavailable("No model is chosen (Settings > AI).")
    options: dict[str, Any] = {"temperature": 0}
    if num_predict:
        options["num_predict"] = num_predict
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "options": options,
                               "keep_alive": cfg.keep_alive or "5m"}
    if fmt:
        payload["format"] = fmt
    data = _request(cfg, "/api/chat", payload)
    msg = data.get("message") or {}
    content = msg.get("content")
    if content is None:
        raise LlmError("The model returned an empty answer.")
    return str(content)


# --------------------------------------------------------------------------- helpers
def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.75)
    return text[:head] + "\n[... text cut ...]\n" + text[-(limit - head):]


def extract_json(text: str) -> Any:
    """Parse a JSON value out of a model answer (tolerates ```json fences and chatter around it). None if hopeless."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = s.find(open_c), s.rfind(close_c)
        if 0 <= a < b:
            try:
                return json.loads(s[a:b + 1])
            except ValueError:
                continue
    return None


def image_to_b64(image, max_side: int = 1600) -> str:
    """PIL image -> base64 JPEG (downscaled; vision models do not need more)."""
    im = image.convert("RGB")
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ask_text(cfg: LlmConfig, prompt: str, system: str = "", images: list[str] | None = None, vision: bool = False,
             fmt: Any = None, num_predict: int | None = None) -> str:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    user: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        user["images"] = images
    messages.append(user)
    return chat(cfg, cfg.vision_model if (vision or images) else cfg.text_model, messages, fmt, num_predict).strip()


def ask_json(cfg: LlmConfig, prompt: str, system: str = "", schema: Any = None, images: list[str] | None = None,
             vision: bool = False) -> Any:
    """Like ask_text but the answer must be JSON (`schema` = a JSON schema dict, or None for 'any JSON')."""
    raw = ask_text(cfg, prompt, system, images, vision, fmt=schema or "json")
    data = extract_json(raw)
    if data is None:
        raise LlmError("The model did not return valid JSON.")
    return data


# --------------------------------------------------------------------------- OCR with a vision model
_OCR_CACHE: "OrderedDict[str, str]" = OrderedDict()
_OCR_CACHE_MAX = 64

OCR_PROMPT = ("Transcribe ALL the text in this image exactly as written, in reading order. Keep line breaks. "
              "Do not translate, summarise, explain or add anything. If a line is unreadable write nothing for it. "
              "If the image has no text at all, answer with nothing.")


def clear_cache() -> None:
    """Forget cached OCR answers (the cache is keyed by picture + model, not by server)."""
    with _lock:
        _OCR_CACHE.clear()


def ocr_image(cfg: LlmConfig, image, languages: str = "") -> str:
    """Read the text of a PIL image with the vision model (cached in memory per image + model)."""
    b64 = image_to_b64(image)
    key = hashlib.sha1((cfg.vision_model + "|" + languages + "|" + b64).encode("ascii")).hexdigest()
    with _lock:
        if key in _OCR_CACHE:
            _OCR_CACHE.move_to_end(key)
            return _OCR_CACHE[key]
    prompt = OCR_PROMPT + (f" Expected languages: {languages}." if languages else "")
    text = ask_text(cfg, prompt, images=[b64], vision=True)
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    with _lock:
        _OCR_CACHE[key] = text
        while len(_OCR_CACHE) > _OCR_CACHE_MAX:
            _OCR_CACHE.popitem(last=False)
    return text
