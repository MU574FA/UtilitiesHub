"""A strict stand-in for tkinter so UI *logic* can run without a display.

Not a renderer: widgets just remember their options / children / bindings.  Real semantics are kept where our code
depends on them (Variables with traces, Treeview items, Listbox, Combobox, after()).  Unknown Tk methods raise
AttributeError, so a typo in our own code fails loudly instead of being swallowed.
"""
import sys
import types

PENDING: list = []          # after() callbacks waiting for flush()
DIALOGS = {"askdirectory": "", "askopenfilenames": (), "asksaveasfilename": "", "askopenfilename": ""}

KNOWN_NOOP = set("""
pack grid place pack_forget grid_forget place_forget grid_remove pack_propagate grid_propagate columnconfigure
rowconfigure grid_columnconfigure grid_rowconfigure focus_set focus focus_force lift lower grab_set grab_release
transient geometry title minsize maxsize protocol iconbitmap iconphoto update_idletasks update bind_all unbind_all
unbind tag_configure tag_bind tag_raise create_image create_text create_rectangle create_line create_window
itemconfigure coords move yview xview yview_moveto xview_moveto yview_scroll xview_scroll config_scrollregion
tk_popup post unpost wait_window wait_visibility attributes state overrideredirect clipboard_clear clipboard_append
event_generate option_add resizable winfo_rootx winfo_rooty winfo_x winfo_y winfo_reqwidth winfo_reqheight
iconify deiconify withdraw
winfo_screenwidth winfo_screenheight winfo_pointerx winfo_pointery tk_focusNext selection_clear
add_command add_cascade add_separator add_checkbutton add_radiobutton entryconfigure delete_menu
heading column set_theme configure_style theme_use map layout element_create lookup tag_add tag_remove
mark_set see insert_text image_names scan_mark scan_dragto sashpos add forget panes pane
start stop step current select_range icursor set invoke flash
""".split())


class TclError(Exception):
    pass


class Widget:
    _n = 0

    def __init__(self, master=None, *args, **kw):
        self.master = master if isinstance(master, Widget) else None
        self._kw = dict(kw)
        self._children = []
        self._destroyed = False
        self._bindings = {}
        Widget._n += 1
        self._name = f"w{Widget._n}"
        if self.master is not None:
            self.master._children.append(self)

    def __str__(self):
        return "." + self._name

    def winfo_children(self):
        return list(self._children)

    def winfo_exists(self):
        return 0 if self._destroyed else 1

    def destroy(self):
        for c in list(self._children):
            c.destroy()
        self._destroyed = True
        if self.master is not None and self in self.master._children:
            self.master._children.remove(self)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            return self._kw.get(cnf)
        self._kw.update(cnf or {})
        self._kw.update(kw)

    config = configure

    def cget(self, key):
        return self._kw.get(key)

    def __setitem__(self, k, v):
        self._kw[k] = v

    def __getitem__(self, k):
        return self._kw.get(k)

    def bind(self, seq=None, fn=None, add=None):
        self._bindings.setdefault(seq, []).append(fn)

    def winfo_width(self):
        return 800

    def winfo_height(self):
        return 600

    def winfo_toplevel(self):
        w = self
        while w.master is not None:
            w = w.master
        return w

    def nametowidget(self, name):
        return self.winfo_toplevel()

    def after(self, ms, fn=None, *args):
        PENDING.append((fn, args))
        return len(PENDING)

    def after_idle(self, fn, *args):
        PENDING.append((fn, args))
        return len(PENDING)

    def after_cancel(self, ident):
        if isinstance(ident, int) and 0 < ident <= len(PENDING):
            PENDING[ident - 1] = (None, ())

    def winfo_ismapped(self):
        return True

    def __getattr__(self, name):
        if name in KNOWN_NOOP:
            return lambda *a, **k: None
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r} (not a known Tk method)")


def flush(limit=100):
    """Run pending after() callbacks (they may schedule more)."""
    for _ in range(limit):
        if not PENDING:
            return
        batch, PENDING[:] = list(PENDING), []
        for fn, args in batch:
            if fn:
                fn(*args)


