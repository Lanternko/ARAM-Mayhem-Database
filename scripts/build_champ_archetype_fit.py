"""Build the shipped (champion x team-archetype) empirical comp-fit artifact.

Productionizes the validated Q3 signal (scripts/ablation_champ_archetype_persistence.py,
cross-patch train->test r=+0.38): for each champion, how much it over/under-performs
when its FOUR teammates (leave-one-out, so the champion isn't credited for making its
own team that archetype) lean toward each of the 6 comp archetypes.

This drives the site's "comp fit" radar with the EMPIRICAL signal (what comp to build
AROUND this champion) instead of the old heuristic blend (tautological: a re-projection
of the champion's own ability bars).  Jinx is the canonical case: heuristic says "AD
carry comp" (her highest axis) but empirically that is her WORST fit (-4pp, redundant
carries); her best is dive (+2.4pp).

Discipline mirrors role_synergy.py: the per-cell delta is the mean residual of an
additive Champion LR (champion main effects removed -> the value is the non-additive
interaction, not "X is good" or "dive wins"), shrunk by n/(n+k) and a persistence
haircut so low-support cells (which flip sign cross-patch) collapse toward 0.  A champion
with no archetype cell clearing --min-cell carries `qualified=false` and the frontend
falls back to the heuristic radar, flagged "estimated".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import numpy as np
from aram_nn import heavy_jobs, parquet_batches
from aram_nn.parquet_batches import TEAM_COLUMNS
from aram_nn.site import stage_memo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ablation_champ_archetype_persistence import (  # noqa: E402
    COMP_FIT_DEFS, COMP_STAT_KEYS, ARCHETYPE_LABELS, build_cap_pct,
    fit_champion_lr, load_comp_vectors,
)

SCHEMA_KIND = "champ_archetype_fit"
_SCRIPTS = Path(__file__).resolve().parent
# Every source file whose edit can change the artifact (see stage_memo).
MEMO_CODE = (Path(__file__), _SCRIPTS / "ablation_champ_archetype_persistence.py",
             _SCRIPTS / "analyze_composition_signals.py",
             Path(parquet_batches.__file__), Path(stage_memo.__file__))


class TeamArrays:
    """Champion-index arrays for a window of games: ``blue``/``red`` are (n, width)
    int32 with -1 padding, ``labels`` float32 blue-win.  Replaces the list-of-lists
    TeamDataset here: 5M games as Python lists cost GBs, as int32 ~200MB."""

    def __init__(self, blue: np.ndarray, red: np.ndarray, labels: np.ndarray):
        self.blue, self.red, self.labels = blue, red, labels

    def __len__(self) -> int:
        return len(self.labels)

    def slice(self, start: int, length: int) -> "TeamArrays":
        stop = start + length
        return TeamArrays(self.blue[start:stop], self.red[start:stop], self.labels[start:stop])


def _pad_lists(flat: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """Ragged champion lists -> (rows, max_len) int64, -1 padded."""
    width = int(lengths.max()) if len(lengths) else 0
    out = np.full((len(lengths), width), -1, dtype=np.int64)
    starts = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    rows = np.repeat(np.arange(len(lengths)), lengths)
    out[rows, np.arange(len(flat)) - np.repeat(starts, lengths)] = flat
    return out


def _read_team_columns(data: Path):
    """TEAM_COLUMNS as numpy, one Arrow batch at a time (no Polars frame: on the
    8GB pooled file pl.read_parquet + sort held >5GB of allocator memory that it
    never returned, for ~300MB of actual columns)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    cols = {k: [] for k in ("duration", "created", "wins", "patch", "blue", "blue_len", "red", "red_len")}
    patch_ids: dict[str | None, int] = {}
    with pq.ParquetFile(data) as parquet:
        for batch in parquet.iter_batches(batch_size=65536, columns=TEAM_COLUMNS, use_threads=False):
            cols["duration"].append(pc.fill_null(batch.column("duration_sec"), -1).to_numpy())
            cols["created"].append(pc.fill_null(batch.column("game_creation_ms"),
                                                np.iinfo(np.int64).min).cast(pa.int64()).to_numpy())
            cols["wins"].append(batch.column("blue_wins").cast(pa.float32()).to_numpy(zero_copy_only=False))
            enc = pc.dictionary_encode(batch.column("patch"))
            local = np.array([patch_ids.setdefault(None if p is None else ".".join(p.split(".")[:2]),
                                                   len(patch_ids))
                              for p in enc.dictionary.to_pylist()] or [0], dtype=np.int32)
            codes = pc.fill_null(enc.indices, 0).to_numpy(zero_copy_only=False)
            cols["patch"].append(local[codes])
            for side in ("blue", "red"):
                arr = batch.column(f"{side}_champions")
                cols[side].append(arr.flatten().to_numpy(zero_copy_only=False).astype(np.int64))
                cols[f"{side}_len"].append(pc.fill_null(pc.list_value_length(arr), 0).to_numpy())
    out = {k: np.concatenate(v) if v else np.array([]) for k, v in cols.items()}
    out["patch_names"] = {i: p for p, i in patch_ids.items()}
    return out


