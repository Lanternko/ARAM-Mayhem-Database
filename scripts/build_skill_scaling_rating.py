"""Per-champion 'skill-scaling' rating = win-rate(high-skill lobbies) - win-rate(low-skill).

Positive = rewards skill / scales up in good lobbies; negative = low-skill stomper.
Validated as a REAL champ property by checking cross-patch stability (16.11 vs 16.12 vs 16.13)
before pooling.  Read-only artifact (does NOT touch the published site).

Inputs: data/ratings/game_skill_by_id.parquet, data/lcu/games.db, ddragon_champion_byid.json
Outputs:
  outputs/skill_scaling_rating.csv  (analysis table; ignored local artifact)
  scripts/site_data/champ_skill_scaling.json  (versioned site snapshot)
"""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
from pathlib import Path
import numpy as np
import polars as pl

PATCHES = ["16.11", "16.12", "16.13"]
MIN_GAMES = 250
MIN_SNAPSHOT_CHAMPIONS = 150
SITE_SNAPSHOT = Path(__file__).resolve().parent / "site_data" / "champ_skill_scaling.json"


def validate_site_snapshot_rows(ss_json: dict[str, dict]) -> None:
    if not isinstance(ss_json, dict) or len(ss_json) < MIN_SNAPSHOT_CHAMPIONS:
        count = len(ss_json) if isinstance(ss_json, dict) else 0
        raise ValueError(
            f"skill-scaling snapshot needs at least {MIN_SNAPSHOT_CHAMPIONS} champions; got {count}"
        )
    normalized_ids: set[int] = set()
    for champion_id, value in ss_json.items():
        cid = int(champion_id)
        pp = float(value["pp"])
        z_score = float(value["z"])
        games = int(value["g"])
        if cid in normalized_ids:
            raise ValueError(f"duplicate normalized champion id: {cid}")
        if not math.isfinite(pp) or not math.isfinite(z_score) or games <= 0:
            raise ValueError(f"invalid metrics for champion {cid}")
        normalized_ids.add(cid)


def write_site_snapshot(ss_json: dict[str, dict]) -> None:
    validate_site_snapshot_rows(ss_json)
    SITE_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SITE_SNAPSHOT.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "queue_id": 2400,
                "region": "TW",
                "patches": PATCHES,
                "metric": "high_skill_wr_minus_low_skill_wr_pp",
                "min_games_per_cohort": MIN_GAMES,
                "champs": ss_json,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def snapshot_from_analysis_csv(path: Path) -> int:
    """Restore the site snapshot from a previously generated analysis table."""
    df = pl.read_csv(path)
    required = {"champ", "scaling_pp", "z", "games"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"analysis CSV is missing columns: {sorted(missing)}")
    ss_json: dict[str, dict] = {}
    for row in df.iter_rows(named=True):
        champion_id = str(int(row["champ"]))
        if champion_id in ss_json:
            raise ValueError(f"analysis CSV contains duplicate champion: {champion_id}")
        ss_json[champion_id] = {
            "pp": round(float(row["scaling_pp"]), 1),
            "z": round(float(row["z"]), 1),
            "g": int(row["games"]),
        }
    write_site_snapshot(ss_json)
    print(f"[done] {SITE_SNAPSHOT}  ({len(ss_json)} champions; restored from {path})")
    return len(ss_json)


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
    # This is a published, cross-patch metric rather than an ephemeral cache.
    # Keep the snapshot in the tracked source tree so the isolated static-site
    # publisher receives the exact same input as a local build.
    write_site_snapshot(ss_json)
    print(f"\n[done] outputs/skill_scaling_rating.csv  ({df.height} champions)")
    print(f"[done] {SITE_SNAPSHOT}  ({len(ss_json)} champions)")
    print("\n--- TOP skill-scaling (reward good lobbies) ---")
    for r in df.head(10).iter_rows(named=True):
        print(f"  {r['alias']:13s} {r['scaling_pp']:+5.1f}pp  (low {r['low_wr']}% -> high {r['high_wr']}%)")
    print("--- BOTTOM (low-skill stompers) ---")
    for r in df.tail(10).iter_rows(named=True):
        print(f"  {r['alias']:13s} {r['scaling_pp']:+5.1f}pp  (low {r['low_wr']}% -> high {r['high_wr']}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-analysis-csv",
        type=Path,
        help="restore the tracked site snapshot from a prior analysis CSV without rescanning games.db",
    )
    args = parser.parse_args()
    if args.from_analysis_csv is not None:
        snapshot_from_analysis_csv(args.from_analysis_csv)
    else:
        main()
