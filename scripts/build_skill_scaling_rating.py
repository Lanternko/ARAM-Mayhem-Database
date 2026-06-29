"""Per-champion 'skill-scaling' rating = win-rate(high-skill lobbies) - win-rate(low-skill).

Positive = rewards skill / scales up in good lobbies; negative = low-skill stomper.
Validated as a REAL champ property by checking cross-patch stability (16.11 vs 16.12 vs 16.13)
before pooling.  Read-only artifact (does NOT touch the published site).

Inputs: data/ratings/game_skill_by_id.parquet, data/lcu/games.db, ddragon_champion_byid.json
Output: outputs/skill_scaling_rating.csv  (champ, alias, scaling_pp, games, per-patch columns)
"""
from __future__ import annotations
import json
import math
import sqlite3
from pathlib import Path
import numpy as np
import polars as pl

PATCHES = ["16.11", "16.12", "16.13"]
MIN_GAMES = 250


def parse_champs(s):
    try:
        v = json.loads(s)
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return [int(x) for x in str(s).replace("[", "").replace("]", "").split(",") if x.strip().lstrip("-").isdigit()]


def main():
    gs = pl.read_parquet("data/ratings/game_skill_by_id.parquet").filter(pl.col("n_known") >= 8)
    gs = gs.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("pfx"))
    thr = {}
    for p in PATCHES:
        v = gs.filter(pl.col("pfx") == p)["lobby_eff_z"].to_numpy()
        if len(v) > 1000:
            thr[p] = np.quantile(v, [0.25, 0.75])
    grp = {}  # game_id -> (pfx, 'hi'/'lo')
    for gid, pfx, e in zip(gs["game_id"].to_list(), gs["pfx"].to_list(), gs["lobby_eff_z"].to_list()):
        if pfx in thr:
            q25, q75 = thr[pfx]
            if e >= q75:
                grp[gid] = (pfx, "hi")
            elif e <= q25:
                grp[gid] = (pfx, "lo")

    con = sqlite3.connect("file:data/lcu/games.db?mode=ro", uri=True)
    cur = con.execute("SELECT game_id, blue_champs, red_champs, blue_wins FROM games WHERE queue_id=2400")
    stat = {}  # (pfx, champ) -> {'hi':[g,w],'lo':[g,w]}
    while True:
        rows = cur.fetchmany(20_000)
        if not rows:
            break
        for gid, bc, rc, bw in rows:
            pg = grp.get(gid)
            if pg is None:
                continue
            pfx, g = pg
            for champs, won in ((parse_champs(bc), int(bw)), (parse_champs(rc), 1 - int(bw))):
                for c in champs:
                    d = stat.setdefault((pfx, c), {"hi": [0, 0], "lo": [0, 0]})
                    d[g][0] += 1
                    d[g][1] += won
    con.close()

    champs = sorted({c for (_, c) in stat})
    champ_map = {int(k): v for k, v in json.load(open("data/cache/ddragon_champion_byid.json", encoding="utf-8")).items()}
    nm = lambda c: champ_map.get(c, {}).get("alias", str(c))

    def scaling(pfx, c):
        d = stat.get((pfx, c))
        if not d:
            return None, 0
        gh, wh = d["hi"]; gl, wl = d["lo"]
        if gh < MIN_GAMES or gl < MIN_GAMES:
            return None, gh + gl
        return (wh / gh - wl / gl), gh + gl

    # cross-patch stability
    print("=== cross-patch stability of skill-scaling (does a champ's scaling persist?) ===")
    for i in range(len(PATCHES)):
        for jx in range(i + 1, len(PATCHES)):
            a, b = PATCHES[i], PATCHES[jx]
            pairs = [(scaling(a, c)[0], scaling(b, c)[0]) for c in champs]
            pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
            if len(pairs) > 10:
                xa, ya = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
                print(f"  {a} vs {b}:  r={np.corrcoef(xa, ya)[0,1]:+.3f}  (n={len(pairs)} champs)")

    # pooled rating
    rows = []
    ss_json = {}
    for c in champs:
        H = sum(stat[(p, c)]["hi"][0] for p in PATCHES if (p, c) in stat)
        WH = sum(stat[(p, c)]["hi"][1] for p in PATCHES if (p, c) in stat)
        L = sum(stat[(p, c)]["lo"][0] for p in PATCHES if (p, c) in stat)
        WL = sum(stat[(p, c)]["lo"][1] for p in PATCHES if (p, c) in stat)
        if H < MIN_GAMES or L < MIN_GAMES:
            continue
        ph, plo = WH / H, WL / L
        pooled = (WH + WL) / (H + L)
        se = math.sqrt(pooled * (1 - pooled) * (1 / H + 1 / L)) or 1e-9
        z = (ph - plo) / se
        sc = 100 * (ph - plo)
        rows.append({"champ": c, "alias": nm(c), "scaling_pp": round(sc, 1), "z": round(z, 1),
                     "high_wr": round(100 * ph, 1), "low_wr": round(100 * plo, 1), "games": H + L,
                     **{f"sc_{p}": (round(100 * scaling(p, c)[0], 1) if scaling(p, c)[0] is not None else None) for p in PATCHES}})
        ss_json[str(c)] = {"pp": round(sc, 1), "z": round(z, 1), "g": H + L}
    df = pl.DataFrame(rows).sort("scaling_pp", descending=True)
    Path("outputs").mkdir(exist_ok=True)
    df.write_csv("outputs/skill_scaling_rating.csv")
    # build-time artifact consumed by build_tier_list.py (per-champ payload field)
    Path("data/cache").mkdir(parents=True, exist_ok=True)
    Path("data/cache/champ_skill_scaling.json").write_text(
        json.dumps({"champs": ss_json}, ensure_ascii=False), encoding="utf-8")
    print(f"\n[done] outputs/skill_scaling_rating.csv  ({df.height} champions)")
    print("\n--- TOP skill-scaling (reward good lobbies) ---")
    for r in df.head(10).iter_rows(named=True):
        print(f"  {r['alias']:13s} {r['scaling_pp']:+5.1f}pp  (low {r['low_wr']}% -> high {r['high_wr']}%)")
    print("--- BOTTOM (low-skill stompers) ---")
    for r in df.tail(10).iter_rows(named=True):
        print(f"  {r['alias']:13s} {r['scaling_pp']:+5.1f}pp  (low {r['low_wr']}% -> high {r['high_wr']}%)")


if __name__ == "__main__":
    main()
