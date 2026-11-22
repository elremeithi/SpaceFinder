"""Space Finder desktop GUI (tkinter - ships with Python, works fully offline)."""
from __future__ import annotations

import json
import os
import queue
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import APP_NAME, __version__
from . import ai, duplicates, junk, organizer, system_clean
from .utils import (IS_WINDOWS, app_data_dir, delete_path, disk_usage, human_size, is_admin,
                    is_protected)

ACCENT = "#2563eb"
GOOD = "#16a34a"
WARN = "#d97706"
BAD = "#dc2626"
BG = "#f8fafc"
CARD = "#ffffff"
MUTED = "#64748b"
CHECK, UNCHECK = "☑", "☐"

SETTINGS_FILE = app_data_dir() / "settings.json"

# Interface size: multiplier on top of the monitor's real DPI scaling.
ZOOM_LEVELS = {"Normal (100%)": 1.0, "Large (125%)": 1.25, "Larger (150%)": 1.5,
               "Huge (175%)": 1.75, "Maximum (200%)": 2.0}
UI_SCALE = 1.0          # set by App: (screen DPI / 96) * zoom


def px(n: float) -> int:
    """Scale a pixel size to the current DPI + interface size."""
    return int(round(n * UI_SCALE))


def enable_dpi_awareness() -> None:
    """Must run BEFORE the Tk window exists, or Tk measures the screen at 96 DPI
    and everything renders tiny on high-resolution displays."""
    if not IS_WINDOWS:
        return
    import ctypes
    for call in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
                 lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            call()
            return
        except Exception:
            continue
DEFAULT_SETTINGS = {
    "engine": "builtin",          # builtin | ollama
    "ollama_model": "llama3.2:3b",
    "recycle": True,              # user files go to Recycle Bin (reversible)
    "include_hidden": False,
    "auto_review": True,
    "deep_zip": False,
    "skip_shortcuts": True,
    "ui_zoom": 1.25,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=1), encoding="utf-8")
    except OSError:
        pass


def open_in_explorer(path: str) -> None:
    try:
        if IS_WINDOWS:
            if os.path.isdir(path):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
    except OSError:
        pass


def user_folders() -> list[str]:
    home = os.path.expanduser("~")
    return [p for p in (os.path.join(home, n) for n in
                        ("Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music"))
            if os.path.isdir(p)]


def drives() -> list[str]:
    if IS_WINDOWS:
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return ["/"]


# ======================================================================================
# Background task runner
# ======================================================================================

class Worker:
    """Runs a function in a thread; marshals progress/results back to the Tk thread."""

    def __init__(self, app: "App"):
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.cancel_flag = threading.Event()
        self.busy = False
        app.after(80, self._poll)

    def run(self, label: str, fn, on_done, on_error=None):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait - another task is still running.")
            return
        self.busy = True
        self.cancel_flag.clear()
        self.app.set_busy(True, label)

        def target():
            try:
                res = fn()
                self.q.put(("done", on_done, res))
            except Exception as e:  # report to UI
                self.q.put(("error", on_error, e))

        threading.Thread(target=target, daemon=True).start()

    def progress(self, text: str, n: int = 0):
        self.q.put(("progress", None, text))

    def cancelled(self) -> bool:
        return self.cancel_flag.is_set()

    def _poll(self):
        try:
            while True:
                kind, cb, payload = self.q.get_nowait()
                if kind == "progress":
                    self.app.set_status(payload)
                    continue
                self.busy = False
                self.app.set_busy(False)
                if kind == "done":
                    cb(payload)
                else:
                    if cb:
                        cb(payload)
                    else:
                        messagebox.showerror(APP_NAME, f"Something went wrong:\n{payload}")
        except queue.Empty:
            pass
        self.app.after(80, self._poll)


# ======================================================================================
# Reusable widgets
# ======================================================================================

class CheckTree(ttk.Frame):
    """Treeview with a checkbox column, sortable headers and an object per row."""

    def __init__(self, master, columns: list[tuple[str, str, int]], tree_mode=False, **kw):
        super().__init__(master, **kw)
        self.cols = [c[0] for c in columns]
        self.tree = ttk.Treeview(self, columns=self.cols, show="tree headings" if tree_mode else "headings",
                                 selectmode="extended")
        self.tree.column("#0", width=px(28) if not tree_mode else px(260), stretch=False)
        for key, title, width in columns:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=px(width), anchor="w", stretch=key not in ("chk",))
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.objs: dict[str, object] = {}
        self.checked: set[str] = set()
        self.on_toggle = None
        self._sort_rev: dict[str, bool] = {}
        self.tree.bind("<Button-1>", self._click, add="+")
        self.tree.bind("<space>", self._space)
        self.tree.tag_configure("delete", foreground=BAD)
        self.tree.tag_configure("review", foreground=WARN)
        self.tree.tag_configure("keep", foreground=GOOD)
        self.tree.tag_configure("group", font=("Segoe UI" if IS_WINDOWS else "TkDefaultFont", 10, "bold"))

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.objs.clear()
        self.checked.clear()

    def add(self, values: dict, obj=None, checked=True, parent="", tags=(), text="", open_=True) -> str:
        vals = [values.get(c, "") for c in self.cols]
        iid = self.tree.insert(parent, "end", text=text, values=vals, tags=tags, open=open_)
        if obj is not None:
            self.objs[iid] = obj
            if "chk" in self.cols:
                self.set_checked(iid, checked)
        return iid

    def set_checked(self, iid: str, on: bool):
        if on:
            self.checked.add(iid)
        else:
            self.checked.discard(iid)
        if "chk" in self.cols:
            self.tree.set(iid, "chk", CHECK if on else UNCHECK)

    def toggle(self, iid: str):
        if iid not in self.objs:            # group row: toggle all its children
            kids = [k for k in self.tree.get_children(iid) if k in self.objs]
            if kids:
                on = not all(k in self.checked for k in kids)
                for k in kids:
                    self.set_checked(k, on)
                if self.on_toggle:
                    self.on_toggle(None)
            return
        if iid in self.objs:
            self.set_checked(iid, iid not in self.checked)
            if self.on_toggle:
                self.on_toggle(iid)

    def _click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        col = self.tree.identify_column(ev.x)
        iid = self.tree.identify_row(ev.y)
        if iid and "chk" in self.cols and (col == f"#{self.cols.index('chk') + 1}" or iid not in self.objs
                                            and self.tree.get_children(iid) and col != "#0"):
            self.toggle(iid)
            return "break"

    def _space(self, _ev):
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def check_all(self, on=True, predicate=None):
        for iid, obj in self.objs.items():
            if predicate is None or predicate(obj):
                self.set_checked(iid, on)
        if self.on_toggle:
            self.on_toggle(None)

    def checked_objs(self):
        return [self.objs[i] for i in self.checked if i in self.objs]

    def selected_objs(self):
        return [self.objs[i] for i in self.tree.selection() if i in self.objs]

    def sort_by(self, col):
        rev = self._sort_rev.get(col, False)
        for parent in [""] + list(self.tree.get_children("")):
            kids = list(self.tree.get_children(parent))
            if not kids:
                continue

            def key(i):
                v = self.tree.set(i, col)
                obj = self.objs.get(i)
                if col == "size" and obj is not None and hasattr(obj, "size"):
                    return obj.size
                return str(v).lower()
            kids.sort(key=key, reverse=rev)
            for n, i in enumerate(kids):
                self.tree.move(i, parent, n)
        self._sort_rev[col] = not rev


