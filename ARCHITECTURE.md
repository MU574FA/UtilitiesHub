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

## Tests
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
