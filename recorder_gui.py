"""
Editable Keyboard & Mouse Macro Recorder / Player
--------------------------------------------------
Records your mouse & keyboard actions into an EDITABLE, human-readable list
(shown in a table you can modify), saves/loads it as JSON, and replays it.

Unlike black-box recorders (e.g. TinyTask), every single action here can be
inspected, edited, deleted, reordered, or manually inserted before playback.

Dependencies:
    pip install pynput

Global hotkeys (work even when this window is not focused):
    F9   - Start / Stop recording
    F10  - Start / Stop playback
    ESC  - Abort playback immediately

Run:
    python recorder_gui.py
"""

import json
import os
import sys
import shutil
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# --- Make this process DPI-aware BEFORE any window is created -------------
# Without this, on any monitor running at non-100% scaling (very common in
# China: 125% / 150% / 175%), Windows silently rescales all coordinates this
# process sees/sets, which is the #1 cause of "recorded position and played
# position don't match". Doing this fixes it for both recording (reading
# real cursor coordinates) and playback (setting real cursor coordinates).
if sys.platform == "win32":
    try:
        import ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (most accurate, Win10 1703+)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

from pynput import mouse, keyboard
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

APP_TITLE = "Editable Macro Recorder"
MOVE_THROTTLE_S = 0.02  # only record a mouse-move at most every 20ms, to keep the list usable

# Must match the MARKER constant in player_template.py exactly -- this is
# how "导出独立EXE" locates where to append the JSON payload, and how the
# exported exe locates where its own data starts.
PLAYER_MARKER = b"\n#####MACRO_PAYLOAD_BEGIN#####\n"


def toggle_ime():
    """Toggle the Chinese/English input method state of the foreground
    window via the Win32 IMM API directly, instead of simulating Ctrl+Space.

    Simulated (SendInput) key combos are marked as 'injected' input by
    Windows, and IME hotkey switching frequently ignores injected input as
    a security measure -- so pressing a recorded Ctrl+Space often silently
    does nothing on playback. Calling ImmSetOpenStatus talks to the IME
    directly and works reliably regardless of that.

    Kept for backward compatibility with macros saved before "set to a
    specific language" (set_ime_status) was added -- prefer that instead.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        hwnd = user32.GetForegroundWindow()
        himc = imm32.ImmGetContext(hwnd)
        if himc:
            status = imm32.ImmGetOpenStatus(himc)
            imm32.ImmSetOpenStatus(himc, 0 if status else 1)
            imm32.ImmReleaseContext(hwnd, himc)
    except Exception:
        pass


def set_ime_status(open_status):
    """Force the foreground window's IME to a specific state, instead of
    toggling: open_status=True -> Chinese (IME open), False -> English
    (IME closed/Latin). This is deterministic regardless of whatever state
    the IME happened to be in before, which a toggle can't guarantee."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        hwnd = user32.GetForegroundWindow()
        himc = imm32.ImmGetContext(hwnd)
        if himc:
            imm32.ImmSetOpenStatus(himc, 1 if open_status else 0)
            imm32.ImmReleaseContext(hwnd, himc)
    except Exception:
        pass


def resource_path(relative):
    """Path to a bundled resource, both when running from source and when
    frozen into an exe by PyInstaller (--add-data extracts to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def app_dir():
    """Directory the actual double-clicked exe (or script) lives in -- NOT
    the PyInstaller onefile temp-extraction folder. Used to store the user's
    saved snippets.json next to the exe so it survives across runs."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SNIPPETS_FILE = os.path.join(app_dir(), "snippets.json")


