"""Anchor-conditional synergy: given champion A on the team, how does each
teammate B shift the team's win rate?

For each anchor A:
  baseline_wr = P(win | A on team, marginalised over teammates)
  For each B != A with co-occurrence games >= min_pair_games:
    pair_wr = P(win | A and B on same team)
    delta   = pair_wr - baseline_wr
    ci95    = Wilson 95% interval for delta

Print top/bottom synergies and noise floor.

A pair effect is "real" if its 95% CI doesn't include 0.
"""
from __future__ import annotations

from pathlib import Path

import click
import math
import numpy as np
import polars as pl


def wilson_ci(k: int, n: int, z: float = 1.96):
    """Wilson score interval for proportion. Returns (lo, hi)."""
    if n == 0: return 0.5, 0.5
    p = k / n
    denom = 1 + z*z/n
    centre = (p + z*z/(2*n)) / denom
    spread = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return centre - spread, centre + spread


def _load_champion_name_map():
    try:
        from aram_nn.lcu.client import LCUClient, get_champion_summary
        from aram_nn.lcu.process import get_credentials
        creds = get_credentials()
        if creds is None: return {}
        with LCUClient(creds) as lcu:
            summary = get_champion_summary(lcu)
        m = {}
        for row in summary:
            cid = row.get("id"); name = row.get("alias") or row.get("name")
            if cid is None or not name: continue
            m[int(cid)] = str(name)
        return m
    except Exception:
        return {}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patches", multiple=True, default=("16.9", "16.10"), show_default=True)
@click.option("--anchor", multiple=True, default=("Lux", "Yasuo", "Lillia", "Akali"),
              show_default=True, help="Champion aliases to analyse")
