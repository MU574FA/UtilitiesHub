"""The 'Test connection' panel at the bottom of Settings > AI (any provider)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core import llm


def build_ai_panel(parent, app) -> None:
    box = ttk.LabelFrame(parent, text="Connection", padding=8)
    box.pack(fill="x", pady=(16, 4))
    out = tk.StringVar(value="Press 'Test connection' to check the default connection and every per-task override.")
    ttk.Label(box, textvariable=out, wraplength=600, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
    bar = ttk.Frame(box)
    bar.pack(fill="x")

    def test() -> None:
        cfg = llm.LlmConfig.from_settings(app.settings)
        cfg.enabled = True                              # testing works even while the master switch is off
        out.set("Testing …")

        def work(job):
            return llm.ping(cfg)

        def done(res) -> None:
            ok, msg, models = res
            out.set(msg + ("\nModels: " + ", ".join(models[:25]) + (" …" if len(models) > 25 else "") if ok and models else ""))

        app.jobs.submit(work, "Testing AI connection", tool="ai", on_done=done, on_error=lambda m: out.set(f"Failed: {m}"),
                        serial_key="ai.test")

    ttk.Button(bar, text="Test connection", style="Accent.TButton", command=test).pack(side="left")
    ttk.Label(box, text="Local: install Ollama from ollama.com, then  ollama pull llama3.2  and  ollama pull qwen2.5vl.\n"
                        "Cloud: pick a provider, paste the address and API key, tick 'Allow cloud AI'. Each task (reasoning, "
                        "extraction, classification, vision) can use its own model under 'optional override'.",
              style="Muted.TLabel", wraplength=600, justify="left").pack(anchor="w", pady=(6, 0))
