from __future__ import annotations

import json
from types import SimpleNamespace

import psutil

from aram_nn.memory_forensics import MemoryPressureRecorder, top_memory_consumers

MB = 1024 * 1024


class FakeProc:
    def __init__(self, pid: int, name: str, private_mb: float, fail: bool = False) -> None:
        self.pid = pid
        self.info = {"name": name}
        self._private = int(private_mb * MB)
        self._fail = fail

    def memory_info(self) -> SimpleNamespace:
        if self._fail:
            raise psutil.AccessDenied(self.pid)
        return SimpleNamespace(private=self._private, rss=self._private // 2)


def test_top_memory_consumers_groups_by_name_and_skips_denied() -> None:
    procs = [
        FakeProc(1, "chrome.exe", 300),
        FakeProc(2, "chrome.exe", 700),
        FakeProc(3, "python.exe", 500),
        FakeProc(4, "System", 9999, fail=True),
    ]

    groups, total_mb = top_memory_consumers(limit=5, processes=procs)

    assert total_mb == 1500.0
    assert groups[0] == {
        "name": "chrome.exe",
        "count": 2,
        "private_mb": 1000.0,
        "rss_mb": 500.0,
        "top_pid": 2,
        "top_pid_private_mb": 700.0,
    }
    assert [g["name"] for g in groups] == ["chrome.exe", "python.exe"]


def make_recorder(tmp_path, clock):
    calls = []

    def sampler():
        calls.append(clock())
        return [{"name": "chrome.exe", "count": 17, "private_mb": 6500.0}], 34000.0

    return MemoryPressureRecorder(tmp_path, interval_sec=600, clock=clock, sampler=sampler), calls


def test_recorder_captures_on_pressure_then_throttles(tmp_path) -> None:
    now = [1_790_280_000.0]
    recorder, calls = make_recorder(tmp_path, lambda: now[0])
    pressured = {"system_pressure": True, "state": "paused"}
    sample = {"commit_total_mb": 64000.0}

    action = recorder.maybe_capture(pressured, sample)
    assert action["action"] == "memory_pressure_snapshot"
    assert action["unattributed_commit_mb"] == 30000.0
    assert action["top"] == ["chrome.exex17=6500MB"]
    record = json.loads(open(action["path"], encoding="utf-8").read())
    assert record["guard_state"] == "paused"
    assert record["top_processes"][0]["name"] == "chrome.exe"

    now[0] += 60
    assert recorder.maybe_capture(pressured, sample) is None
    now[0] += 600
    assert recorder.maybe_capture(pressured, sample) is not None
    assert len(calls) == 2


def test_recorder_ignores_client_only_pressure_and_rearms(tmp_path) -> None:
    now = [1_790_280_000.0]
    recorder, calls = make_recorder(tmp_path, lambda: now[0])

    assert recorder.maybe_capture({"system_pressure": False, "client_pressure": True}, {}) is None
    assert calls == []

    recorder.maybe_capture({"system_pressure": True}, {})
    recorder.maybe_capture({"system_pressure": False}, {})
    now[0] += 5
    # A fresh pressure episode snapshots immediately instead of waiting out the interval.
    assert recorder.maybe_capture({"system_pressure": True}, {}) is not None
    assert len(calls) == 2


def test_recorder_prunes_old_snapshots(tmp_path) -> None:
    now = [1_790_280_000.0]
    recorder, _ = make_recorder(tmp_path, lambda: now[0])
    recorder.keep_files = 2
    for _ in range(4):
        recorder.maybe_capture({"system_pressure": True}, {})
        now[0] += 601

    assert len(list(tmp_path.glob("memory_pressure_*.json"))) == 2


def test_real_process_sampling_smoke() -> None:
    groups, total_mb = top_memory_consumers(limit=3)
    assert groups and total_mb > 0