class FolderList(ttk.Frame):
    def __init__(self, master, initial: list[str], height=4):
        super().__init__(master)
        self.lb = tk.Listbox(self, height=height, activestyle="none", selectmode="extended",
                             relief="solid", borderwidth=1, highlightthickness=0)
        self.lb.grid(row=0, column=0, rowspan=3, sticky="nsew")
        ttk.Button(self, text="Add folder...", command=self.add).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(self, text="Add drive...", command=self.add_drive).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=2)
        ttk.Button(self, text="Remove", command=self.remove).grid(row=2, column=1, sticky="new", padx=(6, 0))
        self.columnconfigure(0, weight=1)
        for p in initial:
            self.lb.insert("end", p)

    def add(self):
        d = filedialog.askdirectory()
        if d:
            d = os.path.normpath(d)
            if is_protected(d):
                messagebox.showwarning(APP_NAME, "That's a protected system folder and can't be scanned here.\n"
                                                 "Use the System Cleaner tab for Windows junk.")
                return
            if d not in self.get():
                self.lb.insert("end", d)

    def add_drive(self):
        ds = drives()
        d = simpledialog.askstring(APP_NAME, f"Drive to scan ({', '.join(ds)}):", initialvalue=ds[0])
        if d:
            d = d.strip()
            if len(d) == 1:
                d += ":\\"
            elif len(d) == 2 and d[1] == ":":
                d += "\\"
            if os.path.isdir(d) and d not in self.get():
                self.lb.insert("end", d)

    def remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def get(self) -> list[str]:
        return list(self.lb.get(0, "end"))


def toolbar(master) -> ttk.Frame:
    f = ttk.Frame(master)
    f.pack(fill="x", pady=(6, 4))
    return f


# ======================================================================================
# Tabs
# ======================================================================================

class DashboardTab(ttk.Frame):
    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=16)
        self.app = app
        ttk.Label(self, text="Your drives", style="H2.TLabel").pack(anchor="w")
        self.drive_box = ttk.Frame(self)
        self.drive_box.pack(fill="x", pady=(6, 14))

        ttk.Label(self, text="Quick actions", style="H2.TLabel").pack(anchor="w")
        qa = ttk.Frame(self)
        qa.pack(fill="x", pady=6)
        actions = [
            ("Quick Clean C: drive", "Temp files, caches, crash dumps and Recycle Bin", self.quick_clean),
            ("Find junk & corrupt files", "Empty, broken, unfinished files in your folders", lambda: app.goto(1)),
            ("Organize a messy folder", "Sort loose files into category folders", lambda: app.goto(2)),
            ("Find duplicates", "Byte-identical copies wasting space", lambda: app.goto(3)),
        ]
        for i, (title, sub, cmd) in enumerate(actions):
            card = ttk.Frame(qa, style="Card.TFrame", padding=12)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=sub, style="CardSub.TLabel").pack(anchor="w", pady=(2, 8))
            ttk.Button(card, text="Start", style="Accent.TButton", command=cmd).pack(anchor="w")
        qa.columnconfigure(0, weight=1)
        qa.columnconfigure(1, weight=1)

        info = ttk.Frame(self)
        info.pack(fill="x", pady=(14, 0))
        admin = is_admin()
        ttk.Label(info, text=("Running as Administrator - all cleanup locations available." if admin else
                              "Not running as Administrator - some Windows locations will be skipped."),
                  foreground=GOOD if admin else WARN).pack(side="left")
        if IS_WINDOWS and not admin:
            ttk.Button(info, text="Restart as Administrator", command=self.relaunch_admin).pack(side="left", padx=8)
        ttk.Label(self, text="\U0001F512 100% offline: Space Finder never connects to the internet. "
                             "All analysis happens on this PC.", foreground=MUTED).pack(anchor="w", pady=(10, 0))
        self.refresh()

    def refresh(self):
        for w in self.drive_box.winfo_children():
            w.destroy()
        for i, d in enumerate(drives()):
            try:
                total, used, free = disk_usage(d)
            except OSError:
                continue
            row = ttk.Frame(self.drive_box)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=d, width=6, style="Bold.TLabel").pack(side="left")
            pct = (total - free) / total * 100 if total else 0
            bar = ttk.Progressbar(row, length=px(360), maximum=100, value=pct,
                                  style="Red.Horizontal.TProgressbar" if pct > 90 else "Horizontal.TProgressbar")
            bar.pack(side="left", padx=8)
            ttk.Label(row, text=f"{human_size(free)} free of {human_size(total)}  ({pct:.0f}% used)").pack(side="left")

    def quick_clean(self):
        self.app.goto(4)
        self.app.tabs[4].scan(then_clean=True)

    def relaunch_admin(self):
        import ctypes
        params = " ".join(f'"{a}"' for a in sys.argv[1:]) if getattr(sys, "frozen", False) else \
            " ".join(f'"{a}"' for a in sys.argv)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if rc > 32:
            self.app.destroy()


