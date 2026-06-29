"""Build per-champion empirical ability axes: scaling + snowball.

These replace the engage/poke ability BARS on the site (engage/poke are now
redundant with the empirical comp-fit radar's dive/poke axes; they stay as
internal inputs to the archetype math).  Unlike the kit-semantic bars
(damage/tank/cc/sustain), these two are measured from real games:

  scaling  = WR(games >= late_min) - WR(games <= early_max), per champion: a
             base-rate-REMOVED late-game tilt ("does this champ win MORE as the
             game runs long, net of its own overall strength?").  Uses FIXED
             duration cuts (default >=22min vs <=16min), NOT a median split --
             the ARAM/Mayhem median (~17min) lands on many champs' mid-game power
             spike, so a median cut shoves that spike into the "short" bucket and
             mis-reads spike champs (e.g. Kassadin) as anti-scalers.  Games in the
             (early_max, late_min) middle are dropped on purpose.  Absolute
             late-WR was rejected as the metric: it is mostly base rate (corr ~0.5
             with overall WR; Kassadin's late-specific lift is ~0), so it would
             just re-skin the tier/WR shown elsewhere.  (caveat: duration is
             endogenous -- long games skew toward even/comeback games -- a limit
             no public WR-by-duration proxy escapes.)
  snowball = 0.6 * avg(largest_killing_spree) + 0.4 * avg(largest_multi_kill).
             "Snowball / multi-kill carry potential."  Gold income was rejected
             (confounded by game length + redundant with damage); kill streaks
             and multi-kills are the direct signal the user asked for.

Output docs/api/champ-empirical-axes.json carries RAW values; the frontend
percentile-ranks them across champions (compNorm), same as every other bar, so
a champion below --min-games (rare in the pooled window) just reads low.
Sanity (pooled 16.10-12): scaling top = Ornn/Yuumi/Belveth/Smolder (true late
scalers), bottom = early-snowball champs (Yorick/Yasuo/Seraphine); snowball top =
Samira/Pyke/assassins, bottom = enchanters.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import click

SCHEMA_KIND = "champ_empirical_axes"
SNOWBALL_SPREE_W = 0.6
SNOWBALL_MULTI_W = 0.4
# scaling = WR(>=LATE_MIN_SEC) - WR(<=EARLY_MAX_SEC): fixed cuts, NOT a median split
# (see module docstring -- median splitting mis-reads mid-game-spike champs).
LATE_MIN_SEC = 22 * 60
EARLY_MAX_SEC = 16 * 60


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_pooled_16_10_12.parquet"),
              type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--patches", default="", show_default=True,
              help="Comma-separated patch prefixes; empty = every patch in the file.")
@click.option("--min-duration", default=300, show_default=True)
@click.option("--late-min", default=LATE_MIN_SEC, show_default=True,
              help="duration_sec >= this is the LATE bucket (default 22min).")
@click.option("--early-max", default=EARLY_MAX_SEC, show_default=True,
              help="duration_sec <= this is the EARLY bucket (default 16min).")
@click.option("--min-bucket", default=200, show_default=True,
              help="Min games in each of the late/early buckets for a scaling value.")
@click.option("--min-games", default=400, show_default=True,
              help="Min total games for a snowball value.")
@click.option("--out", default=Path("docs/api/champ-empirical-axes.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, patches, min_duration, late_min, early_max, min_bucket, min_games, out):
    import polars as pl

    patch_set = {p.strip() for p in patches.split(",") if p.strip()}
    print("[1/3] loading games ...", flush=True)
    df = pl.read_parquet(data, columns=["patch", "duration_sec", "blue_wins", "participants_json"])
    df = df.filter(pl.col("duration_sec") >= min_duration)
    if patch_set:
        df = df.with_columns(
            pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("pp")
        ).filter(pl.col("pp").is_in(list(patch_set)))
    dur = df["duration_sec"].to_list()
    bw = df["blue_wins"].to_list()
    pjs = df["participants_json"].to_list()
    median_dur = sorted(dur)[len(dur) // 2] if dur else 0

    print(f"[2/3] accumulating per-champ (median {median_dur}s, late>={late_min}s / "
          f"early<={early_max}s, {len(dur)} games) ...", flush=True)
    # scaling: [late_g, late_w, early_g, early_w]; snowball: [g, sum_spree, sum_multi, sum_kills]
    # perf: [g, sum_dmg_pm, sum_mit_pm, sum_cc_pm] -- per-min empirical bars (damage/tank/cc).
    # NOTE no sustain here: LCU stats lack a shield field, so total_heal misses shield-enchanters
    # (Lulu/Karma read near-zero) and over-credits self-heal bruisers; sustain stays the semantic
    # score in the comp vector.  These feed NEW comp keys; the semantic damage/cc/front are
    # untouched so the archetype radar/artifact are unaffected.
    scal = defaultdict(lambda: [0, 0, 0, 0])
    snow = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    perf = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for d, b, pj in zip(dur, bw, pjs):
        parts = json.loads(pj) if isinstance(pj, str) else pj
        late = d >= late_min
        early = d <= early_max
        blue_won = b == 1
        mins = (d / 60.0) if d else 1.0
        for p in parts:
            cid = int(p["championId"])
            won = blue_won == (p.get("teamId") == 100)
            s = scal[cid]
            if late:
                s[0] += 1; s[1] += won
            elif early:
                s[2] += 1; s[3] += won
            st = p.get("stats") or {}
            sn = snow[cid]
            sn[0] += 1
            sn[1] += st.get("largest_killing_spree", 0) or 0
            sn[2] += st.get("largest_multi_kill", 0) or 0
            sn[3] += st.get("kills", 0) or 0
            pf = perf[cid]
            pf[0] += 1
            pf[1] += (st.get("total_damage_dealt_to_champions", 0) or 0) / mins
            pf[2] += (st.get("damage_self_mitigated", 0) or 0) / mins
            pf[3] += (st.get("total_time_cc_dealt", 0) or 0) / mins

    print("[3/3] writing artifact ...", flush=True)
    champs_meta = json.loads(Path("docs/api/tier-list.json").read_text(encoding="utf-8")).get("champs", {})
    names = {int(k): (v.get("alias") or v.get("name") or k) for k, v in champs_meta.items()}
    champs_out = {}
    for cid in set(scal) | set(snow) | set(perf):
        s = scal[cid]
        sn = snow[cid]
        pf = perf[cid]
        scaling = (s[1] / s[0] - s[3] / s[2]) if (s[0] >= min_bucket and s[2] >= min_bucket) else None
        if sn[0] >= min_games:
            snowball = SNOWBALL_SPREE_W * (sn[1] / sn[0]) + SNOWBALL_MULTI_W * (sn[2] / sn[0])
        else:
            snowball = None
        enough = pf[0] >= min_games
        champs_out[str(cid)] = {
            "scaling": round(scaling, 4) if scaling is not None else None,
            "snowball": round(snowball, 4) if snowball is not None else None,
            # per-min empirical bars (damage to champs / self-mitigated / cc time); frontend percentile-ranks them
            "e_damage": round(pf[1] / pf[0], 2) if enough else None,
            "e_tank": round(pf[2] / pf[0], 2) if enough else None,
            "e_cc": round(pf[3] / pf[0], 3) if enough else None,
            "late_wr": round(s[1] / s[0], 4) if s[0] else None,
            "early_wr": round(s[3] / s[2], 4) if s[2] else None,
            "avg_spree": round(sn[1] / sn[0], 3) if sn[0] else None,
            "avg_multi": round(sn[2] / sn[0], 3) if sn[0] else None,
            "n": sn[0],
        }

    artifact = {
        "kind": SCHEMA_KIND,
        "version": 2,
        "source_parquet": str(data),
        "median_duration_sec": median_dur,
        "n_games": len(dur),
        "params": {"late_min_sec": late_min, "early_max_sec": early_max,
                   "min_bucket": min_bucket, "min_games": min_games,
                   "scaling": f"WR(>={late_min}s) - WR(<={early_max}s)",
                   "snowball": f"{SNOWBALL_SPREE_W}*spree + {SNOWBALL_MULTI_W}*multi"},
        "champs": champs_out,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    def top(metric, rev, n=8):
        rows = [(names.get(int(c), c), v[metric]) for c, v in champs_out.items() if v[metric] is not None]
        rows.sort(key=lambda r: r[1], reverse=rev)
        return rows[:n]

    print(f"\nwritten: {out}  ({len(champs_out)} champs, scaling=WR(>={late_min}s)-WR(<={early_max}s), median {median_dur}s)")
    print("scaling top (late):", "  ".join(f"{n}:{v:+.3f}" for n, v in top("scaling", True, 6)))
    print("scaling bot (early):", "  ".join(f"{n}:{v:+.3f}" for n, v in top("scaling", False, 6)))
    print("snowball top:", "  ".join(f"{n}:{v:.2f}" for n, v in top("snowball", True, 6)))
    print("snowball bot:", "  ".join(f"{n}:{v:.2f}" for n, v in top("snowball", False, 6)))
    print("damage top (dpm-to-champs):", "  ".join(f"{n}:{v:.0f}" for n, v in top("e_damage", True, 5)))
    print("tank top (self-mitigated/min):", "  ".join(f"{n}:{v:.0f}" for n, v in top("e_tank", True, 5)))
    print("cc top (cc-time/min):", "  ".join(f"{n}:{v:.1f}" for n, v in top("e_cc", True, 5)))


if __name__ == "__main__":
    main()
