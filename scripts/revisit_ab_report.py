"""Compare the revisit-interval A/B arms.

crawl_seen only carries lifetime counters, so raw arm totals are dominated by
history from before the experiment. This diffs against the baseline snapshot
taken at rollout, which is what makes the comparison mean anything.

    python scripts/revisit_ab_report.py
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.lcu.snowball import revisit_arm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lcu" / "games.db"
BASELINE = ROOT / "data" / "monitor" / "revisit_ab_baseline.json"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not BASELINE.exists():
        print(f"No baseline at {BASELINE}; nothing to compare against.")
        return 1
    snap = json.loads(BASELINE.read_text(encoding="utf-8"))
    base = snap["arms"]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    con.execute("pragma query_only=on")
    now = {"control": {"visits": 0, "games": 0}, "treatment": {"visits": 0, "games": 0}}
    for pu, pc, ng in con.execute(
        "SELECT puuid, process_count, new_games_found FROM crawl_seen WHERE process_count>0"
    ):
        a = now[revisit_arm(pu)]
        a["visits"] += pc or 0
        a["games"] += ng or 0
    con.close()

    started = dt.datetime.fromisoformat(snap["taken_at"])
    hours = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 3600
    print(f"實驗開始 {snap['taken_at']}  已進行 {hours:.1f} 小時\n")
    print(f"{'arm':<12}{'新增造訪':>12}{'新增產出':>12}{'每訪產出':>11}")
    out = {}
    for arm in ("control", "treatment"):
        dv = now[arm]["visits"] - base[arm]["visits"]
        dg = now[arm]["games"] - base[arm]["games"]
        per = dg / dv if dv else 0.0
        out[arm] = (dv, dg, per)
        print(f"  {arm:<10}{dv:>12,}{dg:>12,}{per:>11.3f}")

    cv, cg, cper = out["control"]
    tv, tg, tper = out["treatment"]
    print()
    if cv < 500 or tv < 500:
        print("樣本仍太少，先讓它多跑一陣子再看。")
        return 0
    print(f"每訪產出：treatment / control = {tper / cper:.2f}x" if cper else "control 無資料")
    print(f"總產出：  treatment {tg:,} vs control {cg:,}  ({tg - cg:+,})")
    print(f"造訪成本：treatment {tv:,} vs control {cv:,}  ({tv - cv:+,})")
    print(
        "\n注意：treatment 刻意拉長間隔，所以造訪數本來就會較少。"
        "\n要看的是『每訪產出』是否提升，以及『總產出』有沒有因此下降。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
