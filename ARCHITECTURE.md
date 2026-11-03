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
