from pathlib import Path

from aram_nn.desktop.ci_release import next_version

import pytest
from aram_nn.desktop import ci_release
from aram_nn.desktop.release import asset_info
from aram_nn.desktop.update import atomic_json


def test_ci_version_increases_across_manual_releases_and_clock_regression():
    assert next_version("2026.10.09.20", 1, "2026.10.09") == "2026.10.09.21"
    assert next_version("2026.10.09.20", 3, "2026.10.10") == "2026.10.10.3"
    assert next_version("2026.10.09.20", 1, "2026.10.08") == "2026.10.09.21"


@pytest.mark.parametrize("channel_changes", [False, True])
def test_publication_verifies_all_draft_assets_before_latest(tmp_path, monkeypatch, channel_changes):
    import json
    from types import SimpleNamespace
    dist = tmp_path / "candidate"
    dist.mkdir()
    exe = dist / "ARAMRecommender.exe"
    exe.write_bytes(b"candidate already passed two Windows smoke jobs")
    (dist / "ARAMRecommender-windows.zip").write_bytes(b"verified candidate ZIP")
    data = tmp_path / "public-data.zip"
    data.write_bytes(b"verified public JSON archive")
    tag = "recommender-v2026.10.09.3"
    manifest = {"schema_version": 1, "platform": "windows-x64",
                "app": {**asset_info(exe, tag), "version": "2026.10.09.3"},
                "data": {**asset_info(data, tag), "version": asset_info(data, tag)["sha256"][:16],
                         "schema_version": 1, "queue_id": 2400, "current_patch": "16.20",
                         "time_cutoff": "2026-10-08T21:26:28+00:00"}}
    atomic_json(dist / "recommender-manifest.json", manifest)
    current = {**manifest, "app": {**manifest["app"], "version": "2026.10.09.2"}}
    newer = {**current, "data": {**current["data"], "sha256": "0" * 64}}
    manifests = iter([current, newer if channel_changes else current])
    monkeypatch.setattr(ci_release, "fetch_manifest", lambda: next(manifests))
    monkeypatch.setattr(ci_release, "download", lambda *a: data)
    monkeypatch.setattr(ci_release, "install_data", lambda *a: dist)
    calls = []
    uploaded = []
    def gh(command, **kwargs):
        calls.append(command)
        if command[2] == "view" and command[-1] == "isDraft,targetCommitish":
            return SimpleNamespace(returncode=1)
        if command[2] == "upload":
            uploaded.extend(command[4:command.index("--repo")])
        if command[2] == "view":
            return SimpleNamespace(stdout=json.dumps({"assets": [
                {"name": Path(path).name, "size": asset_info(Path(path), tag)["size"],
                 "digest": "sha256:" + asset_info(Path(path), tag)["sha256"]}
                for path in uploaded]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ci_release.subprocess, "run", gh)
    if channel_changes:
        with pytest.raises(ValueError, match="channel changed"):
            ci_release.publish(dist, "a" * 40)
        assert not any(command[2] == "edit" for command in calls)
    else:
        assert ci_release.publish(dist, "a" * 40) == tag
        assert calls[-1][2] == "edit" and "--latest" in calls[-1]
    assert len(uploaded) == 4
    assert any(command[2] == "create" and "--draft" in command for command in calls)