@click.option("--min-pair-games", default=80, show_default=True, type=int)
@click.option("--top", default=10, show_default=True, type=int)
def main(data, patches, anchor, min_pair_games, top):
    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    df = df.filter(pl.col("p").is_in(list(patches)))
    click.echo(f"[data] n_matches={df.height}  patches={list(patches)}")

    name_map = _load_champion_name_map()
    alias_to_id = {v: k for k, v in name_map.items()}
    if not name_map:
        click.echo("[warn] LCU not available — names not resolved")

    # Build team-level solo rows: (team_champs, win)
    blues = df["blue_champions"].to_list()
    reds  = df["red_champions"].to_list()
    wins  = df["blue_wins"].to_list()
    rows = []  # (frozenset(team), win:int)
    for b, r, w in zip(blues, reds, wins):
        rows.append((frozenset(b), int(w)))
        rows.append((frozenset(r), int(1 - w)))
    click.echo(f"[solo] {len(rows)} team-level rows (base rate must be 0.5: {np.mean([w for _, w in rows]):.4f})")

    for anchor_name in anchor:
        anchor_id = alias_to_id.get(anchor_name)
        if anchor_id is None:
            click.echo(f"\n[skip] anchor={anchor_name} not in champion map")
            continue

        # Anchor baseline: win-rate of teams containing anchor
        anchor_rows = [(t, w) for t, w in rows if anchor_id in t]
        if not anchor_rows:
            click.echo(f"\n[skip] anchor={anchor_name} has 0 games"); continue
        n_anchor = len(anchor_rows)
        wins_anchor = sum(w for _, w in anchor_rows)
        baseline_wr = wins_anchor / n_anchor
        ci_lo, ci_hi = wilson_ci(wins_anchor, n_anchor)

        click.echo(f"\n========== ANCHOR: {anchor_name} (id={anchor_id}) ==========")
        click.echo(f"  baseline: {wins_anchor}/{n_anchor} = {baseline_wr*100:.2f}%  Wilson 95% CI [{ci_lo*100:.2f}, {ci_hi*100:.2f}]")

        # For each potential teammate B, count co-occurrence
        co_games = {}; co_wins = {}
        for t, w in anchor_rows:
            for c in t:
                if c == anchor_id: continue
                co_games[c] = co_games.get(c, 0) + 1
                co_wins[c]  = co_wins.get(c, 0) + w

        # Build per-teammate stats
        pair_rows = []
        for c, n_pair in co_games.items():
            if n_pair < min_pair_games: continue
            wins_pair = co_wins[c]
            wr_pair = wins_pair / n_pair
            delta = wr_pair - baseline_wr

            # CI for the DELTA: difference between two proportions
            # Variance(delta) ~= p1(1-p1)/n1 + p0(1-p0)/n0 (independence approximation)
            # Anchor-conditional pair vs anchor-conditional non-pair
            # rest = anchor games without this teammate
            n_rest = n_anchor - n_pair
            wins_rest = wins_anchor - wins_pair
            wr_rest = wins_rest / max(n_rest, 1)
            var_pair = wr_pair * (1 - wr_pair) / max(n_pair, 1)
            var_rest = wr_rest * (1 - wr_rest) / max(n_rest, 1)
            se = math.sqrt(var_pair + var_rest)
            delta_lo = (wr_pair - wr_rest) - 1.96 * se
            delta_hi = (wr_pair - wr_rest) + 1.96 * se
            sig = delta_lo > 0 or delta_hi < 0  # CI excludes 0
            pair_rows.append({
                "champ_id": c,
                "name": name_map.get(c, f"id_{c}"),
                "n_pair": n_pair,
                "wr_pair": wr_pair,
                "n_rest": n_rest,
                "wr_rest": wr_rest,
                "delta_vs_rest": wr_pair - wr_rest,
                "delta_lo": delta_lo, "delta_hi": delta_hi,
                "sig": sig,
            })

        pair_rows.sort(key=lambda r: -r["delta_vs_rest"])
        n_total = len(pair_rows)
        n_sig = sum(1 for r in pair_rows if r["sig"])
        click.echo(f"  {n_total} teammates with >= {min_pair_games} co-games;  {n_sig} have 95% CI excluding 0")

        click.echo(f"\n  --- TOP {top} BEST SYNERGIES (vs anchor's other-teammate average) ---")
        click.echo(f"  {'name':<14} {'n_pair':>6} {'wr_pair':>8} {'wr_rest':>8} {'delta':>8} {'CI95':>22} {'sig':>4}")
        for r in pair_rows[:top]:
            ci = f"[{r['delta_lo']*100:+5.2f}, {r['delta_hi']*100:+5.2f}]"
            click.echo(f"  {r['name']:<14.14} {r['n_pair']:>6} {r['wr_pair']*100:>7.2f}% {r['wr_rest']*100:>7.2f}% {r['delta_vs_rest']*100:>+7.2f}pp {ci:>22} {'*' if r['sig'] else '':>4}")

        click.echo(f"\n  --- BOTTOM {top} WORST SYNERGIES ---")
        for r in pair_rows[-top:]:
            ci = f"[{r['delta_lo']*100:+5.2f}, {r['delta_hi']*100:+5.2f}]"
            click.echo(f"  {r['name']:<14.14} {r['n_pair']:>6} {r['wr_pair']*100:>7.2f}% {r['wr_rest']*100:>7.2f}% {r['delta_vs_rest']*100:>+7.2f}pp {ci:>22} {'*' if r['sig'] else '':>4}")

        # Noise floor diagnostic: median |CI half-width| vs median |delta|
        ci_widths = np.array([(r["delta_hi"] - r["delta_lo"]) / 2 for r in pair_rows])
        deltas    = np.array([abs(r["delta_vs_rest"]) for r in pair_rows])
        click.echo(f"\n  --- NOISE FLOOR ---")
        click.echo(f"  median |delta|       = {np.median(deltas)*100:.2f}pp")
        click.echo(f"  median CI half-width = {np.median(ci_widths)*100:.2f}pp")
        click.echo(f"  ratio (signal/noise) = {np.median(deltas)/np.median(ci_widths):.2f}")
        click.echo(f"  -> if ratio < 1: typical pair effect is smaller than its uncertainty")


if __name__ == "__main__":
    main()
