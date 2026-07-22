"""Measure Mayhem's augment colour ladder and emit the JS const for site.js.

The lobby shares one colour sequence — 98% of the 10 players in a game see the
same colour in the same round — so the unit of observation is the GAME, and the
sequence is taken as the per-slot consensus colour.

Colours are NOT independent draws: silver never repeats at slot 1→2 (0.00% over
17k games) and prismatic suppresses the next prismatic (~17% vs ~27% base). So
the game samples a whole 4-colour sequence from this joint distribution rather
than rolling each slot on its own marginal.

    python scripts/build_augment_ladder.py --patch 16.14

Paste the printed const over AUGMENT_LADDER in scripts/templates/site.js.
Re-run when a patch visibly changes the drop table; the ladder is a game rule,
not a meta statistic, so it does not need a rebuild every publish.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from aram_nn.gamedata import iter_games  # noqa: E402

SHORT = {"kSilver": "S", "kGold": "G", "kPrismatic": "P"}
MIN_ROWS_PER_GAME = 6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patch", default="16.14", help="patch prefix to measure")
    ap.add_argument("--queue", type=int, default=2400)
    ap.add_argument(
        "--payload",
        type=Path,
        default=REPO / "docs" / "api" / "tier-list.json",
        help="tier-list payload (source of augment id → rarity)",
    )
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    augs = json.loads(args.payload.read_text(encoding="utf-8"))["augs"]
    rarity_of = {int(k): v.get("rarity", "") for k, v in augs.items()}

    seq_games: Counter[str] = Counter()
    agreement: list[float] = []
    skipped = 0

    for g in iter_games(
        queue_id=args.queue, patch_prefix=args.patch, parse_participants=True
    ):
        per_slot = [Counter() for _ in range(4)]
        rows = 0
        for p in g.get("participants") or []:
            a = p.get("augments") or []
            if len(a) != 4:
                continue
            cols = [SHORT.get(rarity_of.get(int(x), ""), "?") for x in a]
            if "?" in cols:
                continue
            rows += 1
            for i, c in enumerate(cols):
                per_slot[i][c] += 1
        if rows < MIN_ROWS_PER_GAME:
            skipped += 1
            continue
        seq = ""
        agree = 0.0
        for i in range(4):
            col, n = per_slot[i].most_common(1)[0]
            seq += col
            agree += n / rows
        seq_games[seq] += 1
        agreement.append(agree / 4)

    total = sum(seq_games.values())
    if not total:
        print("no games with a readable ladder", file=sys.stderr)
        return 1

    print(f"# patch {args.patch}  queue {args.queue}", file=sys.stderr)
    print(f"# {total:,} games ({skipped:,} skipped, too few readable rows)", file=sys.stderr)
    print(f"# mean within-lobby agreement: {sum(agreement) / len(agreement):.1%}", file=sys.stderr)
    for i in range(4):
        c: Counter[str] = Counter()
        for s, n in seq_games.items():
            c[s[i]] += n
        marg = "  ".join(f"{k}={c[k] / total:5.1%}" for k in "SGP")
        print(f"#   slot{i + 1} marginals: {marg}", file=sys.stderr)
    for i in range(3):
        chain: dict[str, Counter[str]] = defaultdict(Counter)
        for s, n in seq_games.items():
            chain[s[i]][s[i + 1]] += n
        for prev in "SGP":
            c = chain[prev]
            n = sum(c.values())
            if not n:
                continue
            nxt = "  ".join(f"{k}={c[k] / n:5.1%}" for k in "SGP")
            print(f"#   P(slot{i + 2} | slot{i + 1}={prev}): {nxt}", file=sys.stderr)

    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {"patch": args.patch, "games": total, "sequences": dict(seq_games.most_common())},
                indent=1,
            ),
            encoding="utf-8",
        )

    # JS const — sequences sorted by weight so the common ladders read first.
    items = seq_games.most_common()
    print(f"    // Measured from {total:,} patch-{args.patch} games "
          f"(scripts/build_augment_ladder.py).")
    print("    const AUGMENT_LADDER = {")
    line = "       "
    for seq, n in items:
        piece = f" {seq}: {n},"
        if len(line) + len(piece) > 96:
            print(line)
            line = "       "
        line += piece
    if line.strip():
        print(line)
    print("    };")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
