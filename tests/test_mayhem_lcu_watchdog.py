from __future__ import annotations

import importlib.util
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mayhem_lcu_watchdog_under_test",
    ROOT / "scripts" / "mayhem_lcu_watchdog.py",
)
assert SPEC and SPEC.loader
WATCHDOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHDOG)


def _create_games(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE games (
            game_id TEXT PRIMARY KEY,
            queue_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL
        )
        """
    )


def test_latest_capture_prefers_constant_time_watermark(tmp_path: Path) -> None:
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    _create_games(con)
    con.execute(
        "CREATE TABLE crawl_runtime_state ("
        "state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO games VALUES ('1', 2400, '2026-08-10T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO crawl_runtime_state VALUES "
        "('latest_capture:2400', 'payload-time', '2026-08-11T15:00:00+00:00')"
    )
    con.commit()
    con.close()

    assert WATCHDOG.latest_capture_at(db) == "2026-08-11T15:00:00+00:00"


def test_latest_capture_falls_back_to_last_inserted_matching_queue(tmp_path: Path) -> None:
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    _create_games(con)
    con.executemany(
        "INSERT INTO games VALUES (?, ?, ?)",
        [
            ("1", 2400, "2026-08-11T10:00:00+00:00"),
            ("2", 450, "2026-08-11T11:00:00+00:00"),
            ("3", 2400, "2026-08-11T12:00:00+00:00"),
            ("4", 4310, "2026-08-11T13:00:00+00:00"),
        ],
    )
    con.commit()
    con.close()

    assert WATCHDOG.latest_capture_at(db) == "2026-08-11T12:00:00+00:00"


def _league_process(name: str, rss_mb: float, private_mb: float | None) -> object:
    memory_info = SimpleNamespace(rss=int(rss_mb * 1024 * 1024))
    if private_mb is not None:
        memory_info.private = int(private_mb * 1024 * 1024)
    return SimpleNamespace(
        info={
            "pid": 1234,
            "name": name,
            "exe": f"C:/League/{name}",
            "memory_info": memory_info,
        }
    )


def test_private_memory_drives_threshold_action_when_rss_is_low(monkeypatch) -> None:
    monkeypatch.setattr(
        WATCHDOG,
        "iter_processes",
        lambda: [_league_process("LeagueClient.exe", 1000.0, 5000.0)],
    )

    rows = WATCHDOG.league_processes()
    args = _restart_args(client_restart_mb=4500.0)

    assert rows[0]["rss_mb"] == 1000.0
    assert rows[0]["private_mb"] == 5000.0
    assert rows[0]["pressure_mb"] == 5000.0
    assert WATCHDOG.should_restart_client(
        args, {"ok": True, "phase": "None"}, WATCHDOG.league_main_mb()
    )[0] is True


def test_private_memory_falls_back_to_rss_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        WATCHDOG,
        "iter_processes",
        lambda: [_league_process("LeagueClient.exe", 3200.0, None)],
    )

    row = WATCHDOG.league_processes()[0]

    assert row["rss_mb"] == 3200.0
    assert row["private_mb"] is None
    assert row["pressure_mb"] == 3200.0
    assert row["pressure_metric"] == "rss_mb"


def test_high_rss_remains_protected_when_private_memory_is_lower(monkeypatch) -> None:
    monkeypatch.setattr(
        WATCHDOG,
        "iter_processes",
        lambda: [_league_process("LeagueClient.exe", 5000.0, 1200.0)],
    )

    row = WATCHDOG.league_processes()[0]

    assert row["pressure_mb"] == 5000.0
    assert WATCHDOG.league_main_mb() == 5000.0


def test_only_main_executable_selects_memory_threshold(monkeypatch) -> None:
    monkeypatch.setattr(
        WATCHDOG,
        "iter_processes",
        lambda: [
            _league_process("LeagueClientUx.exe", 1000.0, 9000.0),
            _league_process("LeagueClient.exe", 1000.0, 2000.0),
        ],
    )

    assert WATCHDOG.league_main_mb() == 2000.0


def test_iter_processes_fetches_expensive_fields_only_for_relevant_names(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self, pid: int, name: str) -> None:
            self.info = {"pid": pid, "name": name}
            self.detail_attrs: list[str] | None = None

        def as_dict(self, attrs):
            self.detail_attrs = list(attrs)
            return {"exe": self.info["name"], "cmdline": [], "memory_info": None}

    unrelated = FakeProcess(1, "explorer.exe")
    relevant = FakeProcess(2, "pythonw.exe")
    monkeypatch.setattr(
        WATCHDOG.psutil,
        "process_iter",
        lambda attrs: [unrelated, relevant],
    )

    rows = WATCHDOG.iter_processes()

    assert rows == [relevant]
    assert unrelated.detail_attrs is None
    assert relevant.detail_attrs == ["exe", "cmdline", "memory_info"]


def _watchdog_args(monkeypatch, tmp_path: Path, *, restart_client: bool) -> object:
    argv = ["mayhem_lcu_watchdog", "--once", "--no-site-publisher", "--no-model-refresher"]
    argv.append("--restart-client" if restart_client else "--no-restart-client")
    monkeypatch.setattr(sys, "argv", argv)
    args = WATCHDOG.parse_args()
    args.db = tmp_path / "games.db"
    args.log_dir = tmp_path / "logs"
    args.state_file = tmp_path / "state.jsonl"
    args.model_refresh_state = tmp_path / "model-refresh.json"
    args.restart_client = restart_client
    args.worker_stall_min = 0.0
    args.degrade_client_mb = 3900.0
    args.client_restart_mb = 4500.0
    args.worker_start_max_client_mb = 6500.0
    return args


def _resource_sample(*, commit_percent: float, available_mb: float = 8192.0):
    return WATCHDOG.ResourceSample(
        available_mb=available_mb,
        commit_total_mb=commit_percent,
        commit_limit_mb=100.0,
        commit_percent=commit_percent,
    )


def _stub_check_once(monkeypatch, tmp_path: Path, *, restart_client: bool, workers: list[dict[str, object]], sample):
    args = _watchdog_args(monkeypatch, tmp_path, restart_client=restart_client)
    current_workers = list(workers)
    stopped: list[int] = []
    started: list[int] = []
    closed: list[bool] = []

    def get_workers() -> list[dict[str, object]]:
        return list(current_workers)

    def stop_workers(*args, **kwargs) -> list[int]:
        stopped.append(1)
        current_workers.clear()
        return [101]

    def start_workers(start_args, count: int) -> dict[str, object]:
        started.append(count)
        current_workers[:] = [
            {"pid": 202, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", str(count)]}
        ]
        return {"pid": 202, "workers": count}

    monkeypatch.setattr(WATCHDOG, "snowball_workers", get_workers)
    monkeypatch.setattr(WATCHDOG, "stop_snowball_workers", stop_workers)
    monkeypatch.setattr(WATCHDOG, "start_snowball_fleet", start_workers)
    monkeypatch.setattr(
        WATCHDOG,
        "league_main_metrics",
        lambda: {
            "rss_mb": 100.0,
            "private_mb": 1000.0,
            "pressure_mb": 1000.0,
            "pressure_metric": "max(rss_mb,private_mb)",
        },
    )
    monkeypatch.setattr(WATCHDOG, "lcu_health", lambda: {"ok": True, "phase": "InProgress"})
    monkeypatch.setattr(WATCHDOG, "latest_capture_age_min", lambda db: 226.0)
    monkeypatch.setattr(WATCHDOG, "sample_resources", lambda: sample)
    monkeypatch.setattr(WATCHDOG, "disk_free_sample", lambda path: {"path": "D:\\", "free_mb": 500_000.0})
    monkeypatch.setitem(WATCHDOG._DISK_STATE, "paused", False)
    monkeypatch.setattr(WATCHDOG, "model_refresh_health", lambda path: {"consecutive_failures": 0})
    monkeypatch.setattr(WATCHDOG, "static_site_publishers", lambda: [])
    monkeypatch.setattr(WATCHDOG, "model_refreshers", lambda: [])
    monkeypatch.setattr(WATCHDOG, "append_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(WATCHDOG, "close_league_client", lambda: closed.append(True) or [303])
    monkeypatch.setattr(WATCHDOG, "start_league_client", lambda: {"started": True})
    def unexpected_wait(*args, **kwargs):
        raise AssertionError("Unexpected client restart in mocked cycle")

    monkeypatch.setattr(WATCHDOG, "wait_for_lcu_ready", unexpected_wait)
    monkeypatch.setattr(WATCHDOG, "min_worker_uptime_min", lambda workers: 1000.0)
    return args, current_workers, stopped, started, closed


def test_same_worker_count_does_not_restart_fleet(monkeypatch, tmp_path: Path) -> None:
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch,
        tmp_path,
        restart_client=False,
        workers=workers,
        sample=_resource_sample(commit_percent=50.0),
    )

    record = WATCHDOG.check_once(args)

    assert stopped == []
    assert started == []
    assert closed == []
    assert record["actual_worker_count"] == 2
    assert record["desired_worker_count"] == 2


def test_resource_pause_suppresses_stall_and_unsafe_phase_restart(monkeypatch, tmp_path: Path) -> None:
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch,
        tmp_path,
        restart_client=True,
        workers=workers,
        sample=_resource_sample(commit_percent=95.0),
    )
    args.worker_stall_min = 30.0
    monkeypatch.setitem(WATCHDOG._STALL_STATE, "last_restart_monotonic", 0.0)
    monkeypatch.setitem(WATCHDOG._STALL_STATE, "consecutive_restarts", 0)

    record = WATCHDOG.check_once(args)

    assert stopped == [1]
    assert started == []
    assert closed == []
    assert record["resource_guard"]["capture_suppressed"] is True
    assert record["desired_worker_count"] == 0


def test_pause_after_client_restart_does_not_start_fleet_after_ready_wait(
    monkeypatch, tmp_path: Path
) -> None:
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch,
        tmp_path,
        restart_client=True,
        workers=workers,
        sample=_resource_sample(commit_percent=95.0),
    )
    samples = iter([_resource_sample(commit_percent=50.0), _resource_sample(commit_percent=95.0)])
    monkeypatch.setattr(WATCHDOG, "sample_resources", lambda: next(samples))
    monkeypatch.setattr(
        WATCHDOG,
        "lcu_health",
        lambda: {"ok": True, "phase": "None"},
    )
    monkeypatch.setattr(
        WATCHDOG,
        "league_main_metrics",
        lambda: {
            "rss_mb": 100.0,
            "private_mb": 1000.0,
            "pressure_mb": 6000.0,
            "pressure_metric": "max(rss_mb,private_mb)",
        },
    )
    monkeypatch.setattr(
        WATCHDOG,
        "wait_for_lcu_ready",
        lambda args: {
            "ready": True,
            "health": {"ok": True, "phase": "None"},
            "league_main_mb": 1000.0,
            "league_main_metrics": {
                "rss_mb": 100.0,
                "private_mb": 1000.0,
                "pressure_mb": 1000.0,
                "pressure_metric": "max(rss_mb,private_mb)",
            },
        },
    )

    record = WATCHDOG.check_once(args)

    assert closed == [True]
    assert stopped == [1]
    assert started == []
    assert record["desired_worker_count"] == 0


def test_high_client_pressure_in_unsafe_phase_stays_degraded(monkeypatch, tmp_path: Path) -> None:
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch,
        tmp_path,
        restart_client=True,
        workers=workers,
        sample=_resource_sample(commit_percent=50.0),
    )
    monkeypatch.setattr(
        WATCHDOG,
        "league_main_metrics",
        lambda: {
            "rss_mb": 100.0,
            "private_mb": 6000.0,
            "pressure_mb": 6000.0,
            "pressure_metric": "max(rss_mb,private_mb)",
        },
    )
    monkeypatch.setattr(WATCHDOG, "latest_capture_age_min", lambda db: 0.0)

    record = WATCHDOG.check_once(args)

    assert closed == []
    assert started == [1]
    assert record["desired_worker_count"] == 1
    assert record["actual_worker_count"] == 1



def test_degrade_and_recover_replaces_fleet_once_per_size_change(monkeypatch, tmp_path):
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=False, workers=workers,
        sample=_resource_sample(commit_percent=80.0),
    )
    samples = iter(_resource_sample(commit_percent=p) for p in [80, 80, 50, 50, 50])
    monkeypatch.setattr(WATCHDOG, "sample_resources", lambda: next(samples))
    counts = [WATCHDOG.check_once(args)["actual_worker_count"] for _ in range(5)]
    assert counts == [1, 1, 1, 1, 2]
    assert stopped == [1, 1]
    assert started == [1, 2]
    assert not closed


def test_paused_fleet_resumes_only_after_full_healthy_window(monkeypatch, tmp_path):
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=True, workers=workers,
        sample=_resource_sample(commit_percent=95.0),
    )
    samples = iter(_resource_sample(commit_percent=p) for p in [95, 50, 50, 50])
    monkeypatch.setattr(WATCHDOG, "sample_resources", lambda: next(samples))
    counts = [WATCHDOG.check_once(args)["actual_worker_count"] for _ in range(4)]
    assert counts == [0, 0, 0, 2]
    assert stopped == [1]
    assert started == [2]
    assert not closed


def _disk(free_mb):
    return {"path": "D:\\", "free_mb": free_mb, "total_mb": 700_000.0}


def test_full_disk_pauses_fleet_and_suppresses_stall_restarts(monkeypatch, tmp_path):
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=True, workers=workers,
        sample=_resource_sample(commit_percent=50.0),
    )
    args.worker_stall_min = 30.0
    monkeypatch.setitem(WATCHDOG._STALL_STATE, "last_restart_monotonic", 0.0)
    monkeypatch.setitem(WATCHDOG._STALL_STATE, "consecutive_restarts", 0)
    monkeypatch.setattr(WATCHDOG, "disk_free_sample", lambda path: _disk(0.0))

    first = WATCHDOG.check_once(args)
    second = WATCHDOG.check_once(args)

    assert stopped == [1]
    assert started == []
    assert closed == []
    assert first["desired_worker_count"] == 0
    assert first["resource_guard"]["state"] == "paused_disk"
    assert [a["action"] for a in first["actions"]] == ["pause_workers_disk_full", "pause_workers_resource", "keep_workers_paused_disk"]
    # The incident fires once per pause, not every tick.
    assert [a["action"] for a in second["actions"]] == ["keep_workers_paused_disk"]


def test_disk_pause_holds_until_resume_threshold(monkeypatch, tmp_path):
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=False, workers=[],
        sample=_resource_sample(commit_percent=50.0),
    )
    monkeypatch.setattr(WATCHDOG, "latest_capture_age_min", lambda db: 0.0)
    frees = iter([1_000.0, 8_000.0, 20_000.0])
    monkeypatch.setattr(WATCHDOG, "disk_free_sample", lambda path: _disk(next(frees)))

    desired = [WATCHDOG.check_once(args)["desired_worker_count"] for _ in range(3)]

    # 8 GB clears the 5 GB pause bar but not the 10 GB resume bar.
    assert desired[:2] == [0, 0]
    assert desired[2] > 0
    assert started == [desired[2]]


def test_unknown_disk_free_never_starts_a_pause(monkeypatch, tmp_path):
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=False, workers=workers,
        sample=_resource_sample(commit_percent=50.0),
    )
    monkeypatch.setattr(WATCHDOG, "disk_free_sample", lambda path: {"path": "D:\\", "free_mb": None, "error": "x"})

    record = WATCHDOG.check_once(args)

    assert record["desired_worker_count"] == 2
    assert stopped == []


def test_disk_free_sample_walks_up_to_existing_dir(tmp_path):
    sample = WATCHDOG.disk_free_sample(tmp_path / "missing" / "games.db")

    assert sample["path"] == str(tmp_path)
    assert sample["free_mb"] > 0


def test_high_client_start_gate_is_not_bypassed_for_degraded_fleet(monkeypatch, tmp_path):
    workers = [{"pid": 101, "rss_mb": 10.0, "cmdline": ["pythonw.exe", "--workers", "2"]}]
    args, current, stopped, started, closed = _stub_check_once(
        monkeypatch, tmp_path, restart_client=True, workers=workers,
        sample=_resource_sample(commit_percent=50.0),
    )
    monkeypatch.setattr(WATCHDOG, "league_main_metrics", lambda: {
        "rss_mb": 100, "private_mb": 7000, "pressure_mb": 7000,
        "pressure_metric": "max(rss_mb,private_mb)",
    })
    monkeypatch.setattr(WATCHDOG, "latest_capture_age_min", lambda db: 0.0)
    record = WATCHDOG.check_once(args)
    assert not started
    assert not closed
    assert record["actual_worker_count"] == 0


def test_start_league_client_clears_unreachable_remoting_zombie(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeProcess:
        pid = 4321
        info = {"name": "RiotClientServices.exe"}

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    zombie = FakeProcess()
    riot_client = tmp_path / "RiotClientServices.exe"
    launches: list[tuple[list[str], Path]] = []
    remoting_results = iter(
        [
            ("ERR", "URLError: connection refused"),
            (424, "Failed Dependency"),
            (200, "launch-session-id"),
        ]
    )

    monkeypatch.setattr(
        WATCHDOG,
        "remoting_request",
        lambda *args, **kwargs: next(remoting_results),
    )
    monkeypatch.setattr(WATCHDOG, "iter_processes", lambda: [zombie])
    monkeypatch.setattr(WATCHDOG.psutil, "wait_procs", lambda *args, **kwargs: None)
    monkeypatch.setattr(WATCHDOG, "find_riot_client", lambda: riot_client)
    monkeypatch.setattr(WATCHDOG, "wait_for_riot_remoting", lambda timeout_sec: True)
    monkeypatch.setattr(WATCHDOG.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        WATCHDOG.subprocess,
        "Popen",
        lambda args, cwd, stdout, stderr: launches.append((args, Path(cwd))),
    )

    result = WATCHDOG.start_league_client()

    assert zombie.killed is True
    assert result["killed_zombie_pids"] == [4321]
    assert result["remoting_status_before_launch"] == "ERR"
    assert result["remoting_error_before_launch"] == "URLError: connection refused"
    assert result["started"] is True
    assert result["remoting_launch_status"] == 200
    assert result["remoting_launch_attempts"] == 2
    assert result["remoting_launch_error"] is None
    assert launches == [
        (
            [
                str(riot_client),
                "--launch-product=league_of_legends",
                "--launch-patchline=live",
            ],
            tmp_path,
        )
    ]


def test_start_league_client_recycles_424_remoting_zombie(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeProcess:
        pid = 4242
        info = {"name": "Riot Client.exe"}

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    zombie = FakeProcess()
    riot_client = tmp_path / "RiotClientServices.exe"
    launches: list[tuple[list[str], Path]] = []
    remoting_results = iter(
        [
            (424, "HTTP Error 424: Failed Dependency"),
            (424, "HTTP Error 424: Failed Dependency"),
            (200, "launch-session-id"),
        ]
    )

    monkeypatch.setattr(
        WATCHDOG,
        "remoting_request",
        lambda *args, **kwargs: next(remoting_results),
    )
    monkeypatch.setattr(WATCHDOG, "iter_processes", lambda: [zombie])
    monkeypatch.setattr(WATCHDOG.psutil, "wait_procs", lambda *args, **kwargs: None)
    monkeypatch.setattr(WATCHDOG, "find_riot_client", lambda: riot_client)
    monkeypatch.setattr(WATCHDOG, "wait_for_riot_remoting", lambda timeout_sec: True)
    monkeypatch.setattr(WATCHDOG.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        WATCHDOG.subprocess,
        "Popen",
        lambda args, cwd, stdout, stderr: launches.append((args, Path(cwd))),
    )

    result = WATCHDOG.start_league_client()

    assert zombie.killed is True
    assert result["killed_zombie_pids"] == [4242]
    assert result["remoting_status_before_launch"] == 424
    assert result["remoting_error_before_launch"] == "HTTP Error 424: Failed Dependency"
    assert result["started"] is True
    assert result["remoting_launch_status"] == 200
    assert result["remoting_launch_attempts"] == 2
    assert result["remoting_launch_error"] is None
    assert launches == [
        (
            [
                str(riot_client),
                "--launch-product=league_of_legends",
                "--launch-patchline=live",
            ],
            tmp_path,
        )
    ]


def _restart_args(**overrides: object) -> object:
    import argparse

    defaults = {
        "safe_restart_phase": ["None", "EndOfGame"],
        "client_restart_mb": 5800.0,
        "unsafe_phase_restart_after_min": 45.0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_unsafe_phase_blocks_restart_while_captures_are_landing() -> None:
    """The phase gate must still protect a game that is actually being played."""
    args = _restart_args()
    health = {"ok": True, "phase": "InProgress"}

    restart, reason = WATCHDOG.should_restart_client(args, health, 6000.0, 0.5)

    assert restart is False
    assert "not safe to restart" in reason


def test_unsafe_phase_yields_once_captures_have_been_dead_too_long() -> None:
    """Breaks the 2026-08-28 deadlock: stuck phase + memory gate, no way out.

    LeagueClientUx crashed as a game ended and gameflow stayed at PreEndOfGame
    for 3.5h.  The phase gate refused to restart the client, the client grew to
    11.7GB, and the memory gate then refused to start workers -- 226 minutes
    with no captures and no mechanism able to release the other side.
    """
    args = _restart_args()
    health = {"ok": True, "phase": "PreEndOfGame"}

    restart, reason = WATCHDOG.should_restart_client(args, health, 11706.9, 226.0)

    assert restart is True
    assert "stuck" in reason
    assert "226min" in reason


def test_unsafe_phase_escape_hatch_respects_its_threshold() -> None:
    args = _restart_args(unsafe_phase_restart_after_min=45.0)
    health = {"ok": True, "phase": "PreEndOfGame"}

    assert WATCHDOG.should_restart_client(args, health, 11706.9, 44.0)[0] is False
    assert WATCHDOG.should_restart_client(args, health, 11706.9, 45.0)[0] is True


def test_missing_capture_age_never_forces_a_restart() -> None:
    """A caller with no age reading must not be treated as an infinite stall."""
    args = _restart_args()
    health = {"ok": True, "phase": "PreEndOfGame"}

    assert WATCHDOG.should_restart_client(args, health, 11706.9)[0] is False


def test_safe_phase_paths_are_unchanged() -> None:
    args = _restart_args()

    over_memory = WATCHDOG.should_restart_client(args, {"ok": True, "phase": "None"}, 6000.0, 0.0)
    unhealthy = WATCHDOG.should_restart_client(args, {"ok": False, "phase": "None"}, 900.0, 0.0)
    healthy = WATCHDOG.should_restart_client(args, {"ok": True, "phase": "None"}, 900.0, 0.0)

    assert over_memory[0] is True and "memory" in over_memory[1]
    assert unhealthy[0] is True and "health check failed" in unhealthy[1]
    assert healthy[0] is False
