"""Goal deliverable: do different SR ranks show different build / champ preferences?

Joins the externally-resolved player_ranks (from resolve_player_ranks.py) to games-count-
UNBIASED player behaviour traits, then reports:
  - Spearman(tier, trait) for counter-item adoption, build-school entropy, champ concentration
  - mean trait per tier band  (the "ranks differ" table)
  - champion pick-rate: low-tier vs high-tier players (your Sion/Xin/Ambessa question, with REAL rank)
  - build markers (anti-heal / Serpent's Fang / Collector adoption) low-tier vs high-tier

Traits that depend on a player's full history (entropy, Herfindahl) are computed on a FIXED
10 games/player so captured-games-count cannot leak in (see _games_count_control.py).

Run after: resolve_player_ranks.py has populated player_ranks (needs RIOT_API_KEY).
"""
from __future__ import annotations
import glob, json, sqlite3
import numpy as np
import polars as pl
from scipy import stats

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIV = {"IV": 0, "III": 1, "II": 2, "I": 3, None: 3}
K = 10


def tier_ordinal(tier, div, lp):
    if tier is None:
        return None
    t = TIERS.index(tier) if tier in TIERS else None
    if t is None:
        return None
    base = t * 4 + DIV.get(div, 3)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") and lp:
        base += min(lp, 1500) / 500.0
    return base


