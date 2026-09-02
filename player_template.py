"""
Standalone Macro Player (BUILD TEMPLATE)
------------------------------------------
This file is compiled into PlayerTemplate.exe by the GitHub Actions workflow.

It is NOT meant to be run as-is. The main Recorder app's "导出独立EXE / Export
standalone EXE" feature takes this already-compiled PlayerTemplate.exe and
APPENDS the recorded action list (as JSON) to the very end of the file,
producing a brand new .exe that:

  - needs no Python, no pynput install, no main Recorder app to run
  - just double-click and it plays back the recorded macro
  - is a completely normal, independent file you can rename / move / share

How it finds its data at runtime:
  1. Locate its own exe file on disk
  2. Read the whole file, search for a unique marker near the end
  3. Everything after the marker is the JSON payload (actions + settings)

If no marker is found (i.e. this is the raw, unpatched template), it just
tells the user that and exits -- that's expected when you run the template
directly instead of an exported/exported-with-data copy.
"""

import sys
import os
import json
import time
import threading
import tkinter as tk

# --- Make this process DPI-aware BEFORE any window is created -------------
# Must match the same fix in recorder_gui.py: without this, mouse coordinates
# get silently rescaled by Windows on any monitor running below 100% zoom
# (very common: 125%/150%/175%), causing recorded and played-back positions
# to not match.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

from pynput import mouse, keyboard
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

MARKER = b"\n#####MACRO_PAYLOAD_BEGIN#####\n"


def toggle_ime():
    """Directly toggle the foreground window's IME open/closed state via
    Imm32, instead of relying on a simulated Ctrl+Space (which Windows
    often ignores for synthetic/injected input -- see recorder_gui.py's
    toggle_ime() docstring for the full explanation). Kept for backward
    compatibility with older exported macros; prefer set_ime_status()."""
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
    """Force the foreground window's IME to a specific state (True=Chinese
    / open, False=English / closed) rather than toggling -- deterministic
    regardless of whatever state it happened to be in beforehand."""
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


def maximize_foreground_window():
    """Force the currently foreground (active) window to maximized state,
    regardless of whatever size/position it happened to open at."""
    if sys.platform != "win32":
        return
    try:
        SW_MAXIMIZE = 3
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
    except Exception:
        pass


def key_str_to_key(s):
    if s.startswith("char:"):
        return KeyCode.from_char(s[5:])
    if s.startswith("vk:"):
        return KeyCode.from_vk(int(s[3:]))
    if s.startswith("special:"):
        return getattr(Key, s[8:])
    raise ValueError(f"Unknown key string: {s}")


def str_to_button(s):
    return getattr(Button, s)


def _self_exe_path():
    # When frozen by PyInstaller (--onefile), sys.executable is the real
    # exe on disk (the one the user double-clicked), which is exactly the
    # file we need to read the appended payload from.
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)


def load_payload():
    try:
        with open(_self_exe_path(), "rb") as f:
            data = f.read()
    except Exception:
        return None
    idx = data.rfind(MARKER)
    if idx == -1:
        return None
    payload_bytes = data[idx + len(MARKER):]
    try:
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None


def execute_action(a, mouse_ctl, kb_ctl):
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
        kb_ctl.press(key_str_to_key(a["key"]))
    elif t == "key_up":
        kb_ctl.release(key_str_to_key(a["key"]))
    elif t == "wait":
        time.sleep(a.get("duration_ms", 0) / 1000.0)
    elif t == "ime_toggle":
        toggle_ime()
    elif t == "ime_set":
        set_ime_status(a.get("open", True))
    elif t == "type_text":
        kb_ctl.type(a.get("text", ""))
    elif t == "maximize_window":
        maximize_foreground_window()


def run_gui(payload):
    actions = payload.get("actions", [])
    speed = float(payload.get("speed", 1.0)) or 1.0
    repeats = int(payload.get("repeat", 1)) or 1
    countdown = int(payload.get("countdown_sec", 3))
    force_chinese_start = bool(payload.get("force_chinese_start", False))

    root = tk.Tk()
    root.title("Macro Player")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    w, h = 320, 100
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{sw - w - 30}+30")
    label = tk.Label(root, text="", font=("Segoe UI", 11), justify=tk.CENTER)
    label.pack(expand=True, fill=tk.BOTH)

    abort_flag = threading.Event()

    def on_press(key):
        if key == Key.esc:
            abort_flag.set()

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    def finish():
        listener.stop()
        root.destroy()

    def tick(remaining):
        if abort_flag.is_set():
            finish()
            return
        if remaining > 0:
            label.config(text=f"{remaining} 秒后开始播放\n(随时按 ESC 中止)")
            root.after(1000, tick, remaining - 1)
        else:
            label.config(text="播放中...\n(按 ESC 中止)")
            root.after(50, start_playback)

    def start_playback():
        def worker():
            if force_chinese_start:
                set_ime_status(True)
            mouse_ctl = mouse.Controller()
            kb_ctl = keyboard.Controller()
            for r in range(repeats):
                if abort_flag.is_set():
                    break
                for a in actions:
                    if abort_flag.is_set():
                        break
                    delay = a.get("delay_ms", 0) / 1000.0 / speed
                    if delay > 0:
                        time.sleep(delay)
                    try:
                        execute_action(a, mouse_ctl, kb_ctl)
                    except Exception:
                        pass
            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    if not actions:
        label.config(text="动作列表为空。")
        root.after(1500, finish)
    else:
        tick(countdown)

    root.mainloop()


def show_no_data_message():
    root = tk.Tk()
    root.title("Macro Player - 未包含数据")
    root.geometry("420x140")
    msg = (
        "这是播放器模板文件，本身不包含任何录制数据。\n\n"
        "请用 Recorder 主程序里的“导出独立EXE”功能\n"
        "生成一个真正带数据的独立播放文件。"
    )
    tk.Label(root, text=msg, justify=tk.LEFT, padx=16, pady=16).pack(expand=True, fill=tk.BOTH)
    tk.Button(root, text="知道了", command=root.destroy).pack(pady=(0, 12))
    root.mainloop()


def main():
    payload = load_payload()
    if payload is None:
        show_no_data_message()
        return
    run_gui(payload)


if __name__ == "__main__":
    main()
