from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import count_games


def _tier_module():
    try:
        from scripts import build_tier_list as tier
    except ImportError:
        import build_tier_list as tier  # type: ignore[no-redef]
    return tier


def build_tier_list_payload(
    *,
    db: Path,
    queue_id: int = 2400,
    patch_prefix: str | None = "16.10",
    ddragon_version: str | None = None,
    min_games: int = 50,
    min_pair_games: int = 15,
    top_n: int = 5,
    bot_n: int = 5,
) -> dict[str, Any]:
    """Build an API-friendly tier-list payload from the shared games table."""
    if count_games(db, queue_id=queue_id, patch_prefix=patch_prefix or None) == 0:
        return {
            "meta": {
                "queue_id": queue_id,
                "patch_prefix": patch_prefix or None,
                "data_dragon_version": ddragon_version,
                "total_games": 0,
                "min_games": min_games,
                "min_pair_games": min_pair_games,
            },
            "tiers": {},
            "champions": {},
            "augments": {},
        }

    tier = _tier_module()
    patch_prefix = patch_prefix or None

    version, champ_meta = tier.load_champion_metadata(ddragon_version)
    aug_meta = tier.load_augment_metadata(cache_dir=Path("data/cache"))
    champ_records, champ_aug, _champ_pairs = tier.compute_winrates(db, queue_id, patch_prefix)
    total_games = sum(int(row["games"]) for row in champ_records) // 10
    visible_records = [row for row in champ_records if int(row["games"]) >= min_games]

    aug_prior_strength = tier.estimate_augment_prior_strength(champ_aug)
    champ_profiles = tier.load_champion_pick_profiles(champ_meta)
    picks = tier.build_champ_augment_picks(
        champ_aug,
        aug_meta,
        champ_profiles,
        min_games_per_pair=min_pair_games,
        top_n=top_n,
        bot_n=bot_n,
        prior_strength=aug_prior_strength,
    )

    tiers: dict[str, list[dict[str, Any]]] = {name: [] for name in tier.TIER_ORDER}
    champions: dict[str, dict[str, Any]] = {}
    used_aug_ids: set[int] = set()

    def pack_pick(row: dict[str, Any]) -> dict[str, Any]:
        used_aug_ids.add(int(row["augment_id"]))
        return {
            "id": int(row["augment_id"]),
            "games": int(row["games"]),
            "wins": int(row["wins"]),
            "win_rate": round(float(row["smoothed_wr"]), 4),
            "lift": round(float(row["lift"]), 4),
            "score": round(float(row.get("rank_score", row["lift"])), 4),
            "pick_rate": round(float(row.get("pick_rate", 0.0)), 4),
        }

    for record in visible_records:
        cid = int(record["champion_id"])
        meta = champ_meta.get(cid)
        if not meta:
            continue
        tier_name = tier.assign_tier(float(record["bayes_wr"]))
        summary = {
            "id": cid,
            "name": meta.get("name"),
            "name_zh": meta.get("name_zh", meta.get("name")),
            "name_en": meta.get("name_en", meta.get("alias", meta.get("name"))),
            "alias": meta.get("alias", ""),
            "image": meta.get("image", ""),
            "roles": meta.get("tags") or [],
            "tier": tier_name,
            "games": int(record["games"]),
            "wins": int(record["wins"]),
            "raw_wr": round(float(record["raw_wr"]), 4),
            "bayes_wr": round(float(record["bayes_wr"]), 4),
        }
        tiers[tier_name].append(summary)

        champ_pick = picks.get(cid, {"top": {}, "bot": {}})
        champions[str(cid)] = {
            **summary,
            "augments": {
                "top": {
                    rarity: [pack_pick(row) for row in champ_pick["top"].get(rarity, [])]
                    for rarity in tier.RARITY_ORDER
                },
                "bot": {
                    rarity: [pack_pick(row) for row in champ_pick["bot"].get(rarity, [])]
                    for rarity in tier.RARITY_ORDER
                },
            },
        }

    augments = {
        str(aid): {
            "id": aid,
            "name": aug_meta[aid].get("name"),
            "name_zh": aug_meta[aid].get("name_zh", aug_meta[aid].get("name")),
            "name_en": aug_meta[aid].get("name_en", aug_meta[aid].get("name")),
            "icon": aug_meta[aid].get("icon", ""),
            "rarity": aug_meta[aid].get("rarity", ""),
            "desc": aug_meta[aid].get("desc", ""),
            "desc_zh": aug_meta[aid].get("desc_zh", aug_meta[aid].get("desc", "")),
            "desc_en": aug_meta[aid].get("desc_en", ""),
        }
        for aid in sorted(used_aug_ids)
        if aid in aug_meta
    }

    return {
        "meta": {
            "queue_id": queue_id,
            "patch_prefix": patch_prefix,
            "data_dragon_version": version,
            "total_games": total_games,
            "min_games": min_games,
            "min_pair_games": min_pair_games,
        },
        "tiers": tiers,
        "champions": champions,
        "augments": augments,
    }


def champion_augments(payload: dict[str, Any], champion_id: int) -> dict[str, Any] | None:
    champ = payload.get("champions", {}).get(str(int(champion_id)))
    if not champ:
        return None
    return {
        "champion": {
            key: champ.get(key)
            for key in ("id", "name", "name_zh", "name_en", "alias", "image", "roles", "tier")
        },
        "augments": champ.get("augments", {}),
    }
