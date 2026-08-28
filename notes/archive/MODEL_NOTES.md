# Modeling Notes: ARAM / Mayhem Team Composition Learning

Updated: 2026-05-18

> **Archived / superseded:** This file preserves the 2026-05 Mayhem/LCU modeling
> snapshot. The current modeling, data, and experiment policy lives in
> `../../MODEL.md`; `PLAN.md` and `TODO.md` are historical context as well.

## Core Problem

We are not trying to predict single champion strength only.  The target is team
composition understanding:

- champion identity and baseline strength
- conditional synergy
- frontline / backline balance
- damage profile balance
- wave clear, poke, engage, disengage, CC, sustain
- cases where a pair is good only under a third condition

Example intuition:

```text
Caitlyn + Tristana + real frontline = playable / strong
Caitlyn + Tristana + no frontline   = fragile / bad
```

This is why purely linear logistic regression is an important baseline, but not
the final model family we want to stop at.

## Current High-Level Conclusion

Logistic regression remains very strong for this dataset because champion
identity has a large linear signal.  Neural networks start to improve only when
we give them better structured features:

- raw champion identity alone is not enough
- hand-written ability semantics help a little
- empirical LCU combat averages help more
- role and AD/AP/true damage profile help a bit more

The newest score-based NN improves accuracy over LR slightly, but LR still has
better log-loss.  In plain terms: the NN is starting to rank winners better, but
its probabilities are not as cleanly calibrated yet.

The strongest stable gain so far is not a bigger NN.  It is adding explicit
composition signals on top of champion identity: damage mix, frontline, role
mix, poke/frontline interaction, and a few deficit indicators.  Treat these as
small but real corrections around the champion baseline, not as a replacement
for champion identity.

## Data And Leakage Rules

Use time split only.  Never use random split.

For champion average capability stats such as damage, CC, frontline, and
sustain, compute the averages from the training window only when evaluating a
predictive model.  This is not because these averages need huge samples like
win rate.  It is because full-DB averages would let future patch/meta behavior
leak into validation/test.

Current default for empirical capability stats:

- `min_games = 20`
- use only games where all 10 participants have combat stats
- use train-split-only stats inside training scripts
- full-DB merged CSV is only for inspection / sanity checking

Generated data and model artifacts are ignored by git:

- `data/cache/champion_*.csv/json`
- `data/raw/*.parquet`
- `models/*`

## Feature Families

### 1. Champion Identity

Still mandatory.  This captures champion baseline strength and many hidden
factors not yet represented by explicit features.

Representation used by the LR baseline:

```text
blue champion = +1
red champion  = -1
absent        = 0
```

### 2. Ability / Semantic Features

Built from public Data Dragon ability text plus manual review corrections.
These features are subjective, but useful as priors.

As of 2026-05-19, `scripts/build_semantic_ability_scores.py` moved
`engage_score` and `wave_clear_score` away from pure keyword sums toward a
formula-style pipeline:

- per-skill metadata / reviewed overrides
- normalized subcomponents
- weighted aggregation
- champion-level top-skill combination

This is still heuristic, but it is much easier to audit than the earlier
"keyword count plus a few champion overrides" draft.

As of 2026-05-19, the repo also has an explicit intermediate table:

- `data/cache/skill_semantic_features.json`
- `data/cache/skill_semantic_features.csv`

Built by `scripts/build_skill_semantic_features.py`, this is the intended
"skill_semantic_features" layer between raw Data Dragon ability payloads and the
final champion-level semantic scores.  The scoring script now prefers this
table first and only falls back to local heuristics / reviewed overrides when a
field is still missing there.

Current score columns:

- `wave_clear_score`
- `cc_score`
- `engage_score`
- `damage_score`
- `poke_score`
- `sustain_score`
- `frontline_score`

Important interpretation:

- `wave_clear_score`: ability to clear minion waves, mostly AOE damage / cooldown
- `cc_score`: amount and reliability of crowd control
- `engage_score`: ability to start fights, especially long-range hard CC or forced displacement
- `damage_score`: practical damage pressure, later overwritten by empirical stats when available
- `poke_score`: long-range damage pressure before full engage
- `sustain_score`: healing/shielding value, now preferably empirical
- `frontline_score`: practical tanking/space-making, now preferably empirical

Manual ability scores should not be treated as ground truth.  They are priors.
The model may use them, but empirical stats should replace combat columns when
available.

### 3. Empirical LCU Combat Averages

These come from `participants_json.stats` in `data/lcu/games.db`.

Main empirical replacements:

- `damage_score`: based on damage-to-champions share and per-minute output
- `cc_score`: based on `time_ccing_others` share and per-minute CC
- `frontline_score`: based on `total_damage_taken + 0.5 * damage_self_mitigated`
- `sustain_score`: based on healing stats, with teammate-facing healing valued more

Damage profile features:

- `physical_damage_ratio`
- `magic_damage_ratio`
- `true_damage_ratio`

These let the model recognize AD/AP/true-damage balance instead of forcing it
to infer damage type only from champion identity.

Current interpretation from the 2026-05-18 residual study:

- raw AD/AP ratios are useful, but only after controlling for champion identity
- best observed region is roughly `35-45%` team AD share among expected AD+AP
  damage
- very AP-heavy teams (`<35%` AD) and very AD-heavy teams (`>=65%` AD) both
  underperform after champion-baseline correction
- the AP-heavy raw win-rate bump is partly champion baseline; do not conclude
  "more AP is always better"
- true-damage share did not show a useful aggregate signal in this dataset; keep
  it as a feature candidate, but do not build site heuristics around it yet

### 4. Riot Role Tags

Data Dragon role tags are fuzzy, but still useful as soft priors.

Current one-hot role features:

- `role_assassin`
- `role_fighter`
- `role_mage`
- `role_marksman`
- `role_support`
- `role_tank`

Do not rely on these as hard labels.  Champions like Trundle, Maokai, Sion, or
Kog'Maw can play very differently depending on items and augments.

### 5. Sustain Semantics

Ideal priority order for sustain value:

```text
ally heal > ally shield > self heal > self shield
```

Current DB reality:

- existing stored data has `total_heal`
- existing stored data has `total_units_healed`
- existing stored data does not currently contain teammate heal/shield fields
- parser now attempts to capture teammate heal/shield fields if LCU returns them

Current fallback:

- use `total_heal`
- use `total_units_healed` as a weak proxy for teammate-facing healing
- do not treat `damage_self_mitigated` as shielding, because that belongs more
  to tankiness / frontline

Important distinction:

- `total_heal` is an amount of HP restored
- `total_units_healed` is a count-like field of healed units, not a heal amount

## LCU Field Map

Current fields captured from LCU participant stats include:

| Meaning | Stored key | Raw LCU key |
|---|---|---|
| Kills | `kills` | `kills` |
| Deaths | `deaths` | `deaths` |
| Assists | `assists` | `assists` |
| Largest killing spree | `largest_killing_spree` | `largestKillingSpree` |
| Largest multi kill | `largest_multi_kill` | `largestMultiKill` |
| First blood kill | `first_blood_kill` | `firstBloodKill` |
| First blood assist | `first_blood_assist` | `firstBloodAssist` |
| Total damage to champions | `total_damage_dealt_to_champions` | `totalDamageDealtToChampions` |
| Physical damage to champions | `physical_damage_dealt_to_champions` | `physicalDamageDealtToChampions` |
| Magic damage to champions | `magic_damage_dealt_to_champions` | `magicDamageDealtToChampions` |
| True damage to champions | `true_damage_dealt_to_champions` | `trueDamageDealtToChampions` |
| Total damage dealt, all targets | `total_damage_dealt` | `totalDamageDealt` |
| Physical damage dealt, all targets | `physical_damage_dealt` | `physicalDamageDealt` |
| Magic damage dealt, all targets | `magic_damage_dealt` | `magicDamageDealt` |
| True damage dealt, all targets | `true_damage_dealt` | `trueDamageDealt` |
| Largest critical strike | `largest_critical_strike` | `largestCriticalStrike` |
| Damage to turrets | `damage_dealt_to_turrets` | `damageDealtToTurrets` |
| Damage to objectives | `damage_dealt_to_objectives` | `damageDealtToObjectives` |
| Total damage taken | `total_damage_taken` | `totalDamageTaken` |
| Physical damage taken | `physical_damage_taken` | `physicalDamageTaken` |
| Magic damage taken | `magic_damage_taken` | `magicDamageTaken` / `magicalDamageTaken` |
| True damage taken | `true_damage_taken` | `trueDamageTaken` |
| Self mitigated damage | `damage_self_mitigated` | `damageSelfMitigated` |
| Time CCing others | `time_ccing_others` | `timeCCingOthers` |
| Total CC dealt | `total_time_cc_dealt` | `totalTimeCCDealt` / `totalTimeCrowdControlDealt` |
| Heal amount | `total_heal` | `totalHeal` |
| Units healed | `total_units_healed` | `totalUnitsHealed` |
| Gold earned | `gold_earned` | `goldEarned` |
| Gold spent | `gold_spent` | `goldSpent` |
| Minions killed | `total_minions_killed` | `totalMinionsKilled` |
| Neutral minions killed | `neutral_minions_killed` | `neutralMinionsKilled` |
| Turret kills | `turret_kills` | `turretKills` |
| Inhibitor kills | `inhibitor_kills` | `inhibitorKills` / `inhibKills` |

The parser also tries to capture these if present:

- `total_heals_on_teammates`
- `total_damage_shielded_on_teammates`
- `effective_heal_and_shielding`
- `crowd_control_score`

Existing DB rows do not currently contain teammate heal/shield fields.  New
collector runs or `scripts/lcu_backfill.py` over the last visible games may
capture them if the client endpoint returns them.

## Current Experiments

Dataset used for the latest live-snapshot comparison:

- `data/raw/mayhem_lcu_ml_compare_2026_05_20_live.parquet`
- queue: Mayhem `2400`
- patch prefix: `16.10`
- total Mayhem rows exported: `218,969`
- patch `16.10.*` rows used by split: `190,808`
- train rows: `133,566`
- validation rows: `28,621`
- test rows: `28,621`

Latest test results:

| Model | Val Log Loss | Test Acc | Test Log Loss | Notes |
|---|---:|---:|---:|---|
| Constant base rate | 0.6922 | 51.76% | 0.6925 | baseline |
| LR champion identity | 0.6754 | 57.56% | 0.6754 | reference baseline |
| LR + all composition features | 0.6727 | 58.02% | 0.6740 | strongest stable test log-loss |
| DeepSets embedding-only | 0.6766 | 57.22% | 0.6766 | still trails LR |
| DeepSets + 17 score/role/profile features calibrated | 0.6738 | 57.96% | 0.6746 | improved with more data, still below composition LR |
| LightGBM semantic calibrated | 0.6724 | 57.74% | 0.6742 | best validation log-loss; close to composition LR |
| XGBoost semantic calibrated | 0.6785 | 56.65% | 0.6792 | not competitive |
| RandomForest semantic calibrated | 0.6816 | 55.69% | 0.6828 | slow and not competitive |

The 17 static features are:

- 7 capability scores
- 6 Riot role one-hot features
- 3 empirical damage ratios
- 1 healing target proxy (`units_healed`)

Interpretation:

- Explicit composition features now clearly beat champion-only LR on validation
  calibrated log-loss.
