from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from aram_nn.desktop import release
from aram_nn.desktop.update import atomic_json


def inputs(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    for name in ("model.pkl", "lr_weights.json", "champ_to_idx.json", "role_synergy.json", "single_team_calibration.json"):
        (model / name).write_text("{}")
    atomic_json(model / "summary.json", {"shipped_model_rows": 2})
    parquet = tmp_path / "training.parquet"
    pq.write_table(pa.table({"game_creation_ms": [1000, 2000]}), parquet)
    names = tmp_path / "names.json"
    tier = tmp_path / "tier.json"
    names.write_text("{}")
    tier.write_text("{}")
    return {"model_dir": model, "parquet": parquet, "region": "TW", "output": tmp_path / "published",
            "names": names, "tier": tier}


def test_watermark_only_moves_after_success_and_retries_same_model(tmp_path, monkeypatch):
    options = inputs(tmp_path)
    calls = []
    def unavailable(*args, **kwargs):
        raise OSError("GitHub temporarily unavailable")
    monkeypatch.setattr(release, "publish_data", unavailable)
    with pytest.raises(OSError):
        release.publish_refreshed_models(**options, tag="release-1")
    assert not (options["output"] / "publish-state.json").exists()
    def success(*args, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(release, "publish_data", success)
    assert release.publish_refreshed_models(**options, tag="release-1")
    assert calls[0]["time_cutoff"] == "1970-01-01T00:00:02+00:00"
    assert not release.publish_refreshed_models(**options, tag="release-1")
    assert release.publish_refreshed_models(**options, tag="release-2")
    assert len(calls) == 2


def test_mismatched_training_rows_never_publish(tmp_path, monkeypatch):
    options = inputs(tmp_path)
    atomic_json(options["model_dir"] / "summary.json", {"shipped_model_rows": 3})
    def unexpected(*args, **kwargs):
        pytest.fail("Mismatched model and dataset must not reach public upload")
    monkeypatch.setattr(release, "publish_data", unexpected)
    with pytest.raises(ValueError, match="row counts differ"):
        release.publish_refreshed_models(**options, tag="release-1")


def test_retry_after_asset_upload_reuses_immutable_archive(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    from types import SimpleNamespace
    payload = b"verified JSON archive"
    digest = hashlib.sha256(payload).hexdigest()
    name = f"recommender-data-{digest[:16]}.zip"
    old = {"app": {"version": "2026.10.09.2"}, "data": {"sha256": "0" * 64}}
    output = tmp_path / "publish"
    manifest_uploads = []
    def gh(command, **kwargs):
        if command[2] == "view":
            return SimpleNamespace(stdout=json.dumps({"assets": [
                {"name": "recommender-manifest.json"},
                {"name": name, "digest": f"sha256:{digest}", "size": len(payload)}]}))
        if command[2] == "download":
            atomic_json(output / "recommender-manifest.json", old)
        if command[2] == "upload":
            assert Path(command[4]).name == "recommender-manifest.json"
            manifest_uploads.append(command)
            if len(manifest_uploads) == 1:
                raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(release.subprocess, "run", gh)
    monkeypatch.setattr(release, "export_data", lambda *a, **k: {"current_patch": "16.20"})
    monkeypatch.setattr(release, "zip_data", lambda folder, archive: archive.write_bytes(payload))
    options = {"model_dir": tmp_path, "names": tmp_path, "tier": tmp_path, "output": output,
               "region": "TW", "time_cutoff": "2026-10-09T00:00:00+00:00", "tag": "release-2"}
    with pytest.raises(subprocess.CalledProcessError):
        release.publish_data(**options)
    result = release.publish_data(**options)
    assert result["data"]["sha256"] == digest
    assert len(manifest_uploads) == 2
