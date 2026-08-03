"""Refresh the public payload's current-patch statistics without retraining models.

This is intentionally narrower than the full site builder: it updates the current
patch champion, augment, and same-team pair observations from the local DB, while
preserving the already-built presentation/model sections of the split payload.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from tierlist_engine import (  # noqa: E402
    build_augment_global_stats,
    compute_winrates,
    count_participant_games,
    estimate_augment_prior_strength,
)


def _round(value: object, digits: int = 4) -> float:
    return round(float(value or 0.0), digits)


def main() -> None:
    db = ROOT / "data/lcu/games.db"
    payload_path = ROOT / "docs/api/tier-list.json"
    queue_id = 2400
    patch_prefix = "16.15"

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    champ_records, champ_aug, champ_pairs = compute_winrates(
        db,
        queue_id,
        patch_prefix,
        prev_wr_by_champ=None,
        prev_pair_lift=None,
    )
    champ_by_id = {int(row["champion_id"]): row for row in champ_records}

    aug_meta = {int(aid): {} for aid in payload.get("augs", {})}
    aug_global = build_augment_global_stats(
        champ_aug,
        aug_meta,
        appearance_games=count_participant_games(db, queue_id, patch_prefix),
        prior_strength=estimate_augment_prior_strength(champ_aug),
        prev_champ_aug_records=None,
    )

    for cid_text, champ in payload.get("champs", {}).items():
        current = champ_by_id.get(int(cid_text))
        if current is None:
            continue
        champ["wr"] = _round(current["bayes_wr"])
        champ["rawWr"] = _round(current["raw_wr"])
        champ["g"] = int(current["games"])
        champ["prevMix"] = 0.0

    for aid_text, stats in aug_global.items():
        target = payload.get("augs", {}).get(str(aid_text))
        if target is None:
            continue
        target.update(
            {
                "wr": _round(stats["wr"]),
                "rawWr": _round(stats["rawWr"]),
                "g": int(stats["g"]),
                "lcb": _round(stats["lcb"]),
                "lift": _round(stats["lift"]),
                "pick": _round(stats["pick"]),
                "curG": int(stats["curG"]),
                "prevMix": 0.0,
            }
        )

    pair_by_champ: dict[int, list[dict]] = defaultdict(list)
    for row in champ_pairs:
        pair_by_champ[int(row["champion_id"])].append(row)
    for cid_text, champ in payload.get("champs", {}).items():
        rows = sorted(
            pair_by_champ.get(int(cid_text), []),
            key=lambda row: (-float(row["lift"]), -int(row["games"]), int(row["teammate_id"])),
        )
        keep = rows[:12] + (rows[-12:] if len(rows) > 12 else [])
        champ["pairs"] = [
            {
                "id": int(row["teammate_id"]),
                "g": int(row["games"]),
                "wr": _round(row["raw_wr"]),
                "expected": _round(row["expected_wr"]),
                "lift": _round(row["lift"]),
                "z": round(float(row["z_score"]), 3),
            }
            for row in keep
        ]

    # The old comparison table contains headline values from the previous build;
    # do not leave those mixed values visible after switching the current payload
    # to a pure-patch refresh.  A later full build can recreate it from both patches.
    payload["patch_prefix"] = patch_prefix
    payload["patchChanges"] = None
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"[refresh] current patch={patch_prefix} champions={len(champ_records)} "
        f"games={sum(int(row['games']) for row in champ_records) // 10:,} "
        f"augments={len(aug_global)} payload={payload_path}"
    )


if __name__ == "__main__":
    main()
