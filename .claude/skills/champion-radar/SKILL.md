---
name: champion-radar
description: "Rebuild the per-role champion strength radar (docs/champion-radar.html) and the role×axis→win-rate formula from data/lcu/games.db. Each champion gets a 6-axis radar (輸出/坦度/控場/續航/清線/參戰) ranked WITHIN its role; radar area ∝ win rate. Use when the user wants to recompute / update / rebuild the champion ability radar, the role strength formula, the per-role win-contribution weights, or asks which axes each role should care about. Also covers PLAYER-OVR mode (§6) — scoring one player's own games into a per-role OVR + FIFA-style hexagon (signed cost-criteria + base-floor knob). Triggers on: 重算英雄雷達, 更新雷達, 英雄能力雷達, 職業強度公式, 職業×軸勝率, 各職業看什麼軸, 雷達公式, 重建 champion radar, rebuild champion radar, role winrate formula, per-role strength axes, champion ability radar, recompute strength radar, 球員卡, 玩家 OVR, 我的 OVR, role OVR card, player OVR, /champion-radar. Do NOT trigger for the main tier-list site (that's deploy-tier-list) or for the heuristic semantic ability scores (that's build_semantic_ability_scores.py)."
metadata:
  version: "1.1"
  last_updated: "2026-06-30"
  status: active
  scope: project
---

# champion-radar — Per-role strength radar & role×axis→win-rate formula

One radar per champion, calibrated so **bigger radar = higher win rate**. The
whole pipeline is `scripts/build_champion_radar.py` → `docs/champion-radar.html`
(self-contained: inline JSON + SVG radars, 米色 style matching
`semantic-score-review.html`). This skill is the formula of record.

**Source of truth**: `data/lcu/games.db` queue 2400 (Mayhem), ~1.07M games.
**Cache**: `data/cache/champion_radar_dims.json` — per-champion aggregated dims.
Editing only the HTML? It reads the cache, so a rebuild is seconds, not minutes.

## The formula (this is what the skill encodes)

### 1. Six axes — per-minute volumes from each player's `stats`
| 軸 | 算式 (per game-minute, summed then ÷ total minutes) |
|---|---|
| 輸出 | `total_damage_dealt_to_champions` |
| 坦度 | `total_damage_taken + damage_self_mitigated` |
| 控場 | `total_time_cc_dealt` |
| 續航 | `total_heal` |
| 清線 | `total_damage_dealt − total_damage_dealt_to_champions` (傷害打在非英雄目標) |
| 參戰 | `kills + assists` |

Win = `(teamId==100) == bool(blue_wins)`. Champion win rate ≈ clean average
win-contribution because ARAM randomises teammates (they average out).

### 2. WITHIN-ROLE percentile — the load-bearing choice
Each axis is percentile-ranked **inside the champion's own role** (tankiness vs
other tanks, damage vs other mages), NOT the whole pool. This is why a support's
radar isn't crushed for having low absolute damage. Role = first tag from
`champion_semantic_scores.csv` `tags`.

### 3. Role×axis weights — axes are ROLE-CONDITIONAL
For each role, `weight[axis] = max(0, spearman(within_role_pctile_axis,
win_rate))`, then normalised to sum 1. Clipping at 0 enforces "bigger = better"
(champion mode; to SCORE A PLAYER keep the sign — negatives become penalties, see §6).
This is the core empirical finding: an axis helps some roles and not others
(measured 2026-06-29, will drift per patch — the script recomputes every run):

| 職業 | 主看軸 (正貢獻) | 反指標 |
|---|---|---|
| 坦克 Tank | 坦度 +0.59 · 參戰 +0.43 | 控場 −0.38 |
| 輔助 Support | 參戰 +0.53 · 清線 +0.36 | 坦度 −0.32（別當肉）|
| 法師 Mage | 經濟/輸出 +0.50 · 清線 +0.36 | 坦度 −0.16 |
| 鬥士 Fighter | 輸出 +0.50 · 清線 +0.43 | — |
| 射手 Marksman | 清線 +0.28 · 續航 +0.21 | 控場 −0.18 |
| 刺客 Assassin | (n=17, 全軸偏高=反向因果，別當真) | — |

### 4. Strength score & radar
`strength = Σ weight[role][axis] · within_role_pctile[axis]`. Drives sort order
and radar fill colour (win-rate diverging: 綠 high / 橘 low). Radar polygon
plots the 6 within-role percentiles.

### 5. Role reverse-inference (定位不符偵測)
Logistic `role ~ play-pattern axes`, 5-fold cross-val predict (≈78% acc). A
champ predicted a role NOT in its tags = ★ flagged (Gangplank→射手 99%,
Pyke→刺客 94%). Distinguishes mislabel from confirmed secondary tag.

### 6. Player-OVR mode — negative axes are cost criteria
The champion radar CLIPS negative weights to 0 (a champion isn't scored down for an
anti-axis it happens to rank high on — strength is "bigger = better"). But to score a
PLAYER (an OVR over their own games vs the same-champion average), a negative axis SHOULD
penalise — a tanky support is genuinely worse. Treat it the way MCDA treats a **cost
criterion** ("lower is better"):
- **Signed weights, not clipped**: `w = rho` when `|rho| ≥ ε` (ε≈0.15 noise floor — the
  per-role spearman is over only ~16–45 champs, SD≈0.2, so smaller |rho| is noise), else 0.
  Cost axes keep their negative sign.
