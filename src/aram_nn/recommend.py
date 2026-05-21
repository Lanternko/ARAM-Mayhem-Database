"""Real-time ARAM champion recommendation from composition LR swap deltas.

Why LR and not DeepSets:
  At current data scale (~18k games, 2 patches) the LR baseline outperforms
  the DeepSets NN on classification (test acc 55.86% vs 52.72%, see
  models/tier2_mayhem/summary.json).  LR remains useful as a universal
  champion-strength prior, but same-team pair stats now dominate ranking.

Why opponent visibility doesn't matter for the LR component:
  ARAM champ select hides the opposing team's champions.  But the LR encoding
  is logit = Σ_{c∈blue} w_c − Σ_{c∈red} w_c + b, so swapping my own pick
  Y → X changes the logit by exactly (w_X − w_Y).  The unknown red-team
  contribution cancels out entirely.  Pair synergy is computed only from the
  visible ally anchors, so it also does not require opponent visibility.

Absolute probability assumes "average opponent":
  We set the red-team contribution to 0 in the feature vector.  Since LR was
  trained with +1/-1 encoding and L2 regularization, mean coefficient ≈ 0,
  so this is a reasonable point estimate (not a posterior).  The number is
  decorative; the blended score is the load-bearing output.
"""
from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from aram_nn.pair_synergy import PairSynergyStats

try:
    from scripts.champion_roles import ROLE_ORDER
except ImportError:  # pragma: no cover - packaged runtime fallback.
    ROLE_ORDER = ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank")

SCORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
    "poke_score",
    "sustain_score",
    "frontline_score",
)
CORE_COLUMNS = ("wave_clear_score", "cc_score", "engage_score", "damage_score")
LACK_THRESHOLDS = {
    "wave_clear_score": 3.0,
    "cc_score": 3.0,
    "engage_score": 2.2,
    "damage_score": 5.5,
    "poke_score": 2.0,
    "sustain_score": 1.5,
    "frontline_score": 1.8,
}
ROLE_COLUMNS = tuple(ROLE_ORDER)
AD_BINS = ("<35% AD", "35-45% AD", "45-55% AD", "55-65% AD", ">=65% AD")
FRONT_GROUPS = ("0 front", "1 front", "2+ front")
ENGAGE_GROUPS = ("engage lack", "engage ok")
WAVE_GROUPS = ("wave lack", "wave ok")
POKE_GROUPS = ("poke lack", "poke ok")


@dataclass
class LRModel:
    """Logistic Regression weights + champion vocab.

    Stores plain numpy arrays so inference doesn't touch sklearn at runtime.
    This matters because pulling in sklearn -> scipy can crash during import
    on Python 3.13 (scipy.spatial.distance fails inside @dataclass
    construction with MemoryError) and even when it succeeds it adds 30+s
    of cold-start latency.

    coef_mean / coef_std are precomputed so the recommender can show each
    champion's strength as a z-score in the current meta — much more
    intuitive than P(win) for a 51-55% base-rate game where absolute
    probabilities all look similar.
    """
    coef: np.ndarray             # shape (n_champs,)
    intercept: float
    champ_to_idx: dict[int, int]
    n_champs: int
    coef_mean: float = 0.0
    coef_std: float = 1.0

    def z_score(self, champ_idx: int) -> float:
        """Standardized champion strength: (w - mean(w)) / std(w)."""
        if self.coef_std <= 1e-12:
            return 0.0
        return float((self.coef[champ_idx] - self.coef_mean) / self.coef_std)


@dataclass(frozen=True)
class ChampionProfile:
    cid: int
    scores: dict[str, float]
    roles: dict[str, float]
    physical_dpm: float
    magic_dpm: float
    true_dpm: float


@dataclass(frozen=True)
class TeamProfile:
    ad_share: float
    true_share: float
    ad_ap_balance: float
    front_count: int
    front_sum: float
    score_sums: dict[str, float]
    lacks: dict[str, float]
    roles: dict[str, float]
    core_lacks_count: float
    all_lacks_count: float


