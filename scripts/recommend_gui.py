"""Tk GUI for the ARAM champ-select recommender.

A standalone always-on-top window that shows bench-swap suggestions with
live updates as the LCU champ-select state changes.  Architecturally the
same as `lcu_collector.py recommend` but renders into a Tk window instead
of clearing the terminal.

Threading:
  - main thread: Tk event loop, owns all widgets.
  - poll thread: runs the LCU polling loop, never touches Tk; pushes
    updates onto a queue.Queue that the main thread drains via root.after.

Tkinter is not thread-safe - keep this separation strict.

Usage:
  python scripts/recommend_gui.py
"""
from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path

import click

from aram_nn.icons import IconCache
from aram_nn.lcu.client import (
    LCUClient, get_champion_summary, get_champ_select_session, get_current_summoner,
    get_gameflow_phase,
)
from aram_nn.lcu.process import get_credentials
from aram_nn.recommend import (
    ParsedSession, best_available_team_combos, describe_team_combo, load_composition_lr, load_lr,
    parse_session, session_state_hash, suggest_for_cell,
)
from aram_nn.pair_synergy import PairSynergyStats, load_pair_synergy


APP_NAME = "ARAMRecommender"
DEFAULT_LR_MODEL = Path("models/tier2_mayhem/lr_weights.json")
DEFAULT_VOCAB = Path("models/tier2_mayhem/tier2_checkpoint.champ_to_idx.json")
DEFAULT_PAIR_STATS = Path("models/pair_synergy_16_10.json")
DEFAULT_COMPOSITION_MODEL = Path("models/composition_lr_16_10_2026_05_21_dual_roles/model.pkl")
DEFAULT_CHAMPION_NAMES = Path("data/cache/champion_abilities.json")
DEFAULT_APP_ICON = Path("docs/recommender-app-icon.ico")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resource_path(relative: Path | str) -> Path:
    rel = Path(relative)
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / rel

    cwd_candidate = Path.cwd() / rel
    if cwd_candidate.exists():
        return cwd_candidate
    return _project_root() / rel


def _resolve_resource(path: Path | None, default_relative: Path) -> Path:
    if path is None:
        return _resource_path(default_relative)

    candidate = Path(path)
    if candidate.exists() or candidate.is_absolute():
        return candidate

    bundled_candidate = _resource_path(candidate)
    return bundled_candidate if bundled_candidate.exists() else candidate


def _icon_cache_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "icons"
    return _resource_path("data/icons")


def _set_window_icon(root: tk.Tk) -> None:
    icon_path = _resource_path(DEFAULT_APP_ICON)
    if not icon_path.exists():
        return
    try:
        root.iconbitmap(default=str(icon_path))
    except Exception:
        pass


# ---------- Polling thread ----------

