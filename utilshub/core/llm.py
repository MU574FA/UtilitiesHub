"""Provider-agnostic LLM client: Ollama, any OpenAI-compatible API, and Anthropic.

Only the standard library is used (no extra dependency).  Nothing here runs unless the master switch
`ai.enabled` is on; every function raises `LlmUnavailable` (AI off / server down / bad key / model missing /
cloud not allowed) or `LlmError` (bad answer, timeout, rate limit) so callers can fall back to the rules-based way.

Providers
    ollama      a local (or remote) Ollama server - needs only an address and a model name
    openai      anything that speaks the OpenAI chat-completions API: OpenAI, Groq, OpenRouter, Together, Mistral,
                DeepSeek, Google Gemini (its OpenAI-compatible endpoint), LM Studio, vLLM ... - address + API key + model
    anthropic   Anthropic's Messages API (Claude) - API key + model

A *connection* is (provider, address, API key, model) = `Profile`.  `LlmConfig` holds a default connection (text model and
vision model) plus optional per-task overrides ("slots"): reasoning, extraction, classify, vision.  Every AI job asks for
the connection of its own slot, so each capability can use a different model - or a different provider.

    cfg = LlmConfig.from_settings(settings)
    ok, message, models = ping(cfg)                       # "Test connection"
    text = ask_text(cfg, "Summarise ...", system="...")   # slot "reasoning"
    data = ask_json(cfg, "Extract ...", schema={...}, task="extraction")
    text = ocr_image(cfg, pil_image)                      # slot "vision"
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
SUGGESTED_TEXT_MODELS = ["llama3.2", "qwen2.5", "llama3.1", "mistral", "gemma2"]
SUGGESTED_VISION_MODELS = ["qwen2.5vl", "llama3.2-vision", "minicpm-v", "gemma3", "llava"]

OLLAMA, OPENAI, ANTHROPIC = "ollama", "openai", "anthropic"
PROVIDERS = (OLLAMA, OPENAI, ANTHROPIC)
PROVIDER_LABELS = {OLLAMA: "Ollama", OPENAI: "OpenAI-compatible API", ANTHROPIC: "Anthropic"}
PROVIDER_CHOICES = [(OLLAMA, "Ollama (local, address + model)"),
                    (OPENAI, "OpenAI-compatible API (OpenAI, Groq, OpenRouter, Gemini, LM Studio ...)"),
                    (ANTHROPIC, "Anthropic (Claude)")]
DEFAULT_HOSTS = {OLLAMA: DEFAULT_HOST, OPENAI: "https://api.openai.com/v1", ANTHROPIC: "https://api.anthropic.com"}
ENV_KEYS = {OPENAI: ("OPENAI_API_KEY",), ANTHROPIC: ("ANTHROPIC_API_KEY",)}
ANTHROPIC_VERSION = "2023-06-01"

# (label, provider, address) - copy the address into Settings > AI
PRESETS = [("Ollama (this computer)", OLLAMA, DEFAULT_HOST),
           ("OpenAI", OPENAI, "https://api.openai.com/v1"),
           ("Anthropic", ANTHROPIC, "https://api.anthropic.com"),
           ("Google Gemini", OPENAI, "https://generativelanguage.googleapis.com/v1beta/openai"),
           ("Groq", OPENAI, "https://api.groq.com/openai/v1"),
           ("OpenRouter", OPENAI, "https://openrouter.ai/api/v1"),
           ("Together AI", OPENAI, "https://api.together.xyz/v1"),
           ("Mistral", OPENAI, "https://api.mistral.ai/v1"),
           ("DeepSeek", OPENAI, "https://api.deepseek.com"),
           ("LM Studio (this computer)", OPENAI, "http://localhost:1234/v1")]

# The capabilities that can each have their own model.  Jobs name their slot (see core.aitasks).
SLOTS = ("reasoning", "extraction", "classify", "vision")
SLOT_LABELS = {"reasoning": "Reasoning (questions, name checks)",
               "extraction": "Extraction (fields, short answers, file names)",
               "classify": "Classification (document types, categories)",
               "vision": "Vision (reading pictures, AI OCR)"}


class LlmError(Exception):
    """The model answered badly or too slowly."""


class LlmUnavailable(LlmError):
    """AI is switched off, the server is not running, the key/model is wrong, or cloud use is not allowed."""


class _BadRequest(LlmError):
    """HTTP 400 - lets a provider adapter retry without optional parameters."""


def normalise_provider(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("openai-compatible", "openai_compatible", "compatible", "gemini", "groq", "openrouter"):
        return OPENAI
    return v if v in PROVIDERS else OLLAMA


# --------------------------------------------------------------------------- one connection
def _is_local_host(base_url: str) -> bool:
    """True for this computer / the local network (nothing leaves the building)."""
    host = (urlparse(base_url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".internal", ".localdomain", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return "." not in host                          # a bare machine name on the LAN


@dataclass(frozen=True)
class Profile:
    """Where and how to ask one model."""
    provider: str = OLLAMA
    host: str = DEFAULT_HOST
    api_key: str = ""
    model: str = ""

    @property
    def base_url(self) -> str:
        host = (self.host or "").strip().rstrip("/")
        if not host or (self.provider != OLLAMA and host == DEFAULT_HOST):      # provider changed, address not
            host = DEFAULT_HOSTS.get(self.provider, DEFAULT_HOST)
        if not re.match(r"^https?://", host, re.I):
            host = "http://" + host
        if self.provider == OPENAI and not urlparse(host).path.strip("/"):
            host += "/v1"                                # people type https://api.openai.com or http://localhost:1234
        return host

    @property
    def label(self) -> str:
        return {OLLAMA: "Ollama", OPENAI: "The API server", ANTHROPIC: "Anthropic"}.get(self.provider, "The AI server")

    @property
    def is_local(self) -> bool:
        return _is_local_host(self.base_url)

    @property
    def needs_key(self) -> bool:
        return self.provider == ANTHROPIC or (self.provider == OPENAI and not self.is_local)

    @property
    def fingerprint(self) -> str:
        """Identifies provider + address + model (never the key) - used in cache keys."""
        return f"{self.provider}|{self.base_url}|{self.model}"


def _env_key(provider: str) -> str:
    for name in ENV_KEYS.get(provider, ()):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


# --------------------------------------------------------------------------- configuration
@dataclass
class LlmConfig:
    enabled: bool = False                 # the master switch (Settings > AI)
    host: str = DEFAULT_HOST              # default connection: address ...
    text_model: str = "llama3.2"          # ... text model ...
    vision_model: str = "qwen2.5vl"       # ... and vision model
    timeout: int = 120                    # seconds per request
    max_chars: int = 12000                # document text sent to the model is cut to this length
    cross_check: bool = False             # in "Rules + AI" modes also run the second method and flag disagreements
    keep_alive: str = "5m"                # Ollama: how long the model stays in memory
    provider: str = OLLAMA                # default connection: provider ...
    api_key: str = ""                     # ... and API key (empty = environment variable, or none for local servers)
    allow_cloud: bool = False             # may documents be sent to a server outside this computer / network?
    slots: dict = field(default_factory=dict)   # per-task overrides: {"vision": {"provider","host","api_key","model"}}

    @classmethod
    def from_settings(cls, s) -> "LlmConfig":
        slots: dict[str, dict[str, str]] = {}
        for name in SLOTS:
            ov = {k: str(s.get(f"ai.{name}.{k}", "") or "").strip() for k in ("provider", "host", "api_key", "model")}
            if any(ov.values()):
                slots[name] = ov
        return cls(enabled=s.get_bool("ai.enabled", False), host=s.get("ai.host", DEFAULT_HOST),
                   text_model=s.get("ai.text_model", "llama3.2"), vision_model=s.get("ai.vision_model", "qwen2.5vl"),
                   timeout=max(5, s.get_int("ai.timeout", 120)), max_chars=max(500, s.get_int("ai.max_chars", 12000)),
                   cross_check=s.get_bool("ai.cross_check", False), keep_alive=s.get("ai.keep_alive", "5m"),
                   provider=normalise_provider(s.get("ai.provider", OLLAMA)), api_key=str(s.get("ai.api_key", "") or "").strip(),
                   allow_cloud=s.get_bool("ai.allow_cloud", False), slots=slots)

    def default_profile(self, vision: bool = False) -> Profile:
        """The default connection (no per-task override applied)."""
        prov = normalise_provider(self.provider)
        return Profile(prov, self.host, (self.api_key or "").strip() or _env_key(prov),
                       self.vision_model if vision else self.text_model)

    def profile(self, task: str = "reasoning", vision: bool = False) -> Profile:
        """The connection to use for `task` (one of SLOTS; anything else counts as 'reasoning')."""
        slot = "vision" if vision else (task if task in SLOTS else "reasoning")
        base = self.default_profile(slot == "vision")
        ov = (self.slots or {}).get(slot) or {}
        if not ov:
            return base
        prov = normalise_provider(ov.get("provider") or base.provider)
        own_host = (ov.get("host") or "").strip()
        if prov != base.provider:
            host, key = own_host or DEFAULT_HOSTS[prov], (ov.get("api_key") or "").strip()
        elif own_host:
            host, key = own_host, (ov.get("api_key") or "").strip()      # another address: never reuse the default key
        else:
            host, key = base.host, (ov.get("api_key") or "").strip() or base.api_key
        return Profile(prov, host, key or _env_key(prov), (ov.get("model") or "").strip() or base.model)

    def model_for(self, task: str = "reasoning", vision: bool = False) -> str:
        return self.profile(task, vision).model

    def fingerprint(self, task: str = "reasoning", vision: bool = False) -> str:
        return self.profile(task, vision).fingerprint

    def all_profiles(self) -> list[tuple[str, Profile]]:
        """[(label, profile)] of everything that may be used: default text + vision, then every overridden slot."""
        out = [("text", self.default_profile(False))]
        if "vision" not in (self.slots or {}):
            out.append(("vision", self.default_profile(True)))
        out += [(name, self.profile(name)) for name in SLOTS if name in (self.slots or {})]
        return out

    @property
    def base_url(self) -> str:
        return self.default_profile().base_url


# --------------------------------------------------------------------------- transport
_DOWN: dict[str, float] = {}              # base url -> time until which we do not try again (server was unreachable)
_DOWN_SECONDS = 20.0
_QUIRKS: dict[tuple[str, str], set[str]] = {}   # (base url, model) -> optional parameters this server refused
_lock = threading.Lock()


def reset_backoff() -> None:
    with _lock:
        _DOWN.clear()


def _detail(raw: bytes | str) -> str:
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except Exception:
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "")
    return str(err or (data.get("message") if isinstance(data, dict) else "") or "")


def _http_error(prof: Profile, code: int, detail: str, payload: dict | None, reason: str) -> LlmError:
    if code in (401, 403):
        return LlmUnavailable(f"{prof.label} rejected the API key ({detail or code}). Check the key in Settings > AI.")
    if code == 404:
        model = (payload or {}).get("model", "")
        if prof.provider == OLLAMA:
            return LlmUnavailable(f"Model '{model}' is not installed in Ollama. Install it with:  ollama pull {model}"
                                  if model else f"Ollama answered 404 ({detail or 'not found'}).")
        if model:
            return LlmUnavailable(f"{prof.label} does not know the model '{model}' or the address is wrong "
                                  f"({detail or '404'}). Check Settings > AI.")
        return LlmError(f"{prof.label} answered 404 ({detail or 'not found'}) at {prof.base_url}.")
    if code == 400:
        return _BadRequest(f"{prof.label} refused the request: {detail or reason}")
    if code == 429:
        return LlmError(f"{prof.label} says: too many requests / quota used up ({detail or 429}). Try again later.")
    return LlmError(f"{prof.label} error {code}: {detail or reason}")


def _send(cfg: LlmConfig, prof: Profile, url: str, payload: dict | None = None, headers: dict | None = None,
          timeout: float | None = None) -> dict:
    base = prof.base_url
    with _lock:
        until = _DOWN.get(base, 0.0)
    if until > time.monotonic():
        raise LlmUnavailable(f"{prof.label} is not reachable at {base} (will retry in a few seconds).")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            retryable = exc.code in (429, 529) or (exc.code == 503 and prof.provider != OLLAMA)
            if retryable and attempt < 2:
                attempt += 1
                try:
                    wait = float(exc.headers.get("Retry-After", "") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(min(wait or attempt * 1.5, 8.0))
                continue
            raise _http_error(prof, exc.code, _detail(raw), payload, str(exc.reason)) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise LlmError(f"{prof.label} took longer than {cfg.timeout} s to answer.") from exc
            with _lock:
                _DOWN[base] = time.monotonic() + _DOWN_SECONDS
            hint = "Start it (ollama serve) or check Settings > AI." if prof.provider == OLLAMA else \
                "Check the address and your internet connection in Settings > AI."
            raise LlmUnavailable(f"{prof.label} is not reachable at {base}. {hint}") from exc
        except TimeoutError as exc:
            raise LlmError(f"{prof.label} took longer than {cfg.timeout} s to answer.") from exc
        except OSError as exc:
            raise LlmUnavailable(f"Could not talk to {prof.label}: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise LlmError(f"{prof.label} sent an answer that is not JSON.") from exc


def _auth_headers(prof: Profile) -> dict:
    if prof.provider == ANTHROPIC:
        return {"x-api-key": prof.api_key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": "Bearer " + prof.api_key} if prof.api_key else {}


def _join(prof: Profile, path: str) -> str:
    """URL of an endpoint; `path` is relative to the provider's API root."""
    base = prof.base_url
    if prof.provider == ANTHROPIC:
        return base + path if base.endswith("/v1") else base + "/v1" + path
    if prof.provider == OLLAMA:
        return base + "/api" + path
    return base + path