def main():
    con = sqlite3.connect("data/ratings/player_ranks.db")
    try:
        ranks = con.execute(
            "SELECT lcu_puuid, solo_tier, solo_div, solo_lp, flex_tier, flex_div, flex_lp, status FROM player_ranks"
        ).fetchall()
    except sqlite3.OperationalError:
        print("[abort] player_ranks table missing — run resolve_player_ranks.py (needs RIOT_API_KEY) first.")
        return
    if not ranks:
        print("[abort] player_ranks is empty — run resolve_player_ranks.py with RIOT_API_KEY first.")
        return

    rrows = []
    for lcu, st, sd, slp, ft, fd, flp, status in ranks:
        tier, div, lp = (st, sd, slp) if st else (ft, fd, flp)
        rrows.append({"puuid": lcu, "tier": tier, "ord": tier_ordinal(tier, div, lp), "status": status})
    rank_df = pl.DataFrame(rrows)
    ranked = rank_df.filter(pl.col("ord").is_not_null())
    print(f"[ranks] resolved={rank_df.height:,}  with a tier={ranked.height:,}  "
          f"unranked/none={rank_df.height - ranked.height:,}")
    if ranked.height < 30:
        print("[warn] <30 ranked players — resolve a larger sample before trusting correlations.")

    players = pl.read_parquet(sorted(glob.glob("data/ratings/players__q2400__*.parquet"))[-1])
    part = pl.read_parquet(sorted(glob.glob("data/ratings/participants__q2400__*.parquet"))[-1])
    ids = json.load(open("data/cache/counter_item_ids.json", encoding="utf-8"))
    counter_ids = list(set(ids["anti_heal"] + ids["anti_shield"]))
    collector_ids = ids["collector"]; serpent_ids = ids["anti_shield"]

    # has_counter per slot + champ-controlled residual; counter_adopt mean is games-unbiased
    part = part.with_columns(
        pl.col("items").list.eval(pl.element().is_in(counter_ids)).list.any().fill_null(False).cast(pl.Int8).alias("has_counter")
    )
    cm = part.group_by("champ").agg(pl.col("has_counter").mean().alias("cm"))
    part = part.join(cm, on="champ").with_columns((pl.col("has_counter") - pl.col("cm")).alias("hc_resid"))

    # fixed-K traits (entropy, herf) — unbiased by games-count
    p = part.with_columns(pl.col("pid").count().over("pid").alias("gc"))
    pk = (p.filter(pl.col("gc") >= K).sort("gidx")
          .with_columns(pl.int_range(pl.len()).over("pid").alias("rk")).filter(pl.col("rk") < K))
    ic = pk.select("pid", "items").explode("items").drop_nulls().group_by("pid", "items").len()
    tot = ic.group_by("pid").agg(pl.col("len").sum().alias("t"))
    ent = (ic.join(tot, on="pid").with_columns((pl.col("len") / pl.col("t")).alias("pp"))
           .group_by("pid").agg((-(pl.col("pp") * pl.col("pp").log()).sum()).alias("item_entropy")))
    cc = pk.group_by("pid", "champ").len()
    tt = cc.group_by("pid").agg(pl.col("len").sum().alias("t"))
    herf = (cc.join(tt, on="pid").with_columns((pl.col("len") / pl.col("t")).alias("pp"))
            .group_by("pid").agg((pl.col("pp") * pl.col("pp")).sum().alias("champ_herf")))
    adopt = part.group_by("pid").agg(pl.col("has_counter").mean().alias("counter_adopt"),
                                     pl.col("hc_resid").mean().alias("counter_resid"))
    traits = adopt.join(ent, on="pid", how="left").join(herf, on="pid", how="left")

    # join puuid -> pid -> traits -> rank
    j = (players.select("pid", "puuid").join(traits, on="pid", how="left")
         .join(ranked, on="puuid", how="inner"))
    print(f"[join] ranked players with traits = {j.height:,}")
    if j.height < 30:
        print("[warn] too few joined players for stable stats.")

    print("\n=== Spearman(tier ordinal, trait)  — does rank predict behaviour? ===")
    ordv = j["ord"].to_numpy()
    for col in ("counter_adopt", "counter_resid", "item_entropy", "champ_herf"):
        v = j[col].to_numpy()
        m = ~np.isnan(v) & ~np.isnan(ordv)
        if m.sum() > 10:
            rho, pval = stats.spearmanr(ordv[m], v[m])
            print(f"  {col:16s} rho={rho:+.3f}  p={pval:.1e}  n={int(m.sum()):,}")

    # tier bands
    def band(o):
        return "IRON-BRZ" if o < 8 else "SLV-GLD" if o < 16 else "PLT-EMR" if o < 24 else "DIA+"
    j = j.with_columns(pl.col("ord").map_elements(band, return_dtype=pl.Utf8).alias("band"))
    print("\n=== mean trait by tier band ===")
    cols = ["counter_adopt", "item_entropy", "champ_herf"]
    order = {"IRON-BRZ": 0, "SLV-GLD": 1, "PLT-EMR": 2, "DIA+": 3}
    agg = (j.group_by("band").agg(pl.len().alias("n"), *[pl.col(c).mean() for c in cols])
           .sort(pl.col("band").replace_strict(order, default=9)))
    print(f"  {'band':10s} {'n':>5s} " + " ".join(f"{c:>14s}" for c in cols))
    for r in agg.iter_rows(named=True):
        print(f"  {r['band']:10s} {r['n']:>5d} " + " ".join(f"{r[c]:>14.4f}" for c in cols))

    # champ + build preference: low vs high tier
    lo_pids = set(j.filter(pl.col("ord") < 12)["pid"].to_list())
    hi_pids = set(j.filter(pl.col("ord") >= 16)["pid"].to_list())
    champ_map = {int(k): v for k, v in json.load(open("data/cache/ddragon_champion_byid.json", encoding="utf-8")).items()}
    sub = part.filter(pl.col("pid").is_in(list(lo_pids | hi_pids)))
    sub = sub.with_columns(pl.when(pl.col("pid").is_in(list(lo_pids))).then(pl.lit("lo")).otherwise(pl.lit("hi")).alias("grp"))
    n_lo = sub.filter(pl.col("grp") == "lo").height; n_hi = sub.filter(pl.col("grp") == "hi").height
    print(f"\n=== champ pick-rate: LOW tier (<GOLD, {len(lo_pids)} plyrs/{n_lo} slots) vs HIGH (>=PLAT, {len(hi_pids)}/{n_hi}) ===")
    cp = (sub.group_by("champ", "grp").len()
          .pivot(values="len", index="champ", on="grp", aggregate_function="sum").fill_null(0))
    cp = cp.with_columns((pl.col("lo") / max(n_lo, 1)).alias("lo_r"), (pl.col("hi") / max(n_hi, 1)).alias("hi_r"))
    cp = cp.filter((pl.col("lo") + pl.col("hi")) >= 20).with_columns(((pl.col("hi_r") + 1e-4) / (pl.col("lo_r") + 1e-4)).alias("ratio"))
    nm = lambda c: champ_map.get(int(c), {}).get("alias", str(c))
    hi_skew = cp.sort("ratio", descending=True).head(10)
    lo_skew = cp.sort("ratio").head(10)
    print("  most HIGH-tier-skewed champs:")
    for r in hi_skew.iter_rows(named=True):
        print(f"    {nm(r['champ']):12s} lo={100*r['lo_r']:.2f}% hi={100*r['hi_r']:.2f}%  x{r['ratio']:.2f}")
    print("  most LOW-tier-skewed champs:")
    for r in lo_skew.iter_rows(named=True):
        print(f"    {nm(r['champ']):12s} lo={100*r['lo_r']:.2f}% hi={100*r['hi_r']:.2f}%  x{r['ratio']:.2f}")
    for cid, label in [(14, "Sion"), (876, "Lillia"), (875, "Sett"), (5, "XinZhao"), (799, "Ambessa")]:
        row = cp.filter(pl.col("champ") == cid)
        if row.height:
            r = row.row(0, named=True)
            print(f"  [anchor] {label:9s} lo={100*r['lo_r']:.2f}% hi={100*r['hi_r']:.2f}%  x{r['ratio']:.2f}")

    # build markers
    def rate(pidset, idset):
        s = part.filter(pl.col("pid").is_in(list(pidset)))
        s = s.with_columns(pl.col("items").list.eval(pl.element().is_in(idset)).list.any().fill_null(False).cast(pl.Int8).alias("h"))
        return 100 * s["h"].mean()
    print("\n=== build markers: LOW vs HIGH tier (% of games) ===")
    for label, idset in [("anti-heal/shield", counter_ids), ("Serpent's Fang", serpent_ids), ("Collector", collector_ids)]:
        print(f"  {label:18s} lo={rate(lo_pids, idset):.1f}%  hi={rate(hi_pids, idset):.1f}%")


if __name__ == "__main__":
    main()
