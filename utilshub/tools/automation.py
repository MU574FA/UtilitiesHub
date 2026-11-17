"""Automation: workflows, the Autopilot switchboard (watch folders, run workflows) and History & Undo."""
from __future__ import annotations

from .hub import HubTool


class AutomationHub(HubTool):
    id = "automation"
    title = "Automation"
    icon = "🤖"
    description = "Build workflows, let folders and watchers run them by themselves, and undo anything from History."
    order = 60
    MEMBERS = (("workflows", "WorkflowsTool", "Workflows"),
               ("autopilot", "AutopilotTool", "Autopilot"),
               ("history", "HistoryTool", "History & Undo"))