@dataclass
class CompositionLRModel:
    """Champion-identity + team-composition LR used for live swap deltas."""

    coef: np.ndarray
    intercept: float
    feature_names: list[str]
    champ_to_idx: dict[int, int]
    profiles: dict[int, ChampionProfile]

    def __post_init__(self) -> None:
        self.feature_to_idx = {name: idx for idx, name in enumerate(self.feature_names)}

    def predict_team_prob(self, team_ids: Iterable[int]) -> tuple[float, list[int]]:
        contribution, unknown = self.team_logit_contribution(team_ids)
        if unknown:
            return float("nan"), unknown
        logit = float(contribution + self.intercept)
        return float(_sigmoid(logit)), []

    def team_logit_contribution(self, team_ids: Iterable[int]) -> tuple[float, list[int]]:
        x, unknown = _build_composition_feature_vector(team_ids, self)
        if unknown:
            return float("nan"), unknown
        return float(x @ self.coef), []


def _sigmoid(x: float | np.ndarray) -> float | np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _logit_prob(p: float) -> float:
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _ad_bin_index(ad_share: float) -> int:
    if ad_share < 0.35:
        return 0
    if ad_share < 0.45:
        return 1
    if ad_share < 0.55:
        return 2
    if ad_share < 0.65:
        return 3
    return 4


def _count_group_index(count: float) -> int:
    if count <= 0:
        return 0
    if count == 1:
        return 1
    return 2


def _team_profile_from_ids(
    team_ids: Iterable[int],
    profiles: dict[int, ChampionProfile],
) -> tuple[TeamProfile | None, list[int]]:
    physical = magic = true = 0.0
    score_sums = {name: 0.0 for name in SCORE_COLUMNS}
    roles = {role: 0.0 for role in ROLE_COLUMNS}
    unknown: list[int] = []
    team_profile_rows: list[ChampionProfile] = []

    for cid_raw in team_ids:
        cid = int(cid_raw)
        profile = profiles.get(cid)
        if profile is None:
            unknown.append(cid)
            continue
        team_profile_rows.append(profile)
        physical += profile.physical_dpm
        magic += profile.magic_dpm
        true += profile.true_dpm
        for name in SCORE_COLUMNS:
            score_sums[name] += profile.scores[name]
        for role in ROLE_COLUMNS:
            roles[role] += profile.roles[role]

    if unknown:
        return None, unknown

    ad_ap_den = max(physical + magic, 1e-9)
    all_den = max(physical + magic + true, 1e-9)
    ad_share = physical / ad_ap_den
    true_share = true / all_den
    lacks = {
        name: 1.0 if score_sums[name] < LACK_THRESHOLDS[name] else 0.0
        for name in SCORE_COLUMNS
    }
    front_count = sum(
        1 for profile in team_profile_rows
        if profile.scores["frontline_score"] >= 2.0
    )
    return TeamProfile(
        ad_share=ad_share,
        true_share=true_share,
        ad_ap_balance=1.0 - abs(ad_share - (magic / ad_ap_den)),
        front_count=front_count,
        front_sum=score_sums["frontline_score"],
        score_sums=score_sums,
        lacks=lacks,
        roles=roles,
        core_lacks_count=sum(lacks[name] for name in CORE_COLUMNS),
        all_lacks_count=sum(lacks.values()),
    ), []


def _vocab_sidecar_path(pt_path: Path) -> Path:
    """Return the JSON sidecar path for a given .pt vocab source.

    e.g. models/tier2_mayhem/tier2_checkpoint.pt
       -> models/tier2_mayhem/tier2_checkpoint.champ_to_idx.json
    """
    return pt_path.with_name(pt_path.stem + ".champ_to_idx.json")


