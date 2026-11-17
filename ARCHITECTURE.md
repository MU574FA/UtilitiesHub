# Architecture

    utilshub/
      core/   pure logic, no Tk: settings, schema (Opt/Page), rules, fields, ocr, readers, pdfops, render,
              fileops (Journal), jobs (UiBridge/JobRunner), sheets, matching, syncing, doctypes, templates,
              workflow (bricks + engine for the Workflows tool)
      ui/     reusable widgets: theme, widgets, viewer (PageViewer), thumbs (ThumbGrid), records (RecordEditor,
              RuleTester, StringListEditor), editors, settings_ui, palette, dnd
      tools/  one module per screen, each a subclass of tools.base.Tool
      app.py  shell: window, nav, menus, status bar, automation, logging

## Adding a tool
1. Create `tools/my_tool.py` with `class MyTool(Tool)`: set `id`, `title`, `icon`, `order`.
2. Declare defaults in `options()` returning `Opt("my_tool.key", default, "Label", kind)`; they appear automatically
   on a settings page named after the tool (override `pages()` to customise or return `[]`).
3. Implement `build(parent)`. Use `self.get("key")` / `self.put("key", v)` for settings, `self.app.jobs.submit(fn, ...)`
   for background work, `self.app.bridge.call(fn)` to return to the UI thread, `self.app.journal` for undoable file ops.
4. Optional: `accept_files`, `commands()` (palette), `can_automate/start_auto/stop_auto/auto_running/auto_summary`, `stats()`.
5. Add the module name to `TOOL_MODULES` in `app.py`.