class JunkTab(ttk.Frame):
    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=12)
        self.app = app
        self.findings: list[junk.Finding] = []
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Folders to scan for worthless, corrupt and incomplete files:").pack(anchor="w")
        self.folders = FolderList(top, user_folders())
        self.folders.pack(fill="x", pady=4)

        opts = ttk.Frame(self)
        opts.pack(fill="x")
        self.vars = {}
        for key, label in [("empty_files", "Empty files"), ("empty_folders", "Empty folders"),
                           ("incomplete", "Unfinished downloads"), ("corrupt", "Corrupt / truncated"),
                           ("leftovers", "Leftover & lock files"), ("broken_shortcuts", "Broken shortcuts"),
                           ("system_junk", "Thumbs.db etc.")]:
            v = tk.BooleanVar(value=True)
            self.vars[key] = v
            ttk.Checkbutton(opts, text=label, variable=v).pack(side="left", padx=(0, 10))

        tb = toolbar(self)
        ttk.Button(tb, text="\U0001F50D Scan", style="Accent.TButton", command=self.scan).pack(side="left")
        ttk.Button(tb, text="Cancel", command=app.cancel).pack(side="left", padx=4)
        ttk.Button(tb, text="\U0001F9E0 Smart Review", command=self.review).pack(side="left", padx=4)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(tb, text="Select AI-recommended", command=lambda: self.tree.check_all(
            True, lambda f: f.ai_verdict == "delete") or self.tree.check_all(
            False, lambda f: f.ai_verdict != "delete")).pack(side="left")
        ttk.Button(tb, text="All", command=lambda: self.tree.check_all(True)).pack(side="left", padx=2)
        ttk.Button(tb, text="None", command=lambda: self.tree.check_all(False)).pack(side="left", padx=2)
        ttk.Button(tb, text="Open location", command=self.open_loc).pack(side="left", padx=2)
        ttk.Button(tb, text="\U0001F5D1 Delete selected", style="Danger.TButton",
                   command=self.delete).pack(side="right")
        self.summary = ttk.Label(tb, text="", foreground=MUTED)
        self.summary.pack(side="right", padx=10)

        self.tree = CheckTree(self, [("chk", "", 34), ("name", "Name", 230), ("category", "Problem", 170),
                                     ("size", "Size", 80), ("verdict", "AI verdict", 80),
                                     ("reason", "Why", 330), ("folder", "Location", 300)])
        self.tree.pack(fill="both", expand=True)
        self.tree.on_toggle = lambda _i: self.update_summary()
        self.tree.tree.bind("<Double-1>", lambda e: self.open_loc())

    def scan(self):
        roots = self.folders.get()
        if not roots:
            messagebox.showinfo(APP_NAME, "Add at least one folder to scan.")
            return
        s = self.app.settings
        o = junk.JunkOptions(**{k: v.get() for k, v in self.vars.items()},
                             deep_zip_check=s["deep_zip"], include_hidden=s["include_hidden"])
        w = self.app.worker

        def job():
            res = junk.scan_junk(roots, o, progress=lambda p, n: w.progress(f"Scanned {n:,} files - {p}"),
                                 cancelled=w.cancelled)
            if s["auto_review"] and res and not w.cancelled():
                w.progress("Smart review in progress...")
                self.app.engine().review_junk(res, lambda a, b: w.progress(f"Smart review {a}/{b}"))
            return res
        w.run("Scanning for junk...", job, self.show)

    def review(self):
        if not self.findings:
            messagebox.showinfo(APP_NAME, "Run a scan first.")
            return
        w = self.app.worker
        w.run("Smart review...", lambda: self.app.engine().review_junk(
            self.findings, lambda a, b: w.progress(f"Smart review {a}/{b}")) or self.findings, self.show)

    def show(self, findings):
        self.findings = findings
        self.tree.clear()
        order = {"delete": 0, "review": 1, "keep": 2, "": 3}
        for f in sorted(findings, key=lambda f: (order.get(f.ai_verdict, 3), f.category, -f.size)):
            self.tree.add({"name": os.path.basename(f.path) + (os.sep if f.is_dir else ""),
                           "category": f.category, "size": human_size(f.size) if not f.is_dir else "-",
                           "verdict": f.ai_verdict.title() or "-", "reason": f.ai_reason or f.reason,
                           "folder": os.path.dirname(f.path)},
                          obj=f, checked=f.selected, tags=(f.ai_verdict,))
        self.update_summary()
        self.app.set_status(f"Found {len(findings):,} problem items.")

    def update_summary(self):
        objs = self.tree.checked_objs()
        self.summary.config(text=f"{len(objs):,} selected  •  {human_size(sum(f.size for f in objs))}")

    def open_loc(self):
        sel = self.tree.selected_objs()
        if sel:
            open_in_explorer(sel[0].path)

    def delete(self):
        items = self.tree.checked_objs()
        if not items:
            messagebox.showinfo(APP_NAME, "Nothing is selected.")
            return
        risky = sum(1 for f in items if f.ai_verdict in ("review", "keep"))
        where = "the Recycle Bin (you can restore them)" if self.app.settings["recycle"] else "PERMANENTLY"
        msg = f"Delete {len(items):,} items ({human_size(sum(f.size for f in items))}) to {where}?"
        if risky:
            msg += f"\n\n⚠ {risky} of them were marked 'Review' or 'Keep' by the smart review."
        if not messagebox.askyesno(APP_NAME, msg, icon="warning"):
            return
        self.app.run_delete([f.path for f in items], lambda done: self.after_delete(done))

    def after_delete(self, done: set[str]):
        self.show([f for f in self.findings if f.path not in done])


