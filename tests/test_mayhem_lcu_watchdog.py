from __future__ import annotations

import importlib.util
import sqlite3
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
