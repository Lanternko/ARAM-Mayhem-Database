import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from click.testing import CliRunner

from aram_nn.parquet_batches import SOURCE_ROW, TEAM_COLUMNS, iter_parquet_rows

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_champ_archetype_fit as fit
import build_champ_empirical_axes as axes
from analyze_composition_signals import build_champion_profiles, SCORE_COLUMNS


def legacy(name, tmp_path):
    source = subprocess.check_output(["git", "show", f"f472df59:scripts/{name}.py"], cwd=ROOT)
    path = tmp_path / f"old_{name}.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location(f"old_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def data(tmp_path):
    rows = []
    for i in range(21):
        parts = [{"championId": c, "teamId": 100 if c <= 5 else 200,
                  "stats": {"total_damage_dealt_to_champions": (i+1)*c,
                            "kills": i % 4, "assists": c,
                            "largest_killing_spree": i % 3}}
                 for c in range(1, 11)]
        rows.append(dict(patch=["16.10.1", "16.11.2", "16.12.3"][i % 3],
                         duration_sec=[100, 600, 1320, 1500][i % 4],
                         game_creation_ms=(20-i)//2, blue_wins=i % 2,
                         blue_champions=list(range(1, 6)), red_champions=list(range(6, 11)),
                         participants_json=json.dumps(parts)))
    path = tmp_path / "games.parquet"
    pl.DataFrame(rows).write_parquet(path, row_group_size=5)
    return path


def test_batches_select_exact_rows(data):
    columns = ["blue_wins", "duration_sec", "participants_json"]
    expected = pl.read_parquet(data).select(columns)
    for size in (1, 3, 8):
        assert list(iter_parquet_rows(data, columns, batch_size=size)) == list(expected.iter_rows())
        selected = {0, 4, 5, 12, 20}
        assert list(iter_parquet_rows(data, columns, batch_size=size, selected_rows=selected)) == [
            r for i, r in enumerate(expected.iter_rows()) if i in selected]
    assert list(iter_parquet_rows(data, columns, selected_rows=set())) == []
    with pytest.raises(ValueError):
        list(iter_parquet_rows(data, columns, batch_size=0))


def test_archetype_projection_parity(data, tmp_path, monkeypatch):
    old = legacy("build_champ_archetype_fit", tmp_path)
    expected = old.load_all(data, ["16.11"])
    read = pl.read_parquet
    def projected(*args, **kwargs):
        assert kwargs["columns"] == TEAM_COLUMNS
        return read(*args, **kwargs)
    monkeypatch.setattr(pl, "read_parquet", projected)
    actual = fit.load_all(data, ["16.11"])
    for a, b in zip(actual[:3], expected[:3]):
        assert a.blue == b.blue and a.red == b.red
        np.testing.assert_array_equal(a.labels, b.labels)
    assert actual[3:] == expected[3:]


@pytest.mark.parametrize("patches", ["", "16.11", "99.99"])
def test_axes_artifact_parity(data, tmp_path, monkeypatch, patches):
    old = legacy("build_champ_empirical_axes", tmp_path)
    monkeypatch.chdir(tmp_path)
    Path("docs/api").mkdir(parents=True)
    Path("docs/api/tier-list.json").write_text('{"champs": {}}')
    args = ["--data", str(data), "--patches", patches, "--min-bucket", "1", "--min-games", "1"]
    runner = CliRunner()
    for module, output in [(old, "old.json"), (axes, "new.json")]:
        result = runner.invoke(module.main, args + ["--out", output])
        assert result.exit_code == 0, result.output + str(result.exception)
    assert json.loads(Path("old.json").read_text(encoding="utf-8")) == json.loads(Path("new.json").read_text(encoding="utf-8"))


def test_train_only_profiles_parity(data, tmp_path):
    df = pl.read_parquet(data, row_index_name=SOURCE_ROW).filter(pl.col("duration_sec") >= 300)
    train = df.sort("game_creation_ms").head(7)
    csv = tmp_path / "scores.csv"
    csv.write_text(",".join(["champion_id", "tags", *SCORE_COLUMNS]) + "\n" +
                   "\n".join(",".join([str(c), "", *["1" for _ in SCORE_COLUMNS]]) for c in range(1, 11)))
    kwargs = dict(score_csv=csv, min_games=1, replace_sustain=True)
    expected = build_champion_profiles(train_df=train, **kwargs)
    actual = build_champion_profiles(train_df=train.drop("participants_json"), **kwargs,
        empirical_rows=iter_parquet_rows(data, ["blue_wins", "duration_sec", "participants_json"],
                                        selected_rows=set(train[SOURCE_ROW].to_list()), batch_size=2))
    for cid in expected:
        assert actual[cid].scores == expected[cid].scores
        assert actual[cid].roles == expected[cid].roles
        assert actual[cid].physical_dpm == pytest.approx(expected[cid].physical_dpm)
        assert actual[cid].magic_dpm == pytest.approx(expected[cid].magic_dpm)
        assert actual[cid].true_dpm == pytest.approx(expected[cid].true_dpm)