def load_all(data: Path, patches, *, min_duration=300, val_frac=0.15):
    """Whole-window dataset (vocab over ALL games) + a time-tail val for the LR's
    C-selection.  Residuals are taken on every game; the per-cell estimate uses the
    full window, unlike the ablation which holds out a test split for the gate."""
    raw = _read_team_columns(data)
    keep = raw["duration"] >= min_duration
    if patches:
        wanted = [i for i, p in raw["patch_names"].items() if p in set(patches)]
        keep &= np.isin(raw["patch"], wanted)
    if not keep.any():
        raise click.ClickException("no rows after filters")
    blue_all = _pad_lists(raw["blue"], raw["blue_len"])
    red_all = _pad_lists(raw["red"], raw["red_len"])
    width = max(blue_all.shape[1], red_all.shape[1])
    blue_all = np.pad(blue_all, ((0, 0), (0, width - blue_all.shape[1])), constant_values=-1)
    red_all = np.pad(red_all, ((0, 0), (0, width - red_all.shape[1])), constant_values=-1)
    rows = np.flatnonzero(keep)
    rows = rows[np.argsort(raw["created"][rows], kind="stable")]
    blue_cid, red_cid = blue_all[rows], red_all[rows]
    del blue_all, red_all
    labels = raw["wins"][rows].astype(np.float32)
    patch_list = sorted({raw["patch_names"][int(i)] for i in np.unique(raw["patch"][rows])})
    del raw
    ids = np.unique(np.concatenate([blue_cid[blue_cid >= 0], red_cid[red_cid >= 0]]))
    champ_to_idx = {int(cid): i for i, cid in enumerate(ids)}  # == build_vocab: sorted ids
    lookup = np.full(int(ids.max()) + 2, -1, dtype=np.int32)  # last cell: -1 pad maps to -1
    lookup[ids] = np.arange(len(ids), dtype=np.int32)
    full = TeamArrays(lookup[blue_cid], lookup[red_cid], labels)
    del blue_cid, red_cid
    n = len(full)
    n_val = max(1, int(n * val_frac))
    tr = full.slice(0, n - n_val)
    val = full.slice(n - n_val, n_val)
    return full, tr, val, champ_to_idx, patch_list, n


def sparse_champion_matrix(dataset: TeamArrays, n_champs: int):
    """CSR twin of analyze_composition_signals.champion_matrix (+1 blue, -1 red; a
    later slot overwrites an earlier one for the same champion, like the dense
    assignment).  10 non-zeros per game instead of n_champs float32s."""
    from scipy.sparse import csr_matrix

    cols = np.concatenate([dataset.blue, dataset.red], axis=1)
    vals = np.concatenate([np.ones(dataset.blue.shape, np.float32),
                           np.full(dataset.red.shape, -1.0, np.float32)], axis=1)
    vals[cols < 0] = 0.0
    for j in range(cols.shape[1]):
        for k in range(j + 1, cols.shape[1]):
            vals[(cols[:, j] == cols[:, k]) & (cols[:, j] >= 0), j] = 0.0
    cols = np.where(cols < 0, 0, cols).astype(np.int32)
    n, width = cols.shape
    indptr = np.arange(0, n * width + 1, width, dtype=np.int64)
    return csr_matrix((vals.ravel(), cols.ravel(), indptr), shape=(n, n_champs))