def _enable_windows_dpi_awareness() -> None:
    """Avoid Windows bitmap-scaling Tk, which makes text and icons blurry."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Per-monitor aware when available; fall back for older Windows.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_fallback_champion_names() -> dict[int, str]:
    """Offline championId -> English alias map for --fake or LCU static misses."""
    path = _resource_path(DEFAULT_CHAMPION_NAMES)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    out: dict[int, str] = {}
    for row in data.get("champions", []):
        try:
            cid = int(row.get("champion_id") or 0)
        except (TypeError, ValueError):
            continue
        name = row.get("alias") or row.get("name_en")
        if cid > 0 and isinstance(name, str) and name:
            out[cid] = name
    return out

def poll_loop(
    stop_event: threading.Event, q: queue.Queue, model, pair_stats, composition_model, creds,
    poll_interval: float, verbose: bool = False,
) -> None:
    """Run in background thread.  Pushes messages onto `q`:
      ("static", id_to_name)         - once, after LCU static data loads
      ("idle", phase)                - when not in (or about to leave) champ select
      ("suggestions", parsed, sugs)  - when champ select state changes
      ("error", message)             - on unrecoverable failure

    When verbose, also prints a status line to stdout on every poll so the
    user can see what the LCU is returning (phase + session presence)
    while watching the terminal during a real game.
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    current_creds = creds

    while not stop_event.is_set():
        if current_creds is None:
            current_creds = get_credentials()
            if current_creds is None:
                q.put(("status", "Waiting for League client", "No LCU credentials found."))
                log("[poll] waiting for LCU credentials")
                stop_event.wait(max(poll_interval, 2.0))
                continue

        try:
            with LCUClient(current_creds) as lcu:
                if get_current_summoner(lcu) is None:
                    q.put(("status", "LCU not ready", "Refreshing League client credentials..."))
                    log("[poll] LCU health check failed; refreshing credentials")
                    current_creds = None
                    stop_event.wait(max(poll_interval, 1.0))
                    continue
                id_to_name: dict[int, str] = {}
                for entry in get_champion_summary(lcu):
                    cid = entry.get("id")
                    name = entry.get("name") or entry.get("alias")
                    if isinstance(cid, int) and isinstance(name, str) and cid > 0:
                        id_to_name[cid] = name
                q.put(("static", id_to_name))
                log(f"[poll] loaded {len(id_to_name)} champion names from LCU")

                last_hash: tuple | None = None
                last_phase: str | None = None

                while not stop_event.is_set():
                    session = get_champ_select_session(lcu)
                    parsed = parse_session(session) if session else None

                    if parsed is None:
                        phase = get_gameflow_phase(lcu)
                        if phase == "None" and get_current_summoner(lcu) is None:
                            q.put(("status", "LCU reconnecting", "League client credentials changed."))
                            log("[poll] LCU became unreachable; reconnecting")
                            current_creds = None
                            break
                        phase_label = f"{phase} (session incomplete)" if session else phase
                        if verbose or phase != last_phase:
                            log(f"[poll] idle  phase={phase_label}  "
                                f"session={'yes(incomplete)' if session else 'no'}")
                        if phase != last_phase:
                            q.put(("idle", phase_label))
                            last_phase = phase
                            last_hash = None
                        stop_event.wait(max(poll_interval, 2.0))
                        continue
                    last_phase = "ChampSelect"

                    state = session_state_hash(parsed)
                    if state != last_hash:
                        suggestions = suggest_for_cell(
                            parsed.my_team_ids,
                            parsed.my_current_id,
                            parsed.bench_ids,
                            model,
                            pair_stats,
                            composition_model,
                        )
                        combos = best_available_team_combos(
                            parsed.my_team_ids,
                            parsed.bench_ids,
                            model,
                            composition_model,
                        )
                        current_combo = describe_team_combo(
                            parsed.my_team_ids,
                            model,
                            composition_model,
                        )
                        q.put(("suggestions", parsed, suggestions, combos, current_combo))
                        last_hash = state
                        log(f"[poll] champ-select update  cell={parsed.my_cell_id}  "
                            f"current={parsed.my_current_id}  bench={len(parsed.bench_ids)}")

                    stop_event.wait(poll_interval)
        except Exception as exc:  # pragma: no cover - surfaced to GUI as status
            q.put(("status", "LCU reconnecting", repr(exc)))
            log(f"[poll] reconnect after error: {exc!r}")
            current_creds = None
            stop_event.wait(max(poll_interval, 2.0))


def fake_poll_loop(
    stop_event: threading.Event,
    q: queue.Queue,
    model,
    pair_stats: PairSynergyStats | None,
    composition_model,
    interval: float = 3.0,
) -> None:
    """Synthetic poll loop for --fake mode.

    Emits randomly-generated champ-select states every `interval` seconds so
    the GUI can be validated without an LCU connection.  Predictions use the
    real LR model on the random teams, so delta magnitudes match what real
    play would produce - only the champion picks are synthetic.

    Bench size is randomized between 5 and 10 each tick to exercise the
    GUI's vertical scrolling and to match the bench sizes a real ARAM
    queue produces once teammates start rerolling.
    """
    import random

    q.put(("static", load_fallback_champion_names()))
    all_ids = sorted(model.champ_to_idx.keys())
    cell_id = 2

    while not stop_event.is_set():
        bench_size = random.randint(5, 10)
        sample = random.sample(all_ids, 5 + bench_size)
        my_team = sample[:5]
        bench = sample[5:]
        my_current = my_team[cell_id]

        parsed = ParsedSession(
            my_team_ids=my_team,
            my_current_id=my_current,
            my_cell_id=cell_id,
            bench_ids=bench,
            bench_enabled=True,
        )
        suggestions = suggest_for_cell(my_team, my_current, bench, model, pair_stats, composition_model)
        combos = best_available_team_combos(my_team, bench, model, composition_model)
        current_combo = describe_team_combo(my_team, model, composition_model)
        q.put(("suggestions", parsed, suggestions, combos, current_combo))
        stop_event.wait(interval)


# ---------- GUI ----------

# Palette - aligned with the public site: slate neutrals plus Mayhem-like
# tier accents.  The GUI runs over a visually busy League client, so clarity
# wins over transparency.
BG        = "#0e1116"
SURFACE   = "#161a22"
ROW       = "#11151d"
BEST_BG   = "#202414"
WARN_BG   = "#241817"
FG        = "#e6e8eb"
DIM       = "#9aa0a6"
MUTED     = "#69707a"
DIVIDER   = "#30363d"
GOLD      = "#f5c518"
GREEN     = "#8ec441"
RED       = "#ff6a4a"
BLUE      = "#3aa0ff"

