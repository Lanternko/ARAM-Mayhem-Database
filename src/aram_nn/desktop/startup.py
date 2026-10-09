"""Keep network work off Tk's main thread; show a cancellable startup state."""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .update import UpdateResult, cache_root, read_json, update


def bundled_version(resource_root: Path) -> str:
    return read_json(resource_root / "desktop-data" / "app.json")["version"]


def run_updates(version: str) -> UpdateResult:
    messages: queue.Queue = queue.Queue()
    root = tk.Tk()
    root.title("ARAM Recommender · 更新")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="準備最新推薦資料", font=("Microsoft JhengHei", 14, "bold")).pack(anchor="w")
    status = tk.StringVar(value="檢查版本中…")
    ttk.Label(frame, textvariable=status, width=54, wraplength=410).pack(anchor="w", pady=(12, 8))
    ttk.Label(frame, text="首次下載可能較久；中斷後可在下次啟動續傳。", wraplength=410).pack(anchor="w")
    bar = ttk.Progressbar(frame, mode="indeterminate")
    bar.pack(fill="x", pady=(16, 0))
    bar.start(15)
    root.protocol("WM_DELETE_WINDOW", lambda: root.destroy())
    result = [UpdateResult(message="已取消更新，使用內附資料")]
    done = threading.Event()

    def worker():
        try:
            result[0] = update(cache_root(), version, messages.put)
        except Exception as exc:
            result[0] = UpdateResult(message=f"更新暫時無法完成：{exc}")
        finally:
            done.set()

    def drain():
        while not messages.empty():
            status.set(messages.get_nowait())
        if done.is_set():
            root.destroy()
        else:
            root.after(100, drain)

    threading.Thread(target=worker, daemon=True).start()
    root.after(100, drain)
    root.mainloop()
    # Closing the progress window exits the application; it never starts an
    # inference UI while a daemon could still switch the active data behind it.
    if not done.is_set():
        raise SystemExit(0)
    return result[0]


def launch_updated(executable: Path) -> None:
    import os
    import subprocess
    env = os.environ.copy()
    # PyInstaller onefile children must unpack their own runtime independently.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen([str(executable), *sys.argv[1:]], env=env)