class OrganizerTab(ttk.Frame):
    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=12)
        self.app = app
        self.plan: list[organizer.PlanItem] = []
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text="Folder to tidy:").pack(side="left")
        self.folder = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        ttk.Entry(row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left")
        for name in ("Desktop", "Downloads", "Documents"):
            ttk.Button(row, text=name, command=lambda n=name: self.folder.set(
                os.path.join(os.path.expanduser("~"), n))).pack(side="left", padx=2)

        opt = ttk.Frame(self)
        opt.pack(fill="x", pady=4)
        self.smart = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Smart categories (screenshots, invoices, projects, camera photos...)",
                        variable=self.smart).pack(side="left")
        ttk.Label(opt, text="  Only loose files in the top level are moved; existing sub-folders stay as they are.",
                  foreground=MUTED).pack(side="left")

        tb = toolbar(self)
        ttk.Button(tb, text="\U0001F50D Preview plan", style="Accent.TButton", command=self.preview).pack(side="left")
        ttk.Button(tb, text="Change category...", command=self.recategorize).pack(side="left", padx=4)
        ttk.Button(tb, text="All", command=lambda: self.tree.check_all(True)).pack(side="left", padx=2)
        ttk.Button(tb, text="None", command=lambda: self.tree.check_all(False)).pack(side="left", padx=2)
        ttk.Button(tb, text="↩ Undo last organize", command=self.undo).pack(side="left", padx=8)
        ttk.Button(tb, text="\U0001F4C1 Organize now", style="Accent.TButton", command=self.apply).pack(side="right")
        self.summary = ttk.Label(tb, text="", foreground=MUTED)
        self.summary.pack(side="right", padx=10)

        self.tree = CheckTree(self, [("chk", "", 34), ("size", "Size", 90), ("source", "Decided by", 100),
                                     ("from", "Current file", 420)], tree_mode=True)
        self.tree.pack(fill="both", expand=True)
        self.tree.tree.heading("#0", text="Destination folder / file")
        self.tree.tree.bind("<Double-1>", lambda e: self.recategorize())
        self.tree.on_toggle = lambda _i: self.update_summary()

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get())
        if d:
            self.folder.set(os.path.normpath(d))

    def preview(self):
        folder = self.folder.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror(APP_NAME, "That folder doesn't exist.")
            return
        w = self.app.worker
        smart = self.smart.get()

        def job():
            plan = organizer.build_plan(folder, skip_shortcuts=self.app.settings["skip_shortcuts"])
            if smart and plan:
                w.progress("Working out smart categories...")
                self.app.engine().categorize(plan, organizer.existing_subfolders(folder),
                                             lambda a, b: w.progress(f"Categorizing {a}/{b}"))
            return plan
        w.run("Building plan...", job, self.show)

    def show(self, plan):
        self.plan = plan
        self.tree.clear()
        by_cat: dict[str, list] = {}
        for p in plan:
            by_cat.setdefault(organizer.sanitize_folder_name(p.category), []).append(p)
        for cat in sorted(by_cat, key=lambda c: (c == organizer.OTHER, c.lower())):
            items = by_cat[cat]
            exists = os.path.isdir(os.path.join(self.folder.get(), cat))
            g = self.tree.add({"size": human_size(sum(i.size for i in items)),
                               "source": "existing folder" if exists else "new folder",
                               "from": f"{len(items)} file{'s' if len(items) != 1 else ''}"},
                              text=f"\U0001F4C1 {cat}", tags=("group",))
            for i in sorted(items, key=lambda i: i.name.lower()):
                self.tree.add({"size": human_size(i.size), "source": {"ai": "Smart engine", "user": "You"}
                               .get(i.source, "File type"), "from": i.name}, obj=i, checked=i.include,
                              parent=g, text=i.name)
        self.update_summary()
        if not plan:
            self.app.set_status("No loose files to organize here - it's already tidy!")

    def update_summary(self):
        for iid, obj in self.tree.objs.items():
            obj.include = iid in self.tree.checked
        sel = [p for p in self.plan if p.include]
        cats = {p.category for p in sel}
        self.summary.config(text=f"{len(sel):,} files → {len(cats)} folders")

    def recategorize(self):
        items = self.tree.selected_objs()
        if not items:
            messagebox.showinfo(APP_NAME, "Select one or more files first (Ctrl+click for several).")
            return
        cats = sorted({p.category for p in self.plan})
        new = simpledialog.askstring(APP_NAME, "Move selected files to folder:\n(existing: " +
                                     ", ".join(cats[:12]) + ("..." if len(cats) > 12 else "") + ")",
                                     initialvalue=items[0].category)
        if new and new.strip():
            for i in items:
                i.category, i.source = new.strip(), "user"
            self.show(self.plan)

    def apply(self):
        sel = [p for p in self.plan if p.include]
        if not sel:
            messagebox.showinfo(APP_NAME, "Preview a plan first.")
            return
        cats = {p.category for p in sel}
        if not messagebox.askyesno(APP_NAME, f"Move {len(sel):,} files into {len(cats)} folders?\n\n"
                                             "You can undo this afterwards."):
            return
        folder = self.folder.get()
        w = self.app.worker

        def done(res):
            moved, errors, _log = res
            msg = f"Organized {moved:,} files."
            if errors:
                msg += f"\n\n{len(errors)} could not be moved (in use?):\n" + "\n".join(errors[:10])
            messagebox.showinfo(APP_NAME, msg)
            self.show([])
        w.run("Organizing...", lambda: organizer.apply_plan(
            folder, sel, lambda n, i: w.progress(f"Moving {i}/{len(sel)}: {n}")), done)

    def undo(self):
        logs = organizer.list_undo_logs()
        if not logs:
            messagebox.showinfo(APP_NAME, "Nothing to undo.")
            return
        data = json.loads(logs[0].read_text(encoding="utf-8"))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["time"]))
        if not messagebox.askyesno(APP_NAME, f"Undo organizing '{data['folder']}' from {when}?\n"
                                             f"{len(data['moves'])} files will be moved back."):
            return
        restored, errors = organizer.undo(logs[0])
        messagebox.showinfo(APP_NAME, f"Restored {restored:,} files." +
                            (f"\n{len(errors)} errors." if errors else ""))


