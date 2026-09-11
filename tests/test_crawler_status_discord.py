from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3
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



def _healthy_status() -> dict:
    """A digest sample with nothing wrong, for tests that break one thing."""
    return {
        "ts": "2026-08-28T16:39:28+00:00",
        "worker_count": 2,
        "lcu_ok": True,
        "phase": None,
        "capture_age_min": 3.9,
        "league_main_mb": 565.7,
        "window_hours": 6,
        "watchdog": {"pid": 1, "uptime_min": 624.5},
        "workers_live": [
            {"pid": 2, "worker_id": "fleet", "producers": 2, "rss_mb": 32.3, "uptime_min": 86.1}
        ],
        "db": {
            "ok": True,
            "total_mayhem": 3035473,
            "window_saves": 13678,
            "latest_age_min": 3.9,
        },
        "current_patch": {
            "ok": True,
            "patch": "16.17.810",
            "patch_short": "16.17",
            "by_queue": {2400: 339229, 4310: 2317},
        },
        "incidents": {"ok": True, "counts": {}, "total": 0},
        "publish": {
            "ok": True,
            "synced": True,
            "published_commit": "4cc751f5",
            "publish_commit_age_h": 1.0,
            "stale": False,
            "stale_hours": 3.0,
        },
        "site_publish": {
            "ok": True,
            "last_publish_age_h": 1.0,
            "last_published_total": 44525,
            "stale": False,
            "crashing": False,
            "stale_hours": 12.0,
        },
    }


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
    # Git and site publishing share one "發布" field: two one-line legs, with
    # anything actionable promoted into the embed description instead.
    publish_field = next(field for field in embed["fields"] if field["name"] == "發布")

    assert embed["title"].endswith("運作正常")
    assert "已同步" in publish_field["value"]
    assert "6a8ee05f" in publish_field["value"]
    assert "未上線" not in publish_field["value"]
    assert embed["description"] == "✅ 沒有需要處理的事"


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


def test_fmt_minutes_switches_unit_past_an_hour() -> None:
    """624.5 分 was technically true and unreadable."""
    assert STATUS.fmt_minutes(3.86) == "3.9 分"
    assert STATUS.fmt_minutes(59) == "59.0 分"
    assert STATUS.fmt_minutes(86.1) == "1 小時 26 分"
    assert STATUS.fmt_minutes(624.5) == "10 小時 25 分"
    assert STATUS.fmt_minutes(120) == "2 小時"
    assert STATUS.fmt_minutes(60 * 24 * 3) == "3 天"
    assert STATUS.fmt_minutes(None) == "?"


def test_fmt_int_and_wan() -> None:
    assert STATUS.fmt_int(3035473) == "3,035,473"
    assert STATUS.fmt_int(None) == "?"
def test_current_patch_uses_newest_game_not_highest_version(tmp_path) -> None:
    """A hotfix build landing out of order must not relabel the live patch.

    Lexical ordering also gets this wrong on its own terms: '16.9' sorts above
    '16.17' as a string.
    """
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table games (game_id text primary key, queue_id integer not null,"
        " patch text not null, blue_champs text not null, red_champs text not null,"
        " blue_wins integer not null, duration_sec integer not null,"
        " created_ms integer not null, captured_at text not null)"
    )
    rows = [
        ("16.9.772", 5, 1_000),
        ("16.17.810", 3, 9_000),  # newest games -> the live patch
        ("16.17.809", 2, 8_000),
        ("16.16.804", 7, 5_000),
    ]
    n = 0
    for patch, count, created in rows:
        for _ in range(count):
            n += 1
            con.execute(
                "insert into games values (?,?,?,?,?,?,?,?,?)",
                (f"g{n}", 2400, patch, "[]", "[]", 1, 900, created, "2026-08-28T00:00:00+00:00"),
            )
    con.commit()
    con.close()

    stats = STATUS.current_patch_stats(db)
    assert stats["ok"]
    assert stats["patch"] == "16.17.810"
    assert stats["patch_short"] == "16.17"
    assert stats["games"] == 3
    # Hotfix builds of the same minor are one patch to a reader comparing
    # against the previous one.
    assert stats["games_minor"] == 5