- **Normalise by Σ|w|** (benefits + costs share one budget), then **centre at 50**:
  `roleOVR = 50 + Σ w·(axisOVR − 50)`. All-average player → 50; above-average on a benefit
  adds, above-average on a cost subtracts. `totalOVR` = games-weighted mean of roleOVRs.
- **axisOVR** = player per-min axis ÷ same-champion pool mean, mapped
  `50 + 49·tanh(1.6·(ratio−1))`, clamp 20–99 (50 = league-average player on that champ).
- **Proxy gate**: 控場 may NEVER be a penalty — `total_time_cc_dealt` is duration-biased
  (soft-cc inflated), so the negative tank (−0.38) / marksman (−0.18) CC weights are
  measurement artifacts, not "too much CC loses". Only credible negatives penalise
  (currently just 輔助 坦度 −0.32). Block CC as a cost axis.
- **Optional base floor (every-axis mode)**: pure signed weights leave 3–4 axes at 0 for some
  roles, so they vanish from any chart. To make all six count, blend a designer floor with the
  data: `|w_a| = floor·(1/6) + (1−floor)·(|rho_a| / Σ|rho|)`, sign from the data (proxy axes get
  the floor only, no data magnitude). `floor` trades discrimination for completeness — 0 = pure
  measured (default), 1 = equal hexagon, **~0.3 = all six live with ranking ≈ intact**. OVRs
  compress toward 50 as floor rises (it's then more a *designed* rating than a measured one).
- **Chart**: never a plain radar at floor 0 — centre = 0 can't show the negative axes and some
  axes are absent. Pure-data → **diverging variwide bars** (benefit up, cost down/red). Floored
  (all six non-zero) → an equal-angle **hexagon** works again: plot the six axisOVRs (FIFA-style
  profile), spoke width = |w|, anti-axes red, OVR number = the weighted strength (hexagon AREA is
  the profile, NOT strength — the number is the truth). No `build_player_ovr.py` in-repo yet; spec only.

## Command

```powershell
python scripts/build_champion_radar.py        # full: aggregate → cache → HTML
# flags: --db data/lcu/games.db  --out docs/champion-radar.html
#        --semantic data/cache/champion_semantic_scores.csv  --min-games 1000
#        --cache data/cache/champion_radar_dims.json  (delete to force re-aggregate)
```

First run streams ~1M games (1–2 min) and writes the cache; later runs reuse it
(seconds). Delete the cache after collecting new games to re-aggregate.

## When to invoke

Recompute/rebuild the champion radar or role-strength formula; "which axes does
role X care about"; refresh after new games. Explicit `/champion-radar` always.
**Don't** trigger for the main tier list (deploy-tier-list) or the hand-set
semantic scores (build_semantic_ability_scores.py).

## Validation (the radar is right if)

- Weighted strength vs win rate Pearson r ≈ 0.57; within-role-area vs WR r ≈ 0.46.
- Bottom-quartile radar → ~80% below median win rate ("小不點通常很爛").
- Sanity champs: 賽恩 big balanced radar 55.5%; 李星 tiny 42.8%; 索娜 high 參戰/續航 low 輸出.

## NEVER

- **Never use a single whole-pool weight set** — axes are role-conditional
  (坦度 +0.59 for tanks, −0.32 for supports). Pooling washes them to ≈0 and the
  radar wrongly judges supports/tanks. Always per-role.
- **Never rank axes on whole-pool percentile** — use within-role, or a support's
  radar collapses for low absolute damage that's irrelevant to being a good support.
- **Never read "outcome" volumes (gold/KDA/total damage) as a win CAUSE** — they
  hit CV R²≈0.33 but it's reverse causality (won → did more). Controllable axes
  cap at R²≈0.06–0.08 — champion TYPE barely predicts WR, "how strong it's tuned" does.
- **Never trust the objective proxy to "correct" hand scores beyond damage** —
  CC uses duration (favours soft-cc), `total_heal` excludes shields, 清線 counts
  auto-attacks. Only `damage_score` saturation (full 3.0 on supports/tanks) is a
  clean hand-error.
- **Never over-trust SMALL weights for 刺客/輔助** — 刺客 (n≈18 champs, strong reverse
  causality: every axis reads high) is direction-only; 輔助 (n≈16) is small but its TOP
  weights reproduce (參戰 +0.53, 坦度 −0.32) — trust those, treat any |weight| < 0.2 as noise.
- **Never hand-edit docs/champion-radar.html** — build artifact; edit the script.
- **Never read a clipped-0 weight as "neutral"** — 0 can mean genuinely uncorrelated
  (傷害 for 輔助, rho ≈ 0) OR a real negative that `max(0,·)` hid (坦度 for 輔助, −0.32).
  They are different answers; check the SIGNED rho before saying an axis "doesn't matter".
- **Never let 控場 act as a penalty axis** (player-OVR / signed mode) — `total_time_cc_dealt`
  is duration-biased, so the negative tank/marksman CC weights are proxy artifacts, not a
  real "too much CC loses". Block CC as a cost axis (§6 proxy gate); other negatives are fine.
- **Never publish to Pages from here** — this builds locally; deploying is the
  deploy-tier-list flow (stage only the intended files, never `git add -A`).