class DuplicatesTab(ttk.Frame):
    TYPES = {"All files": None,
             "Images": organizer.CATEGORIES["Images"],
             "Videos": organizer.CATEGORIES["Videos"],
             "Music": organizer.CATEGORIES["Music"],
             "Documents": organizer.CATEGORIES["Documents"] | organizer.CATEGORIES["PDFs"] |
             organizer.CATEGORIES["Spreadsheets"] | organizer.CATEGORIES["Presentations"],
             "Archives & installers": organizer.CATEGORIES["Archives"] | organizer.CATEGORIES["Installers"] |
             organizer.CATEGORIES["Disk Images"]}
    SIZES = {"Any size": 1, "> 100 KB": 100 * 1024, "> 1 MB": 1024 ** 2, "> 10 MB": 10 * 1024 ** 2,
             "> 100 MB": 100 * 1024 ** 2}
    RULES = {"Smart (keep the original)": "smart", "Keep oldest": "oldest", "Keep newest": "newest",
             "Keep shortest path": "shortest"}

    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=12)
        self.app = app
        self.groups: list[duplicates.DupGroup] = []
        ttk.Label(self, text="Folders to search for duplicates:").pack(anchor="w")
        self.folders = FolderList(self, user_folders())
        self.folders.pack(fill="x", pady=4)
        opt = ttk.Frame(self)
        opt.pack(fill="x")
        self.type_var = tk.StringVar(value="All files")
        self.size_var = tk.StringVar(value="> 100 KB")
        self.rule_var = tk.StringVar(value="Smart (keep the original)")
        for lbl, var, vals in [("Type:", self.type_var, self.TYPES), ("Size:", self.size_var, self.SIZES),
                               ("Keep:", self.rule_var, self.RULES)]:
            ttk.Label(opt, text=lbl).pack(side="left", padx=(0, 4))
            cb = ttk.Combobox(opt, textvariable=var, values=list(vals), state="readonly", width=24)
            cb.pack(side="left", padx=(0, 12))
        self.rule_var.trace_add("write", lambda *_: self.reapply_rule())

        tb = toolbar(self)
        ttk.Button(tb, text="\U0001F50D Find duplicates", style="Accent.TButton", command=self.scan).pack(side="left")
        ttk.Button(tb, text="Cancel", command=app.cancel).pack(side="left", padx=4)
        ttk.Button(tb, text="Open location", command=self.open_loc).pack(side="left", padx=4)
        ttk.Button(tb, text="\U0001F5D1 Delete checked copies", style="Danger.TButton",
                   command=self.delete).pack(side="right")
        self.summary = ttk.Label(tb, text="", foreground=MUTED)
        self.summary.pack(side="right", padx=10)
        ttk.Label(self, text="Checked = will be deleted.  Unchecked (green) = the copy that is kept. "
                             "At least one copy of every file is always kept.", foreground=MUTED).pack(anchor="w")
        self.tree = CheckTree(self, [("chk", "", 34), ("size", "Size", 90), ("modified", "Modified", 130),
                                     ("path", "Path", 600)], tree_mode=True)
        self.tree.tree.heading("#0", text="Group / file")
        self.tree.pack(fill="both", expand=True)
        self.tree.on_toggle = lambda _i: self.refresh_marks()
        self.tree.tree.bind("<Double-1>", lambda e: self.open_loc())

    def scan(self):
        roots = self.folders.get()
        if not roots:
            messagebox.showinfo(APP_NAME, "Add at least one folder.")
            return
        exts = self.TYPES[self.type_var.get()]
        min_size = self.SIZES[self.size_var.get()]
        w = self.app.worker

        def job():
            g = duplicates.find_duplicates(roots, min_size=min_size, extensions=exts,
                                           include_hidden=self.app.settings["include_hidden"],
                                           progress=lambda t, n: w.progress(t), cancelled=w.cancelled)
            duplicates.choose_keepers(g, self.RULES[self.rule_var.get()])
            return g
        w.run("Finding duplicates...", job, self.show)

    def reapply_rule(self):
        if self.groups:
            duplicates.choose_keepers(self.groups, self.RULES[self.rule_var.get()])
            self.show(self.groups)

    def show(self, groups):
        self.groups = groups
        self.tree.clear()
        for g in groups:
            gid = self.tree.add({"size": human_size(g.size),
                                 "path": f"{len(g.files)} identical copies • {human_size(g.wasted)} wasted"},
                                text=os.path.basename(g.files[0].path), tags=("group",))
            for f in g.files:
                self.tree.add({"size": human_size(f.size),
                               "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.mtime)),
                               "path": f.path}, obj=f, checked=not f.keep, parent=gid,
                              text="KEEP" if f.keep else "copy", tags=("keep",) if f.keep else ("delete",))
        self.refresh_marks()
        total = sum(g.wasted for g in groups)
        self.app.set_status(f"{len(groups):,} duplicate groups • {human_size(total)} can be freed.")

    def refresh_marks(self):
        for iid, f in self.tree.objs.items():
            on = iid in self.tree.checked
            self.tree.tree.item(iid, text="copy" if on else "KEEP", tags=("delete",) if on else ("keep",))
        objs = self.tree.checked_objs()
        self.summary.config(text=f"{len(objs):,} copies • {human_size(sum(f.size for f in objs))}")

    def open_loc(self):
        sel = self.tree.selected_objs()
        if sel:
            open_in_explorer(sel[0].path)

    def delete(self):
        checked = {id(f) for f in self.tree.checked_objs()}
        to_delete, blocked = [], 0
        for g in self.groups:
            dels = [f for f in g.files if id(f) in checked]
            if len(dels) == len(g.files):   # safety: never delete every copy
                blocked += 1
                dels = dels[1:]
            to_delete += dels
        if not to_delete:
            messagebox.showinfo(APP_NAME, "Nothing is checked.")
            return
        where = "the Recycle Bin" if self.app.settings["recycle"] else "PERMANENTLY"
        msg = f"Delete {len(to_delete):,} duplicate copies ({human_size(sum(f.size for f in to_delete))}) to {where}?"
        if blocked:
            msg += f"\n\n{blocked} group(s) had every copy checked - one copy of each will be kept."
        if messagebox.askyesno(APP_NAME, msg, icon="warning"):
            self.app.run_delete([f.path for f in to_delete], self.after_delete)

    def after_delete(self, done: set[str]):
        groups = []
        for g in self.groups:
            g.files = [f for f in g.files if f.path not in done]
            if len(g.files) > 1:
                groups.append(g)
        self.show(groups)


