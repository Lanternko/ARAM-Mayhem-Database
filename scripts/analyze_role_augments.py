"""Analyze Mayhem role winrates and augment winrates by patch.

Default "new augment" definition is comparative: an augment is new when it
appears in the target patch and does not appear in the comparison patch.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.champion_roles import (  # noqa: E402
    PRIMARY_ROLE_OVERRIDES,
    ROLE_ORDER,
    SECONDARY_ROLE_OVERRIDES,
)


def _load_site_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_augment_metadata(cache_dir: Path, payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for aid, meta in (payload.get("augs") or payload.get("augments") or {}).items():
        out[int(aid)] = dict(meta)

    try:
        from scripts import build_tier_list

        fresh = build_tier_list.load_augment_metadata(cache_dir=cache_dir)
        for aid, meta in fresh.items():
            merged = dict(meta)
            merged.update(out.get(int(aid), {}))
            out[int(aid)] = merged
    except Exception as exc:  # pragma: no cover - analysis fallback.
        print(f"[warn] could not load fresh augment metadata: {exc}", file=sys.stderr)

    return out


def _champion_maps(payload: dict[str, Any]) -> tuple[dict[int, str], dict[int, str], dict[int, list[str]]]:
    id_to_alias: dict[int, str] = {}
    id_to_name: dict[int, str] = {}
    id_to_tags: dict[int, list[str]] = {}
    for raw_cid, champ in (payload.get("champs") or {}).items():
        cid = int(raw_cid)
        id_to_alias[cid] = champ.get("alias") or champ.get("name_en") or str(cid)
        id_to_name[cid] = champ.get("name_zh") or champ.get("name") or champ.get("name_en") or str(cid)
        id_to_tags[cid] = list(champ.get("tags") or [])
    return id_to_alias, id_to_name, id_to_tags


def _roles_for_champion(
    champion_id: int,
    *,
    id_to_alias: dict[int, str],
    id_to_tags: dict[int, list[str]],
    role_cache: dict[int, tuple[str, ...]],
) -> tuple[str, ...]:
    cached = role_cache.get(champion_id)
    if cached is not None:
        return cached

    alias = id_to_alias.get(champion_id, "")
    primary = PRIMARY_ROLE_OVERRIDES.get(alias)
    if not primary:
        tags = id_to_tags.get(champion_id) or []
        primary = tags[0] if tags else "Unknown"
    secondary = SECONDARY_ROLE_OVERRIDES.get(alias)
    roles = (primary, secondary) if secondary and secondary != primary else (primary,)
    role_cache[champion_id] = roles
    return roles


def _patch_rows(
    con: sqlite3.Connection,
    *,
    queue_id: int,
    patch_prefix: str,
):
    return con.execute(
        """
        SELECT blue_champs, red_champs, blue_wins, participants_json
        FROM games
        WHERE queue_id = ?
          AND patch LIKE ?
          AND participants_json IS NOT NULL
        """,
        (queue_id, f"{patch_prefix}%"),
    )


def _collect_patch_stats(
    db_path: Path,
    *,
    queue_id: int,
    patch_prefix: str,
    id_to_alias: dict[int, str],
    id_to_tags: dict[int, list[str]],
) -> dict[str, Any]:
    role_cache: dict[int, tuple[str, ...]] = {}
    role_games: Counter[str] = Counter()
    role_wins: Counter[str] = Counter()
    role_primary_games: Counter[str] = Counter()
    role_primary_wins: Counter[str] = Counter()
    role_aug_games: dict[str, Counter[int]] = defaultdict(Counter)
    role_aug_wins: dict[str, Counter[int]] = defaultdict(Counter)
    champion_games: Counter[int] = Counter()
    champion_wins: Counter[int] = Counter()
    aug_games: Counter[int] = Counter()
    aug_wins: Counter[int] = Counter()
    rows_seen = 0
    participant_rows = 0

    con = sqlite3.connect(str(db_path))
    try:
        for blue_json, red_json, blue_wins, participants_json in _patch_rows(
            con,
            queue_id=queue_id,
            patch_prefix=patch_prefix,
        ):
            rows_seen += 1
            blue_won = bool(blue_wins)
            blue_team = json.loads(blue_json)
            red_team = json.loads(red_json)
            for team, won in ((blue_team, blue_won), (red_team, not blue_won)):
                for champion_id in team:
                    champion_id = int(champion_id)
                    roles = _roles_for_champion(
                        champion_id,
                        id_to_alias=id_to_alias,
                        id_to_tags=id_to_tags,
                        role_cache=role_cache,
                    )
                    champion_games[champion_id] += 1
                    champion_wins[champion_id] += int(won)
                    primary = roles[0]
                    role_primary_games[primary] += 1
                    role_primary_wins[primary] += int(won)
                    for role in roles:
                        role_games[role] += 1
                        role_wins[role] += int(won)

            for participant in json.loads(participants_json or "[]"):
                champion_id = int(participant.get("championId") or 0)
                if champion_id <= 0:
                    continue
                participant_rows += 1
                player_won = int((int(participant.get("teamId") or 0) == 100) == blue_won)
                roles = _roles_for_champion(
                    champion_id,
                    id_to_alias=id_to_alias,
                    id_to_tags=id_to_tags,
                    role_cache=role_cache,
                )
                for raw_aid in participant.get("augments") or ():
                    aid = int(raw_aid or 0)
                    if aid <= 0:
                        continue
                    aug_games[aid] += 1
                    aug_wins[aid] += player_won
                    for role in roles:
                        role_aug_games[role][aid] += 1
                        role_aug_wins[role][aid] += player_won
    finally:
        con.close()

    return {
        "patch_prefix": patch_prefix,
        "rows_seen": rows_seen,
        "participant_rows": participant_rows,
        "role_games": role_games,
        "role_wins": role_wins,
        "role_primary_games": role_primary_games,
        "role_primary_wins": role_primary_wins,
        "role_aug_games": role_aug_games,
        "role_aug_wins": role_aug_wins,
        "champion_games": champion_games,
        "champion_wins": champion_wins,
        "aug_games": aug_games,
        "aug_wins": aug_wins,
    }


def _pct(wins: int, games: int) -> float:
    return (wins / games * 100.0) if games else 0.0


def _augment_name(aid: int, aug_meta: dict[int, dict[str, Any]]) -> str:
    meta = aug_meta.get(aid, {})
    return meta.get("name_zh") or meta.get("name") or meta.get("name_en") or f"#{aid}"


def _augment_set(aid: int, aug_meta: dict[int, dict[str, Any]]) -> str:
    meta = aug_meta.get(aid, {})
    return meta.get("set_zh") or meta.get("set") or meta.get("set_en") or ""


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _role_summary_rows(target: dict[str, Any], compare: dict[str, Any] | None) -> list[dict[str, Any]]:
    roles = list(ROLE_ORDER)
    extra_roles = sorted(set(target["role_games"]) - set(roles))
    rows: list[dict[str, Any]] = []
    for role in roles + extra_roles:
        primary_games = int(target["role_primary_games"][role])
        primary_wins = int(target["role_primary_wins"][role])
        all_games = int(target["role_games"][role])
        all_wins = int(target["role_wins"][role])
        row: dict[str, Any] = {
            "role": role,
            "target_primary_games": primary_games,
            "target_primary_wr": round(_pct(primary_wins, primary_games), 2),
            "target_all_tag_games": all_games,
            "target_all_tag_wr": round(_pct(all_wins, all_games), 2),
        }
        if compare is not None:
            c_games = int(compare["role_primary_games"][role])
            c_wins = int(compare["role_primary_wins"][role])
            c_wr = _pct(c_wins, c_games)
            t_wr = _pct(primary_wins, primary_games)
            row.update(
                {
                    "compare_primary_games": c_games,
                    "compare_primary_wr": round(c_wr, 2),
                    "target_minus_compare_pp": round(t_wr - c_wr, 2),
                }
            )
        rows.append(row)
    return rows


def _champion_change_rows(
    target: dict[str, Any],
    compare: dict[str, Any] | None,
    *,
    id_to_alias: dict[int, str],
    id_to_name: dict[int, str],
    id_to_tags: dict[int, list[str]],
    min_target_games: int,
    min_compare_games: int,
    prior: float,
    prior_k: int,
) -> list[dict[str, Any]]:
    if compare is None:
        return []

    role_cache: dict[int, tuple[str, ...]] = {}
    rows: list[dict[str, Any]] = []
    for champion_id, target_games in target["champion_games"].items():
        target_games = int(target_games)
        compare_games = int(compare["champion_games"][champion_id])
        if target_games < min_target_games or compare_games < min_compare_games:
            continue
        target_wins = int(target["champion_wins"][champion_id])
        compare_wins = int(compare["champion_wins"][champion_id])
        target_wr = _pct(target_wins, target_games)
        compare_wr = _pct(compare_wins, compare_games)
        target_bayes_wr = _pct(
            target_wins + prior * prior_k,
            target_games + prior_k,
        )
        compare_bayes_wr = _pct(
            compare_wins + prior * prior_k,
            compare_games + prior_k,
        )
        rows.append(
            {
                "champion_id": champion_id,
                "champion_name": id_to_name.get(champion_id, str(champion_id)),
                "alias": id_to_alias.get(champion_id, str(champion_id)),
                "primary_role": _roles_for_champion(
                    champion_id,
                    id_to_alias=id_to_alias,
                    id_to_tags=id_to_tags,
                    role_cache=role_cache,
                )[0],
                "target_games": target_games,
                "target_wins": target_wins,
                "target_raw_wr": round(target_wr, 2),
                "target_bayes_wr": round(target_bayes_wr, 2),
                "compare_games": compare_games,
                "compare_wins": compare_wins,
                "compare_raw_wr": round(compare_wr, 2),
                "compare_bayes_wr": round(compare_bayes_wr, 2),
                "raw_delta_pp": round(target_wr - compare_wr, 2),
                "bayes_delta_pp": round(target_bayes_wr - compare_bayes_wr, 2),
            }
        )
    rows.sort(key=lambda row: (float(row["bayes_delta_pp"]), int(row["target_games"])), reverse=True)
    return rows


def _role_augment_rows(
    target: dict[str, Any],
    compare: dict[str, Any] | None,
    *,
    aug_meta: dict[int, dict[str, Any]],
    min_role_augment_picks: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    compare_aug_ids = set(compare["aug_games"]) if compare is not None else set()
    target_aug_ids = set(target["aug_games"])
    new_aug_ids = target_aug_ids - compare_aug_ids

    rows: list[dict[str, Any]] = []
    for role, counter in target["role_aug_games"].items():
        for aid, games in counter.items():
            games = int(games)
            if games < min_role_augment_picks:
                continue
            wins = int(target["role_aug_wins"][role][aid])
            group = "new" if aid in new_aug_ids else "old"
            rows.append(
                {
                    "role": role,
                    "group": group,
                    "augment_id": aid,
                    "augment_name": _augment_name(aid, aug_meta),
                    "set": _augment_set(aid, aug_meta),
                    "rarity": aug_meta.get(aid, {}).get("rarity", ""),
                    "target_picks": games,
                    "target_wins": wins,
                    "target_wr": round(_pct(wins, games), 2),
                    "compare_global_picks": int(compare["aug_games"][aid]) if compare else "",
                }
            )
    rows.sort(key=lambda row: (row["role"], row["group"], -float(row["target_wr"]), -int(row["target_picks"])))
    return rows, new_aug_ids


def _role_extreme_rows(role_aug_rows: list[dict[str, Any]], *, per_group: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    roles = sorted({str(row["role"]) for row in role_aug_rows}, key=lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else 99)
    for role in roles:
        for group in ("new", "old"):
            rows = [row for row in role_aug_rows if row["role"] == role and row["group"] == group]
            strongest = sorted(rows, key=lambda row: (float(row["target_wr"]), int(row["target_picks"])), reverse=True)[:per_group]
            weakest = sorted(rows, key=lambda row: (float(row["target_wr"]), -int(row["target_picks"])))[:per_group]
            for label, selected in (("strongest", strongest), ("weakest", weakest)):
                for row in selected:
                    out.append({"extreme": label, **row})
    return out


def _global_augment_rows(
    target: dict[str, Any],
    compare: dict[str, Any] | None,
    *,
    new_aug_ids: set[int],
    aug_meta: dict[int, dict[str, Any]],
    min_picks: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for aid, games in target["aug_games"].items():
        games = int(games)
        if games < min_picks:
            continue
        wins = int(target["aug_wins"][aid])
        rows.append(
            {
                "group": "new" if aid in new_aug_ids else "old",
                "augment_id": aid,
                "augment_name": _augment_name(aid, aug_meta),
                "set": _augment_set(aid, aug_meta),
                "rarity": aug_meta.get(aid, {}).get("rarity", ""),
                "target_picks": games,
                "target_wins": wins,
                "target_wr": round(_pct(wins, games), 2),
                "compare_global_picks": int(compare["aug_games"][aid]) if compare else "",
            }
        )
    rows.sort(key=lambda row: (row["group"], -float(row["target_wr"]), -int(row["target_picks"])))
    return rows


def _write_report(
    path: Path,
    *,
    args: argparse.Namespace,
    target: dict[str, Any],
    compare: dict[str, Any] | None,
    role_rows: list[dict[str, Any]],
    champion_rows: list[dict[str, Any]],
    global_aug_rows: list[dict[str, Any]],
    extreme_rows: list[dict[str, Any]],
    new_aug_ids: set[int],
) -> None:
    lines: list[str] = []
    lines.append(f"# Role / augment analysis {args.patch_prefix}")
    lines.append("")
    lines.append(f"- queue_id: {args.queue_id}")
    lines.append(f"- target_patch_prefix: {args.patch_prefix}")
    if compare is not None:
        lines.append(f"- compare_patch_prefix: {args.compare_patch_prefix}")
    lines.append(f"- target games with participants_json: {target['rows_seen']:,}")
    lines.append(f"- target participant rows: {target['participant_rows']:,}")
    if compare is not None:
        lines.append(f"- compare games with participants_json: {compare['rows_seen']:,}")
        lines.append(f"- new augment definition: present in target patch, absent in compare patch")
    lines.append(f"- new augment ids in target: {len(new_aug_ids)}")
    lines.append("")
    lines.append("## Role WR")
    if compare is not None:
        lines.append("| Role | Target games | Target WR | Compare games | Compare WR | Delta pp |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in role_rows:
            lines.append(
                f"| {row['role']} | {row['target_primary_games']:,} | "
                f"{row['target_primary_wr']:.2f}% | {row['compare_primary_games']:,} | "
                f"{row['compare_primary_wr']:.2f}% | {row['target_minus_compare_pp']:+.2f} |"
            )
    else:
        lines.append("| Role | Target games | Target WR | All-tag WR |")
        lines.append("|---|---:|---:|---:|")
        for row in role_rows:
            lines.append(
                f"| {row['role']} | {row['target_primary_games']:,} | "
                f"{row['target_primary_wr']:.2f}% | {row['target_all_tag_wr']:.2f}% |"
            )

    if champion_rows:
        lines.append("")
        lines.append(
            f"## Champion WR changes, min target {args.min_champion_games} games "
            f"and compare {args.min_compare_champion_games} games"
        )
        for label, selected in (
            ("largest gains", champion_rows[: args.per_group * 2]),
            ("largest drops", list(reversed(champion_rows[-args.per_group * 2 :]))),
        ):
            lines.append("")
            lines.append(f"### {label}")
            lines.append("| Champion | Role | Target raw | Target bayes | Compare bayes | Bayes delta pp | Target games | Compare games |")
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
            for row in selected:
                lines.append(
                    f"| {row['champion_name']} | {row['primary_role']} | {row['target_raw_wr']:.2f}% | "
                    f"{row['target_bayes_wr']:.2f}% | {row['compare_bayes_wr']:.2f}% | "
                    f"{row['bayes_delta_pp']:+.2f} | "
                    f"{row['target_games']:,} | {row['compare_games']:,} |"
                )

    lines.append("")
    lines.append(f"## Global augment top/bottom, min {args.min_global_augment_picks} picks")
    for group in ("new", "old"):
        rows = [row for row in global_aug_rows if row["group"] == group]
        for label, selected in (
            ("strongest", sorted(rows, key=lambda row: float(row["target_wr"]), reverse=True)[: args.per_group]),
            ("weakest", sorted(rows, key=lambda row: float(row["target_wr"]))[: args.per_group]),
        ):
            lines.append("")
            lines.append(f"### {group} {label}")
            lines.append("| Augment | Set | Rarity | Picks | WR |")
            lines.append("|---|---|---:|---:|---:|")
            for row in selected:
                lines.append(
                    f"| {row['augment_name']} ({row['augment_id']}) | {row['set']} | "
                    f"{row['rarity']} | {row['target_picks']:,} | {row['target_wr']:.2f}% |"
                )

    lines.append("")
    lines.append(f"## Per-role strongest/weakest augments, min {args.min_role_augment_picks} role picks")
    for role in [*ROLE_ORDER, "Unknown"]:
        rows = [row for row in extreme_rows if row["role"] == role]
        if not rows:
            continue
        lines.append("")
        lines.append(f"### {role}")
        lines.append("| Extreme | Group | Augment | Set | Picks | WR |")
        lines.append("|---|---|---|---|---:|---:|")
        for row in rows:
            lines.append(
                f"| {row['extreme']} | {row['group']} | {row['augment_name']} ({row['augment_id']}) | "
                f"{row['set']} | {row['target_picks']:,} | {row['target_wr']:.2f}% |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/lcu/games.db")
    parser.add_argument("--payload", type=Path, default=ROOT / "docs/api/tier-list.json")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data/cache")
    parser.add_argument("--queue-id", type=int, default=2400)
    parser.add_argument("--patch-prefix", required=True, help="Target patch prefix, e.g. 16.12")
    parser.add_argument("--compare-patch-prefix", help="Comparison patch prefix, e.g. 16.11")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "outputs/role_augment_analysis")
    parser.add_argument("--min-global-augment-picks", type=int, default=100)
    parser.add_argument("--min-role-augment-picks", type=int, default=30)
    parser.add_argument("--min-champion-games", type=int, default=30)
    parser.add_argument("--min-compare-champion-games", type=int, default=500)
    parser.add_argument("--champion-prior", type=float, default=0.5)
    parser.add_argument("--champion-prior-k", type=int, default=200)
    parser.add_argument("--per-group", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    payload = _load_site_payload(args.payload)
    id_to_alias, id_to_name, id_to_tags = _champion_maps(payload)
    aug_meta = _load_augment_metadata(args.cache_dir, payload)

    target = _collect_patch_stats(
        args.db,
        queue_id=args.queue_id,
        patch_prefix=args.patch_prefix,
        id_to_alias=id_to_alias,
        id_to_tags=id_to_tags,
    )
    compare = None
    if args.compare_patch_prefix:
        compare = _collect_patch_stats(
            args.db,
            queue_id=args.queue_id,
            patch_prefix=args.compare_patch_prefix,
            id_to_alias=id_to_alias,
            id_to_tags=id_to_tags,
        )

    role_rows = _role_summary_rows(target, compare)
    champion_rows = _champion_change_rows(
        target,
        compare,
        id_to_alias=id_to_alias,
        id_to_name=id_to_name,
        id_to_tags=id_to_tags,
        min_target_games=args.min_champion_games,
        min_compare_games=args.min_compare_champion_games,
        prior=args.champion_prior,
        prior_k=args.champion_prior_k,
    )
    role_aug_rows, new_aug_ids = _role_augment_rows(
        target,
        compare,
        aug_meta=aug_meta,
        min_role_augment_picks=args.min_role_augment_picks,
    )
    extreme_rows = _role_extreme_rows(role_aug_rows, per_group=args.per_group)
    global_aug_rows = _global_augment_rows(
        target,
        compare,
        new_aug_ids=new_aug_ids,
        aug_meta=aug_meta,
        min_picks=args.min_global_augment_picks,
    )

    stem = args.patch_prefix.replace(".", "_")
    if args.compare_patch_prefix:
        stem = f"{stem}_vs_{args.compare_patch_prefix.replace('.', '_')}"

    _write_csv(args.out_dir / f"role_summary_{stem}.csv", role_rows)
    _write_csv(args.out_dir / f"champion_wr_changes_{stem}.csv", champion_rows)
    _write_csv(args.out_dir / f"augment_global_{stem}.csv", global_aug_rows)
    _write_csv(args.out_dir / f"role_augment_detail_{stem}.csv", role_aug_rows)
    _write_csv(args.out_dir / f"role_augment_extremes_{stem}.csv", extreme_rows)
    _write_report(
        args.out_dir / f"report_{stem}.md",
        args=args,
        target=target,
        compare=compare,
        role_rows=role_rows,
        champion_rows=champion_rows,
        global_aug_rows=global_aug_rows,
        extreme_rows=extreme_rows,
        new_aug_ids=new_aug_ids,
    )

    print(args.out_dir / f"report_{stem}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