def _load_vocab(vocab_source: Path) -> dict[int, int]:
    """Load champion-id -> index vocab.

    Path is tried in order:
      1. If the file is a .json, parse directly.
      2. If a .pt was passed but a JSON sidecar exists next to it, use the
         sidecar — avoids the slow `import torch` (30+s on Windows cold start
         with antivirus scanning, which is most of the recommender's boot
         time on this machine).
      3. Otherwise import torch, load the .pt, AND write a JSON sidecar
         next to it so the next startup hits the fast path.
    """
    vocab_source = Path(vocab_source)
    if vocab_source.suffix == ".json":
        raw = json.loads(vocab_source.read_text())
        return {int(k): int(v) for k, v in raw.items()}

    sidecar = _vocab_sidecar_path(vocab_source)
    if sidecar.exists():
        raw = json.loads(sidecar.read_text())
        return {int(k): int(v) for k, v in raw.items()}

    # Cold path — needs torch, writes sidecar for next time.
    import torch
    ckpt = torch.load(vocab_source, map_location="cpu", weights_only=False)
    vocab = {int(k): int(v) for k, v in ckpt["champ_to_idx"].items()}
    try:
        sidecar.write_text(json.dumps({str(k): v for k, v in vocab.items()}))
    except Exception:
        # Sidecar caching is best-effort; failures here shouldn't break loading.
        pass
    return vocab


# ---------- sklearn-free pickle loading ----------

class _LRStub:
    """Pickle stub for sklearn estimators.

    sklearn's pickle format calls __setstate__(dict) with the instance's
    attribute dictionary.  We only need 'coef_' and 'intercept_' off that
    dict, so the stub stores everything and the caller pulls what it needs.
    Crucially, no sklearn classes are imported during unpickling.
    """
    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


class _NoSklearnUnpickler(pickle.Unpickler):
    """Unpickler that swaps sklearn class references for _LRStub.

    Numpy classes still resolve normally — they're needed to materialize
    the coef_/intercept_ arrays.
    """
    def find_class(self, module: str, name: str):
        if module.startswith("sklearn"):
            return _LRStub
        return super().find_class(module, name)


def _load_pickle_no_sklearn(pkl_path: Path) -> tuple[np.ndarray, float]:
    with open(pkl_path, "rb") as f:
        obj = _NoSklearnUnpickler(f).load()
    if not hasattr(obj, "coef_") or not hasattr(obj, "intercept_"):
        raise ValueError(
            f"Pickle at {pkl_path} has no coef_/intercept_ — not a fitted LR model?"
        )
    coef = np.asarray(obj.coef_, dtype=np.float64).reshape(-1)
    intercept = float(np.asarray(obj.intercept_).reshape(-1)[0])
    return coef, intercept


def _as_champion_profile(payload: dict) -> ChampionProfile:
    return ChampionProfile(
        cid=int(payload["cid"]),
        scores={name: float(value) for name, value in payload["scores"].items()},
        roles={name: float(value) for name, value in payload["roles"].items()},
        physical_dpm=float(payload.get("physical_dpm", 0.0)),
        magic_dpm=float(payload.get("magic_dpm", 0.0)),
        true_dpm=float(payload.get("true_dpm", 0.0)),
    )


def load_composition_lr(path: Path) -> CompositionLRModel:
    """Load the saved composition LR without importing sklearn."""
    path = Path(path)
    if path.is_dir():
        path = path / "model.pkl"
    with path.open("rb") as f:
        payload = _NoSklearnUnpickler(f).load()

    model = payload["model"]
    coef = np.asarray(model.coef_, dtype=np.float64).reshape(-1)
    intercept = float(np.asarray(model.intercept_, dtype=np.float64).reshape(-1)[0])
    feature_names = [str(name) for name in payload["feature_names"]]
    if coef.shape[0] != len(feature_names):
        raise ValueError(
            f"Composition LR coef length ({coef.shape[0]}) != feature count ({len(feature_names)})"
        )
    profiles = {
        int(cid): _as_champion_profile(profile)
        for cid, profile in payload["champion_profiles"].items()
    }
    return CompositionLRModel(
        coef=coef,
        intercept=intercept,
        feature_names=feature_names,
        champ_to_idx={int(k): int(v) for k, v in payload["champ_to_idx"].items()},
        profiles=profiles,
    )