class SystemTab(ttk.Frame):
    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=12)
        self.app = app
        self.locs = system_clean.default_locations() if IS_WINDOWS else \
            [l for l in system_clean.default_locations() if l.key == "user_temp"]
        self.rb_size = self.rb_count = 0
        ttk.Label(self, text="Windows junk, temporary files and the Recycle Bin. These are deleted permanently "
                             "(they are junk by definition). Files that are in use are skipped automatically.",
                  foreground=MUTED, wraplength=px(900)).pack(anchor="w")
        tb = toolbar(self)
        ttk.Button(tb, text="\U0001F50D Scan", style="Accent.TButton", command=self.scan).pack(side="left")
        ttk.Button(tb, text="Defaults", command=self.defaults).pack(side="left", padx=4)
        ttk.Button(tb, text="All", command=lambda: self.tree.check_all(True)).pack(side="left", padx=2)
        ttk.Button(tb, text="None", command=lambda: self.tree.check_all(False)).pack(side="left", padx=2)
        ttk.Button(tb, text="\U0001F9F9 Clean selected", style="Danger.TButton", command=self.clean).pack(side="right")
        self.summary = ttk.Label(tb, text="", foreground=MUTED)
        self.summary.pack(side="right", padx=10)
        self.tree = CheckTree(self, [("chk", "", 34), ("name", "Item", 260), ("size", "Size", 90),
                                     ("count", "Files", 70), ("admin", "Needs admin", 90),
                                     ("desc", "Details", 420)])
        self.tree.pack(fill="both", expand=True)
        self.tree.on_toggle = lambda _i: self.update_summary()
        self.RB = object()
        self.show()

    def defaults(self):
        for iid, obj in self.tree.objs.items():
            self.tree.set_checked(iid, obj is self.RB or obj.default)
        self.update_summary()

    def show(self):
        prev = {id(o): (iid in self.tree.checked) for iid, o in self.tree.objs.items()}
        self.tree.clear()
        admin = is_admin()
        if IS_WINDOWS:
            self.tree.add({"name": "\U0001F5D1 Recycle Bin", "size": human_size(self.rb_size),
                           "count": f"{self.rb_count:,}", "admin": "No", "desc": "Everything you already deleted"},
                          obj=self.RB, checked=prev.get(id(self.RB), True))
        for loc in self.locs:
            need = loc.admin and not admin
            self.tree.add({"name": loc.name, "size": human_size(loc.size), "count": f"{len(loc.files):,}",
                           "admin": ("Yes ⚠" if need else "Yes") if loc.admin else "No",
                           "desc": loc.description}, obj=loc,
                          checked=prev.get(id(loc), loc.default), tags=("review",) if need else ())
        self.update_summary()

    def update_summary(self):
        total = 0
        for o in self.tree.checked_objs():
            total += self.rb_size if o is self.RB else o.size
        self.summary.config(text=f"Selected: {human_size(total)}")

    def scan(self, then_clean=False):
        w = self.app.worker

        def job():
            system_clean.scan_locations(self.locs, progress=lambda t, n: w.progress(t), cancelled=w.cancelled)
            self.rb_size, self.rb_count = system_clean.recycle_bin_info()

        def done(_):
            self.show()
            total = sum(l.size for l in self.locs) + self.rb_size
            self.app.set_status(f"Found {human_size(total)} of system junk.")
            if then_clean:
                self.clean()
        w.run("Scanning system junk...", job, done)

    def clean(self):
        sel = self.tree.checked_objs()
        if not sel:
            messagebox.showinfo(APP_NAME, "Nothing selected.")
            return
        if all(o is not self.RB and not o.files for o in sel) and not (self.RB in sel and self.rb_count):
            messagebox.showinfo(APP_NAME, "Nothing to clean - run a scan first (or it's already clean).")
            return
        total = sum(self.rb_size if o is self.RB else o.size for o in sel)
        if not messagebox.askyesno(APP_NAME, f"Permanently clean {human_size(total)} of junk?\n\n"
                                             "Tip: close your browsers first so their caches can be cleared."):
            return
        w = self.app.worker

        def job():
            freed = deleted = skipped = 0
            for o in sel:
                if o is self.RB:
                    w.progress("Emptying Recycle Bin...")
                    if system_clean.empty_recycle_bin():
                        freed += self.rb_size
                    continue
                d, f, s = system_clean.clean_location(o, progress=lambda t, n: w.progress(t))
                deleted, freed, skipped = deleted + d, freed + f, skipped + s
            self.rb_size, self.rb_count = system_clean.recycle_bin_info()
            return deleted, freed, skipped

        def done(res):
            deleted, freed, skipped = res
            self.show()
            self.app.tabs[0].refresh()
            messagebox.showinfo(APP_NAME, f"Freed {human_size(freed)}  ({deleted:,} files removed)." +
                                (f"\n{skipped:,} files were in use and skipped - that's normal." if skipped else ""))
        w.run("Cleaning...", job, done)