def _cap_table(idx_to_cid, cap_pct):
    cap = np.zeros((len(idx_to_cid), len(COMP_STAT_KEYS)), dtype=np.float64)
    has = np.zeros(len(idx_to_cid), dtype=bool)
    for idx, cid in idx_to_cid.items():
        if cid in cap_pct:
            has[idx] = True
            cap[idx] = [cap_pct[cid][k] for k in COMP_STAT_KEYS]
    return cap, has


def _py_sum(terms) -> np.ndarray:
    """Element-wise twin of CPython >= 3.12 ``sum()`` over floats (Neumaier
    compensated: running total + correction, added once at the end).  A 0.0 term
    leaves both unchanged, so masked-out terms can be passed as zeros."""
    total = comp = None
    for x in terms:
        if total is None:
            total = np.zeros_like(x)
            comp = np.zeros_like(x)
        t = total + x
        comp += np.where(np.abs(total) >= np.abs(x), (total - t) + x, (x - t) + total)
        total = t
    return total + comp


def _loo_scores(team: np.ndarray, cap: np.ndarray, has: np.ndarray, archetype: str) -> np.ndarray:
    """(rows, width) archetype score of each slot's OTHER teammates, bit-identical
    to ablation archetype_scores(): Python sum of the valid others' percentiles,
    / their count, then the Python sum of the weighted blend."""
    rows, width = team.shape
    safe = np.where(team < 0, 0, team)
    valid = (team >= 0) & has[safe]
    key_pos = {k: i for i, k in enumerate(COMP_STAT_KEYS)}
    out = np.zeros((rows, width), dtype=np.float64)
    for j in range(width):
        use = [(o, valid[:, o] & (team[:, o] != team[:, j])) for o in range(width) if o != j]
        count = np.zeros(rows, dtype=np.int64)
        for _o, m in use:
            count += m
        with np.errstate(invalid="ignore", divide="ignore"):
            avg = {key: _py_sum(np.where(m, cap[safe[:, o], key_pos[key]], 0.0) for o, m in use) / count
                   for key in COMP_FIT_DEFS[archetype]}
            score = _py_sum(w * avg[key] for key, w in COMP_FIT_DEFS[archetype].items())
        out[:, j] = np.where(count > 0, score, 0.0)
    return out


def _threshold(dataset: TeamArrays, blue: np.ndarray, red: np.ndarray, quantile) -> float:
    """Membership cut: quantile over every real slot's leave-one-out team score."""
    return float(np.quantile(np.concatenate([blue[dataset.blue >= 0], red[dataset.red >= 0]]),
                             quantile))


def calibrate_thresholds(dataset: TeamArrays, idx_to_cid, cap_pct, quantile):
    """Vectorized ablation calibrate_thresholds: per-archetype quantile over every
    real slot's leave-one-out team score.  One archetype at a time keeps the peak at
    one (games x 10) float64 array, not 60 Python floats per game."""
    cap, has = _cap_table(idx_to_cid, cap_pct)
    return {a: _threshold(dataset, _loo_scores(dataset.blue, cap, has, a),
                          _loo_scores(dataset.red, cap, has, a), quantile)
            for a in COMP_FIT_DEFS}


