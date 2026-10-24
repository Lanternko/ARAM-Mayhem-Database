"""Host the arammeta single-champion embed page inside a Tk frame.

pywebview refuses to run off the main thread (Tk owns it), so this drives the
WebView2 WinForms control directly through pythonnet on its own STA thread and
reparents the borderless form into the Tk frame's HWND.  pywebview is only
used as the source of the WebView2 SDK DLLs it ships.

Every public method is called from the Tk thread; events come back through
``on_event`` (called from the .NET thread, so it must only enqueue).
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from aram_nn.quick_picks import SITE_BASE

# ARAM_EMBED_URL points the app at a local build (e.g. a docs/ http.server).
EMBED_URL = os.environ.get("ARAM_EMBED_URL") or SITE_BASE + "embed/champion/"
TABS = ("overview", "compare", "items", "augments")

_GWL_STYLE = -16
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_SW_SHOWNOACTIVATE = 4


def embed_url(champion_id: int, tab: str) -> str:
    return EMBED_URL + "?" + urlencode({"cid": int(champion_id), "tab": tab})


def site_url(alias: str) -> str:
    """The champion's page on the public site, for the browser fallback."""
    slug = "".join(ch for ch in str(alias).lower() if ch.isascii() and ch.isalnum())
    return SITE_BASE + (f"champions/{slug}/" if slug else "champions/")


def show_script(champion_id: int, tab: str) -> str:
    # The page may still be fetching its payload; wait for the hook.
    args = f"{int(champion_id)}, {json.dumps(tab)}"
    return (
        "(function f(n){if(window.aramEmbed){window.aramEmbed.show(" + args + ");}"
        "else if(n<300){setTimeout(function(){f(n+1);},100);}})(0);"
    )


def _load_runtime():
    """Import the WebView2 WinForms types; raises if pythonnet/pywebview/WebView2 are missing."""
    import webview  # noqa: F401  (DLL source only)

    lib = Path(webview.__file__).parent / "lib"
    native = lib / "runtimes" / ("win-arm64" if os.environ.get("PROCESSOR_ARCHITECTURE") == "ARM64" else "win-x64") / "native"
    os.environ["PATH"] = str(native) + os.pathsep + os.environ.get("PATH", "")
    import clr

    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Threading")
    clr.AddReference("System.Drawing")
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.WinForms.dll"))
    return lib


def available() -> bool:
    try:
        _load_runtime()
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment

        return bool(CoreWebView2Environment.GetAvailableBrowserVersionString())
    except Exception:
        return False


class SiteView:
    """One lazily created WebView2 docked in ``parent`` (a Tk frame)."""

    def __init__(self, parent, user_data_dir: Path, on_event: Callable[..., None]) -> None:
        self.parent = parent
        self.user_data_dir = Path(user_data_dir)
        self.on_event = on_event
        self._thread = None
        self._web = None
        self._hwnd = 0
        self._ready = False
        self._want: tuple[int, str] | None = None
        self._shown: tuple[int, str] | None = None
        self._lock = threading.Lock()
        parent.bind("<Configure>", self._on_configure, add="+")

    @property
    def started(self) -> bool:
        return self._thread is not None

    def show(self, champion_id: int, tab: str) -> None:
        tab = tab if tab in TABS else TABS[0]
        want = (int(champion_id), tab)
        with self._lock:
            self._want = want
            if not self.started:
                self._start(want)
                return
            if not self._ready or want == self._shown:
                return
            self._shown = want
        self._invoke(lambda: self._web.ExecuteScriptAsync(show_script(*want)))

    def close(self) -> None:
        web = self._web
        if web is None:
            return

        def stop() -> None:
            from System.Windows.Forms import Application

            web.Dispose()
            Application.ExitThread()

        self._invoke(stop)

    # ---- .NET side -------------------------------------------------------

    def _start(self, want: tuple[int, str]) -> None:
        _load_runtime()
        from System import Action, Uri
        from System.Threading import ApartmentState, Thread, ThreadStart
        from System.Windows.Forms import Application
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

        self._action = Action
        self.parent.update_idletasks()
        parent_hwnd = int(self.parent.winfo_id())
        size = (max(1, self.parent.winfo_width()), max(1, self.parent.winfo_height()))
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        user32 = ctypes.windll.user32

        def ui() -> None:
            # A bare control, not a Form: reparenting a top-level Form made
            # Windows minimize the Tk window when the form got shown.
            web = WebView2()
            props = CoreWebView2CreationProperties()
            props.UserDataFolder = str(self.user_data_dir)
            web.CreationProperties = props
            web.DefaultBackgroundColor = _bg_color()

            def on_init(_sender, args) -> None:
                if not args.IsSuccess:
                    self.on_event("siteview_error", str(args.InitializationException))
                    return
                core = web.CoreWebView2
                core.Settings.AreDevToolsEnabled = False
                core.Settings.IsStatusBarEnabled = False
                core.NavigationStarting += on_navigate
                core.NewWindowRequested += on_new_window
                core.NavigationCompleted += on_loaded

            def on_navigate(_sender, args) -> None:
                if not str(args.Uri).startswith(EMBED_URL):
                    args.Cancel = True
                    webbrowser.open(str(args.Uri))

            def on_new_window(_sender, args) -> None:
                args.Handled = True
                webbrowser.open(str(args.Uri))

            def on_loaded(_sender, args) -> None:
                if not args.IsSuccess:
                    self.on_event("siteview_error", f"navigation failed: {args.WebErrorStatus}")
                    return
                with self._lock:
                    self._ready = True
                    want_now = self._want
                    stale = want_now != self._shown
                    self._shown = want_now
                if stale and want_now:
                    web.ExecuteScriptAsync(show_script(*want_now))

            try:
                hwnd = web.Handle.ToInt64()
                self._hwnd = hwnd
                style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
                user32.SetWindowLongW(hwnd, _GWL_STYLE, (style | _WS_CHILD) & ~_WS_POPUP)
                user32.SetParent(hwnd, parent_hwnd)
                user32.MoveWindow(hwnd, 0, 0, size[0], size[1], True)
                user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
                web.CoreWebView2InitializationCompleted += on_init
                web.Source = Uri(embed_url(*want))
                self._web = web
                Application.Run()
            except Exception as exc:  # pragma: no cover - runtime failure path
                self.on_event("siteview_error", str(exc))

        self._shown = want
        thread = Thread(ThreadStart(ui))
        thread.SetApartmentState(ApartmentState.STA)
        thread.IsBackground = True
        thread.Start()
        self._thread = thread

    def _invoke(self, fn: Callable[[], object]) -> None:
        web = self._web
        if web is None or not web.IsHandleCreated:
            return
        try:
            web.BeginInvoke(self._action(lambda: fn()))
        except Exception:
            pass

    def _on_configure(self, event) -> None:
        if self._hwnd:
            ctypes.windll.user32.MoveWindow(self._hwnd, 0, 0, event.width, event.height, True)


def _bg_color():
    """Match the site's dark background so the first frame does not flash white."""
    from System.Drawing import Color

    return Color.FromArgb(255, 10, 11, 13)  # site.css --bg #0a0b0d