# --------------------------------------------------------------------------- model lists
def list_models(cfg: LlmConfig, prof: Profile | None = None) -> list[str]:
    prof = prof or cfg.default_profile()
    t = min(cfg.timeout, 10)
    if prof.provider == OLLAMA:
        data = _send(cfg, prof, _join(prof, "/tags"), headers=_auth_headers(prof), timeout=t)
        names = [m.get("name", "") for m in data.get("models", [])]
    else:
        if prof.needs_key and not prof.api_key:
            raise LlmUnavailable(f"No API key is set for {PROVIDER_LABELS[prof.provider]}. Enter it in Settings > AI "
                                 f"(or set the {ENV_KEYS.get(prof.provider, ('API key',))[0]} environment variable).")
        url = _join(prof, "/models") + ("?limit=100" if prof.provider == ANTHROPIC else "")
        data = _send(cfg, prof, url, headers=_auth_headers(prof), timeout=t)
        names = [str(m.get("id", "")).removeprefix("models/") for m in (data.get("data") or data.get("models") or [])
                 if isinstance(m, dict)]
    return sorted(n for n in names if n)


def _has_model(models: list[str], wanted: str, provider: str = OLLAMA) -> bool:
    wanted = (wanted or "").strip().lower().removeprefix("models/")
    if not wanted:
        return False
    names = [m.lower() for m in models]
    if provider != OLLAMA:                               # exact id, or a dated variant of the alias that was typed
        return wanted in names or any(n.startswith(wanted + "-") for n in names)
    return wanted in names or any(n.split(":")[0] == wanted.split(":")[0] and ":" not in wanted for n in names)