- The strongest current tabular model is `LR + all composition features`, but
  the lean `selected core signals` bundle captures almost all of the gain. The
  core signal family is damage mix, frontline, roles, AD/frontline,
  poke/frontline, and role-by-AD interactions.
- Score features still help DeepSets substantially, but the NN probabilities
  remain worse than composition LR on log-loss.
- LightGBM is now the closest tree candidate by validation log-loss, but its
  test log-loss remains slightly behind composition LR on the 2026-05-20 split.
  Treat it as worth retesting, not promoted.

Tree experiments:

- semantic-only features are too weak alone
- LightGBM/XGBoost with champion identity plus semantic/empirical scores are
  close to LR but have not clearly beaten it
- RandomForest with the same champion identity plus semantic/empirical score
  frame is not competitive and is slow enough that it should not be part of the
  routine benchmark unless there is a specific bagging hypothesis
- tree models are still worth revisiting with better cross-validation and more
  mature empirical fields

Tree benchmark on the 2026-05-20 live `16.10` snapshot:

| Model | Val Log Loss | Val Acc | Test Log Loss | Test Acc | Notes |
|---|---:|---:|---:|---:|---|
| LR champion identity | 0.6754 | 57.71% | 0.6754 | 57.55% | reference baseline |
| LightGBM semantic calibrated | 0.6724 | 58.42% | 0.6742 | 57.74% | close to composition LR; needs more validation |
| XGBoost semantic calibrated | 0.6785 | 56.69% | 0.6792 | 56.65% | not competitive |
| RandomForest semantic calibrated | 0.6816 | 55.83% | 0.6828 | 55.69% | slow and clearly worse than boosted trees |

RandomForest was tested in
`models/semantic_tree_16_10_2026_05_20_live_empirical_rf` with the same feature
matrix, swap augmentation, time split, and Platt calibration as LightGBM and
XGBoost.  The calibrated probabilities improved ECE but remained far behind on
log-loss, so this is a deprioritized branch.

Composition LR experiments on the 2026-05-20 live `16.10` snapshot:

| Model | Val Log Loss | Test Acc | Test Log Loss | Notes |
|---|---:|---:|---:|---|
| LR champion identity | 0.6754 | 57.56% | 0.6754 | baseline to beat |
| LR + all composition features | 0.6727 | 58.02% | 0.6740 | best stable tabular gain |

Use validation calibrated log-loss as the primary selection metric.  Test
metrics are only final confirmation; do not pick a candidate because it happened
to improve test accuracy on one split.

Subjective/composition feature takeaways:

- useful core: AD/AP damage mix, frontline count/score, role mix,
  AD/frontline interaction, poke/frontline interaction, role-by-AD interaction
- weak or noisy alone: wave and engage main effects; adding wave to the selected
  core bundle did not improve validation log-loss on the 2026-05-18 live
  snapshot
- still worth testing as interactions: wave with poke, engage with follow-up
  damage, sustain with poke/disengage
- when testing subjective features, add them one family at a time and compare
  validation log-loss; do not ship a large subjective bundle without ablation

## Fresh Benchmark Protocol

When the user asks for a new ML round ("機器學習", "新一輪 ML", "benchmark"),
do not run only the currently strongest NN.  Use this protocol:

1. Run `python scripts/lcu_collector.py metrics`, then export a fresh Mayhem
   queue-2400 parquet from `data/lcu/games.db`.
2. Use one frozen snapshot, one patch prefix, and one `game_creation_ms` time
   split for every model in the comparison.
3. Always include these horizontal baselines: LR champion identity, LR with all
   composition features, score NN, LightGBM, and XGBoost.
4. Pick candidates by validation calibrated log-loss first; use validation
   accuracy/ECE and test calibrated log-loss/accuracy as secondary confirmation.
5. Signal candidates should start from the useful core signal family above
   rather than trying every subjective signal permutation. Add one candidate
   family at a time only when there is a specific hypothesis.
