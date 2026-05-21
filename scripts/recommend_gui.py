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
  python scripts/recommend_gui.py \
      --lr-model models/tier2_mayhem/lr_model.pkl \
      --vocab    models/tier2_mayhem/tier2_checkpoint.pt \
      --pair-stats models/pair_synergy_16_10.json
"""
from __future__ import annotations

import math
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
    ParsedSession, load_composition_lr, load_lr, parse_session, session_state_hash,
    suggest_for_cell,
)
from aram_nn.pair_synergy import PairSynergyStats, load_pair_synergy


# ---------- Polling thread ----------

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
                        q.put(("suggestions", parsed, suggestions))
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

    q.put(("static", {}))  # empty name map - GUI falls back to "#<id>"
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
        q.put(("suggestions", parsed, suggestions))
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
FONT_DECISION = ("Microsoft JhengHei UI", 17, "bold")
FONT_NUM      = ("Consolas", 11)
FONT_NUM_B    = ("Consolas", 11, "bold")
FONT_NUM_BEST = ("Consolas", 14, "bold")

# U+2212 MINUS SIGN - proper typographic minus instead of HYPHEN-MINUS.
# Same width as "+" in Consolas so the columns still align.
MINUS = "−"


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


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache

        root.title("ARAM Recommender")
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)
        root.geometry("760x520+40+40")
        root.configure(bg=BG)
        root.minsize(680, 460)

        # Pixel-perfect column geometry, applied identically to every row
        # frame so cells line up regardless of font.  Tk widget `width=N`
        # is in font-average chars; mixing FONT_SECTION (Segoe UI
        # proportional) with FONT_NUM (Consolas mono) at the same `width=N`
        # produced visibly different pixel widths in earlier versions.
        # Pinning to minsize fixes it regardless of font.
        self.COL_ICON = 42
        self.COL_DELTA = 74
        self.COL_PROB = 64
        self.COL_Z = 58

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).  We use that distinction to set
        # generous outer rhythm without bloating the labels themselves.
        self.header = tk.Label(
            root, text="Loading…",
            bg=BG, fg=FG, font=FONT_HEAD,
            anchor="w", padx=16,
        )
        self.header.pack(fill="x", pady=(14, 0))

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=FONT_SUB,
            anchor="w", padx=16,
        )
        self.subheader.pack(fill="x", pady=(2, 12))

        # Thin divider between header and the dynamic body - replaces what
        # a bottom border on the header would do, without violating the
        # absolute ban on accent borders.
        tk.Frame(root, bg=DIVIDER, height=1).pack(fill="x", padx=16)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(10, 14))

        # Begin draining the queue.
        self.root.after(100, self._drain)

    # ----- Queue handling -----

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
            _, parsed, suggestions = msg
            self._render(parsed, suggestions)

    # ----- Rendering -----

    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self, parsed, suggestions) -> None:
        cur_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        self.header.config(text=f"Cell {parsed.my_cell_id} · {cur_name}", fg=FG)
        self.subheader.config(
            text="MLΔ 是換人後勝率變化；z 是英雄本體強度"
        )

        self._clear_body()

        self._render_decision_band(self.body, parsed, suggestions)

        self.body.grid_columnconfigure(0, weight=2, minsize=260)
        self.body.grid_columnconfigure(1, weight=3, minsize=360)
        self.body.grid_rowconfigure(1, weight=1)

        left = tk.Frame(self.body, bg=BG)
        left.grid(row=1, column=0, sticky="new", padx=(0, 22), pady=(12, 0))
        right = tk.Frame(self.body, bg=BG)
        right.grid(row=1, column=1, sticky="new", pady=(12, 0))

        self._render_team_section(left, parsed, suggestions)
        self._render_bench_table(right, suggestions)

    def _render_decision_band(self, parent, parsed, suggestions) -> None:
        keep = next((s for s in suggestions if s.source == "keep" and s.is_known), None)
        bench = [s for s in suggestions if s.source == "bench" and s.is_known]
        best = bench[0] if bench else None
        should_swap = best is not None and best.delta > 0
        action = best if should_swap else keep

        current_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        action_name = (
            self.id_to_name.get(action.champion_id, f"#{action.champion_id}")
            if action is not None else "未知"
        )
        title = f"建議換成 {action_name}" if should_swap else f"建議保留 {current_name}"

        if best is None:
            detail = "沒有可用替補資料"
            best_delta = float("nan")
        elif should_swap:
            best_name = self.id_to_name.get(best.champion_id, f"#{best.champion_id}")
            detail = f"替補池最高收益：{best_name}"
            best_delta = best.delta
        else:
            detail = f"最佳替補仍是 {_fmt_signed_pct(best.delta * 100)}，留著比較好"
            best_delta = best.delta

        band_bg = BEST_BG if should_swap else SURFACE
        band = tk.Frame(parent, bg=band_bg, highlightthickness=1, highlightbackground=DIVIDER)
        band.grid(row=0, column=0, columnspan=2, sticky="ew")
        band.grid_columnconfigure(1, weight=1)

        if action is not None:
            self._icon_cell(band, action.champion_id, bg=band_bg)

        text_box = tk.Frame(band, bg=band_bg)
        text_box.grid(row=0, column=1, sticky="ew", padx=(4, 8), pady=10)
        tk.Label(
            text_box, text=title, bg=band_bg, fg=GREEN if should_swap else GOLD,
            font=FONT_DECISION, anchor="w",
        ).pack(fill="x")
        tk.Label(
            text_box, text=detail, bg=band_bg, fg=DIM,
            font=FONT_SUB, anchor="w",
        ).pack(fill="x", pady=(2, 0))

        metrics = tk.Frame(band, bg=band_bg)
        metrics.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=8)
        self._metric(metrics, 0, "替補最高", _fmt_signed_pct(best_delta * 100), self._delta_color(best_delta))
        self._metric(metrics, 1, "預估勝率", _fmt_prob(action.win_prob) if action else "n/a", FG)
        self._metric(
            metrics, 2, "本體 z",
            _fmt_signed_z(action.z_score) if action else "n/a",
            self._z_color(action.z_score) if action else DIM,
        )

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

    def _render_team_section(self, parent, parsed, suggestions) -> None:
        """Show all 5 blue-team champions in the left column.

        Teammates are dimmed (you can't swap them, they're context).  Your
        own row gets a gold name + ⊙ marker and the z-score inline so the
        user always knows their current meta strength as an anchor for
        comparing the bench candidates on the right.
        """
        keep = next((s for s in suggestions if s.source == "keep" and s.is_known), None)
        own_z = keep.z_score if keep is not None else None

        tk.Label(
            parent, text="目前隊伍",
            bg=BG, fg=DIM, anchor="w", font=FONT_SECTION,
        ).pack(fill="x", pady=(0, 8))

        if keep is not None:
            summary = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=DIVIDER)
            summary.pack(fill="x", pady=(0, 8), ipady=5)
            tk.Label(
                summary, text=f"目前預估 {_fmt_prob(keep.win_prob)}",
                bg=SURFACE, fg=FG, font=FONT_NAME_B, anchor="w", padx=8,
            ).pack(side="left")
            tk.Label(
                summary, text=f"本體 z {_fmt_signed_z(keep.z_score)}",
                bg=SURFACE, fg=self._z_color(keep.z_score), font=FONT_NUM_B, anchor="e", padx=8,
            ).pack(side="right")

        for cid in parsed.my_team_ids:
            is_me = (cid == parsed.my_current_id)
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=2)
            self._configure_team_row(row)

            self._icon_cell(row, cid, bg=BG)

            name = self.id_to_name.get(cid, f"#{cid}")
            if is_me:
                z_str = f"   {_fmt_signed_z(own_z)}" if own_z is not None else ""
                tk.Label(
                    row, text=f"你 · {name}{z_str}",
                    bg=BG, fg=GOLD, font=FONT_NAME_B, anchor="w",
                ).grid(row=0, column=1, sticky="w")
            else:
                tk.Label(
                    row, text=name, bg=BG, fg=DIM,
                    font=FONT_NAME, anchor="w",
                ).grid(row=0, column=1, sticky="w")

    def _render_bench_table(self, parent, suggestions) -> None:
        bench = [s for s in suggestions if s.source == "bench"]

        tk.Label(
            parent, text=f"替補池 · {len(bench)} 個選項",
            bg=BG, fg=DIM, anchor="w", font=FONT_SECTION,
        ).pack(fill="x", pady=(0, 6))

        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(0, 4))
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
            row.pack(fill="x", pady=1, ipady=4)
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
    def _metric(parent: tk.Frame, col: int, label: str, value: str, fg: str) -> None:
        box = tk.Frame(parent, bg=parent["bg"])
        box.grid(row=0, column=col, padx=(10 if col else 0, 0), sticky="e")
        tk.Label(
            box, text=label, bg=parent["bg"], fg=DIM,
            font=FONT_SECTION, anchor="e",
        ).pack(fill="x")
        tk.Label(
            box, text=value, bg=parent["bg"], fg=fg,
            font=FONT_NUM_B, anchor="e",
        ).pack(fill="x")

    @staticmethod
    def _cell(
        parent: tk.Frame, col: int, text: str, fg: str,
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
            font=font, anchor="w",
        ).grid(row=0, column=col, sticky="w")


# ---------- Entry point ----------

@click.command()
@click.option("--lr-model", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LR pickle, loaded without sklearn) or lr_weights.json.")
@click.option("--vocab", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json - used for champion vocab.")
@click.option("--pair-stats", default=Path("models/pair_synergy_16_10.json"),
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to pair synergy JSON from scripts/build_pair_stats.py.")
@click.option("--composition-model", default=Path("models/composition_lr_16_10_2026_05_19_live/model.pkl"),
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
    lr_model: Path,
    vocab: Path,
    pair_stats: Path,
    composition_model: Path,
    poll_interval: float,
    fake: bool,
    verbose: bool,
) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
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
    icon_cache = IconCache(Path("data/icons"), lcu_creds=creds_for_icons)
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
            root.configure(bg=BG)
            tk.Label(
                root, text="League client not running",
                bg=BG, fg=RED, font=FONT_HEAD, padx=24, pady=(20, 4), anchor="w",
            ).pack(fill="x")
            tk.Label(
                root, text="No LCU credentials found.\n\nTip: pass --fake to demo the GUI without League.",
                bg=BG, fg=DIM, font=FONT_NAME, padx=24, pady=(0, 24),
                anchor="w", justify="left",
            ).pack(fill="x")
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
