from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from aram_nn.desktop.release import zip_data
from aram_nn.desktop.update import (DATA_FILES, REPO, UpdateError, atomic_json, download,
                                   install_app, install_data, read_json, update, validate_manifest)


def asset(data: bytes, name="data.zip"):
    return {"url": f"https://github.com/{REPO}/releases/download/recommender-v2026.10.09.1/{name}",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def manifest(data: bytes):
    item = asset(data)
    return {"schema_version": 1, "platform": "windows-x64",
            "app": {**asset(b"application", "ARAMRecommender.exe"), "version": "2026.10.09.1"},
            "data": {**item, "version": item["sha256"][:16], "schema_version": 1,
                     "queue_id": 2400, "current_patch": "16.20", "time_cutoff": "2026-10-09T00:00:00+00:00"}}


def bundle(tmp_path):
    directory = tmp_path / "fixture"
    directory.mkdir()
    vocab = {str(n): n - 1 for n in range(1, 6)}
    for name in DATA_FILES:
        atomic_json(directory / name, {})
    atomic_json(directory / "lr_weights.json", {"coef": [0.1] * 5, "intercept": 0.0})
    atomic_json(directory / "champ_to_idx.json", vocab)
    atomic_json(directory / "composition.json", {
        "schema_version": 1, "coef": [0.1], "intercept": 0.0,
        "feature_names": ["champ_1"], "champ_to_idx": vocab,
        "champion_profiles": {str(n): {"cid": n, "scores": {}, "roles": {}} for n in range(1, 6)},
    })
    atomic_json(directory / "single_team_calibration.json", {"single_team_intercept": 0.0})
    atomic_json(directory / "role_synergy.json", {
        "kind": "champ_role_synergy", "queue_id": 2400, "patch_prefix": "16.20",
        "role_by_champ": {str(n): "Fighter" for n in range(1, 6)}, "cells": [],
    })
    atomic_json(directory / "metadata.json", {
        "schema_version": 1, "queue_id": 2400, "current_patch": "16.20", "region": "TW",
        "time_cutoff": "2026-10-09T00:00:00+00:00", "row_count": 100,
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in DATA_FILES - {"metadata.json"}},
    })
    archive = tmp_path / "data.zip"
    zip_data(directory, archive)
    return archive, manifest(archive.read_bytes())


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def test_resume_exact_range_and_digest(tmp_path):
    body = b"0123456789"
    item = asset(body)
    (tmp_path / (item["sha256"] + ".download")).write_bytes(body[:4])
    def opener(url, headers):
        assert headers["Range"] == "bytes=4-"
        return Response(body[4:], 206, {"Content-Range": "bytes 4-9/10"})
    assert download(item, tmp_path, opener=opener).read_bytes() == body


def test_server_ignoring_range_restarts(tmp_path):
    body = b"complete"
    item = asset(body)
    (tmp_path / (item["sha256"] + ".download")).write_bytes(b"com")
    assert download(item, tmp_path, opener=lambda *_: Response(body)).read_bytes() == body


def test_interrupted_read_resumes_on_retry(tmp_path):
    body = b"0123456789"
    class Interrupted(Response):
        def read(self, size=-1):
            if self.tell():
                raise ConnectionResetError("disconnected")
            return super().read(4)
    calls = []
    def opener(url, headers):
        calls.append(headers)
        if len(calls) == 1:
            return Interrupted(body)
        assert headers["Range"] == "bytes=4-"
        return Response(body[4:], 206, {"Content-Range": "bytes 4-9/10"})
    assert download(asset(body), tmp_path, opener=opener).read_bytes() == body


def test_corrupt_download_is_not_installed(tmp_path):
    item = asset(b"correct")
    with pytest.raises(UpdateError):
        download(item, tmp_path, opener=lambda *_: Response(b"corrupt"), attempts=1)
    assert not (tmp_path / (item["sha256"] + ".download")).exists()


def test_wrong_range_is_rejected(tmp_path):
    item = asset(b"complete")
    (tmp_path / (item["sha256"] + ".download")).write_bytes(b"com")
    with pytest.raises(UpdateError):
        download(item, tmp_path, opener=lambda *_: Response(b"plete", 206,
                 {"Content-Range": "bytes 2-7/8"}), attempts=1)


@pytest.mark.parametrize("kind,value", [("platform", "linux-x64"), ("schema_version", 2)])
def test_incompatible_manifest(tmp_path, kind, value):
    _, info = bundle(tmp_path)
    info[kind] = value
    with pytest.raises(UpdateError):
        validate_manifest(info)


def test_wrong_queue_and_external_url(tmp_path):
    _, info = bundle(tmp_path)
    info["data"]["queue_id"] = 450
    with pytest.raises(UpdateError):
        validate_manifest(info)
    info["data"]["queue_id"] = 2400
    info["app"]["url"] = "https://example.com/program.exe"
    with pytest.raises(UpdateError):
        validate_manifest(info)