def load_lr(lr_path: Path, vocab_source: Path) -> LRModel:
    """Load LR coefficients + champ_to_idx vocab without importing sklearn.

    lr_path can be either:
      - lr_weights.json — bare {coef, intercept} JSON; fastest.
      - lr_model.pkl — sklearn LogisticRegression pickle; loaded via a
        custom Unpickler that stubs out sklearn classes so scipy/sklearn
        are never imported.  Still slightly slower than the JSON path
        because numpy unpacks the pickled array buffers.

    vocab_source can be a .pt checkpoint or a champ_to_idx.json file.
    """
    champ_to_idx = _load_vocab(vocab_source)

    lr_path = Path(lr_path)
    if lr_path.suffix == ".json":
        payload = json.loads(lr_path.read_text())
        coef = np.asarray(payload["coef"], dtype=np.float64)
        intercept = float(payload["intercept"])
    else:
        coef, intercept = _load_pickle_no_sklearn(lr_path)

    if coef.shape[0] != len(champ_to_idx):
        raise ValueError(
            f"LR coef length ({coef.shape[0]}) != vocab size ({len(champ_to_idx)}); "
            "model and vocab were trained on different splits."
        )

    return LRModel(
        coef=coef, intercept=intercept,
        champ_to_idx=champ_to_idx, n_champs=len(champ_to_idx),
        coef_mean=float(coef.mean()),
        coef_std=float(coef.std()),
    )


def _build_feature_vector(
    my_team_ids: Iterable[int],
    model: LRModel,
) -> tuple[np.ndarray, list[int]]:
    """Build +1/-1/0 feature vector with red team = 0 (unknown opponent).

    Returns (X, unknown_ids) where unknown_ids lists championIds not in vocab.
    """
    X = np.zeros(model.n_champs, dtype=np.float64)
    unknown: list[int] = []
    for cid in my_team_ids:
        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            unknown.append(int(cid))
            continue
        X[idx] = 1.0
    return X, unknown


def _set_feature(x: np.ndarray, model: CompositionLRModel, name: str, value: float) -> None:
    idx = model.feature_to_idx.get(name)
    if idx is not None:
        x[idx] = value


def _build_composition_feature_vector(
    my_team_ids: Iterable[int],
    model: CompositionLRModel,
) -> tuple[np.ndarray, list[int]]:
    team_ids = [int(cid) for cid in my_team_ids]
    x = np.zeros(len(model.feature_names), dtype=np.float64)
    unknown = []
    for cid in team_ids:
        idx = model.champ_to_idx.get(cid)
        if idx is None:
            unknown.append(cid)
            continue
        _set_feature(x, model, f"champion:{cid}", 1.0)

    team, profile_unknown = _team_profile_from_ids(team_ids, model.profiles)
    unknown.extend(profile_unknown)
    if unknown or team is None:
        return x, sorted(set(unknown))

    linear_values = {
        "ad_share": team.ad_share,
        "ad_ap_balance": team.ad_ap_balance,
        "true_share": team.true_share,
        "front_count": float(team.front_count),
        "front_sum": team.front_sum,
        "core_lacks_count": team.core_lacks_count,
        "all_lacks_count": team.all_lacks_count,
    }
    for name in SCORE_COLUMNS:
        linear_values[f"sum_{name}"] = team.score_sums[name]
        linear_values[f"lack_{name}"] = team.lacks[name]
    for role in ROLE_COLUMNS:
        linear_values[f"role_{role.lower()}"] = team.roles[role]
    for name, value in linear_values.items():
        _set_feature(x, model, name, float(value))

    ad_bin = AD_BINS[_ad_bin_index(team.ad_share)]
    front_group = FRONT_GROUPS[_count_group_index(float(team.front_count))]
    wave_group = WAVE_GROUPS[int(team.lacks["wave_clear_score"] == 0.0)]
    engage_group = ENGAGE_GROUPS[int(team.lacks["engage_score"] == 0.0)]
    poke_group = POKE_GROUPS[int(team.lacks["poke_score"] == 0.0)]

    _set_feature(x, model, f"ad_front:{front_group}:{ad_bin}", 1.0)
    _set_feature(x, model, f"wave_engage:{wave_group}:{engage_group}", 1.0)
    _set_feature(x, model, f"poke_front:{front_group}:{poke_group}", 1.0)
    for role in ROLE_COLUMNS:
        _set_feature(x, model, f"role_ad:{ad_bin}:{role.lower()}", team.roles[role])

    return x, []