## Hubs (tools as tabs)
`tools/hub.py` defines `HubTool`: one sidebar entry that holds several ordinary tools as tabs. The four hubs are
`cleanup.py`, `extract.py`, `match.py` and `automation.py`; each only lists `MEMBERS = ((module, class, tab label), ...)`.
Member tools are **unchanged** `Tool` subclasses and are *not* in `TOOL_MODULES`: they keep their id, their `<id>.*`
settings and journal entries. The hub registers their options/pages, builds a tab on first open, forwards
`on_show/on_hide/on_close`, `commands()`, `keys()` (only the open tab's), `stats()`, `badge()` and automation, and adds the
shared files bar. A member that cannot be imported is skipped (logged), so one missing library never removes the others.

For the shell: `App.aliases` maps a member id to its hub, so `show_tool("passport")` and `send_to("duplicates", files)`
open the hub on that tab; `App.tool(id)` finds hubs *and* tabs; `App.iter_tools()` unpacks hubs (automation, switchboard).
Inside a tab use `self.is_current()` instead of `self.app.current is self`. Old saved order / hidden / last-tool ids are
migrated to their hub on start (`App._migrate_tool_ids`).

`core/triggers.py` (pure logic, tested in `tests/test_triggers.py`) is the engine behind Autopilot's folder triggers:
`Trigger` + `TriggerStore` (`triggers.json`) + `TriggerRunner` (one `FolderWatcher` per trigger, handles a file once,
pauses a trigger that fires more than `autopilot.max_per_minute` times).

## Tool order and visibility
`core/toolorder.py` holds the rules (pure logic): Home is pinned first and cannot be hidden; a tool the user never placed
(added by an update) lands after its default predecessor. `App.sorted_tools()/visible_tools()/hidden_tools()` apply them;
`App.set_tool_order / set_tool_hidden / move_tool / show_all_tools` change them. Changing order or visibility only redraws
the sidebar buttons (open tools keep their state). `ui/toolmanager.py` is the drag-and-drop list on the
*Home & tools* settings page; any code may call the `App` methods (Home cards and sidebar buttons have right-click menus).

## Engines with their own UI-free tests
`core/similarity.py` (Duplicate Finder): fingerprints (bytes / text MinHash / picture hashes), a cached scan, scores as a
hand-calibrated likelihood, star-shaped groups, and the *strict* stroke check that stops two forms with another name from
looking identical. `core/scanclean.py` (Scan Cleaner): `analyse_image` finds what is wrong, `PageEdit` holds manual
overrides (None = automatic), `process_page` / `clean_file` fix it. Batches of file operations are tagged with
`fileops.batch_note()` so `Journal.undo_batch()` can undo them in one go.

## Local AI (Ollama)
`core/llm.py` is the only code that talks to Ollama (standard library only; `LlmConfig`, `ping`, `ask_text`, `ask_json`,
`ocr_image`; raises `LlmUnavailable` / `LlmError`, remembers an unreachable server for a few seconds). `core/aitasks.py`
holds the prompts and checks every answer (allowed ids only, clamped confidence, lists of strings); documents are sent
between `<document>` tags as data. `core/aimode.py` is the one place that combines the classic way with the AI:
`mode_for(settings, feature)` (master switch `ai.enabled` wins, then `ai.<feature>_mode`, features listed in
`FEATURES`) and `run(mode, rules_fn, ai_fn, ...)` for the four modes `rules | ai | rules_ai | ai_rules`, with optional
cross-check and `AiPending`. Rules-only mode calls the old code path unchanged.
Integration points: `ocr.ocr_image` (modes via `OcrConfig.ai_mode`), `fields.FieldEngine.extract_detailed` (one model
call per document for all fields; per-field `FieldSpec.ai_mode`; `grounded` / `accept_ai_value` validate),
`core/aidocs.py` (DocPacker: `recognise`, `page_labels` / `detect_groups`, `run_ai_check`, name checks, `fill_cover`)
and the `ai_*` workflow bricks. The UI thread never waits for a model: `aidocs.ai_call` caches answers and, when the
tool sets `AiContext.background`, computes them in a job and calls `on_ready` so the screen re-checks.
To add an AI feature: add `ai.<feature>_mode` (+ entry in `aimode.FEATURES`), write the AI function in `aitasks.py`,
wrap both methods in `aimode.run`, and mark AI-made values (`source == "ai"`) so the UI can ask for review.

## Tests
`tests/test_ai.py` (fake Ollama in `tests/fake_ollama.py`) and `tests/ui_smoke/ai_docpacker.py` cover the AI paths offline.
`tests/test_similarity.py`, `test_scanclean.py`, `test_toolorder.py` need no display. `tests/test_ui_smoke.py` runs
`tests/ui_smoke/run.py` in a subprocess with a fake tkinter (`faketk.py`, strict about unknown methods) to check what the
Home, tool manager, Duplicate Finder and Scan Cleaner screens *do*; it cannot judge layout, so still look at them once.

## Reuse
Rules: `core.rules` + `ui.records.RuleTester`. Fields: `core.fields.FieldEngine`. Tables of records with add/edit/import:
`ui.records.RecordEditor`. Previews: `app.renderer` + `ui.viewer.PageViewer`. Page thumbnails/reorder/rotate:
`ui.thumbs.ThumbGrid`. Threading: never touch Tk off the UI thread; use `UiBridge`.

## Adding a workflow brick
Open `core/workflow.py`, write one function and decorate it. It receives the running `Flow` (`flow.files` = the files
flowing through; set it to pass new ones on) and its settings (`Params`):

    @brick("rotate_all", "Rotate PDFs", "pdf", "🔄", "Turns every page.", [Param("deg", "Degrees", "int", 90)])
    def b_rotate_all(flow, p):
        for f in flow.files: ...
        return "12 turned"                      # shown on the brick after a run

Rules: write new files with `flow.new_file(stem, suffix)` (never overwrite originals); bricks that change things
outside the work folder set `side_effect=True`, journal what they do and, when `flow.preview` is true, only *say* what
they would do (add made-up paths to `flow.virtual`); raise `FlowStop("message")` to end the run politely.
The brick appears in the box, gets a settings form from its `Param` list and is covered by `tests/test_workflow.py`.

## Visa Tracker (extending it)
`core/visa.py` (model `Person`, `Policy`, deadline / level / next-due / todo / issues rules, `Store` with atomic saves, daily
backups, undo and unknown-key preservation), `core/visa_io.py` (import mapping, Excel/CSV rows, `.ics`, templates, WhatsApp /
mailto links), `core/visa_files.py` (page scoring, dates on a page, `extract_pages` via `pdfops.assemble`),
`ui/visa_dialogs.py` (person, contact, page picker, import, templates), `tools/visa_tracker.py` (screen).
All rules take `today` as an argument, so they are tested without a display (`tests/test_visa.py`; the screen is covered by
`tests/ui_smoke/visa_tracker.py`).

* **New field on a person**: add it to `Person`, then to `PersonDialog`, `COLUMNS` / `_row` and `_tab_details`; add a synonym
  line to `visa_io.IMPORT_FIELDS` and a column in `visa_io.PEOPLE_HEADERS` to import / export it. Old files keep working
  (missing keys use the default; unknown keys are kept).
* **User-defined fields**: Settings > Visa Tracker > *Extra fields* (stored in `Person.extra`, available as `{placeholders}`).
* **New rule / warning**: add to `visa.issues()` (alert) or `visa.todo()` (counts for the badge and 'To do today').
* **New status, channel, ticket state or document type**: add a `(value, label)` to the lists at the top of `core/visa.py`.
* **New filter chip**: `visa.FILTERS` + `visa.matches_filter` + `CHIPS` in the tool.
* **Tool badge**: any tool can return a count from `Tool.badge()` and call `app.refresh_badges()`; `stats_when_closed = True`
  lets Home show `stats()` before the tool was opened.