# Fonts - Segoe UI for prose (Windows default sans, ships with the OS and
# pairs well next to League's own Latin UI), Consolas for tabular numbers
# so Δ% and z columns stay aligned across rows.
FONT_HEAD    = ("Microsoft JhengHei UI", 15, "bold")
FONT_SUB     = ("Microsoft JhengHei UI", 9)
FONT_SECTION = ("Microsoft JhengHei UI", 8, "bold")
FONT_NAME    = ("Microsoft JhengHei UI", 11)
FONT_NAME_B  = ("Microsoft JhengHei UI", 11, "bold")
FONT_SCORE    = ("Microsoft JhengHei UI", 18, "bold")
FONT_ICON     = ("Segoe UI Symbol", 12, "bold")
FONT_NUM      = ("Consolas", 11)
FONT_NUM_B    = ("Consolas", 11, "bold")
FONT_NUM_BEST = ("Consolas", 14, "bold")

# U+2212 MINUS SIGN - proper typographic minus instead of HYPHEN-MINUS.
# Same width as "+" in Consolas so the columns still align.
MINUS = "−"
COPY_ICON = "⧉"
COPIED_ICON = "✓"


def _fmt_signed_pct(value_pp: float) -> str:
    """Format a percentage-point delta with a typographic minus for negatives."""
    if math.isnan(value_pp):
        return "n/a"
    if value_pp > 0:
        return f"+{value_pp:.1f}%"
    if value_pp < 0:
        return f"{MINUS}{abs(value_pp):.1f}%"
    return f" {value_pp:.1f}%"


def _fmt_signed_z(z: float) -> str:
    """Format a z-score with a typographic minus for negatives."""
    if math.isnan(z):
        return "n/a"
    if z > 0:
        return f"+{z:.2f}"
    if z < 0:
        return f"{MINUS}{abs(z):.2f}"
    return f" {z:.2f}"