def ping(cfg: LlmConfig) -> tuple[bool, str, list[str]]:
    """'Test connection': (ok, human message, models seen).  Checks the default connection and every per-task override.
    Never raises."""
    reset_backoff()
    probe = replace(cfg, enabled=True, allow_cloud=True)             # testing works whatever the switches say
    listed: dict[tuple, Any] = {}                                    # connection -> model list, or the error it gave
    reported: set[tuple] = set()
    lines: list[str] = []
    problems: list[str] = []
    all_models: set[str] = set()
    answered = cloud = False
    for label, prof in cfg.all_profiles():
        ck = (prof.provider, prof.base_url, prof.api_key)
        cloud = cloud or not prof.is_local
        if ck not in listed:
            try:
                listed[ck] = list_models(probe, prof)
                all_models.update(listed[ck])
                answered = True
                n = len(listed[ck])
                lines.append(f"Connected to {PROVIDER_LABELS[prof.provider]} at {prof.base_url}"
                             + (f" - {n} model(s) installed." if prof.provider == OLLAMA else f" - {n} model(s) offered." if n else "."))
            except LlmError as exc:                                  # unreachable, bad key, or no model list
                listed[ck] = exc
        res = listed[ck]
        if isinstance(res, LlmError):
            hard = isinstance(res, LlmUnavailable) or prof.provider == OLLAMA
            if hard:
                if ck not in reported:                               # say it once per connection, not once per model
                    reported.add(ck)
                    problems.append(str(res))
                continue
            try:                                                     # no model list: ask a tiny real question instead
                chat(probe, prof, [{"role": "user", "content": "Reply with the single word OK."}], num_predict=8)
                answered = True
                lines.append(f"{label} model '{prof.model}': answered a test question (the server has no model list).")
            except LlmError as exc:
                problems.append(f"{label} model '{prof.model}': {exc}")
            continue
        found = _has_model(res, prof.model, prof.provider)
        if prof.provider == OLLAMA:
            lines.append(f"{label} model '{prof.model}': " + ("installed" if found else f"NOT installed (ollama pull {prof.model})"))
        else:
            lines.append(f"{label} model '{prof.model}': " + ("available" if found else "not in the server's model list (may still work)"))
    if cloud and not cfg.allow_cloud:
        lines.append("Note: a model is outside this computer / network. Tick 'Allow cloud AI' in Settings > AI before "
                     "documents can be sent to it.")
    if not answered:
        return False, "\n".join(problems), []
    return not problems, "\n".join(lines + problems), sorted(all_models)


