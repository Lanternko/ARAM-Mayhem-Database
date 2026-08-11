# Predicting ARAM/Mayhem Match Outcomes from Champion Compositions Using Neural Networks

> **Superseded draft:** 本稿已被 `final_project_report_2026_06_14.md` 取代，只保留早期文字與結果脈絡。

Student: Ko Tzu-Chieh, 61447101S

## Abstract

This project studies whether the outcome of a League of Legends ARAM/Mayhem match can be predicted from champion compositions alone. Each match is represented by the five champions on the blue side and the five champions on the red side, and the target is whether the blue side wins. The main research question is whether champion-composition information contains a measurable predictive signal beyond the blue-side base rate.

Using a local dataset of approximately 0.34M to 0.38M Mayhem matches collected during May 2026, the best current model reaches about 58% test accuracy, compared with an approximately 52% constant blue-side baseline. The result suggests that champion composition is meaningfully predictive, although the task remains inherently noisy because player skill, item builds, augments, in-game decisions, and team coordination are not available as input features.

## 1. Introduction

ARAM and Mayhem are team-based game modes where both teams fight with five champions each. Although individual champion strength matters, the final outcome also depends on team composition: damage balance, frontline availability, crowd control, poke, sustain, engage tools, and synergy between champions.

The goal of this project is to test whether a machine learning model can learn these composition-level signals. Instead of predicting a player's performance or using post-game statistics, the model only uses pre-game champion selections. This makes the task closer to a practical draft-time recommendation problem.

The project has three main objectives:

1. Build a clean match dataset for ARAM/Mayhem outcome prediction.
2. Compare simple baselines, logistic regression, and neural network models.
3. Evaluate whether model performance is significantly better than the blue-side base rate.

## 2. Dataset

The dataset was collected from League Client local APIs because Mayhem queue data is not available through the public Riot Match API. Each record corresponds to one completed match and contains:

- blue-side champion IDs
- red-side champion IDs
- match creation time
- queue ID and patch information
- blue-side win/loss label
- optional participant statistics for feature engineering

To avoid artificial leakage, exact match deduplication is performed by `game_id`, not by champion composition. Champion IDs inside each team are sorted because ARAM has no meaningful lane-position order. The model therefore treats each team as a set of champions rather than an ordered sequence.

The latest raw snapshot checked for this report contains 381,608 Mayhem rows. The main controlled benchmark reported below uses the May 25 snapshot:

| Item | Count |
|---|---:|
| Raw snapshot | 338,804 matches |
| Patch-filtered training rows | 215,406 |
| Patch-filtered validation rows | 46,158 |
| Patch-filtered test rows | 46,158 |
| Champions | 172 |
| Blue-side base rate | about 52% |

The split is time-based rather than random. This is important because random splitting can leak the same patch/meta environment into both training and test sets.

## 3. Methods

### 3.1 Constant Baseline

The simplest baseline always predicts the training split's blue-side win rate. This baseline reaches about 52% accuracy, reflecting the natural blue-side advantage in the dataset.

### 3.2 Logistic Regression Baseline

The first machine learning baseline is logistic regression with champion identity features. Each champion is encoded as:

```text
+1 if the champion is on blue side
-1 if the champion is on red side
 0 if the champion is absent
```

This model is simple and interpretable. It captures individual champion strength and provides a strong reference point for more complex models.

### 3.3 Composition Features

In addition to champion identity, the project adds explicit composition features such as:

- damage profile balance
- frontline score
- poke score
- sustain score
- crowd control score
- role mix
- AD/AP damage ratio
- interaction features such as poke with frontline or role with damage profile

These features are designed to represent team-level properties that are hard to learn from champion IDs alone.

### 3.4 Neural Network Model

The neural network model follows a set-based design. Champion embeddings are summed within each team so that the model is invariant to champion order. The model then compares the blue and red team representations and outputs a predicted probability of blue-side victory.

The best-performing neural model also uses additional score features for each champion, including wave clear, crowd control, engage, damage, poke, sustain, frontline, role tags, and empirical damage-profile features.

## 4. Evaluation Metrics

The main metrics are:

- Accuracy: whether the predicted winner is correct.
- Log loss: whether predicted probabilities are confident and correct.
- Expected Calibration Error (ECE): whether predicted probabilities match observed win rates.

Accuracy alone is not sufficient because a model can be correct more often while giving poorly calibrated probabilities. For a win-rate prediction model, calibration is important: a predicted 60% win probability should correspond to roughly 60% empirical win rate.

## 5. Results