def _incident_log(path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for ts, actions in rows:
            handle.write(
                json.dumps({"ts": ts, "actions": [{"action": a} for a in actions]}) + "\n"
            )


def test_recent_incidents_counts_only_inside_the_window(tmp_path) -> None:
    """'Is it up now' and 'did it stay up' are different questions.

    A window that restarted the client nine times and a window that never
    blinked both used to render as 運作正常.
    """
    now = dt.datetime.now(dt.timezone.utc)
    log = tmp_path / "watchdog.jsonl"
    _incident_log(
        log,
        [
            ((now - dt.timedelta(hours=20)).isoformat(), ["restart_league_client"]),
            ((now - dt.timedelta(hours=2)).isoformat(), ["restart_league_client"]),
            ((now - dt.timedelta(hours=1)).isoformat(), ["start_worker", "degrade_workers"]),
            ((now - dt.timedelta(minutes=5)).isoformat(), ["static_site_publisher_already_running"]),
        ],
    )

    out = STATUS.recent_incidents(log, window_hours=6)
    assert out["ok"]
    assert out["counts"] == {
        "restart_league_client": 1,
        "start_worker": 1,
        "degrade_workers": 1,
    }
    assert out["total"] == 3


def test_recent_incidents_ignores_planned_stop_half_of_a_restart(tmp_path) -> None:
    """stop_workers_before_client_restart is the planned half of a restart.

    Counting it alongside restart_league_client doubles every incident.
    """
    now = dt.datetime.now(dt.timezone.utc)
    log = tmp_path / "watchdog.jsonl"
    _incident_log(
        log,
        [
            (
                (now - dt.timedelta(minutes=30)).isoformat(),
                ["stop_workers_before_client_restart", "restart_league_client"],
            )
        ],
    )

    out = STATUS.recent_incidents(log, window_hours=6)
    assert out["total"] == 1
    assert "stop_workers_before_client_restart" not in out["counts"]


def test_recent_incidents_survives_a_mid_record_seek(tmp_path) -> None:
    """The tail read seeks by bytes, so the first line is usually a fragment."""
    now = dt.datetime.now(dt.timezone.utc)
    log = tmp_path / "watchdog.jsonl"
    with log.open("w", encoding="utf-8") as handle:
        handle.write('{"ts": "broken-frag')
        handle.write("\n")
        handle.write(
            json.dumps(
                {
                    "ts": (now - dt.timedelta(minutes=10)).isoformat(),
                    "actions": [{"action": "restart_league_client"}],
                }
            )
            + "\n"
        )

    out = STATUS.recent_incidents(log, window_hours=6)
    assert out["ok"]
    assert out["total"] == 1


def test_quiet_window_says_so_rather_than_staying_silent() -> None:
    status = _healthy_status()
    status["incidents"] = {"ok": True, "counts": {}, "total": 0}
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"] == "穩定度")
    assert "無中斷" in field["value"]


def test_incidents_surface_even_while_the_headline_is_healthy() -> None:
    """The whole point: a green digest must still admit it restarted 9 times."""
    status = _healthy_status()
    status["incidents"] = {
        "ok": True,
        "counts": {"restart_league_client": 9, "restart_workers_capture_stalled": 2},
        "total": 11,
    }
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"] == "穩定度")
    assert "客戶端重啟 **9** 次" in field["value"]
    assert "零產出強制重啟 **2** 次" in field["value"]
    assert embed["title"].endswith("運作正常")


def test_actionable_problem_lands_in_the_description_not_field_eight() -> None:
    """The 2026-08-28 digest buried a 26h publisher stall in the 8th of 10 boxes."""
    status = _healthy_status()
    status["site_publish"] = {
        "ok": True,
        "last_publish_age_h": 26.3,
        "last_published_total": 44525,
        "stale": True,
        "crashing": False,
        "stale_hours": 12.0,
    }
    embed = STATUS.format_message(status)["embeds"][0]
    assert "沒重建" in embed["description"]
    assert embed["title"].endswith("爬蟲正常但網站太久沒更新")
    # And it is not repeated verbatim further down.
    publish_field = next(f for f in embed["fields"] if f["name"] == "發布")
    assert "見上方" in publish_field["value"]


def test_idle_patch_does_not_read_as_a_stuck_publisher() -> None:
    """No new games means the publish gate legitimately has nothing to publish."""
    status = _healthy_status()
    status["db"] = dict(status["db"], window_saves=0)
    status["site_publish"] = {
        "ok": True,
        "last_publish_age_h": 26.3,
        "stale": True,
        "crashing": False,
        "stale_hours": 12.0,
    }
    embed = STATUS.format_message(status)["embeds"][0]
    assert "沒重建" not in embed["description"]


def _crawl_db(tmp_path, games, queue_rows=()):
    """A DB with just the columns these probes touch."""
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table games (game_id text primary key, queue_id integer not null,"
        " patch text not null, blue_champs text not null, red_champs text not null,"
        " blue_wins integer not null, duration_sec integer not null,"
        " created_ms integer not null, captured_at text not null)"
    )
    con.execute(
        "create table crawl_queue (queue_idx integer primary key autoincrement,"
        " puuid text not null unique, enqueued_at text not null,"
        " seed_family text not null default '')"
    )
    n = 0
    for queue_id, count in games:
        for _ in range(count):
            n += 1
            con.execute(
                "insert into games values (?,?,?,?,?,?,?,?,?)",
                (f"g{n}", queue_id, "16.17.810", "[]", "[]", 1, 900, n, "2026-08-28T00:00:00+00:00"),
            )
    for i, (enqueued_at, family) in enumerate(queue_rows):
        con.execute(
            "insert into crawl_queue (puuid, enqueued_at, seed_family) values (?,?,?)",
            (f"p{i}", enqueued_at, family),
        )
    con.commit()
    con.close()
    return db
def test_frontier_intake_counts_high_tier_seed_families(tmp_path) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(hours=1)).isoformat()
    old = (now - dt.timedelta(hours=30)).isoformat()
    db = _crawl_db(
        tmp_path,
        [(2400, 1)],
        [
            (recent, "opgg_tier:diamond:p10"),
            (recent, "apex"),
            (recent, "opgg_tier:gold:p9"),
            (recent, "opgg_level:p9"),
            (old, "apex"),
        ],
    )
    out = STATUS.frontier_intake(db, window_hours=6)
    assert out["ok"]
    assert out["total"] == 4
    # opgg_level is a level-based page with no rank attached, so it is not
    # high-tier however many players it brings in.
    assert out["high_tier"] == 2