# --------------------------------------------------------------------------- chat adapters
_THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)


def _chat_ollama(cfg: LlmConfig, prof: Profile, messages: list[dict], fmt: Any, num_predict: int | None) -> str:
    options: dict[str, Any] = {"temperature": 0}
    if num_predict:
        options["num_predict"] = num_predict
    payload: dict[str, Any] = {"model": prof.model, "messages": messages, "stream": False, "options": options,
                               "keep_alive": cfg.keep_alive or "5m"}
    if fmt:
        payload["format"] = fmt
    data = _send(cfg, prof, _join(prof, "/chat"), payload, _auth_headers(prof))
    content = (data.get("message") or {}).get("content")
    if content is None:
        raise LlmError("The model returned an empty answer.")
    return str(content)


def _next_flag(flags: set[str], payload: dict) -> str | None:
    """Which optional parameter to give up after a 400 (cheapest first)."""
    if ("temperature" in payload or "max_tokens" in payload) and "nosampling" not in flags:
        return "nosampling"
    rf = payload.get("response_format")
    if rf and rf.get("type") == "json_schema":
        return "jsonobject"
    if rf:
        return "nojson"
    return None


def _openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        imgs = m.get("images") or []
        if imgs:
            parts: list[dict] = [{"type": "text", "text": m.get("content", "")}]
            parts += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in imgs]
            out.append({"role": m["role"], "content": parts})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _chat_openai(cfg: LlmConfig, prof: Profile, messages: list[dict], fmt: Any, num_predict: int | None) -> str:
    qkey = (prof.base_url, prof.model)
    flags = set(_QUIRKS.get(qkey, ()))
    msgs = _openai_messages(messages)
    while True:
        payload: dict[str, Any] = {"model": prof.model, "messages": msgs, "stream": False}
        if "nosampling" not in flags:
            payload["temperature"] = 0
            if num_predict:
                payload["max_tokens"] = num_predict
        if fmt and "nojson" not in flags:
            if isinstance(fmt, dict) and "jsonobject" not in flags:
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": fmt}}
            else:
                payload["response_format"] = {"type": "json_object"}
        try:
            data = _send(cfg, prof, _join(prof, "/chat/completions"), payload, _auth_headers(prof))
            break
        except _BadRequest:
            nxt = _next_flag(flags, payload)
            if nxt is None:
                raise
            flags.add(nxt)
    if flags:
        with _lock:
            _QUIRKS.setdefault(qkey, set()).update(flags)
    choices = data.get("choices") or []
    content = ((choices[0] if choices else {}).get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if content is None:
        raise LlmError("The model returned an empty answer.")
    return str(content)


def _chat_anthropic(cfg: LlmConfig, prof: Profile, messages: list[dict], fmt: Any, num_predict: int | None) -> str:
    system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    if fmt:
        system += ("\n\n" if system else "") + "Respond with ONLY one valid JSON value" + (
            " matching this JSON schema:\n" + json.dumps(fmt) if isinstance(fmt, dict) else "") + ". No prose, no code fences."
    msgs: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        imgs = m.get("images") or []
        if imgs:
            blocks: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                                  for b in imgs]
            blocks.append({"type": "text", "text": m.get("content", "")})
            msgs.append({"role": m["role"], "content": blocks})
        else:
            msgs.append({"role": m["role"], "content": m.get("content", "")})
    qkey = (prof.base_url, prof.model)
    flags = set(_QUIRKS.get(qkey, ()))
    while True:
        payload: dict[str, Any] = {"model": prof.model, "max_tokens": num_predict or 4096, "messages": msgs}
        if system:
            payload["system"] = system
        if "nosampling" not in flags:
            payload["temperature"] = 0
        try:
            data = _send(cfg, prof, _join(prof, "/messages"), payload, _auth_headers(prof))
            break
        except _BadRequest:
            if "nosampling" in flags or "temperature" not in payload:
                raise
            flags.add("nosampling")
    if flags:
        with _lock:
            _QUIRKS.setdefault(qkey, set()).update(flags)
    text = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise LlmError("The model returned an empty answer.")
    return text


