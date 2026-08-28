from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "crawler_status_discord_under_test",
    ROOT / "scripts" / "crawler_status_discord.py",
)
assert SPEC and SPEC.loader
STATUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATUS)


def _write_state(path: Path, *, commit: str, age_hours: float) -> None:
    path.write_text(
        json.dumps(
            {
                "last_commit": commit,
                "last_publish_at_unix": time.time() - age_hours * 3600,
            }
        ),
        encoding="utf-8",
    )


def test_publish_status_ignores_diverged_primary_head(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "static_publish_state.json"
    _write_state(state, commit="6a8ee05f", age_hours=1.0)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "ls-remote":
            return subprocess.CompletedProcess(
                args, 0, "6a8ee05f2780968a2c8b5b67446ef9013a870308\trefs/heads/main\n", ""
            )
        if args[1:3] == ["merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(STATUS.subprocess, "run", fake_run)

    result = STATUS.publish_status(tmp_path, state, stale_hours=3.0)

    assert result["ok"] is True
    assert result["synced"] is True
    assert result["stale"] is False
    assert all("rev-list" not in call for call in calls)


def test_publish_status_flags_publisher_commit_missing_from_remote(
    monkeypatch, tmp_path: Path
) -> None:
    state = tmp_path / "static_publish_state.json"
    _write_state(state, commit="12345678", age_hours=4.0)

    def fake_run(args, **kwargs):
        if args[1] == "ls-remote":
            return subprocess.CompletedProcess(
                args, 0, "abcdef0123456789abcdef0123456789abcdef01\trefs/heads/main\n", ""
            )
        if args[1:3] == ["merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(STATUS.subprocess, "run", fake_run)

    result = STATUS.publish_status(tmp_path, state, stale_hours=3.0)

    assert result["ok"] is True
    assert result["synced"] is False
    assert result["stale"] is True
    assert result["publish_commit_age_h"] == 4.0


def test_format_message_reports_synced_publisher_commit() -> None:
    status = {
        "worker_count": 2,
        "lcu_ok": True,
        "capture_age_min": 0.5,
        "db": {"window_saves": 100, "total_mayhem": 1000},
        "publish": {
            "ok": True,
            "synced": True,
            "published_commit": "6a8ee05f",
            "remote_head": "6a8ee05f2780968a2c8b5b67446ef9013a870308",
            "publish_commit_age_h": 0.5,
            "stale": False,
            "stale_hours": 3.0,
        },
        "site_publish": {
            "ok": True,
            "last_publish_age_h": 0.5,
            "last_published_total": 41711,
            "stale": False,
            "crashing": False,
        },
        "workers_live": [],
        "worker_logs": [],
        "patch_mix_recent": {"16.16": 1},
        "window_hours": 6,
        "watchdog": None,
        "league_main_mb": 479.0,
    }

    payload = STATUS.format_message(status)
    embed = payload["embeds"][0]
    git_field = next(field for field in embed["fields"] if field["name"] == "Git 推送")

    assert embed["title"].endswith("運作正常")
    assert "已同步" in git_field["value"]
    assert "6a8ee05f" in git_field["value"]
    assert "未上線" not in git_field["value"]


def test_fleet_supervisor_counts_as_its_producers() -> None:
    """The fleet is one process running N producers, and must not read as zero.

    Before the 2026-08-27 single-writer migration each producer was its own
    `snowball` process.  The supervisor runs `snowball-workers` instead, so a
    detector keyed on the old subcommand reported workers=0 while four
    producers were happily saving games.
    """
    fleet_cmdline = [
        "C:/Python313/pythonw.exe",
        "-u",
        "D:/Projects/CODING/aram-winrate-nn/scripts/lcu_collector.py",
        "snowball-workers",
        "--db",
        "D:/Projects/CODING/aram-winrate-nn/data/lcu/games.db",
        "--workers",
        "2",
    ]
    legacy_cmdline = [
        "C:/Python313/pythonw.exe",
        "-u",
        "D:/Projects/CODING/aram-winrate-nn/scripts/lcu_collector.py",
        "snowball",
        "--worker-id",
        "W01",
    ]

    assert STATUS.snowball_subcommand(fleet_cmdline) == "snowball-workers"
    assert STATUS.snowball_subcommand(legacy_cmdline) == "snowball"
    assert STATUS.snowball_subcommand(["python", "scripts/build_tier_list.py"]) is None

    assert STATUS.producer_count(fleet_cmdline) == 2
    assert STATUS.producer_count(legacy_cmdline) == 1
    # A supervisor whose --workers is missing or unparseable still counts once,
    # so a malformed cmdline degrades to "something is running", never to zero.
    assert STATUS.producer_count(fleet_cmdline[:4]) == 1


def test_fleet_worker_count_drives_a_healthy_color() -> None:
    workers = [{"pid": 1, "worker_id": "fleet", "producers": 2, "rss_mb": 40.0, "uptime_min": 12.0}]
    count = sum(int(w.get("producers") or 1) for w in workers)

    assert count == 2
    # workers<=0 forced yellow/red on every digest while the fleet was invisible.
    assert STATUS.health_color(count, True, 2.0) == 0x57F287
    assert STATUS.health_color(0, True, 2.0) != 0x57F287