class Variable:
    def __init__(self, master=None, value=None, name=None):
        self._v = value
        self._traces = []

    def get(self):
        return self._v

    def set(self, v):
        self._v = self._coerce(v)
        for cb in list(self._traces):
            cb("v", "", "write")

    def _coerce(self, v):
        return v

    def trace_add(self, mode, cb):
        self._traces.append(cb)
        return "t"

    def trace_remove(self, *a):
        pass


class StringVar(Variable):
    def __init__(self, master=None, value="", name=None):
        super().__init__(master, value, name)

    def _coerce(self, v):
        return str(v)


class IntVar(Variable):
    def __init__(self, master=None, value=0, name=None):
        super().__init__(master, value, name)

    def _coerce(self, v):
        return int(float(v))


class DoubleVar(Variable):
    def __init__(self, master=None, value=0.0, name=None):
        super().__init__(master, value, name)

    def _coerce(self, v):
        return float(v)


class BooleanVar(Variable):
    def __init__(self, master=None, value=False, name=None):
        super().__init__(master, value, name)

    def _coerce(self, v):
        return bool(v)


class Treeview(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._items, self._order, self._sel, self._n2 = {}, [], [], 0

    def insert(self, parent, index, iid=None, **kw):
        if iid is None:
            self._n2 += 1
            iid = f"I{self._n2:03d}"
        self._items[iid] = {"values": list(kw.get("values", [])), "tags": tuple(kw.get("tags", ())), "text": kw.get("text", "")}
        self._order.append(iid)
        return iid

    def delete(self, *iids):
        for i in iids:
            self._items.pop(i, None)
            if i in self._order:
                self._order.remove(i)
            if i in self._sel:
                self._sel.remove(i)

    def get_children(self, item=""):
        return tuple(self._order)

    def exists(self, iid):
        return iid in self._items

    def item(self, iid, option=None, **kw):
        if kw:
            self._items[iid].update({k: (list(v) if k == "values" else v) for k, v in kw.items()})
            return None
        return self._items[iid] if option is None else self._items[iid].get(option)

    def set(self, iid, col=None, value=None):
        return None

    def selection(self):
        return tuple(self._sel)

    def selection_set(self, *iids):
        self._sel = [i for it in iids for i in (it if isinstance(it, (list, tuple)) else [it])]

    def selection_remove(self, *a):
        self._sel = []

    def index(self, iid):
        return self._order.index(iid)

    def move(self, iid, parent, index):
        self._order.remove(iid)
        self._order.insert(index, iid)

    def identify_row(self, y):
        return self._order[0] if self._order else ""

    def identify_column(self, x):
        return "#1"

    def heading(self, *a, **k):
        return None

    def column(self, *a, **k):
        return None

    def tag_configure(self, *a, **k):
        return None


class Listbox(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._items, self._sel = [], ()

    def insert(self, idx, *items):
        self._items.extend(items)

    def delete(self, a, b=None):
        self._items.clear()

    def curselection(self):
        return self._sel

    def get(self, a, b=None):
        return self._items[a] if b is None else self._items[a:]

    def size(self):
        return len(self._items)

    def selection_set(self, *a):
        self._sel = tuple(a)


class Canvas(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items = []

    def delete(self, *a):
        self.items.clear()

    def create_image(self, *a, **k):
        self.items.append(("image", a, k))

    def create_text(self, *a, **k):
        self.items.append(("text", a, k))

    def create_rectangle(self, *a, **k):
        self.items.append(("rect", a, k))


class Combobox(Widget):
    def __init__(self, master=None, textvariable=None, **kw):
        super().__init__(master, **kw)
        self._var = textvariable


class Menu(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.entries = []

    def add_command(self, label="", command=None, **kw):
        self.entries.append(("command", label, command))

    def add_cascade(self, label="", menu=None, **kw):
        self.entries.append(("cascade", label, menu))

    def add_separator(self, **kw):
        self.entries.append(("sep", "", None))

    def delete(self, a, b=None):
        self.entries.clear()


class Entry(Widget):
    def __init__(self, master=None, textvariable=None, **kw):
        super().__init__(master, **kw)
        self._var = textvariable
        self._text = ""

    def get(self):
        return self._var.get() if self._var else self._text

    def insert(self, i, s):
        self._text += s

    def delete(self, a, b=None):
        self._text = ""


class Text(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._text = ""

    def insert(self, idx, s, *tags):
        self._text += s

    def get(self, a, b=None):
        return self._text

    def delete(self, a, b=None):
        self._text = ""

    def index(self, i):
        return f"{self._text.count(chr(10)) + 1}.0"


class Paned(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._panes = []

    def add(self, w, **kw):
        if w not in self._panes:
            self._panes.append(w)

    def forget(self, w):
        if w in self._panes:
            self._panes.remove(w)

    def panes(self):
        return tuple(str(w) for w in self._panes)


class Tk(Widget):
    pass


def _generic(name):
    return type(name, (Widget,), {})


class StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = _generic(name)
        setattr(self, name, cls)
        return cls


def install():
    tk = StubModule("tkinter")
    for n, c in dict(Widget=Widget, Misc=Widget, Tk=Tk, Toplevel=Widget, Frame=Widget, Label=Widget, Button=Widget, Canvas=Canvas,
                     Listbox=Listbox, Menu=Menu, Text=Text, Entry=Entry, Scrollbar=Widget, PhotoImage=Widget,
                     StringVar=StringVar, IntVar=IntVar, DoubleVar=DoubleVar, BooleanVar=BooleanVar, Variable=Variable,
                     TclError=TclError, Event=Widget).items():
        setattr(tk, n, c)
    for n in ("END", "LEFT", "RIGHT", "TOP", "BOTTOM", "BOTH", "X", "Y", "W", "E", "N", "S", "NSEW", "CENTER", "NORMAL",
              "DISABLED", "HORIZONTAL", "VERTICAL", "WORD", "NW", "NE", "SW", "SE"):
        setattr(tk, n, n.lower())
    ttk = StubModule("tkinter.ttk")
    for n, c in dict(Treeview=Treeview, Combobox=Combobox, Entry=Entry, Frame=Widget, Label=Widget, Button=Widget,
                     Scrollbar=Widget, Checkbutton=Widget, Radiobutton=Widget, Scale=Widget, Spinbox=Widget, LabelFrame=Widget,
                     Panedwindow=Paned, PanedWindow=Paned, Notebook=Widget, Progressbar=Widget, Separator=Widget,
                     Style=Widget, Sizegrip=Widget).items():
        setattr(ttk, n, c)
    fd = StubModule("tkinter.filedialog")
    for n, v in DIALOGS.items():
        setattr(fd, n, (lambda *a, _n=n, **k: DIALOGS[_n]))
    mb = StubModule("tkinter.messagebox")
    for n in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askquestion"):
        setattr(mb, n, lambda *a, **k: True)
    for name, mod in (("tkinter", tk), ("tkinter.ttk", ttk), ("tkinter.filedialog", fd), ("tkinter.messagebox", mb),
                      ("tkinter.font", StubModule("tkinter.font")), ("tkinter.simpledialog", StubModule("tkinter.simpledialog")),
                      ("tkinter.scrolledtext", StubModule("tkinter.scrolledtext"))):
        sys.modules[name] = mod
    tk.ttk, tk.filedialog, tk.messagebox = ttk, fd, mb
    tk.font = sys.modules["tkinter.font"]
    imagetk = types.ModuleType("PIL.ImageTk")

    class PhotoImage:
        def __init__(self, image=None, **kw):
            self.image = image

        def width(self):
            return self.image.width

        def height(self):
            return self.image.height
    imagetk.PhotoImage = PhotoImage
    sys.modules["PIL.ImageTk"] = imagetk
    import PIL
    PIL.ImageTk = imagetk
    return tk
