"""Overwolf augment bridge: receiver files, port ownership, live-offer gating."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aram_nn.augment_bridge import make_server, start_bridge_thread

try:
    import scripts.recommend_gui as rg

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-dependent
    rg = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def _offer(*names: str) -> dict:
    return {
        "app": "aram-mayhem-augment-probe",
        "event": {
            "type": "offer",
            "source": "onInfoUpdates2",
            "augments": [{"slot": f"augment_{i}", "name": n, "raw": {"name": n}} for i, n in enumerate(names, 1)],
        },
    }


class BridgeServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.server = make_server(self.out, "127.0.0.1", 0, persist_probe_log=False)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_offer_written_to_latest_and_log(self) -> None:
        self.assertTrue(self._post("/augment-event", _offer("神聖之火", "靈魂淨化", "普羅能量炮"))["ok"])
        latest = json.loads((self.out / "latest_augments.json").read_text(encoding="utf-8"))
        names = [a["name"] for a in latest["payload"]["event"]["augments"]]
        self.assertEqual(names, ["神聖之火", "靈魂淨化", "普羅能量炮"])
        self.assertEqual(len((self.out / "augment_events.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_fragment_offer_does_not_replace_latest(self) -> None:
        self._post("/augment-event", _offer("速度碎片", "攻擊碎片", "生命碎片"))
        self.assertFalse((self.out / "latest_augments.json").exists())
        self.assertTrue((self.out / "latest_fragments.json").exists())

    def test_probe_log_not_persisted_by_default(self) -> None:
        self._post("/probe-log", {"kind": "raw_info_update", "payload": {}})
        self.assertTrue((self.out / "latest_probe.json").exists())
        self.assertFalse((self.out / "probe_events.jsonl").exists())

    def test_second_bridge_on_same_port_backs_off(self) -> None:
        self.assertIsNone(start_bridge_thread(self.out, "127.0.0.1", self.port))


@unittest.skipIf(rg is None, f"recommend_gui import failed: {_IMPORT_ERROR}")
class LiveOfferTest(unittest.TestCase):
    NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

    def _event(self, kind: str = "offer", age_sec: float = 5) -> dict:
        return {
            "type": kind,
            "received_at": (self.NOW - timedelta(seconds=age_sec)).isoformat(),
            "augments": [{"slot": "augment_1", "name": "神聖之火"}],
        }

    def test_fresh_offer_is_live(self) -> None:
        self.assertTrue(rg.is_live_offer(self._event(), now=self.NOW))

    def test_stale_offer_hidden(self) -> None:
        self.assertFalse(rg.is_live_offer(self._event(age_sec=rg.AUGMENT_OFFER_TTL_SEC + 1), now=self.NOW))

    def test_picked_hides_offer(self) -> None:
        self.assertFalse(rg.is_live_offer(self._event(kind="picked"), now=self.NOW))

    def test_missing_or_empty(self) -> None:
        self.assertFalse(rg.is_live_offer(None, now=self.NOW))
        event = self._event()
        event["augments"] = []
        self.assertFalse(rg.is_live_offer(event, now=self.NOW))


@unittest.skipIf(rg is None, f"recommend_gui import failed: {_IMPORT_ERROR}")
class AdvisorStatsTest(unittest.TestCase):
    def test_uses_full_champion_table_and_keeps_screen_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp) / "api"
            (api / "champions").mkdir(parents=True)
            augs = {str(i): {"name_zh": f"A{i}", "rarity": "kGold"} for i in (1, 2, 3)}
            # tier-list only lists augment 1 for the champion; 2 lives in the full table
            champs = {"904": {"name_zh": "薩亨", "top": {"kGold": [{"id": 1, "wr": 0.50, "lift": -0.01, "score": -0.01}]}}}
            (api / "tier-list.json").write_text(json.dumps({"augs": augs, "champs": champs}), encoding="utf-8")
            stats = {"904": {"kGold": [{"id": 2, "wr": 0.58, "lift": 0.02, "score": 0.02}]}}
            (api / "champions" / "augment-stats.json").write_text(json.dumps(stats), encoding="utf-8")
            advisor = rg.AugmentAdvisor(api / "tier-list.json")
            rows = advisor.recommend(904, [{"id": 1}, {"id": 2}, {"id": 3}])
            self.assertEqual([r["id"] for r in rows], [2, 1, 3])  # best first
            self.assertEqual([r["order"] for r in rows], [2, 1, 3])  # screen slot kept
            self.assertEqual(rows[0]["wr"], 0.58)
            self.assertIsNone(rows[2]["wr"])

    def test_rank_is_by_win_rate_within_rarity_and_pick_passes_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp) / "api"
            (api / "champions").mkdir(parents=True)
            augs = {str(i): {"name_zh": f"A{i}", "rarity": "kGold"} for i in (1, 2, 3, 9)}
            (api / "tier-list.json").write_text(json.dumps({"augs": augs, "champs": {}}), encoding="utf-8")
            stats = {"904": {
                # listed by score; rank must follow win rate
                "kGold": [{"id": 1, "wr": 0.55, "score": 0.03, "pick": 0.04},
                          {"id": 2, "wr": 0.60, "score": 0.01, "pick": 0.02},
                          {"id": 3, "wr": 0.50, "score": 0.00}],
                "kSilver": [{"id": 9, "wr": 0.70}],  # other rarity: ranked separately
            }}
            (api / "champions" / "augment-stats.json").write_text(json.dumps(stats), encoding="utf-8")
            rows = rg.AugmentAdvisor(api / "tier-list.json").recommend(904, [{"id": 1}, {"id": 2}, {"id": 3}])
            by_id = {r["id"]: r for r in rows}
            self.assertEqual({i: by_id[i]["rank"] for i in (1, 2, 3)}, {1: 2, 2: 1, 3: 3})
            self.assertEqual((by_id[1]["pick"], by_id[3]["pick"]), (0.04, None))


class AliasMapTest(unittest.TestCase):
    def setUp(self) -> None:
        if rg is None:
            self.skipTest(f"recommend_gui import failed: {_IMPORT_ERROR}")

    def test_site_payload_backs_up_missing_abilities_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tier-list.json"
            payload.write_text(json.dumps({"champs": {
                "72": {"alias": "Skarner", "name_en": "Skarner"},
                "62": {"alias": "MonkeyKing", "name_en": "Wukong"},
            }}), encoding="utf-8")
            with mock.patch.object(rg, "load_fallback_champion_names", return_value={}):
                aliases = rg.load_champion_alias_to_id(payload)
        self.assertEqual(aliases["skarner"], 72)
        self.assertEqual(aliases["monkeyking"], 62)  # Live Client rawChampionName form


class ChampionRecheckTest(unittest.TestCase):
    def setUp(self) -> None:
        if rg is None:
            self.skipTest(f"recommend_gui import failed: {_IMPORT_ERROR}")

    def _app(self, advisor_rows: dict[int, list[dict]]):
        from types import SimpleNamespace

        advisor = SimpleNamespace(recommend=lambda cid, augs: advisor_rows.get(cid, [{"id": 1, "wr": None}]))
        return SimpleNamespace(active_champion_id=None, augment_advisor=advisor, _alias_to_id={"skarner": 72})

    def test_no_data_asks_live_client_once_per_offer(self) -> None:
        app = self._app({72: [{"id": 1, "wr": 0.49}]})
        augs = [{"id": 1}]
        with mock.patch.object(rg, "get_live_active_champion", return_value=72) as live:
            rows = rg.RecommenderApp._recheck_augment_champion(app, augs, [{"id": 1, "wr": None}])
            again = rg.RecommenderApp._recheck_augment_champion(app, augs, [{"id": 1, "wr": None}])
        self.assertEqual((app.active_champion_id, rows[0]["wr"]), (72, 0.49))
        self.assertEqual(again[0]["wr"], None)  # same offer: not asked again (Tk thread)
        self.assertEqual(live.call_count, 1)


if __name__ == "__main__":
    unittest.main()