class SettingsTab(ttk.Frame):
    def __init__(self, nb, app: "App"):
        super().__init__(nb, padding=16)
        self.app = app
        s = app.settings
        ttk.Label(self, text="Interface size", style="H2.TLabel").pack(anchor="w")
        zrow = ttk.Frame(self)
        zrow.pack(anchor="w", pady=(4, 16))
        cur = min(ZOOM_LEVELS, key=lambda k: abs(ZOOM_LEVELS[k] - float(s.get("ui_zoom", 1.25))))
        self.zoom = tk.StringVar(value=cur)
        ttk.Combobox(zrow, textvariable=self.zoom, values=list(ZOOM_LEVELS), state="readonly",
                     width=18).pack(side="left")
        ttk.Button(zrow, text="Apply (restarts app)", command=self.apply_zoom).pack(side="left", padx=8)
        ttk.Label(zrow, text="Text, buttons and lists get bigger. Shortcut: Ctrl + / Ctrl -",
                  foreground=MUTED).pack(side="left")

        ttk.Label(self, text="Smart engine (offline AI)", style="H2.TLabel").pack(anchor="w")
        self.engine = tk.StringVar(value=s["engine"])
        ttk.Radiobutton(self, text="Built-in Smart Engine - instant, no setup, works on any PC (recommended)",
                        variable=self.engine, value="builtin", command=self.save).pack(anchor="w", pady=2)
        ttk.Radiobutton(self, text="Local LLM via Ollama installed on this PC (slower, needs a downloaded model)",
                        variable=self.engine, value="ollama", command=self.save).pack(anchor="w", pady=2)
        row = ttk.Frame(self)
        row.pack(anchor="w", padx=24, pady=4)
        ttk.Label(row, text="Model:").pack(side="left")
        self.model = tk.StringVar(value=s["ollama_model"])
        self.model_cb = ttk.Combobox(row, textvariable=self.model, width=28)
        self.model_cb.pack(side="left", padx=6)
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self.save())
        self.model_cb.bind("<FocusOut>", lambda e: self.save())
        ttk.Button(row, text="Detect local models", command=self.detect).pack(side="left")
        ttk.Label(self, text="Space Finder only ever talks to 127.0.0.1 (this computer). No data leaves your PC.",
                  foreground=MUTED).pack(anchor="w", padx=24)

        ttk.Label(self, text="Safety", style="H2.TLabel").pack(anchor="w", pady=(18, 0))
        self.bools = {}
        for key, label in [
            ("recycle", "Send deleted user files to the Recycle Bin (recommended - lets you restore mistakes)"),
            ("auto_review", "Run the smart review automatically after every junk scan"),
            ("include_hidden", "Include hidden files and folders in scans"),
            ("deep_zip", "Deep-check archives and Office files (slower, finds more corruption)"),
            ("skip_shortcuts", "Organizer: leave shortcuts (.lnk/.url) where they are"),
        ]:
            v = tk.BooleanVar(value=s[key])
            self.bools[key] = v
            ttk.Checkbutton(self, text=label, variable=v, command=self.save).pack(anchor="w", pady=2)
        ttk.Label(self, text=f"\n{APP_NAME} {__version__}  •  Settings: {SETTINGS_FILE}",
                  foreground=MUTED).pack(anchor="w")

    def apply_zoom(self):
        self.app.settings["ui_zoom"] = ZOOM_LEVELS[self.zoom.get()]
        save_settings(self.app.settings)
        if self.app.worker.busy:
            messagebox.showinfo(APP_NAME, "Saved - the new size applies next time you open Space Finder.")
            return
        self.app.restart()

    def detect(self):
        try:
            models = ai.LocalLLM().list_models()
        except ai.AIError as e:
            messagebox.showwarning(APP_NAME, f"{e}\n\nInstall Ollama, then (one time, while online or via USB)\n"
                                             "run:  ollama pull llama3.2:3b")
            return
        self.model_cb["values"] = models
        if models and self.model.get() not in models:
            self.model.set(models[0])
        self.save()
        messagebox.showinfo(APP_NAME, f"Found {len(models)} local model(s)." if models else
                            "Ollama is running but has no models installed.")

    def save(self):
        s = self.app.settings
        s["engine"] = self.engine.get()
        s["ollama_model"] = self.model.get().strip() or DEFAULT_SETTINGS["ollama_model"]
        for k, v in self.bools.items():
            s[k] = v.get()
        save_settings(s)


# ======================================================================================
# Main window
# ======================================================================================

