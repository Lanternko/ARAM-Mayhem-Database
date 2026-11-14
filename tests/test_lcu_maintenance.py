from datetime import datetime, timezone
import json

import pytest

from aram_nn.lcu import maintenance as M


NOW = datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc)
MESSAGE = "我們正在進行例行維護，預計2026/10/21 11:00 GMT+08結束，在此期間遊戲暫不開放遊玩。"


def test_notice_expires_at_the_stated_local_deadline():
    notice = M.active_notice([{"message": MESSAGE}], NOW)
    assert notice["ends_at"] == "2026-10-21T11:00:00+08:00"
    assert M.active_notice([{"message": MESSAGE}], datetime(2026, 10, 21, 3, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize("text", [
    MESSAGE.replace("正在進行", "即將進行"),
    MESSAGE.replace("11:00", "25:00"),
    MESSAGE.replace("2026/10/21", "2026/10/22"),
    MESSAGE.replace("GMT+08", ""),
    "我們正在進行維護，稍後結束。",
])
def test_planned_unbounded_and_malformed_notices_do_not_disable_recovery(text):
    assert M.active_notice([{"message": text}], NOW) is None


def test_lcu_outage_uses_confirmed_notice_only_until_expiry(monkeypatch, tmp_path):
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps(M.active_notice([{"message": MESSAGE}], NOW)), encoding="utf-8")
    monkeypatch.setattr(M, "get_credentials", lambda: None)
    assert M.probe_maintenance(path, NOW)["ends_at"].endswith("11:00:00+08:00")
    assert M.probe_maintenance(path, datetime(2026, 10, 21, 3, tzinfo=timezone.utc)) is None


def test_confirmed_notice_is_cleared_when_riot_removes_it(monkeypatch, tmp_path):
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps(M.active_notice([{"message": MESSAGE}], NOW)), encoding="utf-8")
    monkeypatch.setattr(M, "get_credentials", lambda: type("Creds", (), {"port": 1, "token": "private"})())
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, path):
            return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: []})()
    monkeypatch.setattr(M.httpx, "Client", Client)
    assert M.probe_maintenance(path, NOW) is None
    assert json.loads(path.read_text()) is None