class _Cells:
    """Signed-residual sums / counts per (champion-idx, archetype).  Slots are
    flattened in the ablation loop's order (game, blue then red, slot) and
    np.bincount adds sequentially, so each cell sums the same residuals in the
    same order."""

    def __init__(self, dataset: TeamArrays, residual, n_champs: int):
        self.n_champs = n_champs
        self.team = np.stack([dataset.blue, dataset.red], axis=1)  # (n, 2, width)
        self.signed = np.broadcast_to(
            np.asarray(residual, dtype=np.float64)[:, None, None]
            * np.array([1.0, -1.0])[None, :, None], self.team.shape)
        self.ssum: dict[tuple[int, str], float] = {}
        self.cnt: dict[tuple[int, str], int] = {}

    def add(self, archetype: str, blue: np.ndarray, red: np.ndarray, threshold: float) -> None:
        hit = (np.stack([blue, red], axis=1) >= threshold) & (self.team >= 0)
        idx = self.team[hit]
        sums = np.bincount(idx, weights=self.signed[hit], minlength=self.n_champs)
        counts = np.bincount(idx, minlength=self.n_champs)
        for c in np.flatnonzero(counts):
            self.ssum[(int(c), archetype)] = float(sums[c])
            self.cnt[(int(c), archetype)] = int(counts[c])


def accumulate(dataset: TeamArrays, residual, idx_to_cid, cap_pct, thresholds):
    """Vectorized ablation accumulate (ssum/cnt; the unused continuous variant is
    dropped)."""
    cap, has = _cap_table(idx_to_cid, cap_pct)
    cells = _Cells(dataset, residual, len(idx_to_cid))
    for a in COMP_FIT_DEFS:
        cells.add(a, _loo_scores(dataset.blue, cap, has, a),
                  _loo_scores(dataset.red, cap, has, a), thresholds[a])
    return cells.ssum, cells.cnt, None


def thresholds_and_cells(dataset: TeamArrays, residual, idx_to_cid, cap_pct, quantile):
    """calibrate_thresholds + accumulate on the SAME dataset in one pass.  The
    leave-one-out scores are by far the costliest step (8 of this script's 9
    minutes on 4M games) and both need exactly the same ones; computing them once
    per archetype halves that, with identical thresholds and cells."""
    cap, has = _cap_table(idx_to_cid, cap_pct)
    cells = _Cells(dataset, residual, len(idx_to_cid))
    thresholds = {}
    for a in COMP_FIT_DEFS:
        blue = _loo_scores(dataset.blue, cap, has, a)
        red = _loo_scores(dataset.red, cap, has, a)
        thresholds[a] = _threshold(dataset, blue, red, quantile)
        cells.add(a, blue, red, thresholds[a])
    return thresholds, cells.ssum, cells.cnt


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_pooled_16_10_12.parquet"),
              type=click.Path(exists=True, path_type=Path), show_default=True,
              help="Pooled cross-patch parquet -> most stable per-cell support.")
@click.option("--patches", default="", show_default=True,
              help="Comma-separated patch prefixes to include; empty = every patch in the file.")
