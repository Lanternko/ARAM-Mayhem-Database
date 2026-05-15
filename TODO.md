# TODO — aram-winrate-nn

Status as of 2026-05-15, dataset = 27k Mayhem (16.9: 8,334 + 16.10: 18,373).

## Current best (apples-to-apples on 16.10 last-30% test, 2,757 matches)

| Setup | Model | Test Acc | Test Log Loss |
|---|---|---:|---:|
| Both teams visible | **LR combined** | **56.45%** | **0.6809** |
| Both teams visible | DeepSets-reg combined | 55.78% | 0.6854 |
| Solo (single team) | **LR-solo combined** | **54.57%** | **0.6868** |
| Solo (single team) | Residual NN on LR | 54.66% | 0.6869 |

Per-champ signal is essentially saturated by linear models. Pairwise / NN / patch-embed all fall within noise. Tier 1 hypothesis ("comp beats base rate") confirmed ~5-6 pp.

## Validation status

All 6 sanity tests PASS (see `scripts/sanity_tests.py`):
- Antisymmetry, permutation invariance, untrained-near-0.5, label-shuffle (no leak), planted-signal recovery, overfit-tiny.

## High-ROI next steps (do these first)

### 1. Scale data 27k → 50k+
- Continue `snowball-workers` on Mayhem.
- LR plateau is at ~56.5%; estimates of pair effects need ~2× sample to halve SE.
- **Stop criterion**: Mayhem 16.10 reaches 30k single-patch OR signal/noise ratio in anchor-synergy crosses 1.0 (currently 0.4–0.5).

### 2. Bayesian / partial-pooled pair model
- Instead of independent pair estimates, share strength via hierarchical prior.
- Architecture: `champ_strength[a]` + `champ_strength[b]` + `pair_factor[a,b] ~ Normal(0, τ)` with τ learned.
- Implement in `pymc` or `numpyro`. Cheap fit (<1min on this data).
- Goal: identify which pairs are *actually* synergistic above noise.
- File target: `scripts/bayes_pair_model.py`.

### 3. K-fold time-aware validation
- Single split → noisy comparisons (saw +0.22pp "improvements" that are likely noise).
- Use 5 expanding-window folds over time.
- Re-rank current model leaderboard with 5-fold CV mean ± std.
- File target: `scripts/kfold_eval.py`.

## Mid-ROI

### 4. Multi-seed ensemble
- DeepSets-reg: train 5 seeds, average probabilities.
- Likely +0.2 pp test acc, mainly variance reduction.

### 5. Anchor-synergy refresh for top-WR anchors only
- `scripts/anchor_synergy.py` works; useful as a stats tool, not a model.
- Restrict to anchors with ≥1,500 anchor-games (Yasuo, Lillia, Vayne, Jinx, Brand, etc.).
- Output a per-anchor markdown report.

### 6. Cross-patch + patch-embed when ≥4 patches accumulate
- Currently only 2 useful patches (16.9, 16.10) → patch-embed adds noise.
- When 16.11 + 16.12 reach 5k+ each, retry `train_tier2.py` and `scripts/compare_combined.py`.

## Productization

### 7. LR-solo as pick-advisor function
- Input: 5 champion IDs (your rolled candidates + reroll options).
- Output: P(win | random opponent), per-champ marginal value for swap decisions.
- Bundle: `models/pick_advisor.pkl` + `scripts/pick_advisor.py` CLI.

### 8. Tier-list cron / weekly refresh
- `scripts/tier_list.py` already works.
- Schedule: when DB grows by ≥1k games, regenerate `models/tier_list_solo_latest.csv` + diff vs last week.

## Verified DEAD ENDS (don't redo without new context)

- **Tier 1 DeepSets (default params)** — overfits; reg version is the only useful one.
- **Patch embedding at 2 patches** — adds noise > signal. Revisit at 4+ patches.
- **Pairwise LR (172 + 14,706 features)** — best C collapses all pair weights to 0; no acc gain. Revisit at 50k+.
- **Token-masking SSL** — pretext task ("predict masked champion") learns co-occurrence ≠ win signal. Target misaligned.
- **Apex / ladder / riot-tier seed families** — verified dead (per CLAUDE.md, 0 transitive captures).

## Verified IRRELEVANT for our use case

- **Augment features** — augments unknown at pick time, so excluded.
- **Riot API queueId=2400** — Riot blocked, LCU-only.

## Datasets / artifacts

- `data/raw/mayhem_27k.parquet` — current frozen export.
- `data/lcu/games.db` — live DB (~27k games, growing).
- `models/tier_list_solo_27k.csv` — current 172-champion tier list.
- `models/tier2_mayhem_27k/`, `models/tier2_mayhem_27k_reg/` — Tier 2 checkpoints (NOT recommended for prod; LR-combined wins).
- `scripts/sanity_tests.py` — regression test for pipeline correctness; run before any architecture change.

## Theoretical ceiling estimate

| Quantity | Value | Source |
|---|---:|---|
| Base rate (blue WR) | 51.6% | data |
| Solo (per-champ only) achieved | 54.57% test acc | LR-solo combined |
| Both-teams (per-champ relative) achieved | 56.45% test acc | LR combined |
| Per-champ contribution | +4.6 pp | solo − base |
| Matchup (relative) contribution | +1.9 pp | combined − solo |
| Estimated remaining (synergy + counter + skill) | ~3–9 pp | back-of-envelope |
| Skill-cap ceiling (random matchmaking) | ~60–65% | literature for similar tasks |

**Implication**: even a perfect synergy/matchup model probably caps at ~60% test acc on this data without additional features (player skill, account history, etc., which we can't access).
