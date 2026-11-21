"""Autocomplete for Tk text boxes: the suggestion pop-up, key handling and the right-click menu of a box.

    from ..ui.autocomplete import attach
    attach(entry, "visa.position")                                   # ttk.Entry / ttk.Combobox / tk.Text
    attach(entry, "visa.group", extra=lambda: store.groups())        # more candidates, asked each time

Keys while the list is open:  Up / Down choose  -  Tab accepts (setting)  -  Enter accepts only a suggestion that was chosen
with the arrows (so Enter still submits a form)  -  Esc closes  -  Ctrl+Space opens the list on demand.
Space right after a shortcut expands it; Backspace straight after puts the shortcut back.
Right-click a box: switch autocomplete off / on for THIS box, add a keyword or shortcut, open its settings.

The pop-up never takes the keyboard focus, so typing is never interrupted.  Everything is decided by core/autocomplete.py;
this module only reads and writes the text of the widget.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable

from ..core.autocomplete import Autocomplete, Expansion, Result, Suggestion, parse_shortcuts
from .widgets import app_of, ask_text, info

log = logging.getLogger(__name__)

_NAV_KEYS = {"Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next", "Tab", "ISO_Left_Tab", "Return", "KP_Enter",
             "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Caps_Lock",
             "Num_Lock", "Scroll_Lock", "Super_L", "Super_R", "Win_L", "Win_R", "F1", "F2", "F3", "F4", "F5", "F6", "F7",
             "F8", "F9", "F10", "F11", "F12", "Insert", "Menu"}
_HIDE_KEYS = {"Left", "Right", "Home", "End", "Prior", "Next", "Escape"}
_CTRL = 0x4


def attach(widget: tk.Misc, field_id: str, extra: Callable[[], Iterable[str]] | Iterable[str] | None = None,
           on_accept: Callable[[str], None] | None = None, learn: bool = True) -> "AutocompleteController | None":
    """Give `widget` autocomplete for `field_id`.  No-op (returns None) when there is no app, or the box is read-only."""
    app = app_of(widget)
    service: Autocomplete | None = getattr(app, "autocomplete", None)
    if service is None:
        return None
    try:
        if str(widget.cget("state")) == "readonly":
            return None
    except tk.TclError:
        pass
    return AutocompleteController(app, service, widget, field_id, extra, on_accept, learn)


class AutocompleteController:
    def __init__(self, app, service: Autocomplete, widget, field_id: str, extra=None, on_accept=None,
                 learn: bool = True) -> None:
        self.learn = learn
        self.app, self.service, self.w, self.field_id = app, service, widget, field_id
        self.extra, self.on_accept = extra, on_accept
        self.is_text = isinstance(widget, tk.Text)
        self.popup: tk.Toplevel | None = None
        self.listbox: tk.Listbox | None = None
        self.result: Result | None = None
        self.sel = 0
        self.navigated = False               # the user chose with the arrows: Enter may accept
        self._job = None
        self._hide_job = None
        self._undo: tuple[int, str, int, str] | None = None     # (start, text that was there, end of expansion, extra char)
        self._dead = False
        w = widget
        w.bind("<KeyPress>", self._key_press, add="+")
        w.bind("<KeyRelease>", self._key_release, add="+")
        w.bind("<Control-space>", self._force, add="+")
        w.bind("<<Paste>>", lambda e: self._later(), add="+")
        w.bind("<<Cut>>", lambda e: self._later(), add="+")
        w.bind("<FocusOut>", self._focus_out, add="+")
        w.bind("<Button-1>", lambda e: self.hide(), add="+")
        w.bind("<Button-3>", self._menu, add="+")
        w.bind("<Button-2>", self._menu, add="+")
        w.bind("<Destroy>", self._destroy, add="+")
        self._unsub = service.on_change(self._service_changed)

    # ------------------------------------------------------------------ reading / writing the widget
    def _state(self) -> tuple[str, int]:
        if self.is_text:
            return self.w.get("1.0", "end-1c"), len(self.w.get("1.0", "insert"))
        return self.w.get(), int(self.w.index("insert"))

    def _replace(self, start: int, end: int, new: str, caret: int) -> None:
        if self.is_text:
            self.w.delete(f"1.0+{start}c", f"1.0+{end}c")
            self.w.insert(f"1.0+{start}c", new)
            self.w.mark_set("insert", f"1.0+{caret}c")
            self.w.see("insert")
        else:
            self.w.delete(start, end)
            self.w.insert(start, new)
            self.w.icursor(caret)

    def _extra(self) -> list[str]:
        src = self.extra
        try:
            vals = src() if callable(src) else (src or [])
            return [str(v) for v in vals]
        except Exception:
            log.exception("autocomplete extra values failed for %s", self.field_id)
            return []

    def _box_values(self) -> list[str]:
        if isinstance(self.w, ttk.Combobox):
            try:
                raw = self.w.cget("values")
                vals = self.w.tk.splitlist(raw) if isinstance(raw, str) else raw
                return [str(v) for v in vals]
            except Exception:
                return []
        return []

    def _active(self) -> bool:
        return not self._dead and self.service.is_enabled(self.field_id)

    # ------------------------------------------------------------------ events
    def _key_press(self, e):
        if not self._active():
            return None
        key = e.keysym
        ctrl = bool(e.state & _CTRL)
        if self.result is not None:
            if key == "Down":
                self._move(1)
                return "break"
            if key == "Up":
                self._move(-1)
                return "break"
            if key == "Tab" and self.service.options().tab_accepts:
                self._accept()
                return "break"
            if key in ("Return", "KP_Enter"):
                if self.navigated:
                    self._accept()
                    return "break"
                self.hide()
            if key == "Escape":
                self.hide()
                return "break"
        if key in ("Return", "KP_Enter") and not self.is_text:
            self._commit()                                       # Enter / Ctrl+Enter saves the form: remember the value now
        if key == "BackSpace" and self._undo and not ctrl:
            if self._revert():
                return "break"
        if key not in ("Shift_L", "Shift_R"):
            self._undo = None
        if key in ("space", "Tab") and not ctrl and self.service.options().expand_on_space:
            if self._expand(key):
                return "break"
        return None

    def _key_release(self, e):
        if not self._active():
            self.hide()
            return
        key = e.keysym
        if key in _HIDE_KEYS:
            self.hide()
            return
        if key in _NAV_KEYS or (e.state & _CTRL and key not in ("v", "V", "x", "X")):
            return
        self._later()

    def _force(self, _e=None):
        if self._active():
            self._refresh(force=True)
        return "break"

    def _later(self) -> None:
        if self._job is not None:
            try:
                self.w.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.w.after(15, self._refresh)

    def _focus_out(self, _e=None) -> None:
        self._commit()
        if self.result is not None and self._hide_job is None:
            self._hide_job = self.w.after(150, self.hide)       # a click on the list arrives right after the focus change

    def _destroy(self, e=None) -> None:
        if e is not None and e.widget is not self.w:
            return
        self._dead = True
        self.hide(destroy=True)
        self._unsub()

    def _service_changed(self) -> None:
        if not self._active():
            self.hide()

    # ------------------------------------------------------------------ suggestions
    def _refresh(self, force: bool = False) -> None:
        self._job = None
        if not self._active() or self._dead:
            self.hide()
            return
        if not force and not self.service.options().auto_popup:
            self.hide()
            return
        try:
            text, cursor = self._state()
        except tk.TclError:
            return
        if not force and cursor < len(text) and (text[cursor].isalnum() and self.service.definition(self.field_id).mode == "text"):
            self.hide()                                          # in the middle of a word of a note
            return
        res = self.service.suggest(self.field_id, text, cursor, extra=self._extra(), box_values=self._box_values(),
                                   force=force, multiline=self.is_text)
        if not res or not res.items:
            self.hide()
            return
        self._show(res)

    def _show(self, res: Result) -> None:
        self.result, self.sel, self.navigated = res, 0, False
        if self.popup is None:
            self._build()
        self.popup.deiconify()
        self.popup.lift()
        lb = self.listbox
        lb.delete(0, "end")
        for i, it in enumerate(res.items):
            lb.insert("end", " " + it.label)
            if it.kind == "shortcut":
                lb.itemconfigure(i, foreground=self.app.theme.accent)
        lb.configure(height=min(len(res.items), 12))
        lb.selection_clear(0, "end")
        lb.selection_set(0)
        self._place()

    def _build(self) -> None:
        pop = tk.Toplevel(self.w)
        pop.wm_overrideredirect(True)
        try:
            pop.attributes("-topmost", True)
        except tk.TclError:
            pass
        lb = tk.Listbox(pop, exportselection=False, activestyle="none", width=0, borderwidth=0, takefocus=0)
        try:
            self.app.theme.style_listbox(lb)
        except Exception:
            pass
        lb.pack(fill="both", expand=True)
        lb.bind("<Button-1>", self._click)
        lb.bind("<Motion>", self._hover)
        self.popup, self.listbox = pop, lb

    def _place(self) -> None:
        pop, w = self.popup, self.w
        try:
            bb = w.bbox("insert")
        except tk.TclError:
            bb = None
        if bb:
            x, y, _cw, ch = bb
        else:
            x, y, ch = 0, 0, w.winfo_height()
        px, py = w.winfo_rootx() + x, w.winfo_rooty() + y + ch + 2
        pop.update_idletasks()
        pw, ph = max(pop.winfo_reqwidth(), 180), pop.winfo_reqheight()
        try:
            if py + ph > w.winfo_screenheight() - 40:            # no room below: show above the line
                py = max(0, w.winfo_rooty() + y - ph - 2)
            px = max(0, min(px, w.winfo_screenwidth() - pw - 8))
        except tk.TclError:
            pass
        pop.wm_geometry(f"{pw}x{ph}+{px}+{py}")

    def hide(self, destroy: bool = False) -> None:
        if self._hide_job is not None:
            try:
                self.w.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        self.result = None
        if self.popup is not None:
            try:
                if destroy:
                    self.popup.destroy()
                else:
                    self.popup.withdraw()                        # keep it: creating a window per keystroke flickers
            except tk.TclError:
                pass
            if destroy:
                self.popup = self.listbox = None

    # ------------------------------------------------------------------ choosing
    def _move(self, delta: int) -> None:
        n = len(self.result.items)
        self.sel = (self.sel + delta) % n
        self.navigated = True
        lb = self.listbox
        lb.selection_clear(0, "end")
        lb.selection_set(self.sel)
        lb.see(self.sel)

    def _hover(self, e) -> None:
        if self.listbox is None:
            return
        i = self.listbox.nearest(e.y)
        if i != self.sel and 0 <= i < len(self.result.items):
            self.sel = i
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(i)

    def _click(self, e):
        if self.listbox is None:
            return "break"
        self.sel = max(0, min(self.listbox.nearest(e.y), len(self.result.items) - 1))
        self._accept()
        self.w.focus_set()
        return "break"

    def _accept(self) -> None:
        res = self.result
        if res is None:
            return
        item: Suggestion = res.items[self.sel]
        text, cursor = self._state()
        _new, caret = self.service.accept(text, cursor, res, item)
        original = text[res.start:cursor]
        self.hide()
        self._replace(res.start, cursor, item.text, caret)
        self._undo = (res.start, original, res.start + len(item.text), "") if item.kind == "shortcut" else None
        if self.on_accept:
            self.on_accept(item.text)

    # ------------------------------------------------------------------ shortcuts on Space / Tab
    def _expand(self, key: str) -> bool:
        text, cursor = self._state()
        exp: Expansion | None = self.service.expansion(self.field_id, text, cursor, multiline=self.is_text)
        if exp is None:
            return False
        mode = self.service.definition(self.field_id).mode
        _new, caret = self.service.apply_expansion(text, exp)
        tail = " " if (key == "space" and mode != "value") else ""     # value boxes swallow the space
        self.hide()
        self._replace(exp.start, exp.end, exp.text + tail, caret + len(tail) if exp.caret is None else caret)
        # Backspace restores what was typed, plus the space the user pressed (so it is not expanded a second time)
        self._undo = (exp.start, text[exp.start:exp.end], exp.start + len(exp.text) + len(tail), " " if key == "space" else "")
        return True

    def _revert(self) -> bool:
        """Backspace right after an expansion: the shortcut comes back (and the expansion is not re-triggered)."""
        start, original, end, again = self._undo
        self._undo = None
        try:
            text, cursor = self._state()
        except tk.TclError:
            return False
        if cursor != end:
            return False
        self._replace(start, end, original + again, start + len(original) + len(again))
        self.hide()
        return True

    # ------------------------------------------------------------------ learning
    def _commit(self) -> None:
        """The box was left: remember the finished value."""
        if self._dead or not self.learn or not self._active():
            return
        try:
            text, _ = self._state()
        except tk.TclError:
            return
        if text.strip():
            self.service.learn(self.field_id, text)

    # ------------------------------------------------------------------ right-click menu
    def _menu(self, e):
        d = self.service.definition(self.field_id)
        on = self.service.config(self.field_id).enabled
        m = tk.Menu(self.w, tearoff=False)
        m.add_command(label=f"✔ Autocomplete is ON for “{d.label}” - turn off" if on
                      else f"Autocomplete is OFF for “{d.label}” - turn on",
                      command=lambda: self.service.set_enabled(self.field_id, not on))
        if not self.service.master():
            m.add_command(label="Autocomplete is off everywhere - turn it on", command=lambda: self.service.set_master(True))
        m.add_separator()
        sel = self._selection()
        m.add_command(label="Add the selected text as a keyword" if sel else "Add the text of this box as a keyword",
                      command=lambda: self._add_keyword(sel))
        m.add_command(label="Add a shortcut…", command=lambda: self._add_shortcut(sel))
        m.add_separator()
        m.add_command(label="Keywords & shortcuts for this box…", command=self._open_settings)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break" if self.is_text else None

    def _selection(self) -> str:
        try:
            if self.is_text:
                return self.w.get("sel.first", "sel.last").strip()
            return self.w.selection_get().strip() if self.w.selection_present() else ""
        except tk.TclError:
            return ""

    def _add_keyword(self, sel: str) -> None:
        text = sel or self._state()[0].strip()
        if not text:
            info(self.w, "Select some text (or type it in the box) first.")
        elif self.service.add_keyword(self.field_id, text):
            self.app.toast.show(f"Added “{text}” to the keywords of this box.", "ok")
        else:
            self.app.toast.show("That keyword is already there.", "info")

    def _add_shortcut(self, sel: str) -> None:
        d = self.service.definition(self.field_id)
        raw = ask_text(self.w, "Add a shortcut", f"Shortcut = text, for “{d.label}”.\nExample:  nr = No response",
                       initial=f" = {sel}" if sel else "")
        if not raw:
            return
        parsed, problems = parse_shortcuts(raw)
        if problems or not parsed:
            info(self.w, "\n".join(problems) or "Write it as   shortcut = text")
            return
        for abbr, exp in parsed.items():
            err = self.service.add_shortcut(self.field_id, abbr, exp)
            if err:
                info(self.w, err)
                return
        self.app.toast.show("Shortcut added.", "ok")

    def _open_settings(self) -> None:
        self.hide()
        self.service.select_field(self.field_id)
        self.app.open_settings("autocomplete")