def load_snippets():
    if os.path.exists(SNIPPETS_FILE):
        try:
            with open(SNIPPETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_snippets(snippets):
    try:
        with open(SNIPPETS_FILE, "w", encoding="utf-8") as f:
            json.dump(snippets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Canonical hotkey format: modifiers (in this fixed order) + "+" + main key,
# all lowercase, e.g. "ctrl+1", "ctrl+alt+p", "ctrl+f2".
MOD_ORDER = ["ctrl", "alt", "shift"]


def normalize_hotkey(raw):
    """Parse/validate a user-typed hotkey string. Returns the canonical
    string, or None if invalid. At least one modifier is REQUIRED, so a
    snippet hotkey can never collide with ordinary recorded typing."""
    if not raw:
        return None
    parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    if not parts:
        return None
    mods = [p for p in parts if p in MOD_ORDER]
    mods_sorted = [m for m in MOD_ORDER if m in mods]
    others = [p for p in parts if p not in MOD_ORDER]
    if len(others) != 1 or not mods_sorted:
        return None
    return "+".join(mods_sorted + [others[0]])


# =========================================================================
# Key / Button <-> string helpers (so actions can be saved as plain JSON)
# =========================================================================

def key_to_str(key):
    if isinstance(key, KeyCode):
        if key.char is not None:
            return f"char:{key.char}"
        return f"vk:{key.vk}"
    return f"special:{key.name}"


def str_to_key(s):
    if s.startswith("char:"):
        return KeyCode.from_char(s[5:])
    if s.startswith("vk:"):
        return KeyCode.from_vk(int(s[3:]))
    if s.startswith("special:"):
        return getattr(Key, s[8:])
    raise ValueError(f"Unknown key string: {s}")


def button_to_str(button):
    return button.name


def str_to_button(s):
    return getattr(Button, s)


def action_summary(a):
    """Human readable one-line summary for the table's 'Details' column."""
    t = a["type"]
    if t == "move":
        return f"to ({a['x']}, {a['y']})"
    if t == "click":
        state = "down" if a["pressed"] else "up"
        return f"{a['button']} {state} @ ({a['x']}, {a['y']})"
    if t == "scroll":
        return f"dx={a['dx']} dy={a['dy']} @ ({a['x']}, {a['y']})"
    if t == "key_down":
        return f"press '{a['key']}'"
    if t == "key_up":
        return f"release '{a['key']}'"
    if t == "wait":
        return f"pause {a['duration_ms']} ms"
    if t == "ime_toggle":
        return "切换中/英文输入法（旧，建议改用下方明确版本）"
    if t == "ime_set":
        return "设为 中文输入法" if a.get("open", True) else "设为 英文输入法"
    if t == "type_text":
        preview = a.get("text", "")
        if len(preview) > 20:
            preview = preview[:20] + "..."
        return f'输入文本: "{preview}"'
    return str(a)


# =========================================================================
# Main Application
# =========================================================================

class MacroApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("780x560")

        self.actions = []          # list of dict, the editable action list
        self.mode = "idle"         # idle | recording | playing
        self._last_event_time = None
        self._last_move_time = 0.0

        self.mouse_listener = None
        self.kb_hotkey_listener = None
        self._abort_playback = threading.Event()

        # snippet ("quick text") support
        self.snippets = load_snippets()          # list of {name, content, hotkey}
        self._rebuild_hotkey_map()
        self._mod_ctrl = False
        self._mod_alt = False
        self._mod_shift = False
        self._swallow_next_release = set()

        self.speed_var = tk.DoubleVar(value=1.0)
        self.repeat_var = tk.IntVar(value=1)
        self.status_var = tk.StringVar(value="就绪。F9=录制 F10=回放 ESC=中止回放")

        self._build_ui()
        self._start_hotkey_listener()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------
    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.btn_record = ttk.Button(toolbar, text="● 开始录制 (F9)", command=self.toggle_recording)
        self.btn_record.pack(side=tk.LEFT, padx=3)

        self.btn_play = ttk.Button(toolbar, text="▶ 播放 (F10)", command=self.toggle_playback)
        self.btn_play.pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        ttk.Label(toolbar, text="速度:").pack(side=tk.LEFT)
        speed_box = ttk.Spinbox(toolbar, from_=0.1, to=10.0, increment=0.1,
                                 textvariable=self.speed_var, width=5)
        speed_box.pack(side=tk.LEFT, padx=3)

        ttk.Label(toolbar, text="重复次数:").pack(side=tk.LEFT, padx=(10, 0))
        repeat_box = ttk.Spinbox(toolbar, from_=1, to=9999, textvariable=self.repeat_var, width=6)
        repeat_box.pack(side=tk.LEFT, padx=3)

        self.force_chinese_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="播放前先设为中文输入法",
                         variable=self.force_chinese_var).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        ttk.Button(toolbar, text="打开", command=self.load_file).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="保存", command=self.save_file).pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="导出独立EXE", command=self.export_standalone_exe).pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="文本片段管理", command=self.open_snippet_manager).pack(side=tk.LEFT, padx=3)

        # --- editable table ---
        columns = ("idx", "type", "details", "delay")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("idx", text="#")
        self.tree.heading("type", text="类型")
        self.tree.heading("details", text="详情")
        self.tree.heading("delay", text="延时(ms)")
        self.tree.column("idx", width=45, anchor=tk.CENTER)
        self.tree.column("type", width=90, anchor=tk.CENTER)
        self.tree.column("details", width=380)
        self.tree.column("delay", width=90, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", lambda e: self.edit_selected_row())
        self.tree.bind("<Button-3>", self._show_tree_context_menu)

        # --- edit toolbar ---
        edit_bar = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        edit_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(edit_bar, text="编辑该行", command=self.edit_selected_row).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="上移", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="下移", command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="插入等待", command=self.insert_wait).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="插入-设为中文", command=lambda: self.insert_ime_set(True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="插入-设为英文", command=lambda: self.insert_ime_set(False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(edit_bar, text="清空全部", command=self.clear_all).pack(side=tk.LEFT, padx=3)

        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _set_status(self, text):
        self.status_var.set(text)

    def _show_tree_context_menu(self, event):
        """Right-click menu on the action table: insert a predefined text
        snippet directly at the clicked row, no hotkey needed."""
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        pos = int(row) + 1 if row else len(self.actions)

        menu = tk.Menu(self.root, tearoff=0)
        if self.snippets:
            sub = tk.Menu(menu, tearoff=0)
            for sn in self.snippets:
                label = sn["name"]
                if sn.get("hotkey"):
                    label += f"  ({sn['hotkey']})"
                sub.add_command(label=label, command=lambda sn=sn, pos=pos: self._insert_snippet_action(sn, pos))
            menu.add_cascade(label="插入文本片段", menu=sub)
        else:
            menu.add_command(label="插入文本片段 (还没有片段)", state=tk.DISABLED)
        menu.add_separator()
        menu.add_command(label="管理文本片段...", command=self.open_snippet_manager)
        menu.tk_popup(event.x_root, event.y_root)

    def _insert_snippet_action(self, snippet, pos):
        self.actions.insert(pos, {"type": "type_text", "text": snippet["content"], "delay_ms": 200})
        self._refresh_table()
        self._set_status(f"已插入文本片段“{snippet['name']}”。")

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for i, a in enumerate(self.actions):
            self.tree.insert("", tk.END, iid=str(i),
                              values=(i + 1, a["type"], action_summary(a), a.get("delay_ms", 0)))

    # ----------------------------------------------------------------
    # Global hotkey listener (always running: F9 / F10 / ESC)
    # ----------------------------------------------------------------
    def _start_hotkey_listener(self):
        def on_press(key):
            # track modifier state, used to build "ctrl+1"-style live combos
            if key in (Key.ctrl_l, Key.ctrl_r):
                self._mod_ctrl = True
            elif key in (Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None)):
                self._mod_alt = True
            elif key in (Key.shift, Key.shift_r):
                self._mod_shift = True
            elif self.mode in ("idle", "recording"):
                # only non-modifier keys can complete a snippet hotkey combo
                combo = self._live_combo(key)
                if combo and combo in self.hotkey_map:
                    self._swallow_next_release.add(key)
                    self.root.after(0, self._trigger_snippet, self.hotkey_map[combo])
                    return

            if self.mode == "idle":
                if key == Key.f9:
                    self.root.after(0, self.start_recording)
                elif key == Key.f10:
                    self.root.after(0, self.start_playback)
            elif self.mode == "recording":
                if key == Key.f9:
                    self.root.after(0, self.stop_recording)
                    return
                self._record_key_event(key, True)
            elif self.mode == "playing":
                if key == Key.esc:
                    self._abort_playback.set()

        def on_release(key):
            if key in (Key.ctrl_l, Key.ctrl_r):
                self._mod_ctrl = False
            elif key in (Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None)):
                self._mod_alt = False
            elif key in (Key.shift, Key.shift_r):
                self._mod_shift = False

            if key in self._swallow_next_release:
                self._swallow_next_release.discard(key)
                return

            if self.mode == "recording":
                if key == Key.f9:
                    return
                self._record_key_event(key, False)

        self.kb_hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.kb_hotkey_listener.start()

    # ----------------------------------------------------------------
    # Recording
    # ----------------------------------------------------------------
    def toggle_recording(self):
        if self.mode == "recording":
            self.stop_recording()
        elif self.mode == "idle":
            self.start_recording()

    def start_recording(self):
        if self.mode != "idle":
            return
        if self.actions and not messagebox.askyesno(APP_TITLE, "开始新录制会清空当前动作列表，继续吗？"):
            return
        self.actions = []
        self.mode = "recording"
        self._last_event_time = time.perf_counter()
        self._last_move_time = 0.0
        self.btn_record.config(text="■ 停止录制 (F9)")
        self._set_status("录制中... 按 F9 停止")

        self.mouse_listener = mouse.Listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll,
        )
        self.mouse_listener.start()

    def stop_recording(self):
        if self.mode != "recording":
            return
        self.mode = "idle"
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        self.btn_record.config(text="● 开始录制 (F9)")
        self._refresh_table()
        self._set_status(f"录制完成，共 {len(self.actions)} 个动作。")

    def _elapsed_ms(self):
        now = time.perf_counter()
        dt = (now - self._last_event_time) * 1000.0
        self._last_event_time = now
        return max(0, round(dt))

    def _record_key_event(self, key, pressed):
        try:
            k = key_to_str(key)
        except Exception:
            return
        self.actions.append({
            "type": "key_down" if pressed else "key_up",
            "key": k,
            "delay_ms": self._elapsed_ms(),
        })

    def _on_mouse_move(self, x, y):
        if self.mode != "recording":
            return
        now = time.perf_counter()
        if now - self._last_move_time < MOVE_THROTTLE_S:
            return
        self._last_move_time = now
        self.actions.append({
            "type": "move", "x": x, "y": y,
            "delay_ms": self._elapsed_ms(),
        })

    def _on_mouse_click(self, x, y, button, pressed):
        if self.mode != "recording":
            return
        self.actions.append({
            "type": "click", "x": x, "y": y,
            "button": button_to_str(button), "pressed": pressed,
            "delay_ms": self._elapsed_ms(),
        })

    def _on_mouse_scroll(self, x, y, dx, dy):
        if self.mode != "recording":
            return
        self.actions.append({
            "type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy,
            "delay_ms": self._elapsed_ms(),
        })

    # ----------------------------------------------------------------
    # Playback
    # ----------------------------------------------------------------
    def toggle_playback(self):
        if self.mode == "playing":
            self._abort_playback.set()
        elif self.mode == "idle":
            self.start_playback()

    def start_playback(self):
        if self.mode != "idle":
            return
        if not self.actions:
            messagebox.showinfo(APP_TITLE, "动作列表为空，请先录制或加载一个文件。")
            return
        self.mode = "playing"
        self._abort_playback.clear()
        self.btn_play.config(text="■ 停止 (ESC)")
        self._set_status("回放中... 按 ESC 立即中止")
        t = threading.Thread(target=self._playback_worker, daemon=True)
        t.start()

    def _playback_worker(self):
        mouse_ctl = mouse.Controller()
        kb_ctl = keyboard.Controller()
        try:
            speed = max(0.01, float(self.speed_var.get()))
        except Exception:
            speed = 1.0
        try:
            repeats = max(1, int(self.repeat_var.get()))
        except Exception:
            repeats = 1

        if self.force_chinese_var.get():
            set_ime_status(True)

        aborted = False
        for r in range(repeats):
            if aborted:
                break
            for a in self.actions:
                if self._abort_playback.is_set():
                    aborted = True
                    break
                delay = a.get("delay_ms", 0) / 1000.0 / speed
                if delay > 0:
                    time.sleep(delay)
                try:
                    self._execute_action(a, mouse_ctl, kb_ctl)
                except Exception:
                    pass  # skip a bad/edited action instead of crashing playback

        self.root.after(0, self._finish_playback, aborted)

    def _execute_action(self, a, mouse_ctl, kb_ctl):
        t = a["type"]
        if t == "move":
            mouse_ctl.position = (a["x"], a["y"])
        elif t == "click":
            mouse_ctl.position = (a["x"], a["y"])
            btn = str_to_button(a["button"])
            if a["pressed"]:
                mouse_ctl.press(btn)
            else:
                mouse_ctl.release(btn)
        elif t == "scroll":
            mouse_ctl.position = (a["x"], a["y"])
            mouse_ctl.scroll(a["dx"], a["dy"])
        elif t == "key_down":
            kb_ctl.press(str_to_key(a["key"]))
        elif t == "key_up":
            kb_ctl.release(str_to_key(a["key"]))
        elif t == "wait":
            time.sleep(a.get("duration_ms", 0) / 1000.0)
        elif t == "ime_toggle":
            toggle_ime()
        elif t == "ime_set":
            set_ime_status(a.get("open", True))
        elif t == "type_text":
            # Direct Unicode text injection (pynput's Controller.type on
            # Windows uses KEYEVENTF_UNICODE), bypasses IME composition
            # entirely -- reliable for Chinese/any Unicode text, unlike
            # replaying raw pinyin keystrokes.
            kb_ctl.type(a.get("text", ""))

    def _finish_playback(self, aborted):
        self.mode = "idle"
        self.btn_play.config(text="▶ 播放 (F10)")
        self._set_status("回放已中止。" if aborted else "回放完成。")

    # ----------------------------------------------------------------
    # Table editing
    # ----------------------------------------------------------------
    def _selected_indices(self):
        return sorted(int(i) for i in self.tree.selection())

    def edit_selected_row(self):
        sel = self._selected_indices()
        if len(sel) != 1:
            messagebox.showinfo(APP_TITLE, "请只选中一行进行编辑。")
            return
        idx = sel[0]
        a = self.actions[idx]
        new_a = self._edit_action_dialog(a)
        if new_a is not None:
            self.actions[idx] = new_a
            self._refresh_table()

    def _edit_action_dialog(self, a):
        """Simple modal dialog to edit an action's fields based on its type."""
        win = tk.Toplevel(self.root)
        win.title(f"编辑动作 - {a['type']}")
        win.grab_set()
        win.resizable(False, False)

        entries = {}
        row = 0

        def add_field(label, key, value):
            nonlocal row
            ttk.Label(win, text=label).grid(row=row, column=0, sticky=tk.W, padx=8, pady=4)
            var = tk.StringVar(value=str(value))
            ttk.Entry(win, textvariable=var, width=24).grid(row=row, column=1, padx=8, pady=4)
            entries[key] = var
            row += 1

        add_field("延时 (ms):", "delay_ms", a.get("delay_ms", 0))

        t = a["type"]
        if t in ("move", "click", "scroll"):
            add_field("X 坐标:", "x", a.get("x", 0))
            add_field("Y 坐标:", "y", a.get("y", 0))
        if t == "click":
            add_field("按钮 (left/right/middle):", "button", a.get("button", "left"))
            add_field("按下(True)/松开(False):", "pressed", a.get("pressed", True))
        if t == "scroll":
            add_field("dx:", "dx", a.get("dx", 0))
            add_field("dy:", "dy", a.get("dy", 0))
        if t in ("key_down", "key_up"):
            add_field("按键 (如 char:a / special:enter):", "key", a.get("key", ""))
        if t == "wait":
            add_field("等待时长 (ms):", "duration_ms", a.get("duration_ms", 0))

        text_widget = None
        if t == "type_text":
            ttk.Label(win, text="文本内容:").grid(row=row, column=0, sticky=tk.NW, padx=8, pady=4)
            text_widget = tk.Text(win, width=30, height=4)
            text_widget.insert("1.0", a.get("text", ""))
            text_widget.grid(row=row, column=1, padx=8, pady=4)
            row += 1

        result = {}

        def on_ok():
            try:
                new_a = dict(a)
                new_a["delay_ms"] = int(float(entries["delay_ms"].get()))
                if t in ("move", "click", "scroll"):
                    new_a["x"] = int(float(entries["x"].get()))
                    new_a["y"] = int(float(entries["y"].get()))
                if t == "click":
                    new_a["button"] = entries["button"].get().strip()
                    new_a["pressed"] = entries["pressed"].get().strip().lower() in ("true", "1", "yes")
                if t == "scroll":
                    new_a["dx"] = int(float(entries["dx"].get()))
                    new_a["dy"] = int(float(entries["dy"].get()))
                if t in ("key_down", "key_up"):
                    new_a["key"] = entries["key"].get().strip()
                if t == "wait":
                    new_a["duration_ms"] = int(float(entries["duration_ms"].get()))
                if t == "type_text":
                    new_a["text"] = text_widget.get("1.0", "end-1c")
                result["value"] = new_a
                win.destroy()
            except ValueError:
                messagebox.showerror(APP_TITLE, "输入的数值格式不正确。", parent=win)

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="确定", command=on_ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side=tk.LEFT, padx=6)

        win.wait_window()
        return result.get("value")

    def delete_selected(self):
        sel = self._selected_indices()
        if not sel:
            return
        for i in reversed(sel):
            del self.actions[i]
        self._refresh_table()

    def move_selected(self, direction):
        sel = self._selected_indices()
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + direction
        if 0 <= j < len(self.actions):
            self.actions[i], self.actions[j] = self.actions[j], self.actions[i]
            self._refresh_table()
            self.tree.selection_set(str(j))

    def insert_wait(self):
        ms = simpledialog.askinteger(APP_TITLE, "插入等待时长 (毫秒):", initialvalue=500, minvalue=0)
        if ms is None:
            return
        sel = self._selected_indices()
        pos = sel[-1] + 1 if sel else len(self.actions)
        self.actions.insert(pos, {"type": "wait", "duration_ms": ms, "delay_ms": 0})
        self._refresh_table()

    def insert_ime_set(self, open_status):
        """Insert a deterministic 'set IME to Chinese/English' action (uses
        the Imm32 API directly instead of simulating Ctrl+Space, and unlike
        a toggle, always ends up in the same state regardless of whatever
        state the IME happened to be in beforehand)."""
        sel = self._selected_indices()
        pos = sel[-1] + 1 if sel else len(self.actions)
        self.actions.insert(pos, {"type": "ime_set", "open": bool(open_status), "delay_ms": 200})
        self._refresh_table()
        label = "中文" if open_status else "英文"
        self._set_status(f"已插入“设为{label}输入法”动作。")

    def clear_all(self):
        if self.actions and messagebox.askyesno(APP_TITLE, "确定清空所有动作吗？"):
            self.actions = []
            self._refresh_table()

    # ----------------------------------------------------------------
    # Text snippets ("快速输入固定内容")
    # ----------------------------------------------------------------
    def _rebuild_hotkey_map(self):
        self.hotkey_map = {s["hotkey"]: s for s in self.snippets if s.get("hotkey")}

    def open_snippet_manager(self):
        win = tk.Toplevel(self.root)
        win.title("文本片段管理")
        win.geometry("520x360")
        win.grab_set()

        cols = ("name", "content", "hotkey")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        tree.heading("name", text="名称")
        tree.heading("content", text="内容")
        tree.heading("hotkey", text="快捷键")
        tree.column("name", width=110)
        tree.column("content", width=250)
        tree.column("hotkey", width=100, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        def refresh():
            tree.delete(*tree.get_children())
            for i, sn in enumerate(self.snippets):
                preview = sn["content"][:30] + ("..." if len(sn["content"]) > 30 else "")
                tree.insert("", tk.END, iid=str(i), values=(sn["name"], preview, sn.get("hotkey", "")))

        def selected_index():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def on_add():
            sn = self._snippet_edit_dialog(win, None)
            if sn:
                self.snippets.append(sn)
                self._rebuild_hotkey_map()
                save_snippets(self.snippets)
                refresh()

        def on_edit():
            idx = selected_index()
            if idx is None:
                messagebox.showinfo(APP_TITLE, "请先选中一个片段。", parent=win)
                return
            sn = self._snippet_edit_dialog(win, self.snippets[idx])
            if sn:
                self.snippets[idx] = sn
                self._rebuild_hotkey_map()
                save_snippets(self.snippets)
                refresh()

        def on_delete():
            idx = selected_index()
            if idx is None:
                return
            if messagebox.askyesno(APP_TITLE, f"删除片段“{self.snippets[idx]['name']}”？", parent=win):
                del self.snippets[idx]
                self._rebuild_hotkey_map()
                save_snippets(self.snippets)
                refresh()

        btn_bar = ttk.Frame(win, padding=(8, 0, 8, 8))
        btn_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(btn_bar, text="新增", command=on_add).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_bar, text="编辑", command=on_edit).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_bar, text="删除", command=on_delete).pack(side=tk.LEFT, padx=3)
        tree.bind("<Double-1>", lambda e: on_edit())

        refresh()

    def _snippet_edit_dialog(self, parent, existing):
        """Add/edit dialog for one snippet. Returns the new snippet dict,
        or None if cancelled."""
        win = tk.Toplevel(parent)
        win.title("编辑文本片段" if existing else "新增文本片段")
        win.grab_set()
        win.resizable(False, False)

        ttk.Label(win, text="名称:").grid(row=0, column=0, sticky=tk.W, padx=8, pady=4)
        name_var = tk.StringVar(value=existing["name"] if existing else "")
        ttk.Entry(win, textvariable=name_var, width=32).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(win, text="内容:").grid(row=1, column=0, sticky=tk.NW, padx=8, pady=4)
        content_text = tk.Text(win, width=32, height=4)
        if existing:
            content_text.insert("1.0", existing["content"])
        content_text.grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(win, text="快捷键 (可留空):").grid(row=2, column=0, sticky=tk.W, padx=8, pady=4)
        hotkey_var = tk.StringVar(value=existing.get("hotkey", "") if existing else "")
        ttk.Entry(win, textvariable=hotkey_var, width=32).grid(row=2, column=1, padx=8, pady=4)
        ttk.Label(win, text="格式如 ctrl+1 或 ctrl+alt+p，必须带 ctrl/alt/shift 中至少一个",
                  foreground="#666").grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=8)

        result = {}

        def on_ok():
            name = name_var.get().strip()
            content = content_text.get("1.0", "end-1c")
            raw_hotkey = hotkey_var.get().strip()

            if not name:
                messagebox.showerror(APP_TITLE, "名称不能为空。", parent=win)
                return
            if not content:
                messagebox.showerror(APP_TITLE, "内容不能为空。", parent=win)
                return

            hotkey = None
            if raw_hotkey:
                hotkey = normalize_hotkey(raw_hotkey)
                if hotkey is None:
                    messagebox.showerror(
                        APP_TITLE,
                        "快捷键格式不对。需要至少一个修饰键(ctrl/alt/shift)加一个主键，\n"
                        "例如: ctrl+1  、  ctrl+alt+p  、  alt+shift+9",
                        parent=win)
                    return
                # uniqueness check (ignore the entry being edited itself)
                for i, sn in enumerate(self.snippets):
                    if sn.get("hotkey") == hotkey and sn is not existing:
                        messagebox.showerror(APP_TITLE, f"快捷键 {hotkey} 已经被“{sn['name']}”占用了。", parent=win)
                        return

            result["value"] = {"name": name, "content": content, "hotkey": hotkey}
            win.destroy()

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="确定", command=on_ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side=tk.LEFT, padx=6)

        win.wait_window()
        return result.get("value")

    def _key_canonical_name(self, key):
        """Map a pynput key to a stable name usable in a hotkey string.
        Uses the raw virtual-key code for letters/digits (not .char), since
        .char can be None or different when a modifier is held."""
        if isinstance(key, KeyCode):
            vk = key.vk
            if vk is not None:
                if 0x30 <= vk <= 0x39:       # '0'-'9'
                    return chr(vk)
                if 0x41 <= vk <= 0x5A:       # 'A'-'Z'
                    return chr(vk).lower()
                return f"vk{vk}"
            if key.char:
                return key.char.lower()
            return None
        return key.name  # e.g. 'f2', 'space', ...

    def _live_combo(self, key):
        mods = []
        if self._mod_ctrl:
            mods.append("ctrl")
        if self._mod_alt:
            mods.append("alt")
        if self._mod_shift:
            mods.append("shift")
        if not mods:
            return None
        main = self._key_canonical_name(key)
        if main is None:
            return None
        return "+".join(mods + [main])

    def _trigger_snippet(self, snippet):
        text = snippet["content"]
        if self.mode == "recording":
            self.actions.append({"type": "type_text", "text": text, "delay_ms": self._elapsed_ms()})
            self.root.after(0, self._refresh_table)
            self.root.after(0, self._set_status, f"已插入文本片段“{snippet['name']}”并正在输入...")

        def worker():
            try:
                keyboard.Controller().type(text)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------------------------------------------------
    # File I/O
    # ----------------------------------------------------------------
    def save_file(self):
        if not self.actions:
            messagebox.showinfo(APP_TITLE, "动作列表为空，没有可保存的内容。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                             filetypes=[("Macro JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.actions, f, ensure_ascii=False, indent=2)
        self._set_status(f"已保存到 {path}")

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Macro JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.actions = json.load(f)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"加载失败: {e}")
            return
        self._refresh_table()
        self._set_status(f"已加载 {path}，共 {len(self.actions)} 个动作")

    # ----------------------------------------------------------------
    # Export a fully standalone playback exe (no dependency on this app)
    # ----------------------------------------------------------------
    def export_standalone_exe(self):
        if not self.actions:
            messagebox.showinfo(APP_TITLE, "动作列表为空，请先录制或加载再导出。")
            return

        template_path = resource_path("PlayerTemplate.exe")
        if not os.path.exists(template_path):
            # Fallback: also check a local dist/ folder (useful when testing
            # from source before everything is bundled together).
            alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "PlayerTemplate.exe")
            if os.path.exists(alt):
                template_path = alt
            else:
                messagebox.showerror(
                    APP_TITLE,
                    "找不到 PlayerTemplate.exe 播放器模板。\n\n"
                    "这个模板由 GitHub Actions 自动构建并打包进 MacroRecorder.exe 内部，"
                    "如果你是直接运行源码 recorder_gui.py 在测试，需要先手动用 PyInstaller "
                    "编译一次 player_template.py 并把生成的 dist/PlayerTemplate.exe 放到本文件同目录下。"
                )
                return

        out_path = filedialog.asksaveasfilename(
            defaultextension=".exe",
            filetypes=[("Windows 可执行文件", "*.exe")],
            initialfile="MyMacro.exe",
            title="导出独立可执行文件"
        )
        if not out_path:
            return

        try:
            speed = float(self.speed_var.get())
        except Exception:
            speed = 1.0
        try:
            repeat = int(self.repeat_var.get())
        except Exception:
            repeat = 1

        payload = {
            "actions": self.actions,
            "speed": speed,
            "repeat": repeat,
            "countdown_sec": 3,
            "force_chinese_start": bool(self.force_chinese_var.get()),
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            shutil.copyfile(template_path, out_path)
            with open(out_path, "ab") as f:
                f.write(PLAYER_MARKER)
                f.write(payload_bytes)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败: {e}")
            return

        self._set_status(f"已导出独立可执行文件: {out_path}")
        messagebox.showinfo(
            APP_TITLE,
            f"导出成功！\n\n{out_path}\n\n"
            "这个 exe 是完全独立的，双击即可回放，不需要本程序、"
            "不需要 Python，可以直接拷贝给别人用。"
        )

    # ----------------------------------------------------------------
    def _on_close(self):
        if self.kb_hotkey_listener:
            self.kb_hotkey_listener.stop()
        if self.mouse_listener:
            self.mouse_listener.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
