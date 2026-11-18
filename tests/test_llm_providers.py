"""Tests for the provider-agnostic AI client (Ollama / OpenAI-compatible / Anthropic) and the per-task slots.

    python tests/test_llm_providers.py        (or: pytest tests/test_llm_providers.py)

Fully offline: fake servers answer every request.
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from fake_ollama import FakeOllama  # noqa: E402
from fake_providers import FakeApi  # noqa: E402
from PIL import Image  # noqa: E402

from utilshub import options  # noqa: E402
from utilshub.core import aitasks, llm  # noqa: E402
from utilshub.core.settings import Settings  # noqa: E402

SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}


def oa_cfg(srv, **kw) -> llm.LlmConfig:
    kw.setdefault("api_key", "sk-test")
    kw.setdefault("text_model", "gpt-test")
    kw.setdefault("vision_model", "gpt-test")
    return llm.LlmConfig(enabled=True, provider="openai", host=srv.url + "/v1", **kw)


def an_cfg(srv, **kw) -> llm.LlmConfig:
    kw.setdefault("api_key", "ak-test")
    kw.setdefault("text_model", "claude-test")
    kw.setdefault("vision_model", "claude-test")
    return llm.LlmConfig(enabled=True, provider="anthropic", host=srv.url, **kw)


def fresh() -> None:
    llm.reset_backoff()
    llm.clear_cache()
    llm._QUIRKS.clear()


# --------------------------------------------------------------------------- OpenAI-compatible
def test_openai_request_and_answer() -> None:
    fresh()
    with FakeApi(lambda k, p, h: {"label": "cv"}, api_key="sk-test") as srv:
        out = llm.ask_json(oa_cfg(srv), "classify this", system="SYS", schema=SCHEMA)
        assert out == {"label": "cv"}
        kind, p, h = srv.calls[0]
        assert kind == "openai" and h["authorization"] == "Bearer sk-test"
        assert p["model"] == "gpt-test" and p["temperature"] == 0 and p["stream"] is False
        assert p["messages"][0] == {"role": "system", "content": "SYS"}
        rf = p["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA


def test_openai_url_forms() -> None:
    fresh()
    with FakeApi(lambda k, p, h: "pong") as srv:                      # no /v1 typed: it is added
        cfg = llm.LlmConfig(enabled=True, provider="openai", host=srv.url, text_model="m")
        assert cfg.base_url.endswith("/v1")
        assert llm.ask_text(cfg, "ping") == "pong"
    # provider changed but the address was left at the Ollama default -> the provider's own address
    assert llm.LlmConfig(provider="openai").base_url == "https://api.openai.com/v1"
    assert llm.LlmConfig(provider="anthropic").base_url == "https://api.anthropic.com"
    assert llm.LlmConfig(provider="ollama").base_url == "http://localhost:11434"


def test_openai_images_become_image_url_parts() -> None:
    fresh()
    with FakeApi(lambda k, p, h: "text in image") as srv:
        img = Image.new("RGB", (40, 20), "white")
        assert llm.ocr_image(oa_cfg(srv), img) == "text in image"
        msg = srv.calls[0][1]["messages"][-1]
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "image_url" and msg["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_openai_gives_up_unsupported_parameters_and_remembers() -> None:
    fresh()

    def handler(kind, p, h):
        if "temperature" in p:                                  # a reasoning model that only accepts the default
            return 400
        if p.get("response_format", {}).get("type") == "json_schema":     # a server that only knows json_object
            return 400
        return {"label": "a"}
    with FakeApi(handler) as srv:
        cfg = oa_cfg(srv)
        assert llm.ask_json(cfg, "x", schema=SCHEMA) == {"label": "a"}
        n = len(srv.calls)
        last = srv.calls[-1][1]
        assert "temperature" not in last and last["response_format"] == {"type": "json_object"}
        assert llm.ask_json(cfg, "y", schema=SCHEMA) == {"label": "a"}
        assert len(srv.calls) == n + 1, "the second call goes straight to the accepted form"


def test_plain_400_is_still_an_error() -> None:
    fresh()
    with FakeApi(lambda k, p, h: 400) as srv:
        try:
            llm.ask_text(oa_cfg(srv), "x")
            raise AssertionError("expected an error")
        except llm.LlmError as exc:
            assert "refused" in str(exc)
        assert not llm._QUIRKS, "a failed request must not teach the client wrong quirks"


def test_bad_key_is_unavailable_with_a_clear_message() -> None:
    fresh()
    with FakeApi(api_key="right") as srv:
        try:
            llm.ask_text(oa_cfg(srv, api_key="wrong"), "x")
            raise AssertionError("expected an error")
        except llm.LlmUnavailable as exc:
            assert "rejected the API key" in str(exc)
        ok, msg, _ = llm.ping(oa_cfg(srv, api_key="wrong"))
        assert not ok and "rejected the API key" in msg


def test_rate_limit_is_retried() -> None:
    fresh()
    state = {"n": 0}

    def handler(kind, p, h):
        state["n"] += 1
        return 429 if state["n"] == 1 else "fine"
    with FakeApi(handler) as srv:
        assert llm.ask_text(oa_cfg(srv), "x") == "fine" and state["n"] == 2


def test_thinking_blocks_are_removed() -> None:
    fresh()
    with FakeApi(lambda k, p, h: "<think>let me see...</think>{\"label\": \"x\"}") as srv:
        assert llm.ask_json(oa_cfg(srv), "q", schema=SCHEMA) == {"label": "x"}


def test_missing_key_for_a_cloud_server_is_caught_before_sending() -> None:
    fresh()
    saved = {k: os.environ.pop(k, None) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    try:
        cfg = llm.LlmConfig(enabled=True, provider="openai", host="https://api.openai.com/v1", text_model="m", allow_cloud=True)
        try:
            llm.ask_text(cfg, "x")
            raise AssertionError("expected an error")
        except llm.LlmUnavailable as exc:
            assert "No API key" in str(exc)
        os.environ["OPENAI_API_KEY"] = "sk-env"
        assert cfg.profile().api_key == "sk-env", "the environment variable is the fallback"
        assert llm.LlmConfig(provider="openai", api_key="mine").profile().api_key == "mine"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_no_model_listing_falls_back_to_a_test_question() -> None:
    fresh()
    with FakeApi(lambda k, p, h: "OK") as srv:
        srv.list_status = 404
        ok, msg, _ = llm.ping(oa_cfg(srv))
        assert ok and "answered a test question" in msg, msg


# --------------------------------------------------------------------------- Anthropic
def test_anthropic_request_and_answer() -> None:
    fresh()
    with FakeApi(lambda k, p, h: '{"label": "visa"}', api_key="ak-test") as srv:
        out = llm.ask_json(an_cfg(srv), "which?", system="SYS", schema=SCHEMA)
        assert out == {"label": "visa"}
        kind, p, h = srv.calls[0]
        assert kind == "anthropic" and h["x-api-key"] == "ak-test" and h["anthropic-version"] == llm.ANTHROPIC_VERSION
        assert p["model"] == "claude-test" and p["max_tokens"] >= 1000
        assert p["system"].startswith("SYS") and "JSON" in p["system"]
        assert all(m["role"] != "system" for m in p["messages"])


def test_anthropic_images() -> None:
    fresh()
    with FakeApi(lambda k, p, h: "scanned text") as srv:
        assert llm.ocr_image(an_cfg(srv), Image.new("RGB", (30, 30), "white")) == "scanned text"
        blocks = srv.calls[0][1]["messages"][-1]["content"]
        assert blocks[0]["type"] == "image" and blocks[0]["source"]["type"] == "base64"
        assert blocks[0]["source"]["media_type"] == "image/jpeg" and blocks[-1]["type"] == "text"


def test_anthropic_models_and_ping() -> None:
    fresh()
    with FakeApi(api_key="ak-test") as srv:
        assert "claude-test-20250101" in llm.list_models(an_cfg(srv))
        ok, msg, models = llm.ping(an_cfg(srv, text_model="claude-test", vision_model="claude-test"))
        assert ok and "available" in msg and models, msg                  # alias matches the dated id
        ok, msg, _ = llm.ping(an_cfg(srv, api_key="nope"))
        assert not ok and "rejected" in msg


# --------------------------------------------------------------------------- privacy gate
def test_cloud_is_blocked_until_allowed() -> None:
    fresh()
    cfg = llm.LlmConfig(enabled=True, provider="openai", host="https://api.openai.com/v1", api_key="k", text_model="m")
    assert not cfg.profile().is_local
    try:
        llm.ask_text(cfg, "secret document")
        raise AssertionError("expected the gate to stop the request")
    except llm.LlmUnavailable as exc:
        assert "Allow cloud AI" in str(exc)
    assert not llm._DOWN, "blocked before any network traffic"
    for host in ("http://localhost:11434", "http://127.0.0.1:1234/v1", "http://192.168.1.20:11434", "http://myserver:11434"):
        assert llm.LlmConfig(host=host).profile().is_local, host
    for host in ("https://api.groq.com/openai/v1", "http://8.8.8.8:11434"):
        assert not llm.LlmConfig(provider="openai", host=host).profile().is_local, host


# --------------------------------------------------------------------------- slots
def test_slot_resolution_and_key_safety() -> None:
    base = dict(enabled=True, provider="openai", host="https://api.openai.com/v1", api_key="DEFAULT-KEY",
                text_model="gpt-text", vision_model="gpt-vis")
    cfg = llm.LlmConfig(**base, slots={
        "classify": {"model": "small-model"},                                          # only the model
        "extraction": {"host": "https://api.groq.com/openai/v1", "model": "llama-x"},  # another address
        "vision": {"provider": "anthropic", "api_key": "AK", "model": "claude-v"}})    # another provider
    r = cfg.profile("reasoning")
    assert (r.model, r.api_key) == ("gpt-text", "DEFAULT-KEY")
    c = cfg.profile("classify")
    assert (c.model, c.api_key, c.base_url) == ("small-model", "DEFAULT-KEY", "https://api.openai.com/v1")
    e = cfg.profile("extraction")
    assert e.base_url == "https://api.groq.com/openai/v1" and e.api_key == "", "the default key is never sent to another address"
    v = cfg.profile("vision")
    assert (v.provider, v.base_url, v.api_key, v.model) == ("anthropic", "https://api.anthropic.com", "AK", "claude-v")
    assert cfg.profile("nonsense").model == "gpt-text"
    assert cfg.profile("classify", vision=True).model == "claude-v", "pictures always use the vision slot"
    fps = {cfg.fingerprint(t) for t in ("reasoning", "classify", "extraction", "vision")}
    assert len(fps) == 4 and not any("KEY" in f or "AK" in f.split("|")[0] for f in fps)


def test_each_capability_reaches_its_own_model() -> None:
    """Ollama for text + OpenAI-style server for classification + Anthropic-style server for vision, in one run."""
    fresh()
    with FakeOllama(lambda path, p: {"label": "none", "answer": "x", "ok": True}, models=("llama3.2:latest",)) as oll, \
            FakeApi(lambda k, p, h: {"label": "cv"}, api_key="sk-test") as oa, \
            FakeApi(lambda k, p, h: "VISION TEXT", api_key="ak-test") as an:
        cfg = llm.LlmConfig(enabled=True, host=oll.url, text_model="llama3.2", vision_model="llama3.2",
                            slots={"classify": {"provider": "openai", "host": oa.url + "/v1", "api_key": "sk-test", "model": "gpt-cls"},
                                   "vision": {"provider": "anthropic", "host": an.url, "api_key": "ak-test", "model": "claude-v"}})
        assert aitasks.classify_into(cfg, "some CV text", ["cv", "invoice"]) == "cv"
        assert oa.calls and oa.calls[0][1]["model"] == "gpt-cls" and not oll.calls
        assert llm.ocr_image(cfg, Image.new("RGB", (20, 20), "white")) == "VISION TEXT"
        assert an.calls[0][1]["model"] == "claude-v"
        aitasks.answer_question(cfg, "text", "what?")                                 # extraction slot -> the default (Ollama)
        assert oll.calls and oll.calls[-1][1]["model"] == "llama3.2"
        assert len(oa.calls) == 1 and len(an.calls) == 1


def test_ping_checks_every_connection_once() -> None:
    fresh()
    with FakeOllama(models=("llama3.2:latest", "qwen2.5vl:latest")) as oll, FakeApi(api_key="sk-test") as oa:
        cfg = llm.LlmConfig(enabled=True, host=oll.url,
                            slots={"classify": {"provider": "openai", "host": oa.url + "/v1", "api_key": "sk-test", "model": "gpt-test"}})
        ok, msg, models = llm.ping(cfg)
        assert ok, msg
        assert msg.count("Connected to Ollama") == 1 and msg.count("Connected to OpenAI-compatible API") == 1, msg
        assert "classify model 'gpt-test': available" in msg and "text model 'llama3.2': installed" in msg
        assert "gpt-test" in models and "llama3.2:latest" in models


def test_cache_keys_follow_provider_and_model() -> None:
    a = llm.LlmConfig(host="http://localhost:11434", text_model="m")
    b = llm.LlmConfig(provider="openai", host="http://localhost:11434", text_model="m")      # same words, other provider
    assert a.fingerprint("classify") != b.fingerprint("classify")


# --------------------------------------------------------------------------- settings
def test_settings_round_trip_and_secrets() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = Settings(Path(d) / "s.json", autosave=False)
        s.register(options.AI)
        cfg0 = llm.LlmConfig.from_settings(s)                                         # an old settings file: still Ollama
        assert cfg0.provider == "ollama" and cfg0.host == "http://localhost:11434" and not cfg0.slots and not cfg0.allow_cloud
        s.set("ai.enabled", True, save=False)
        s.set("ai.provider", "openai", save=False)
        s.set("ai.api_key", "sk-secret", save=False)
        s.set("ai.allow_cloud", True, save=False)
        s.set("ai.classify.model", "gpt-mini", save=False)
        s.set("ai.vision.provider", "anthropic", save=False)
        s.set("ai.vision.api_key", "ak-secret", save=False)
        cfg = llm.LlmConfig.from_settings(s)
        assert cfg.enabled and cfg.allow_cloud and cfg.provider == "openai" and cfg.api_key == "sk-secret"
        assert cfg.profile("classify").model == "gpt-mini" and cfg.profile("vision").provider == "anthropic"
        out = Path(d) / "export.json"
        s.export_to(out)
        text = out.read_text(encoding="utf-8")
        assert "sk-secret" not in text and "ak-secret" not in text and "gpt-mini" in text, "keys never leave in an export"


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok", name)
        except Exception as exc:                                  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("FAIL", name, exc)
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} provider tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