def _fmt_prob(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_rating(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    value = max(0.0, min(5.0, float(value)))
    bands = (
        (4.75, "S+"),
        (4.40, "S"),
        (4.05, "S-"),
        (3.75, "A+"),
        (3.35, "A"),
        (3.05, "A-"),
        (2.75, "B+"),
        (2.35, "B"),
        (2.05, "B-"),
        (1.75, "C+"),
        (1.35, "C"),
        (1.05, "C-"),
        (0.75, "D+"),
        (0.35, "D"),
    )
    for cutoff, tier in bands:
        if value >= cutoff:
            return tier
    return "D-"


def _rating_value_text(rating) -> str:
    if rating.label == "AD佔比" and rating.detail:
        return f"{rating.detail} {_fmt_rating(rating.value)}"
    return _fmt_rating(rating.value)


def _rating_copy_text(rating) -> str:
    if rating.label == "AD佔比" and rating.detail:
        return f"AD佔比：{rating.detail} {_fmt_rating(rating.value)}"
    detail = f"（{rating.detail}）" if rating.detail else ""
    return f"{rating.label}：{_fmt_rating(rating.value)}{detail}"


def _strip_rating_prefix(label: str) -> str:
    for prefix in ("高風險：", "風險：", "低點："):
        if label.startswith(prefix):
            return label.removeprefix(prefix)
    return label


def _is_risk_rating(rating) -> bool:
    return rating.label.startswith(("高風險：", "風險：", "低點："))


def _compact_rating_name(rating, combo=None, is_risk: bool = False) -> str:
    label = _strip_rating_prefix(rating.label)
    if label == "AD佔比":
        detail = rating.detail or ""
        share = getattr(combo, "ad_share", float("nan"))
        if is_risk and not math.isnan(share):
            if share >= 0.62:
                return f"AD過高{detail}"
            if share <= 0.38:
                return f"AD過低{detail}"
        if is_risk:
            return f"AD佔比{detail}"
        return "AD/AP均衡"
    if label == "英雄強度" and is_risk:
        return "英雄本體偏弱"
    return label


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _make_observer_window(root: tk.Tk) -> None:
    """Keep the overlay visible without activating the IME/input focus on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = int(root.winfo_id())
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW
        set_style = user32.SetWindowLongPtrW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t

        gwl_exstyle = -20
        ws_ex_noactivate = 0x08000000
        style = get_style(hwnd, gwl_exstyle)
        set_style(hwnd, gwl_exstyle, style | ws_ex_noactivate)

        # Re-show without activation so Chinese IME toolbars do not pop up
        # over League when this passive panel refreshes or starts.
        sw_shownoactivate = 4
        user32.ShowWindow(hwnd, sw_shownoactivate)
    except Exception:
        # The overlay is still usable if the platform call fails.
        return


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache
        self.font_scale = 1.0
        self._last_render_args: tuple | None = None

        root.title("ARAM Recommender")
        _set_window_icon(root)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)
        root.geometry("840x620+40+40")
        root.configure(bg=BG)
        root.minsize(740, 520)

        # Pixel-perfect column geometry, applied identically to every row
        # frame so cells line up regardless of font.  Tk widget `width=N`
        # is in font-average chars; mixing FONT_SECTION (Segoe UI
        # proportional) with FONT_NUM (Consolas mono) at the same `width=N`
        # produced visibly different pixel widths in earlier versions.
        # Pinning to minsize fixes it regardless of font.
        self._base_cols = {
            "icon": 42,
            "delta": 74,
            "prob": 64,
            "z": 58,
        }
        self._apply_zoom_geometry()

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).  We use that distinction to set
        # generous outer rhythm without bloating the labels themselves.
        self.header = tk.Label(
            root, text="Loading…",
            bg=BG, fg=FG, font=self._font(FONT_HEAD),
            anchor="w", padx=12,
        )
        self.header.pack(fill="x", pady=(8, 0))

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=self._font(FONT_SUB),
            anchor="w", padx=12,
        )
        self.subheader.pack(fill="x", pady=(0, 8))

        # Thin divider between header and the dynamic body - replaces what
        # a bottom border on the header would do, without violating the
        # absolute ban on accent borders.
        tk.Frame(root, bg=DIVIDER, height=1).pack(fill="x", padx=12)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=(8, 10))

        _make_observer_window(root)
        self.root.after_idle(lambda: _make_observer_window(root))
        self._bind_zoom_shortcuts()

        # Begin draining the queue.
        self.root.after(100, self._drain)

    # ----- Queue handling -----

    def _font(self, base: tuple) -> tuple:
        family, size, *style = base
        scaled_size = max(7, int(round(size * self.font_scale)))
        return (family, scaled_size, *style)

    def _apply_zoom_geometry(self) -> None:
        self.COL_ICON = self._base_cols["icon"]
        self.COL_DELTA = int(round(self._base_cols["delta"] * self.font_scale))
        self.COL_PROB = int(round(self._base_cols["prob"] * self.font_scale))
        self.COL_Z = int(round(self._base_cols["z"] * self.font_scale))

    def _bind_zoom_shortcuts(self) -> None:
        for seq in ("<Control-plus>", "<Control-KP_Add>", "<Control-equal>", "<Control-Shift-equal>"):
            self.root.bind_all(seq, lambda _event, delta=0.1: self._adjust_font_scale(delta))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.root.bind_all(seq, lambda _event, delta=-0.1: self._adjust_font_scale(delta))
        for seq in ("<Control-0>", "<Control-KP_0>"):
            self.root.bind_all(seq, lambda _event: self._reset_font_scale())

    def _adjust_font_scale(self, delta: float) -> str:
        self.font_scale = max(0.8, min(1.6, round(self.font_scale + delta, 2)))
        self._apply_zoom()
        return "break"

    def _reset_font_scale(self) -> str:
        self.font_scale = 1.0
        self._apply_zoom()
        return "break"

    def _apply_zoom(self) -> None:
        self._apply_zoom_geometry()
        self.header.config(font=self._font(FONT_HEAD))
        self.subheader.config(font=self._font(FONT_SUB))
        if self._last_render_args is not None:
            self._render(*self._last_render_args)

    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "static":
            self.id_to_name = msg[1]
            self.header.config(text="Waiting for champ select", fg=FG)
            self.subheader.config(text=f"{len(self.id_to_name)} champions loaded")
            self._clear_body()
        elif kind == "idle":
            phase = msg[1]
            self.header.config(text=f"Idle · {phase}", fg=DIM)
            self.subheader.config(text="Queue for ARAM/Mayhem to see swap suggestions.")
            self._clear_body()
        elif kind == "status":
            _, title, detail = msg
            self.header.config(text=title, fg=DIM)
            self.subheader.config(text=detail)
            self._clear_body()
        elif kind == "error":
            self.header.config(text="LCU error", fg=RED)
            self.subheader.config(text=msg[1])
            self._clear_body()
        elif kind == "suggestions":
            _, parsed, suggestions, *rest = msg
            combos = rest[0] if rest else []
            current_combo = rest[1] if len(rest) > 1 else None
            self._render(parsed, suggestions, combos, current_combo)

    # ----- Rendering -----

    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self, parsed, suggestions, combos=None, current_combo=None) -> None:
        combos = combos or []
        self._last_render_args = (parsed, suggestions, combos, current_combo)
        cur_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        self.header.config(text=f"Cell {parsed.my_cell_id} · {cur_name}", fg=FG)
        self.subheader.config(
            text="MLΔ 是換人後勝率變化；z 是英雄本體強度；隊伍評分 B 約等於普通"
        )

        self._clear_body()

        self.body.grid_columnconfigure(0, weight=2, minsize=260)
        self.body.grid_columnconfigure(1, weight=3, minsize=360)
        self.body.grid_rowconfigure(0, weight=0)
        self.body.grid_rowconfigure(1, weight=1)
        self.body.grid_rowconfigure(2, weight=0)

        combo_host = tk.Frame(self.body, bg=BG)
        combo_host.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 0))
        self._render_combo_section(combo_host, combos, current_combo, wraplength=560)

        left = tk.Frame(self.body, bg=BG)
        left.grid(row=1, column=0, sticky="new", padx=(0, 18), pady=(8, 0))
        right = tk.Frame(self.body, bg=BG)
        right.grid(row=1, column=1, sticky="new", pady=(8, 0))

        self._render_team_section(left, parsed, suggestions, current_combo)
        self._render_bench_table(right, suggestions)

    def _configure_team_row(self, row: tk.Frame) -> None:
        """Team rows only need icon + name (no Δ / no z column for teammates)."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, weight=1)

    def _configure_bench_row(self, row: tk.Frame) -> None:
        """Bench rows: icon + delta + prob + z + name."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, minsize=self.COL_DELTA)
        row.grid_columnconfigure(2, minsize=self.COL_PROB)
        row.grid_columnconfigure(3, minsize=self.COL_Z)
        row.grid_columnconfigure(4, weight=1)

    def _render_team_section(self, parent, parsed, suggestions, current_combo=None) -> None:
        """Show all 5 blue-team champions in the left column.

        Teammates are dimmed (you can't swap them, they're context).  Your
        own row gets a gold name + ⊙ marker and the z-score inline so the
        user always knows their current meta strength as an anchor for
        comparing the bench candidates on the right.
        """
        keep = next((s for s in suggestions if s.source == "keep" and s.is_known), None)
        own_z = keep.z_score if keep is not None else None
        current_prob = (
            current_combo.win_prob
            if current_combo is not None and not math.isnan(current_combo.win_prob)
            else keep.win_prob if keep is not None else float("nan")
        )

        current = tk.Frame(parent, bg=BG)
        current.pack(fill="x", pady=(0, 6))
        tk.Label(
            current, text="當前勝率",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            current, text=_fmt_prob(current_prob),
            bg=BG, fg=FG, anchor="w", font=self._font(FONT_SCORE),
        ).pack(side="left")

        if current_combo is not None and getattr(current_combo, "ratings", None):
            self._render_team_ratings(parent, current_combo.ratings, getattr(current_combo, "ad_share", float("nan")))

        for cid in parsed.my_team_ids:
            is_me = (cid == parsed.my_current_id)
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=1)
            self._configure_team_row(row)

            self._icon_cell(row, cid, bg=BG)

            name = self.id_to_name.get(cid, f"#{cid}")
            if is_me:
                z_str = f"   {_fmt_signed_z(own_z)}" if own_z is not None else ""
                tk.Label(
                    row, text=f"你 · {name}{z_str}",
                    bg=BG, fg=GOLD, font=self._font(FONT_NAME_B), anchor="w",
                ).grid(row=0, column=1, sticky="w")
            else:
                tk.Label(
                    row, text=name, bg=BG, fg=DIM,
                    font=self._font(FONT_NAME), anchor="w",
                ).grid(row=0, column=1, sticky="w")

    def _render_bench_table(self, parent, suggestions) -> None:
        bench = [s for s in suggestions if s.source == "bench"]

        tk.Label(
            parent, text="替補池",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(fill="x", pady=(0, 4))

        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(0, 2))
        self._configure_bench_row(header)
        self._cell(header, 1, "MLΔ", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 2, "換後", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 3, "z", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 4, "候選", DIM, bg=BG, font=FONT_SECTION)

        best_idx = next((i for i, s in enumerate(bench) if s.is_known), None)

        for i, s in enumerate(bench):
            is_best = i == best_idx
            if is_best and s.is_known:
                row_bg = BEST_BG if s.delta > 0 else WARN_BG
            else:
                row_bg = ROW

            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=2)
            self._configure_bench_row(row)
            self._icon_cell(row, s.champion_id, bg=row_bg)

            if not s.is_known:
                self._cell(row, 1, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 2, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 3, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 4, f"{name}   (not in vocab)", MUTED, bg=row_bg, font=FONT_NAME)
                continue

            delta_pp = s.delta * 100
            delta_font = FONT_NUM_BEST if is_best else FONT_NUM
            name_color = (GREEN if s.delta > 0 else RED) if is_best else FG
            marker = "首選 · " if is_best else ""

            self._cell(row, 1, _fmt_signed_pct(delta_pp), self._delta_color(s.delta), bg=row_bg, font=delta_font)
            self._cell(row, 2, _fmt_prob(s.win_prob), FG, bg=row_bg, font=FONT_NUM)
            self._cell(row, 3, _fmt_signed_z(s.z_score), self._z_color(s.z_score), bg=row_bg, font=FONT_NUM)
            self._cell(row, 4, f"{marker}{name}", name_color, bg=row_bg, font=FONT_NAME_B if is_best else FONT_NAME)

    def _render_combo_section(self, parent, combos, current_combo=None, wraplength: int = 260) -> None:
        if not combos:
            return

        total = combos[0].total_combos
        copy_bar = tk.Frame(parent, bg=BG)
        copy_bar.pack(fill="x", pady=(0, 4))
        tk.Label(
            copy_bar, text=f"可用池最佳 5 人 · 掃描 {total} 組",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left")
        copy_btn = tk.Button(
            copy_bar,
            text=COPY_ICON,
            command=lambda c=combos[0], current=current_combo: self._copy_combo(current, c, copy_btn),
            bg=SURFACE,
            fg=FG,
            activebackground=BEST_BG,
            activeforeground=FG,
            relief="flat",
            bd=0,
            width=2,
            padx=5,
            pady=1,
            font=self._font(FONT_ICON),
            cursor="hand2",
            takefocus=0,
        )
        copy_btn.pack(side="right")

        for combo in combos:
            row_bg = SURFACE if combo.rank == 1 else ROW
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=3)
            row.grid_columnconfigure(0, minsize=38)
            row.grid_columnconfigure(1, weight=1)
            row.grid_columnconfigure(2, minsize=58)
            row.grid_columnconfigure(3, minsize=64)

            tk.Label(
                row, text=f"#{combo.rank}", bg=row_bg,
                fg=GOLD if combo.rank == 1 else DIM,
                font=self._font(FONT_NUM_B if combo.rank == 1 else FONT_NUM),
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=(8, 4))

            names = [self.id_to_name.get(cid, f"#{cid}") for cid in combo.champion_ids]
            if combo.rank == 1:
                self._combo_icon_strip(row, combo.champion_ids, row_bg)
            else:
                tk.Label(
                    row, text=" / ".join(names), bg=row_bg, fg=FG,
                    font=self._font(FONT_NAME), anchor="w", wraplength=wraplength, justify="left",
                ).grid(row=0, column=1, sticky="ew", padx=(0, 8))

            tk.Label(
                row, text=_fmt_prob(combo.win_prob), bg=row_bg, fg=FG,
                font=self._font(FONT_NUM), anchor="e",
            ).grid(row=0, column=2, sticky="e", padx=(0, 8))

            tk.Label(
                row, text=_fmt_signed_pct(combo.delta * 100), bg=row_bg,
                fg=self._delta_color(combo.delta), font=self._font(FONT_NUM),
                anchor="e",
            ).grid(row=0, column=3, sticky="e", padx=(0, 8))

    def _combo_icon_strip(self, parent: tk.Frame, champion_ids, bg: str) -> None:
        strip = tk.Frame(parent, bg=bg)
        strip.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=1)
        for cid in champion_ids:
            self._packed_icon(strip, int(cid), bg=bg)

    def _render_team_ratings(self, parent, ratings, ad_share: float = float("nan")) -> None:
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(5, 3))
        tk.Label(
            header, text="隊伍評分",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left")

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x", pady=(0, 4))
        column_count = min(max(len(ratings), 1), 5)
        for col in range(column_count):
            grid.grid_columnconfigure(col, weight=1, uniform="team_rating")

        for i, rating in enumerate(ratings[:5]):
            row = i // column_count
            col = i % column_count
            chip = tk.Frame(grid, bg=ROW, highlightthickness=1, highlightbackground=DIVIDER)
            chip.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0), pady=(0, 3))
            label_color = RED if rating.label.startswith(("高風險", "風險")) else DIM

            tk.Label(
                chip, text=rating.label, bg=ROW, fg=label_color,
                font=self._font(FONT_SECTION), anchor="w",
            ).pack(side="left", padx=(5, 3), pady=2)
            tk.Label(
                chip, text=_rating_value_text(rating), bg=ROW,
                fg=self._rating_color(rating.value), font=self._font(FONT_NUM_B),
                anchor="w",
            ).pack(side="left", pady=2)
            if rating.detail and rating.label != "AD佔比":
                tk.Label(
                    chip, text=rating.detail, bg=ROW, fg=MUTED,
                    font=self._font(FONT_SECTION), anchor="e",
                ).pack(side="right", padx=(3, 5), pady=2)

    def _team_profile_clipboard_text(self, combo) -> str:
        if combo is None:
            return "當前勝率：n/a\n優勢：n/a | 風險：n/a"
        win_rate = f"當前勝率：{_fmt_prob(combo.win_prob)}"
        ratings = getattr(combo, "ratings", None)
        if not ratings:
            return win_rate

        advantages = _dedupe_keep_order([
            _compact_rating_name(rating, combo)
            for rating in ratings[:5]
            if (
                not _is_risk_rating(rating)
                and rating.label not in {"英雄強度", "AD佔比"}
            )
        ])[:2]

        risks: list[str] = []
        for rating in ratings[:5]:
            if _is_risk_rating(rating):
                risks.append(_compact_rating_name(rating, combo, is_risk=True))
            elif rating.label == "AD佔比" and not math.isnan(rating.value) and rating.value < 2.05:
                risks.append(_compact_rating_name(rating, combo, is_risk=True))
            elif rating.label == "英雄強度" and not math.isnan(rating.value) and rating.value < 2.05:
                risks.append(_compact_rating_name(rating, combo, is_risk=True))

        advantages_text = "、".join(advantages) if advantages else "n/a"
        risks_text = "、".join(_dedupe_keep_order(risks)[:2]) if risks else "無明顯硬傷"
        return f"{win_rate}\n優勢：{advantages_text} | 風險：{risks_text}"

    def _combo_clipboard_text(self, current_combo, recommended_combo) -> str:
        names = [self.id_to_name.get(cid, f"#{cid}") for cid in recommended_combo.champion_ids]
        delta = ""
        if not math.isnan(recommended_combo.delta):
            delta = f"，{_fmt_signed_pct(recommended_combo.delta * 100)}"
        return (
            f"{self._team_profile_clipboard_text(current_combo)}"
            f"\n\nAI推薦隊伍：{', '.join(names)}（勝率：{_fmt_prob(recommended_combo.win_prob)}{delta}）"
        )

    def _copy_combo(self, current_combo, recommended_combo, button: tk.Button | None = None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self._combo_clipboard_text(current_combo, recommended_combo))
        self.root.update_idletasks()
        if button is not None:
            button.config(text=COPIED_ICON, fg=GREEN)
            self.root.after(
                1200,
                lambda: button.winfo_exists() and button.config(text=COPY_ICON, fg=FG),
            )

    def _icon_cell(self, parent: tk.Frame, champion_id: int, bg: str = BG) -> None:
        """Place the champion icon in column 0 of `parent`.

        bg matches the parent row's background so the icon's surrounding
        pixels blend on tinted (best-pick) rows.  Falls back to a hollow
        placeholder Label of the same width if the IconCache can't produce
        a PhotoImage, so row alignment stays stable.
        """
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            # Hold the reference on the widget too - Tk doesn't keep it, and
            # the redundancy is cheap and removes a class of GC bugs.
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.grid(row=0, column=0, padx=(0, 6))
        else:
            tk.Label(
                parent, text="", bg=bg, width=4, height=2,
            ).grid(row=0, column=0, padx=(0, 6))

    def _packed_icon(self, parent: tk.Frame, champion_id: int, bg: str = BG) -> None:
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.pack(side="left", padx=(0, 6))
        else:
            tk.Label(parent, text="", bg=bg, width=4, height=2).pack(side="left", padx=(0, 6))

    @staticmethod
    def _delta_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value > 0:
            return GREEN
        if value < 0:
            return RED
        return DIM

    @staticmethod
    def _z_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value > 0.5:
            return GREEN
        if value < -0.5:
            return RED
        return FG

    @staticmethod
    def _rating_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value >= 4.0:
            return GREEN
        if value < 2.0:
            return RED
        if value < 3.0:
            return GOLD
        return FG

    def _cell(
        self, parent: tk.Frame, col: int, text: str, fg: str,
        bg: str = BG, font: tuple = FONT_NUM,
    ) -> None:
        """Place a left-aligned label at `col` in the row's shared grid.

        Width is no longer passed explicitly: column widths come from
        the row's grid_columnconfigure(minsize=...) so every row pins
        to the same x positions regardless of which font the content
        is set in.
        """
        tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=self._font(font), anchor="w",
        ).grid(row=0, column=col, sticky="w")


# ---------- Entry point ----------

@click.command()
@click.option("--lr-model", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LR pickle, loaded without sklearn) or lr_weights.json.")
@click.option("--vocab", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json - used for champion vocab.")
@click.option("--pair-stats", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to pair synergy JSON from scripts/build_pair_stats.py.")
@click.option("--composition-model", default=None,
              type=click.Path(path_type=Path, dir_okay=True),
              help="Path to composition LR model.pkl or its model directory. Used for primary ML swap deltas.")
@click.option("--poll-interval", default=1.0, show_default=True, type=float,
              help="Seconds between LCU polls while in ChampSelect.")
@click.option("--fake", is_flag=True, default=False,
              help="Demo mode: skip LCU, generate random champ-select states every 3s. "
                   "Useful to verify the GUI works without launching League.")
@click.option("--verbose", is_flag=True, default=False,
              help="Print per-poll status (phase + session presence) to stdout. "
                   "Useful for diagnosing why a champ-select isn't being detected.")
def main(
    lr_model: Path | None,
    vocab: Path | None,
    pair_stats: Path | None,
    composition_model: Path | None,
    poll_interval: float,
    fake: bool,
    verbose: bool,
) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    _enable_windows_dpi_awareness()

    lr_model = _resolve_resource(lr_model, DEFAULT_LR_MODEL)
    vocab = _resolve_resource(vocab, DEFAULT_VOCAB)
    pair_stats = _resolve_resource(pair_stats, DEFAULT_PAIR_STATS)
    composition_model = _resolve_resource(composition_model, DEFAULT_COMPOSITION_MODEL)

    print(f"[gui] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    print(f"[gui] vocab covers {model.n_champs} champions")
    comp_model = None
    if composition_model.exists():
        comp_model = load_composition_lr(composition_model)
        print(
            f"[gui] composition LR features={len(comp_model.feature_names)} "
            f"champions={len(comp_model.champ_to_idx)}"
        )
    else:
        print(f"[gui] WARN: composition model not found at {composition_model}; using old blend score")
    pair_model = None
    if pair_stats.exists():
        pair_model = load_pair_synergy(pair_stats)
        print(
            f"[gui] pair synergy rows={len(pair_model.rows):,} "
            f"patch={pair_model.patch_prefix} min_pair={pair_model.min_pair}"
        )
    else:
        print(f"[gui] WARN: pair stats not found at {pair_stats}; old blend fallback will use LR only")

    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    # IconCache works in both modes: prefers LCU (local, fast) when creds
    # are present, otherwise falls back to Riot's Data Dragon CDN.  In
    # --fake without League running, only the CDN path is used; that needs
    # internet but caches to disk so future runs are instant offline.
    creds_for_icons = get_credentials()  # may be None, that's fine
    icon_cache = IconCache(_icon_cache_dir(), lcu_creds=creds_for_icons)
    threading.Thread(target=icon_cache.prefetch_all, daemon=True).start()

    if fake:
        print("[gui] --fake: synthesizing champ-select states every 3s, no LCU needed")
        thread = threading.Thread(
            target=fake_poll_loop, args=(stop_event, q, model, pair_model, comp_model), daemon=True,
        )
    else:
        creds = creds_for_icons  # reuse - same credentials work for both
        if not creds:
            # Show the error in a window - easier to notice than a stderr message
            # that scrolls off when the user double-clicks the script.
            root = tk.Tk()
            root.title("ARAM Recommender")
            _set_window_icon(root)
            root.configure(bg=BG)
            title = tk.Label(
                root, text="League client not running",
                bg=BG, fg=RED, font=FONT_HEAD, padx=24, anchor="w",
            )
            title.pack(fill="x", pady=(20, 4))
            body = tk.Label(
                root, text="No LCU credentials found.\n\nTip: pass --fake to demo the GUI without League.",
                bg=BG, fg=DIM, font=FONT_NAME, padx=24,
                anchor="w", justify="left",
            )
            body.pack(fill="x", pady=(0, 24))
            root.mainloop()
            sys.exit(1)
        thread = threading.Thread(
            target=poll_loop,
            args=(stop_event, q, model, pair_model, comp_model, creds, poll_interval, verbose),
            daemon=True,
        )

    thread.start()  # crucial - without this, the poll loop never runs and
                    # the GUI stays on its placeholder "Loading..." header forever.

    root = tk.Tk()
    RecommenderApp(root, q, icon_cache=icon_cache)
    try:
        root.mainloop()
    finally:
        # Signal the poll thread to exit cleanly so the httpx client closes.
        stop_event.set()


if __name__ == "__main__":
    main()