def predict_blue_prob(
    my_team_ids: Iterable[int],
    model: LRModel,
) -> float:
    """Predicted P(blue wins) given the 5 blue champions, opponent unknown.

    Red contribution is set to 0 — see module docstring on 'average opponent'.
    """
    X, _ = _build_feature_vector(my_team_ids, model)
    logit = float(X @ model.coef + model.intercept)
    return float(_sigmoid(logit))


@dataclass
class Suggestion:
    champion_id: int
    source: str            # "keep" or "bench"
    win_prob: float        # absolute P(blue wins) under unknown/zeroed opponent
    delta: float           # display alias for score
    prob_delta_lr: float   # LR win_prob - baseline
    synergy_delta: float   # candidate anchor synergy minus current anchor synergy
    synergy_se: float      # combined SE for the synergy estimate
    anchors_covered: int   # number of teammate anchors with usable pair stats
    score: float           # ML swap delta, or old synergy/LR blend fallback
    z_score: float         # standardized champion strength in the current meta:
                           #   (coef[champ] - mean(coef)) / std(coef)
                           # ~ +1 means roughly top 16%, ~ +2 means top 2.5%.
    is_known: bool         # False if championId is outside training vocab


def _combine_synergy(
    anchors: Iterable[int],
    candidate_id: int,
    pair_stats: PairSynergyStats | None,
) -> tuple[float, float, int]:
    """Return (synergy_delta, combined_se, anchors_covered)."""
    if pair_stats is None:
        return 0.0, float("inf"), 0

    weighted_sum = 0.0
    weight_sum = 0.0
    se_sq_sum = 0.0
    deltas: list[float] = []

    for anchor in anchors:
        row = pair_stats.get(anchor, candidate_id)
        if row is None:
            continue
        weight = 1.0 / (row.se * row.se + 0.0004)
        weighted_sum += row.delta * weight
        weight_sum += weight
        se_sq_sum += row.se * row.se
        deltas.append(row.delta)

    anchors_covered = len(deltas)
    if anchors_covered == 0:
        return 0.0, float("inf"), 0

    combined_se = math.sqrt(se_sq_sum) / anchors_covered
    if anchors_covered >= 2:
        synergy = weighted_sum / weight_sum if weight_sum > 0 else float(np.mean(deltas))
    else:
        synergy = 0.5 * deltas[0]

    if combined_se > 0.04:
        synergy *= 0.5

    return float(synergy), float(combined_se), anchors_covered