@click.option("--payload", default=Path("docs/api/tier-list.json"),
              type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--membership-quantile", default=0.667, show_default=True)
@click.option("--shrink-k", default=150.0, show_default=True,
              help="n/(n+k) shrink: kills low-support cells (same k as role_synergy).")
@click.option("--persistence-factor", default=0.5, show_default=True,
              help="Conservative train->test regression-to-mean haircut (role_synergy uses 0.5).")
@click.option("--min-cell", default=300, show_default=True,
              help="Below this game count an archetype cell is unqualified (frontend falls back).")
@click.option("--out", default=Path("docs/api/champ-archetype-fit.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, patches, payload, membership_quantile, shrink_k, persistence_factor, min_cell, out):
    heavy_jobs.lower_priority()
    patch_set = [p.strip() for p in patches.split(",") if p.strip()]
    print("[1/4] comp vectors + percentile norm ...", flush=True)
    comp_by_cid = load_comp_vectors(payload)
    import sklearn

    # Between two model refreshes neither the parquet nor the comp vectors move,
    # so nearly every publish cycle can reuse the last artifact.
    memo_key = stage_memo.stage_key(
        code=MEMO_CODE, data=str(data), parquet=stage_memo.file_identity(data),
        patches=patch_set, comp={str(c): v for c, v in comp_by_cid.items()},
        params=[membership_quantile, shrink_k, persistence_factor, min_cell],
        libs=[np.__version__, sklearn.__version__])
    if stage_memo.restore(SCHEMA_KIND, memo_key, out):
        print(f"inputs unchanged since the last build; reused the stored artifact: {out}")
        return
    cap_pct = build_cap_pct(comp_by_cid)
    champ_meta = json.loads(Path(payload).read_text(encoding="utf-8")).get("champs", {})
    names = {int(k): (v.get("alias") or v.get("name") or k) for k, v in champ_meta.items()}

    print("[2/4] loading whole window + fitting additive Champion LR ...", flush=True)
    full, tr, val, champ_to_idx, patch_list, n_games = load_all(data, patch_set)
    n_champs = len(champ_to_idx)
    idx_to_cid = {i: c for c, i in champ_to_idx.items()}
    model = fit_champion_lr(sparse_champion_matrix(tr, n_champs), np.asarray(tr.labels, dtype=np.float64),
                            sparse_champion_matrix(val, n_champs), np.asarray(val.labels, dtype=np.float64))
    res_full = (np.asarray(full.labels, dtype=np.float64)
                - model.predict_proba(sparse_champion_matrix(full, n_champs))[:, 1])

    print("[3/4] thresholds + accumulating champion x archetype cells ...", flush=True)
    thresholds, ssum, cnt = thresholds_and_cells(full, res_full, idx_to_cid, cap_pct,
                                                 membership_quantile)

    print("[4/4] shrink + persistence haircut -> artifact ...", flush=True)
    champs_out: dict[str, dict] = {}
    qualified_cells = 0
    for cid in sorted(idx_to_cid.values()):
        idx = champ_to_idx[cid]
        cell = {}
        any_q = False
        for a in COMP_FIT_DEFS:
            n = cnt.get((idx, a), 0)
            raw_pp = (ssum.get((idx, a), 0.0) / n * 100) if n else 0.0
            shrunk = raw_pp * (n / (n + shrink_k)) * persistence_factor
            q = n >= min_cell
            any_q = any_q or q
            if q:
                qualified_cells += 1
            cell[a] = {"delta": round(shrunk, 3), "raw_pp": round(raw_pp, 3), "n": int(n), "q": q}
        champs_out[str(cid)] = {"fit": cell, "qualified": any_q}

    artifact = {
        "kind": SCHEMA_KIND,
        "version": 1,
        "source_parquet": str(data),
        "patches": patch_list,
        "n_games": n_games,
        "params": {
            "membership_quantile": membership_quantile,
            "shrink_k": shrink_k,
            "persistence_factor": persistence_factor,
            "min_cell": min_cell,
        },
        "archetypes": list(COMP_FIT_DEFS.keys()),
        "archetype_labels": ARCHETYPE_LABELS,
        "thresholds": {a: round(v, 4) for a, v in thresholds.items()},
        "champs": champs_out,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    stage_memo.store(SCHEMA_KIND, memo_key, out)

    n_qual_champ = sum(1 for v in champs_out.values() if v["qualified"])
    print(f"\npatches={patch_list}  games={n_games}  champs={len(champs_out)}  "
          f"qualified champs={n_qual_champ}  qualified cells={qualified_cells}")
    print(f"written: {out}\n")
    print("sanity (shrunk delta pp by archetype; + = build this comp around him, - = avoid):")
    for who in ("Jinx", "Malphite", "Soraka", "Amumu", "Kaisa"):
        cid = next((c for c, nm in names.items() if nm.lower() == who.lower()), None)
        if cid is None or str(cid) not in champs_out:
            print(f"  {who}: n/a"); continue
        fit = champs_out[str(cid)]["fit"]
        ordered = sorted(COMP_FIT_DEFS, key=lambda a: fit[a]["delta"], reverse=True)
        parts = "  ".join(f"{a}:{fit[a]['delta']:+.2f}(n={fit[a]['n']})" for a in ordered)
        print(f"  {who:9} {parts}")


if __name__ == "__main__":
    main()