def test_first_install_and_offline_reuse(tmp_path):
    archive, info = bundle(tmp_path)
    root = tmp_path / "中文 空白使用者"
    result = update(root, "2026.10.09.1", manifest_loader=lambda: info,
                    downloader=lambda *_: archive)
    assert result.data_dir and result.data_dir.is_dir()
    assert read_json(root / "active.json") == info
    def offline():
        raise OSError("offline")
    reused = update(root, "2026.10.09.1", manifest_loader=offline)
    assert reused.data_dir == result.data_dir
    assert "上次完整資料" in reused.message


def test_bad_new_archive_preserves_active_pointer(tmp_path):
    archive, info = bundle(tmp_path)
    root = tmp_path / "cache"
    result = update(root, "2026.10.09.1", manifest_loader=lambda: info, downloader=lambda *_: archive)
    old = (root / "active.json").read_bytes()
    replacement = manifest(b"different data")
    failed = update(root, "2026.10.09.1", manifest_loader=lambda: replacement, downloader=lambda *_: archive)
    assert failed.data_dir == result.data_dir
    assert (root / "active.json").read_bytes() == old


def test_damaged_installed_file_falls_back_to_bundle(tmp_path):
    archive, info = bundle(tmp_path)
    root = tmp_path / "cache"
    result = update(root, "2026.10.09.1", manifest_loader=lambda: info, downloader=lambda *_: archive)
    (result.data_dir / "composition.json").write_text("{}")
    def offline():
        raise OSError("offline")
    assert update(root, "2026.10.09.1", manifest_loader=offline).data_dir is None


@pytest.mark.parametrize("name", ["../evil.exe", "evil.pkl", "composition.json"])
def test_extra_traversal_or_duplicate_files_rejected(tmp_path, name):
    archive, info = bundle(tmp_path)
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr(name, b"untrusted")
    info = asset(archive.read_bytes())
    with pytest.raises(UpdateError):
        install_data(archive, tmp_path / "cache", info)
    assert not (tmp_path / "evil.exe").exists()


def test_cached_newer_app_launches_offline(tmp_path):
    root = tmp_path / "cache"
    app = {**asset(b"cached app", "ARAMRecommender.exe"), "version": "2026.10.09.2"}
    exe = root / "apps" / app["sha256"] / "ARAMRecommender.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"cached app")
    atomic_json(root / "app-active.json", app)
    def offline():
        raise OSError("offline")
    assert update(root, "2026.10.09.1", manifest_loader=offline).app_path == exe


def test_app_update_installs_verified_x64_and_keeps_old_exe(tmp_path):
    import struct
    body = bytearray(70)
    body[:2] = b"MZ"
    struct.pack_into("<I", body, 60, 64)
    body[64:] = b"PE\0\0\x64\x86"
    path = tmp_path / "new.exe"
    path.write_bytes(body)
    old = tmp_path / "original.exe"
    old.write_bytes(b"running original")
    installed = install_app(path, tmp_path / "cache", asset(bytes(body), "ARAMRecommender.exe"))
    assert installed.read_bytes() == body
    assert old.read_bytes() == b"running original"
    body[-2:] = b"\x4c\x01"  # x86 is incompatible with this channel
    path.write_bytes(body)
    with pytest.raises(UpdateError):
        install_app(path, tmp_path / "cache", asset(bytes(body), "ARAMRecommender.exe"))


def test_stale_manifest_does_not_downgrade_cached_app(tmp_path):
    archive, info = bundle(tmp_path)
    root = tmp_path / "cache"
    newer = {**asset(b"newer app", "ARAMRecommender.exe"), "version": "2026.10.09.2"}
    exe = root / "apps" / newer["sha256"] / "ARAMRecommender.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"newer app")
    atomic_json(root / "app-active.json", newer)
    def downloader(item, *_):
        assert item == info["data"]
        return archive
    result = update(root, "2026.10.08.1", manifest_loader=lambda: info, downloader=downloader)
    assert result.app_path == exe
    assert read_json(root / "app-active.json") == newer


def test_concurrent_updater_preserves_existing_data(tmp_path):
    archive, info = bundle(tmp_path)
    root = tmp_path / "cache"
    first = update(root, "2026.10.09.1", manifest_loader=lambda: info, downloader=lambda *_: archive)
    lock = (root / "update.lock").open("r+b")
    try:
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        def unexpected():
            pytest.fail("Concurrent instance must not request the manifest")
        second = update(root, "2026.10.09.1", manifest_loader=unexpected)
        assert second.data_dir == first.data_dir
    finally:
        lock.close()