_ADAPTERS = {OLLAMA: _chat_ollama, OPENAI: _chat_openai, ANTHROPIC: _chat_anthropic}


def chat(cfg: LlmConfig, model: "str | Profile", messages: list[dict], fmt: Any = None, num_predict: int | None = None) -> str:
    """One non-streaming chat call on a connection; returns the assistant's text.

    `model` is a `Profile` (provider + address + key + model) or just a model name on the default connection.
    `messages` use Ollama's shape: {"role", "content", optional "images": [base64 jpeg]} - adapters translate."""
    if not cfg.enabled:
        raise LlmUnavailable("AI is switched off (Settings > AI).")
    prof = model if isinstance(model, Profile) else replace(cfg.default_profile(), model=str(model or ""))
    if not (prof.model or "").strip():
        raise LlmUnavailable("No model is chosen (Settings > AI).")
    if not prof.is_local and not cfg.allow_cloud:
        raise LlmUnavailable(f"The model '{prof.model}' runs outside this computer/network ({prof.base_url}), so your "
                             "documents would be sent over the internet. Tick 'Allow cloud AI' in Settings > AI to permit that.")
    if prof.needs_key and not prof.api_key:
        raise LlmUnavailable(f"No API key is set for {PROVIDER_LABELS[prof.provider]}. Enter it in Settings > AI "
                             f"(or set the {ENV_KEYS.get(prof.provider, ('API key',))[0]} environment variable).")
    text = _ADAPTERS[prof.provider](cfg, prof, messages, fmt, num_predict)
    return _THINK.sub("", text) if "<think" in text.lower() else text


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
             fmt: Any = None, num_predict: int | None = None, task: str = "reasoning") -> str:
    """Ask the model of slot `task` (pictures always go to the 'vision' slot)."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    user: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        user["images"] = images
    messages.append(user)
    return chat(cfg, cfg.profile(task, vision=bool(vision or images)), messages, fmt, num_predict).strip()


def ask_json(cfg: LlmConfig, prompt: str, system: str = "", schema: Any = None, images: list[str] | None = None,
             vision: bool = False, task: str = "reasoning") -> Any:
    """Like ask_text but the answer must be JSON (`schema` = a JSON schema dict, or None for 'any JSON')."""
    raw = ask_text(cfg, prompt, system, images, vision, fmt=schema or "json", task=task)
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
    """Forget cached OCR answers (the cache is keyed by picture + connection + model)."""
    with _lock:
        _OCR_CACHE.clear()


def ocr_image(cfg: LlmConfig, image, languages: str = "") -> str:
    """Read the text of a PIL image with the vision model (cached in memory per image + model)."""
    b64 = image_to_b64(image)
    key = hashlib.sha1((cfg.fingerprint("vision", True) + "|" + languages + "|" + b64).encode("utf-8")).hexdigest()
    with _lock:
        if key in _OCR_CACHE:
            _OCR_CACHE.move_to_end(key)
            return _OCR_CACHE[key]
    prompt = OCR_PROMPT + (f" Expected languages: {languages}." if languages else "")
    text = ask_text(cfg, prompt, images=[b64], vision=True, task="vision")
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    with _lock:
        _OCR_CACHE[key] = text
        while len(_OCR_CACHE) > _OCR_CACHE_MAX:
            _OCR_CACHE.popitem(last=False)
    return text
