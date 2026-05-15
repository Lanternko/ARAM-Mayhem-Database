"""Synergy-lift ranking: how much MORE a champion wins on your team vs its solo baseline.

For each candidate X and your team {A1, A2, A3, A4}:
  solo_wr_X         = WR over all team-rows containing X       (X's universal baseline)
  expected_with_you = mean over A_i of WR(team contains A_i AND X)
  lift              = expected_with_you - solo_wr_X
  SE                = sqrt(var_expected + var_solo)            (independence approx)
  z                 = lift / SE                                 (>1.96 = 95% significant)

Rank by lift. Positive = team boosts this champ; negative = team drags it.

This surfaces champs whose synergy with your specific team beats their average,
even if they're not universally OP — exactly the "fun pick" filter.
"""
from __future__ import annotations

import math
from pathlib import Path

import click
import numpy as np
import polars as pl


def _load_name_map():
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
@click.option("--patches", multiple=True, default=("16.9", "16.10"))
@click.option("--team", required=True, help="comma-separated champion aliases")
@click.option("--min-total", default=400, type=int, help="min total games for candidate")
@click.option("--min-pair", default=30, type=int, help="min co-occurrence games per anchor")
@click.option("--top", default=20, type=int)
def main(data, patches, team, min_total, min_pair, top):
    name_map = _load_name_map()
    alias_to_id = {v: k for k, v in name_map.items()}

    team_aliases = [s.strip() for s in team.split(",")]
    team_ids = []
    for a in team_aliases:
        if a not in alias_to_id:
            click.echo(f"[err] unknown alias: {a}"); return
        team_ids.append(alias_to_id[a])
    team_set = set(team_ids)
    click.echo(f"[team] {team_aliases}")

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    df = df.filter(pl.col("p").is_in(list(patches)))

    # Mirror to solo
    rows = []
    for b, r, w in zip(df["blue_champions"].to_list(), df["red_champions"].to_list(), df["blue_wins"].to_list()):
        rows.append((frozenset(b), int(w)))
        rows.append((frozenset(r), int(1 - w)))

    # For each candidate champion: solo stats (over ALL team-rows containing X)
    solo_games = {}
    solo_wins  = {}
    # For each (anchor in team) x (candidate X): co-stats
    co = {a: {} for a in team_ids}  # co[a][x] = [n, wins]

    for t, w in rows:
        for c in t:
            solo_games[c] = solo_games.get(c, 0) + 1
            solo_wins[c]  = solo_wins.get(c,  0) + w
        for a in team_ids:
            if a in t:
                for c in t:
                    if c == a or c in team_set: continue
                    bucket = co[a].setdefault(c, [0, 0])
                    bucket[0] += 1; bucket[1] += w

    # Compute lift per candidate
    results = []
    all_candidates = set()
    for a in team_ids: all_candidates.update(co[a].keys())
    all_candidates -= team_set

    for x in all_candidates:
        n_solo = solo_games.get(x, 0)
        if n_solo < min_total: continue
        wr_solo = solo_wins[x] / n_solo
        var_solo = wr_solo * (1 - wr_solo) / n_solo

        # Conditional WR given each anchor
        pair_wrs = []
        pair_ns  = []
        pair_vars = []
        per_anchor = []
        for a in team_ids:
            if x not in co[a]: continue
            n_pair, w_pair = co[a][x]
            if n_pair < min_pair: continue
            wr_pair = w_pair / n_pair
            var_pair = wr_pair * (1 - wr_pair) / n_pair
            pair_wrs.append(wr_pair)
            pair_ns.append(n_pair)
            pair_vars.append(var_pair)
            per_anchor.append((name_map.get(a, a), n_pair, wr_pair))

        if len(pair_wrs) < 2:
            continue

        expected = float(np.mean(pair_wrs))
        # SE of expected (mean of pairwise WRs): assuming independence, var = mean of vars / n_anchors
        var_expected = float(np.mean(pair_vars)) / len(pair_wrs)
        lift = expected - wr_solo
        se_lift = math.sqrt(var_expected + var_solo)
        z = lift / se_lift if se_lift > 0 else 0.0
        # 95% lower confidence bound — combines magnitude AND certainty
        lower_bound = lift - 1.96 * se_lift

        results.append({
            "id": x,
            "name": name_map.get(x, f"id_{x}"),
            "n_solo": n_solo,
            "wr_solo": wr_solo,
            "expected": expected,
            "lift": lift,
            "se": se_lift,
            "z": z,
            "lower_bound": lower_bound,
            "n_anchors": len(pair_wrs),
            "per_anchor": per_anchor,
        })

    # Rank by 95% lower bound (combines magnitude + certainty)
    results.sort(key=lambda r: -r["lower_bound"])

    click.echo(f"\n[data] {df.height} matches  base 0.5 (mirrored)  min_total={min_total}  min_pair={min_pair}")
    click.echo(f"[result] {len(results)} candidates passed filters\n")

    click.echo(f"========== TOP {top}: RANKED BY 95% LOWER BOUND ==========")
    click.echo(f"(lower_bound = lift - 1.96*SE; combines effect size + confidence)")
    click.echo(f"(rank by this if you want 'will I really get the boost')\n")
    click.echo(f"  {'name':<14} {'solo_wr':>8} {'expected':>9} {'lift':>8} {'se':>5} {'low95':>7} {'z':>5} {'anch':>5}")
    for r in results[:top]:
        sig = "*" if abs(r['z']) > 1.96 else " "
        click.echo(f"  {r['name']:<14} {r['wr_solo']*100:>7.2f}% {r['expected']*100:>8.2f}% "
                   f"{r['lift']*100:>+6.2f}pp {r['se']*100:>4.2f}pp {r['lower_bound']*100:>+5.2f}pp "
                   f"{r['z']:>+5.2f}{sig} {r['n_anchors']:>3}/4")

    click.echo(f"\n========== Also: TOP {top} by raw lift (effect size only) ==========")
    click.echo(f"(rank by this if you don't mind some uncertainty for bigger upside)\n")
    by_lift = sorted(results, key=lambda r: -r["lift"])[:top]
    click.echo(f"  {'name':<14} {'lift':>8} {'low95':>7} {'z':>5}")
    for r in by_lift:
        sig = "*" if abs(r['z']) > 1.96 else " "
        click.echo(f"  {r['name']:<14} {r['lift']*100:>+6.2f}pp {r['lower_bound']*100:>+5.2f}pp {r['z']:>+5.2f}{sig}")

    click.echo(f"\n========== BOTTOM 10: BIGGEST NEGATIVE SYNERGY (team drags down) ==========")
    by_neg = sorted(results, key=lambda r: r["lift"])[:10]
    for r in by_neg:
        sig = "*" if abs(r['z']) > 1.96 else " "
        click.echo(f"  {r['name']:<14} {r['wr_solo']*100:>7.2f}% {r['expected']*100:>8.2f}% "
                   f"{r['lift']*100:>+6.2f}pp {r['se']*100:>4.2f}pp {r['z']:>+5.2f}{sig} {r['n_anchors']:>5}/4")

    # Detailed breakdown of top 5
    click.echo(f"\n========== Top 5 by lift: per-anchor breakdown ==========")
    for r in results[:5]:
        click.echo(f"\n  {r['name']}  (solo_wr={r['wr_solo']*100:.1f}%, expected={r['expected']*100:.1f}%, lift={r['lift']*100:+.2f}pp z={r['z']:+.2f})")
        for an, n_pair, wr_pair in r['per_anchor']:
            click.echo(f"    + {an:<12}  n={n_pair:>4}  WR={wr_pair*100:>5.1f}%")

    # Also flag "fun picks": medium solo_wr but big positive lift
    fun = [r for r in results if 0.48 < r['wr_solo'] < 0.54 and r['lift'] > 0.04]
    if fun:
        click.echo(f"\n========== FUN PICKS (solo WR 48-54%, lift > +4pp) ==========")
        click.echo("(普通強度英雄、在你 comp 會超水準發揮)")
        for r in fun[:10]:
            sig = "*" if abs(r['z']) > 1.96 else " "
            click.echo(f"  {r['name']:<14} solo={r['wr_solo']*100:.1f}%  expected={r['expected']*100:.1f}%  lift={r['lift']*100:+.2f}pp z={r['z']:+.2f}{sig}")


if __name__ == "__main__":
    main()
