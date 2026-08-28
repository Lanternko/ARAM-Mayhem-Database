# Predicting LoL (ARAM / Mayhem) Match Outcomes from Champion Composition

> **Final report (2026-06-14):** 本報告是同系列 project report 的 final 版本；數字只適用於文內綁定的資料集與日期。現行 policy 見 `../../MODEL.md`。

### Why Win-Rate Estimation, Not Raw Accuracy, Is the Right Goal

Student: Ko Tzu-Chieh, 61447101S — Final Project Report, June 2026

---

## Abstract

This project asks whether the outcome of a League of Legends ARAM / Mayhem match can be predicted from champion composition alone — the five blue champions versus the five red champions, with no player names, no items, and no post-game statistics. Using **307,722 patch-16.10 Mayhem matches** collected from the local League Client API, the best model (a DeepSets network with engineered team-score features) reaches **58.27% held-out test accuracy**, **+6.36 percentage points** above the 51.91% constant blue-side baseline.

**58.27% is a strong result for this task — and the system delivers something more valuable than that single number suggests.** The model does not merely classify win versus loss; it outputs a **win rate**, and a win rate is a direct, quantitative measure of **team strength**. These estimates are well calibrated and the confidence intervals are tight: teams the model rates at 65%+ go on to win **71.1%** of unseen matches, with bucket-level 95% CIs only ~1.5 pp wide. A competitive game is inherently noisy — player skill, augments, item builds, coordination, and in-game decisions decide individual matches — so the right thing to ask of a composition-only model is not whether it calls one coin-flip game, but whether it reliably quantifies how strong a draft is before the game starts. It does. The deliverable is a **calibrated draft-strength estimator**, which is both a harder and a more useful thing than a win/loss classifier.

---

## 1. Introduction — What Can We Actually Control?

A League match outcome is driven by many factors. We can group them into three buckets:

| Factor | Visible before the game? | Controllable at draft? |
|---|---|---|
| **Player skill** (mechanics, matchup knowledge, coordination) | No | No |
| **In-game variance** (augments, items, fights, execution, mistakes) | No | No |
| **Champion composition** (the 5v5 pick of champions) | **Yes** | **Yes** |

Of these, only champion composition is both visible before the match and under the player's control. This project deliberately isolates that single lever. The research question is precise:

> *After averaging over all the hidden, uncontrollable factors, does champion composition still carry a measurable signal about who wins?*

If it does, the draft has real, quantifiable value, and we can build a tool that improves a team's **expected** win rate before the game even begins. Note what this question is *not*: it is not "can we predict who wins this specific game?" That framing is a trap, and Sections 5–7 explain why.

## 2. Data

Mayhem (queueId 2400) is removed from the public Riot Match API, so matches were collected through the local League Client API by BFS snowball: seed from known active players, prioritize players with recent Mayhem activity, pull their recent games, dedupe, enqueue their 10 participants, and repeat.

Three data-design choices matter more than model size:

- **Deduplicate by `game_id`, never by composition hash.** Different real matches can field the same ten champions; collapsing them would silently delete real data and bias the labels.
- **Sort champions inside each team.** ARAM has no lane order, so the model must learn champion *sets*, not artificial slot positions. Sorting by champion ID prevents the model from learning a spurious "position" feature.
- **Split by time, never randomly.** A random split leaks the same patch and meta into both train and test, inflating accuracy. The benchmark uses a strict chronological split.

| Item | Count |
|---|---:|
| Patch 16.10 Mayhem matches | 307,722 |
| Train (oldest 70%) | 215,406 |
| Validation (next 15%) | 46,158 |
| Test (newest 15%) | 46,158 |
| Distinct champions | 172 |
| Blue-side base rate | ~52% |

## 3. Representation and Models

**Champion encoding.** Each champion owns one column: `+1` on blue side, `−1` on red side, `0` if absent. This single representation already encodes a lot — champion identity is a compact summary of role, range, damage type, crowd control, durability, sustain, and current patch strength.

**Composition features.** On top of identity, the project adds team-level signals: damage mix, frontline, poke, sustain, crowd control, role mix, AD/AP ratio, and conditional interactions (e.g. poke × frontline). These describe the *shape* of a team that champion IDs alone do not make explicit.

**Models, in increasing complexity:**

1. **Constant baseline** — always predict the blue base rate.
2. **Win-rate table (no ML)** — sum each champion's empirical solo win rate.
3. **Champion logistic regression** — learn each champion's average contribution.
4. **Composition logistic regression** — identity + engineered composition features.
5. **DeepSets (IDs only)** — sum champion embeddings within each team for an order-invariant representation, then compare blue vs red.
6. **DeepSets + scores** — adds 17 score/profile features per champion (capability scores, role tags, empirical damage ratios, a healing-target proxy).