def suggest_for_cell(
    my_team_ids: list[int],
    my_current_id: int,
    bench_ids: list[int],
    model: LRModel,
    pair_stats: PairSynergyStats | None = None,
    composition_model: CompositionLRModel | None = None,
) -> list[Suggestion]:
    """Rank candidates for the local player's cell.

    Candidates are the current champion plus the reroll bench.  If the
    composition model is available, the score is its direct probability delta.
    Otherwise the old anchor-synergy/LR blend is used as a fallback.

    Args:
      my_team_ids : list of 5 championIds currently locked into the blue team
                    (must include my_current_id).
      my_current_id : the championId currently in the local player's cell.
      bench_ids   : championIds sitting on the reroll bench.
    """
    if my_current_id not in my_team_ids:
        raise ValueError(
            f"my_current_id={my_current_id} not found in my_team_ids={my_team_ids}; "
            "session parsing bug."
        )

    baseline = predict_blue_prob(my_team_ids, model)
    baseline_logit = _logit_prob(baseline)
    ml_baseline_logit = float("nan")
    use_ml_delta = False
    if composition_model is not None:
        ml_baseline_logit, ml_unknown = composition_model.team_logit_contribution(my_team_ids)
        use_ml_delta = not ml_unknown and not math.isnan(ml_baseline_logit)
    anchors = [int(c) for c in my_team_ids if int(c) != int(my_current_id)]
    current_synergy, _, _ = _combine_synergy(anchors, int(my_current_id), pair_stats)

    seen: set[int] = set()
    out: list[Suggestion] = []
    for source, cid in [("keep", my_current_id)] + [("bench", c) for c in bench_ids]:
        if cid in seen:
            continue
        seen.add(cid)

        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            out.append(Suggestion(
                champion_id=int(cid), source=source,
                win_prob=float("nan"), delta=float("nan"),
                prob_delta_lr=float("nan"),
                synergy_delta=float("nan"), synergy_se=float("nan"),
                anchors_covered=0, score=float("nan"),
                z_score=float("nan"), is_known=False,
            ))
            continue

        swapped = [c if c != my_current_id else cid for c in my_team_ids]
        lr_prob = predict_blue_prob(swapped, model)
        prob_delta_lr = lr_prob - baseline
        prob = lr_prob
        candidate_synergy, synergy_se, anchors_covered = _combine_synergy(
            anchors, int(cid), pair_stats
        )
        synergy_delta = (
            candidate_synergy - current_synergy
            if anchors_covered > 0 else 0.0
        )
        if use_ml_delta and composition_model is not None:
            ml_logit, ml_unknown = composition_model.team_logit_contribution(swapped)
            if not ml_unknown and not math.isnan(ml_logit):
                prob = float(_sigmoid(baseline_logit + ml_logit - ml_baseline_logit))
                score = prob - baseline
            else:
                score = 0.7 * synergy_delta + 0.3 * prob_delta_lr
        else:
            score = 0.7 * synergy_delta + 0.3 * prob_delta_lr
        out.append(Suggestion(
            champion_id=int(cid), source=source,
            win_prob=prob, delta=score,
            prob_delta_lr=prob_delta_lr,
            synergy_delta=synergy_delta,
            synergy_se=synergy_se,
            anchors_covered=anchors_covered,
            score=score,
            z_score=model.z_score(idx), is_known=True,
        ))

    out.sort(key=lambda s: (not s.is_known, -s.score if s.is_known else 0.0))
    return out


# ---------- Session parsing ----------

@dataclass
class ParsedSession:
    my_team_ids: list[int]   # 5 championIds for blue team
    my_current_id: int       # local player's current champion
    my_cell_id: int          # localPlayerCellId
    bench_ids: list[int]     # championIds on reroll bench
    bench_enabled: bool


def parse_session(session: dict) -> ParsedSession | None:
    """Extract the recommender's inputs from a /lol-champ-select/v1/session payload.

    Returns None if the session is incomplete (not all 5 cells have a champion
    locked in yet — recommendations are noise until everyone has a starting champ).
    """
    my_cell = session.get("localPlayerCellId")
    my_team = session.get("myTeam") or []
    bench = session.get("benchChampions") or []

    if my_cell is None or not my_team:
        return None

    my_team_ids: list[int] = []
    my_current_id: int | None = None
    for cell in my_team:
        cid = int(cell.get("championId") or 0)
        if cid == 0:
            return None  # someone hasn't been assigned a champion yet
        my_team_ids.append(cid)
        if cell.get("cellId") == my_cell:
            my_current_id = cid

    if my_current_id is None:
        return None

    bench_ids = [int(b.get("championId") or 0) for b in bench]
    bench_ids = [c for c in bench_ids if c > 0]

    return ParsedSession(
        my_team_ids=my_team_ids,
        my_current_id=my_current_id,
        my_cell_id=int(my_cell),
        bench_ids=bench_ids,
        bench_enabled=bool(session.get("benchEnabled", False)),
    )


def session_state_hash(parsed: ParsedSession) -> tuple:
    """Stable hash so the CLI can detect 'state changed, redraw' vs idle ticks."""
    return (
        tuple(sorted(parsed.my_team_ids)),
        parsed.my_current_id,
        parsed.my_cell_id,
        tuple(sorted(parsed.bench_ids)),
    )