The following benchmark uses the May 25 patch-filtered split described above.

| Model | Test Log Loss | Test Accuracy | Test ECE |
|---|---:|---:|---:|
| Constant blue-side baseline | 0.6924 | 51.91% | 0.0012 |
| Logistic regression, champion identity | 0.6767 | 57.34% | N/A |
| Logistic regression + composition features | 0.6741 | 57.94% | 0.0026 |
| DeepSets neural network | 0.6743 | 57.88% | 0.0194 |
| DeepSets + score features | 0.6724 | 58.27% | 0.0192 |

The best neural network reaches 58.27% test accuracy, which is about 6.36 percentage points above the constant blue-side baseline. With 46,158 test matches, this improvement is much larger than ordinary sampling noise under a simple binomial approximation. Therefore, the result is statistically meaningful, although the exact significance should still be interpreted with caution because matches are not perfectly independent: the same patch, popular champions, repeated player populations, and meta trends can create correlations.

The result is also practically meaningful, but the more intuitive achievement is not just "58% accuracy." The model separates strong compositions from weak compositions. On the held-out test split, teams assigned higher predicted win rates also have higher observed win rates:

| Predicted win-rate bucket | Matches | Mean prediction | Observed win rate | 95% CI |
|---|---:|---:|---:|---:|
| Below 45% | 15,001 | 38.1% | 40.5% | 39.7-41.3% |
| 45-50% | 8,028 | 47.5% | 49.4% | 48.3-50.5% |
| 50-55% | 8,115 | 52.5% | 54.6% | 53.5-55.6% |
| 55-60% | 6,640 | 57.4% | 59.2% | 58.1-60.4% |
| 60-65% | 4,583 | 62.3% | 62.3% | 60.9-63.7% |
| 65% or higher | 3,791 | 68.9% | 71.1% | 69.7-72.6% |

This means that when the final DeepSets+score model predicts a team composition to be around 60-65% win rate, the actual win rate is about 62.3% on unseen matches. When it predicts 65% or higher, the actual win rate is about 71.1%. Therefore, the model is useful as a draft-improvement tool: it does not guarantee a single-game win, but it can identify compositions that are much stronger in expectation.

These bucket results are a final held-out test audit for the final DeepSets+score model. The labels in this table were used only to measure calibration after training, not to train, tune, or choose the model.

## 6. Discussion

The experiments show that champion identity is already a strong signal. Logistic regression improves from the 52% baseline to about 57.3% accuracy, which means much of the predictable structure comes from individual champion strength.

Adding explicit composition features improves performance further. This suggests that team-level properties such as damage balance and frontline availability contain additional information beyond individual champion identity.

The neural network with score features achieves the best accuracy and log loss in the current benchmark. However, its ECE is worse than the composition logistic regression model. This means the neural network ranks outcomes better, but its probability outputs are less cleanly calibrated. For a production recommendation system, calibration may matter as much as raw accuracy.

The current ceiling is also important. Even with a large dataset, predicting match outcomes from champion composition alone is noisy. Missing variables include:

- player skill
- champion familiarity
- item builds
- Mayhem augments
- team coordination
- in-game decisions
- early-game events

Because of these missing factors, an accuracy around 58% is a meaningful result rather than a weak one. If a composition-only model reached an extremely high accuracy, such as above 65%, it would likely indicate data leakage rather than genuine predictive power.

## 7. Limitations

The project has several limitations:

1. The data comes from local League Client APIs and may reflect the Taiwan server population and Mayhem patch environment.
2. The model currently focuses on champion composition and does not fully use player-level or augment-level information.
3. The main split is time-based, but more robust expanding-window validation would provide stronger evidence.
4. Neural network calibration is not yet as strong as the logistic regression composition model.
5. Feature engineering for semantic champion properties still contains heuristic components and should be further validated.

## 8. Conclusion

This project demonstrates that ARAM/Mayhem champion compositions can predict match outcomes better than the blue-side base rate. A constant baseline reaches about 52% accuracy, while the best current model reaches about 58% test accuracy on a large held-out test set. The improvement is statistically meaningful at this dataset size and supports the hypothesis that team composition contains learnable win-rate signals.

The strongest current direction is not simply increasing neural network size. Instead, the best results come from combining champion identity with interpretable composition features and structured neural representations. Future work should focus on stronger calibration, expanding-window validation, augment-level features, and more explicit modeling of conditional team synergies.

## Academic Integrity Note

This project was developed in May 2026 for this course project and has not been submitted or used for any other course.