class App(tk.Tk):
    def __init__(self):
        enable_dpi_awareness()
        super().__init__()
        self.settings = load_settings()
        self._apply_scale()
        self.title(f"{APP_NAME} - offline disk cleaner & organizer")
        self._size_window()
        self.configure(bg=BG)
        self._style()

        header = tk.Frame(self, bg=ACCENT)
        header.pack(fill="x")
        tk.Label(header, text="\U0001F50E " + APP_NAME, bg=ACCENT, fg="white",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=14, pady=8)
        tk.Label(header, text="Find junk • Organize clutter • Remove duplicates • Clean C: drive"
                              "  —  100% offline", bg=ACCENT, fg="#dbeafe",
                 font=("Segoe UI", 10)).pack(side="left")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.worker = Worker(self)
        self.tabs = []
        for title, cls in [("  Dashboard  ", DashboardTab), ("  Junk & Corrupt Finder  ", JunkTab),
                           ("  File Organizer  ", OrganizerTab), ("  Duplicates  ", DuplicatesTab),
                           ("  System Cleaner  ", SystemTab), ("  Settings  ", SettingsTab)]:
            t = cls(self.nb, self)
            self.nb.add(t, text=title)
            self.tabs.append(t)

        status = ttk.Frame(self, padding=(10, 4))
        status.pack(fill="x", side="bottom")
        self.status = ttk.Label(status, text="Ready.", foreground=MUTED)
        self.status.pack(side="left", fill="x", expand=True)
        self.pbar = ttk.Progressbar(status, mode="indeterminate", length=px(220))
        self.pbar.pack(side="right")
        for seq, step in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-KP_Add>", 1),
                          ("<Control-minus>", -1), ("<Control-KP_Subtract>", -1)):
            self.bind_all(seq, lambda e, d=step: self.change_zoom(d))

    def change_zoom(self, direction: int):
        levels = sorted(ZOOM_LEVELS.values())
        cur = float(self.settings.get("ui_zoom", 1.25))
        idx = min(range(len(levels)), key=lambda i: abs(levels[i] - cur))
        new = levels[max(0, min(len(levels) - 1, idx + direction))]
        if new == cur:
            return
        name = next(k for k, v in ZOOM_LEVELS.items() if v == new)
        if self.worker.busy:
            return
        if messagebox.askyesno(APP_NAME, f"Change interface size to {name}?\nThe app will restart."):
            self.settings["ui_zoom"] = new
            save_settings(self.settings)
            self.restart()

    def _apply_scale(self):
        global UI_SCALE
        try:
            zoom = float(self.settings.get("ui_zoom", 1.25))
        except (TypeError, ValueError):
            zoom = 1.25
        zoom = min(max(zoom, 0.75), 2.5)
        dpi = self.winfo_fpixels("1i") or 96.0
        UI_SCALE = dpi / 96.0 * zoom
        # point-sized fonts follow tk scaling (pixels per point)
        self.tk.call("tk", "scaling", dpi * zoom / 72.0)
        family = "Segoe UI" if IS_WINDOWS else None
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=10, **({"family": family} if family else {}))
            except tk.TclError:
                pass
        self.option_add("*TCombobox*Listbox.font", "TkDefaultFont")

    def _size_window(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = px(1180), px(760)
        self.minsize(min(px(900), sw - 40), min(px(560), sh - 80))
        if w > sw * 0.95 or h > sh * 0.9:
            w, h = int(sw * 0.95), int(sh * 0.88)
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+10")
            if IS_WINDOWS:
                self.after(50, lambda: self.state("zoomed"))
        else:
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(10, (sh - h) // 3)}")

    def restart(self):
        """Relaunch the app (keeps admin rights) - used after changing the interface size."""
        if getattr(sys, "frozen", False):
            cmd = [sys.executable] + sys.argv[1:]
        else:
            cmd = [sys.executable] + sys.argv
        try:
            subprocess.Popen(cmd, close_fds=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't restart automatically: {e}\nPlease reopen the app.")
            return
        self.destroy()

    def _style(self):
        st = ttk.Style(self)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
        base = ("Segoe UI", 10) if IS_WINDOWS else ("TkDefaultFont", 10)
        st.configure(".", font=base)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG)
        st.configure("TCheckbutton", background=BG)
        st.configure("TRadiobutton", background=BG)
        st.configure("H2.TLabel", font=(base[0], 12, "bold"))
        st.configure("Bold.TLabel", font=(base[0], 10, "bold"))
        st.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1)
        st.configure("CardTitle.TLabel", background=CARD, font=(base[0], 11, "bold"))
        st.configure("CardSub.TLabel", background=CARD, foreground=MUTED)
        st.configure("Accent.TButton", font=(base[0], 10, "bold"), foreground=ACCENT)
        st.configure("Danger.TButton", font=(base[0], 10, "bold"), foreground=BAD)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        st.configure("Treeview", rowheight=line + px(8))
        st.configure("Treeview.Heading", font=(base[0], 10, "bold"))
        st.configure("TButton", padding=(px(10), px(5)))
        st.configure("TNotebook.Tab", padding=(px(10), px(6)))
        st.configure("Red.Horizontal.TProgressbar", background=BAD)

    # ---- helpers used by tabs ----
    def engine(self):
        if self.settings["engine"] == "ollama":
            return ai.LocalLLM(self.settings["ollama_model"])
        return ai.SmartEngine()

    def goto(self, idx: int):
        self.nb.select(idx)

    def cancel(self):
        self.worker.cancel_flag.set()
        self.set_status("Cancelling...")

    def set_status(self, text: str):
        self.status.config(text=text if len(text) < 160 else text[:157] + "...")

    def set_busy(self, busy: bool, label: str = ""):
        if busy:
            self.pbar.start(12)
            self.set_status(label)
            self.config(cursor="watch")
        else:
            self.pbar.stop()
            self.config(cursor="")

    def run_delete(self, paths: list[str], on_done):
        permanent = not self.settings["recycle"]
        w = self.worker

        def job():
            done, errors = set(), []
            for i, p in enumerate(paths, 1):
                if w.cancelled():
                    break
                try:
                    delete_path(p, permanent=permanent)
                    done.add(p)
                except Exception as e:
                    errors.append(f"{os.path.basename(p)}: {e}")
                if i % 20 == 0:
                    w.progress(f"Deleting {i:,}/{len(paths):,}...")
            return done, errors

        def finished(res):
            done, errors = res
            msg = f"Deleted {len(done):,} items."
            if errors:
                msg += f"\n\n{len(errors)} could not be deleted:\n" + "\n".join(errors[:8])
            messagebox.showinfo(APP_NAME, msg)
            self.tabs[0].refresh()
            on_done(done)
        w.run("Deleting...", job, finished)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