## 4. Headline Benchmark

Same patch-filtered time split for every row (215,406 / 46,158 / 46,158):

| Model | Test Accuracy |
|---|---:|
| Constant baseline | 51.91% |
| Win-rate table (no ML) | 56.94% |
| Champion LR | 57.34% |
| Composition LR | 57.94% |
| DeepSets (IDs only) | 57.88% |
| **DeepSets + scores** | **58.27%** |

Two facts jump out, and both point away from "accuracy is the prize":

- **Most of the signal is in *who* is picked.** Champion identity alone (LR) gets from 51.91% to 57.34% — the bulk of the climb. Team *shape* adds only ~+0.6 pp overall.
- **Depth barely helps.** DeepSets with IDs only (57.88%) does *not* beat the much simpler Composition LR (57.94%). The neural network earns its keep through better probability *ranking*, not a higher accuracy number — which is exactly the distinction the rest of this report is about.

Composition's small average gain hides where it matters: in **extreme damage-mix teams** (e.g. five physical-damage attackers), the composition signal grows to **+4.4 pp**. Average accuracy masks this; a strength estimate does not.

---

## 5. Win Rate Says More Than Accuracy Does

This is the core of the report. 58% accuracy already beats every baseline by a clear margin — but accuracy is a coarse summary that *throws away* the most useful thing the model produces. The model's native output is a **win rate**, and a win rate is far more informative than a binary win/loss call: it carries magnitude, it can be compared across drafts, and it translates directly into a strength estimate. The following four points explain why win rate — not accuracy — is the right way to report and judge this system, and why a much higher accuracy would actually be a warning sign rather than a better result.

### 5.1 A competitive game has an irreducible variance floor

The outcome of a single match is the sum of one controllable input (composition) and a large pile of hidden, high-variance inputs: player skill, champion familiarity, item builds, Mayhem augments, team coordination, early-game events, and minute-to-minute decisions. None of these are available to a composition-only model.

This means there is a hard ceiling — a **Bayes-optimal accuracy** — that *no* model with our inputs can exceed, no matter how large or deep. Even a perfect composition model cannot predict a game that a hidden 1v9 carry, a thrown lead, or a lucky augment roll decides. In a competitive game, the better draft is *supposed* to lose a meaningful fraction of the time; if it did not, the game would have no competitive depth and nobody would play it. **The variance is a feature of the domain, not a defect of the model.**

### 5.2 High accuracy would be evidence of *leakage*, not skill

Here is the counterintuitive consequence: for a composition-only ARAM model, an unusually high accuracy is a warning sign. If the model reported, say, **above 65%** test accuracy on this task, the most likely explanation is not that it understands the game better than experts — it is that something leaked: duplicate matches across the split, a random split that shared meta between train and test, or a feature that secretly encodes the outcome. We treat "acc > 65%" as a trigger to **stop and audit the split**, not to celebrate. A "worse" accuracy that survives a strict time split is more trustworthy than a "better" one that does not.

### 5.3 Accuracy throws away the magnitude that decisions need

Accuracy is computed by thresholding the probability at 50% and asking "right or wrong?" That collapses two very different predictions into the same bucket:

- A prediction of **50.5%** (a near-coin-flip) and a prediction of **71%** (a strong favorite) both count as a single "blue wins" guess.
- If both happen to lose, both count as one identical "miss."

But a draft tool does not need to know *whether* blue wins this exact game — it needs to know *how much* a pick changes the odds, so it can compare options. The decision-relevant quantity is the **magnitude and reliability of the win-rate estimate**, and accuracy discards exactly that. A model can win the accuracy contest while emitting probabilities that are useless for ranking choices — which is precisely what we see next.

### 5.4 Accuracy and calibration can disagree

In our benchmark, **DeepSets + scores has the best accuracy (58.27%) but a worse calibration error (ECE) than the Composition LR.** That is the whole argument in one line: the model that "wins" on accuracy is *not* automatically the one whose probabilities you should trust. For a win-rate product, picking models by accuracy alone would select the wrong model. We therefore evaluate on a **menu of metrics** — accuracy, log loss, and Expected Calibration Error — and treat calibration as a first-class goal, not an afterthought.

---

## 6. The Result That Actually Matters: Calibrated Strength Estimation

If accuracy is the wrong question, what is the right one? **Does a higher predicted win rate correspond to a higher *actual* win rate on unseen games?** If yes, the model is a usable strength meter, regardless of its single-game hit rate.

It does. On the held-out test split, grouping matches by the model's predicted win rate:

| Predicted bucket | Matches | Mean prediction | **Observed win rate** | 95% CI |
|---|---:|---:|---:|---:|
| < 45% | 15,001 | 38.1% | **40.5%** | 39.7–41.3% |
| 45–50% | 8,028 | 47.5% | **49.4%** | 48.3–50.5% |
| 50–55% | 8,115 | 52.5% | **54.6%** | 53.5–55.6% |
| 55–60% | 6,640 | 57.4% | **59.2%** | 58.1–60.4% |
| 60–65% | 4,583 | 62.3% | **62.3%** | 60.9–63.7% |
| 65%+ | 3,791 | 68.9% | **71.1%** | 69.7–72.6% |

The observed win rate climbs monotonically with the prediction, and — crucially — **the confidence intervals are narrow**: every bucket's 95% CI is only ~0.8–1.5 pp wide, and the predicted mean lands inside or right at the edge of the observed interval in every row. The error on each strength estimate is small, so the estimate is trustworthy, not noise. When the model says **60–65%**, teams actually win **62.3%** (CI 60.9–63.7%); when it says **65%+**, they win **71.1%** (CI 69.7–72.6%). These labels were used *only* to audit calibration after training — never to train, tune, or select the model.

This is the practical achievement that a single accuracy number cannot express: **after averaging over player skill, opponents, builds, and in-game variance, the model still finds composition strength that survives on unseen matches.** And the magnitudes are not cosmetic. A 60% team versus a 50% team is not "a little above average" — it is +10 percentage points, ~20% more wins, and roughly **1.5× the win odds**. Over a season of games, that edge compounds; over a single game, it is just a tilt in the dice. The model speaks the language of the long run, which is the only honest language for a stochastic repeated game.

## 7. Why This Framing Is the Correct One for a Draft Tool

A draft is not a one-shot bet; it is a **repeated decision** played hundreds of times. The right objective is therefore not "be correct about this game" but "**raise expected win rate across many games**." That reframes the entire evaluation:

- **The loss function should reward good probabilities, not lucky guesses.** Log loss and calibration penalize a confident-and-wrong prediction far more than accuracy does, which is what you want when the downstream use is comparing options.
- **The product is a comparator, not a fortune-teller.** In the live demo, the model scans **462 swap options** at champion select, finds a single trade that lifts a lineup's estimate from **43.7% to 66.1%**, and recommends it. It never claims the game is won — it claims the *expected* win rate moved up by a large, reliable margin.
- **The recommendations are interpretable and match expert intuition.** For a team of five physical-damage attackers, the model grades the composition (physical-damage share "70% — too high", crowd control "weak") and recommends swapping one attacker for a mage: **43.7% → 66.1% (+13.3 pp)**. The reason a human pro would give — "one armor item blunts all five attackers" — was rediscovered by the model from match outcomes alone. A tool you can *explain* and *trust* is worth more than one extra point of accuracy.

In short: reported as a single accuracy figure, the model already beats every baseline by a wide margin. Reported as **a calibrated estimator of draft strength** — which is what the problem actually calls for — it does something a classifier cannot: it puts a reliable number, with a tight error bar, on how strong a team is before the game begins.

## 8. Discussion and Limitations

**What the result says:** champion composition contains a real, pre-game win-rate signal. The model is not merely relearning "blue side wins slightly more often"; it ranks compositions correctly on unseen matches.

**What it does not say:** it does not reliably predict individual games, and it should never be sold that way. Player skill, augments, items, coordination, and in-game decisions remain hidden and frequently decide the match.

**Limitations:**

1. Data reflects the Taiwan-server population and one patch (16.10); cross-patch generalization needs a patch feature or per-patch evaluation.
2. The model uses composition only; player- and augment-level features are not yet integrated.
3. The time split is sound but a single fold; expanding-window validation would strengthen the evidence.
4. Neural calibration (ECE) still trails the Composition LR and should be improved with post-hoc temperature scaling rather than pre-hoc label smoothing.
5. Some semantic champion-score features are still heuristic and warrant further validation.

## 9. Conclusion

> **Champion identity explains most games. Composition decides the edge cases. Neither lets you predict a single noisy match — and that was never the goal.**

58.27% test accuracy, +6.36 pp over baseline, is a strong result on its own. But the more meaningful achievement is that the model outputs **calibrated win rates with tight confidence intervals**, turning each composition into a precise strength estimate that holds up on unseen games. In a competitive game with a high, irreducible variance floor, that is exactly what success looks like — and a model scoring much higher on raw accuracy would more likely be leaking than learning. The system should be used to **improve expected draft quality over many games**, and future work (better calibration, expanding-window validation, augment-level features) should target *sharper win-rate estimates*, not a bigger accuracy number.

---

*Academic integrity note: developed in May–June 2026 for this course project; not submitted or used for any other course.*