def test_zero_intake_warns_while_still_collecting() -> None:
    """Throughput stays healthy for hours after the frontier stops being fed."""
    status = _healthy_status()
    status["frontier_intake"] = {"ok": True, "total": 0, "high_tier": 0}
    embed = STATUS.format_message(status)["embeds"][0]
    assert "沒有新玩家入隊" in embed["description"]
    assert not any(f["name"] == "前線新血" for f in embed["fields"])


def test_zero_intake_is_silent_when_nothing_is_being_collected() -> None:
    """A stopped crawler enqueues nothing by definition; that is not the news."""
    status = _healthy_status()
    status["db"] = dict(status["db"], window_saves=0)
    status["frontier_intake"] = {"ok": True, "total": 0, "high_tier": 0}
    embed = STATUS.format_message(status)["embeds"][0]
    assert "沒有新玩家入隊" not in embed["description"]


def test_intake_field_shows_the_high_tier_share() -> None:
    status = _healthy_status()
    status["frontier_intake"] = {"ok": True, "total": 716, "high_tier": 245}
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"] == "前線新血")
    assert "**716** 人" in field["value"]
    assert "高分段 **245** 人" in field["value"]


def test_intake_probe_failure_hides_the_field_rather_than_showing_zero() -> None:
    status = _healthy_status()
    status["frontier_intake"] = {"ok": False, "error": "db missing"}
    embed = STATUS.format_message(status)["embeds"][0]
    assert not any(f["name"] == "前線新血" for f in embed["fields"])
    assert "沒有新玩家入隊" not in embed["description"]
def _patch_db(tmp_path, rows):
    """rows: (queue_id, patch, count, created_ms)."""
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table games (game_id text primary key, queue_id integer not null,"
        " patch text not null, blue_champs text not null, red_champs text not null,"
        " blue_wins integer not null, duration_sec integer not null,"
        " created_ms integer not null, captured_at text not null)"
    )
    n = 0
    for queue_id, patch, count, created in rows:
        for _ in range(count):
            n += 1
            con.execute(
                "insert into games values (?,?,?,?,?,?,?,?,?)",
                (f"g{n}", queue_id, patch, "[]", "[]", 1, 900, created, "2026-08-28T00:00:00+00:00"),
            )
    con.commit()
    con.close()
    return db