6. Save each run under a new output directory so old model artifacts and fresh
   live-snapshot results cannot overwrite each other.

## Current Scripts

Feature generation:

```powershell
python scripts/fetch_champion_abilities.py
python scripts/build_semantic_ability_scores.py
python scripts/build_empirical_champion_scores.py --db data/lcu/games.db --queue 2400 --patch-prefix 16.10
```

Training:

```powershell
python scripts/train_score_nn.py `
  --data data/raw/mayhem_lcu_ml_compare_2026_05_20_live.parquet `
  --score-csv data/cache/champion_semantic_scores.csv `
  --patch-prefix 16.10 `
  --out models/score_nn_16_10_2026_05_20_live_empirical_roles_profile_sustain_weighted `
  --embed-dim 16 `
  --score-dim 8 `
  --hidden 64 `
  --dropout 0.35 `
  --lr 0.0015 `
  --weight-decay 0.02

python scripts/train_composition_lr.py `
  --data data/raw/mayhem_lcu_ml_compare_2026_05_20_live.parquet `
  --score-csv data/cache/champion_semantic_scores.csv `
  --patch-prefix 16.10 `
  --out models/composition_lr_16_10_2026_05_20_live `
  --feature-set all_composition
```

## Modeling Direction

The next useful step is not just adding more raw columns.  The model needs
better conditional composition features.

High-value conditional patterns:

- high backline damage plus low frontline should be penalized
- high poke plus wave clear is different from poke without wave clear
- engage plus follow-up damage matters more than engage alone
- sustain is more valuable when the team has poke/disengage or extended-fight
  champions
- CC value depends on range, reliability, and whether the team can capitalize
- AD/AP/true damage profile matters when the team is one-dimensional

Possible feature blocks:

```text
team_damage * team_frontline
team_poke * team_wave_clear
team_engage * team_followup_damage
lacks_frontline AND double_marksman
lacks_magic_damage
lacks_cc
high_sustain AND high_poke
```

These are interpretable, cheap, and may help both tree models and NN.

Current production recommendation formula should stay conservative:

```text
recommendation = pair residual fit + small composition correction
```

The composition correction should be capped and explainable.  As of 2026-05-18,
the site uses it for damage mix and lineup deficits, especially AD/AP balance
around the `35-45%` AD target region.  This is intentionally a small correction:
pair history remains the main score, and composition only nudges candidates
when the current 1-4 picked champions are visibly skewed.

## Recommended Experiment Order

1. Keep LR champion-identity baseline as the reference.
2. Rebuild empirical stats after new LCU parser fields accumulate.
3. Add teammate heal/shield fields if they appear in new DB rows.
4. Run time-aware k-fold or expanding-window validation, because single split
   comparisons are noisy.
5. Try LightGBM/XGBoost with champion identity, capability aggregates, role
   aggregates, damage profile, and explicit conditional features.
6. Try smaller/calibrated NN variants:
   - stronger dropout
   - lower embedding dimension
   - temperature scaling
   - multi-seed ensemble
7. Only try attention/team encoders after the tabular baselines are exhausted.

## What Not To Do

- Do not use full-DB empirical averages for evaluation.
- Do not treat Riot role tags as hard truth.
- Do not treat `damage_self_mitigated` as shield.
- Do not assume `total_heal` means ally sustain.
- Do not judge a model only by accuracy.  Track log-loss and calibration.
- Do not rerun old broad pairwise LR as-is unless data/features materially
  change.

## Open Questions

- Does the LCU endpoint ever return teammate healing/shielding for Mayhem, or
  only for some queues / EoG views?
- Are `total_units_healed` values reliable enough as a teammate-heal proxy?
- Does objective/turret damage matter in ARAM/Mayhem, or is it mostly noise?
- Can explicit conditional features close the log-loss gap against LR?
- Will these feature gains survive expanding-window validation?