def test_current_patch_uses_newest_game_not_highest_version(tmp_path) -> None:
    """A hotfix build landing out of order must not relabel the live patch.

    Lexical ordering also gets this wrong on its own terms: '16.9' sorts above
    '16.17' as a string.
    """
    db = _patch_db(
        tmp_path,
        [
            (2400, "16.9.772", 5, 1_000),
            (2400, "16.17.810", 3, 9_000),  # newest games -> the live patch
            (2400, "16.17.809", 2, 8_000),
            (2400, "16.16.804", 7, 5_000),
        ],
    )
    stats = STATUS.current_patch_stats(db)
    assert stats["ok"]
    assert stats["patch"] == "16.17.810"
    assert stats["patch_short"] == "16.17"
    # Hotfix builds of the same minor are one patch to a reader.
    assert stats["by_queue"] == {2400: 5}


def test_current_patch_counts_mayhem_and_classic_separately(tmp_path) -> None:
    db = _patch_db(
        tmp_path,
        [
            (2400, "16.17.810", 40, 9_000),
            (4310, "16.17.810", 3, 8_500),
            (4310, "16.16.804", 9, 5_000),  # previous patch, must not be added
            (450, "16.17.810", 6, 9_500),   # ARAM is not one of the two lanes
        ],
    )
    stats = STATUS.current_patch_stats(db)
    assert stats["by_queue"] == {2400: 40, 4310: 3}


def test_current_patch_names_the_patch_from_mayhem_alone(tmp_path) -> None:
    """Classic is a 10% side budget and can go a whole patch without a game."""
    db = _patch_db(tmp_path, [(2400, "16.17.810", 4, 9_000)])
    stats = STATUS.current_patch_stats(db)
    assert stats["ok"]
    assert stats["patch_short"] == "16.17"
    assert stats["by_queue"] == {2400: 4}


def test_current_patch_reports_no_mayhem_rather_than_guessing(tmp_path) -> None:
    db = _patch_db(tmp_path, [(4310, "16.17.810", 3, 9_000)])
    stats = STATUS.current_patch_stats(db)
    assert not stats["ok"]
    assert stats["error"] == "no mayhem rows"


def test_patch_cell_shows_both_lanes() -> None:
    embed = STATUS.format_message(_healthy_status())["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"].startswith("本 patch"))
    assert field["name"] == "本 patch `16.17`"
    assert "Mayhem **339,229** 場" in field["value"]
    assert "經典 **2,317** 場" in field["value"]


def test_patch_cell_shows_a_silent_classic_lane_as_zero() -> None:
    """Zero classic games this patch is a fact worth stating, not a blank."""
    status = _healthy_status()
    status["current_patch"] = dict(status["current_patch"], by_queue={2400: 100})
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"].startswith("本 patch"))
    assert "經典 **0** 場" in field["value"]


def test_patch_cell_survives_a_json_round_trip() -> None:
    """Queue ids are ints in-process and strings once serialized."""
    status = json.loads(json.dumps(_healthy_status()))
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"].startswith("本 patch"))
    assert "Mayhem **339,229** 場" in field["value"]


def test_capture_age_still_drives_the_headline_though_it_is_not_shown() -> None:
    """距上次收場 left the layout; it must not leave the health logic."""
    status = _healthy_status()
    status["worker_count"] = 0
    status["capture_age_min"] = 2.0
    embed = STATUS.format_message(status)["embeds"][0]
    assert "重啟中" in embed["title"]

    status["capture_age_min"] = 90.0
    embed = STATUS.format_message(status)["embeds"][0]
    assert embed["title"].endswith("爬蟲已停止")


def test_rate_is_reported_per_minute() -> None:
    """13,938 games over 6h is 38.7/min; a whole number would drop ~2% of it."""
    status = _healthy_status()
    status["db"] = dict(status["db"], window_saves=13938)
    embed = STATUS.format_message(status)["embeds"][0]
    field = next(f for f in embed["fields"] if f["name"].startswith("近 6 小時"))
    assert "約 38.7 場/分" in field["value"]
    assert "場/時" not in field["value"]
