from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from aram_nn.site.player_history_public import (
    PlayerHistoryPublicConfig,
    PlayerHistoryPublicConfigurationError,
    create_player_history_public_app,
    main,
)
from aram_nn.site.player_history_snapshot import (
    build_player_history_graph_v1,
    publish_player_history_snapshot_v1,
)
from test_player_history_snapshot import LOOKUP_SECRET, config, source_row


CANDIDATE_SECRET = bytes(range(96, 128))
RATE_SECRET = bytes(range(160, 192))


class PlayerHistoryPublicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.snapshot = root / "snapshot.sqlite"
        graph = build_player_history_graph_v1([source_row()], config=config())
        publish_player_history_snapshot_v1(self.snapshot, graph)
        self.public_pem = root / "candidate-public.pem"
        self.public_pem.write_bytes(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        self.quarantine = root / "quarantine.sqlite"
        self.rate = root / "rate.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def app(
        self,
        *,
        limit: int = 20,
        trusted=frozenset(),
        origins=frozenset({"https://aram.test"}),
        snapshot: Path | None = None,
    ):
        return create_player_history_public_app(
            PlayerHistoryPublicConfig(
                snapshot_path=self.snapshot if snapshot is None else snapshot,
                lookup_secret=LOOKUP_SECRET,
                candidate_secret=CANDIDATE_SECRET,
                rate_secret=RATE_SECRET,
                candidate_public_key=self.private_key.public_key(),
                public_key_path=self.public_pem,
                quarantine_path=self.quarantine,
                rate_path=self.rate,
                allowed_origins=origins,
                trusted_proxy_peers=trusted,
                rate_limit=limit,
                clock_ms=lambda: 1000,
            )
        )

    def client(self, app, host="198.51.100.10"):
        return TestClient(app, client=(host, 50000))

    def assert_headers(self, response) -> None:
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def assert_cors(self, response, origin="https://aram.test") -> None:
        self.assertEqual(response.headers["access-control-allow-origin"], origin)
        self.assertEqual(response.headers["vary"], "Origin")

    def test_strict_preflight_and_post_cors_do_not_charge_invalid_requests(self) -> None:
        app = self.app(limit=1)
        good_headers = {
            "Origin": "https://aram.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        }
        with self.client(app) as client:
            preflight = client.options("/api/player-history/query", headers=good_headers)
            malformed = (
                client.options("/api/player-history/query", headers={}),
                client.options(
                    "/api/player-history/query",
                    headers={key: value for key, value in good_headers.items() if "Headers" not in key},
                ),
                client.options(
                    "/api/player-history/query",
                    headers={**good_headers, "Access-Control-Request-Method": "post"},
                ),
                client.options(
                    "/api/player-history/query",
                    headers={**good_headers, "Access-Control-Request-Headers": "content-type,x-test"},
                ),
                client.options(
                    "/api/player-history/query",
                    headers={**good_headers, "Access-Control-Request-Headers": "content-type,content-type"},
                ),
            )
            forbidden = (
                client.options(
                    "/api/player-history/query",
                    headers={**good_headers, "Origin": "https://evil.test"},
                ),
                client.options(
                    "/api/player-history/query",
                    headers={**good_headers, "Origin": "https://aram.test,https://evil.test"},
                ),
                client.options(
                    "/api/player-history/query",
                    headers=[*good_headers.items(), ("Origin", "https://aram.test")],
                ),
            )
            other_path = client.options("/not-query", headers=good_headers)
            admitted = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"Origin": "https://aram.test"},
            )
            limited = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"Origin": "https://aram.test"},
            )

        self.assertEqual(preflight.status_code, 204)
        self.assert_cors(preflight)
        self.assertEqual(preflight.headers["access-control-allow-methods"], "POST")
        self.assertEqual(preflight.headers["access-control-allow-headers"], "content-type")
        self.assertEqual(preflight.headers["access-control-max-age"], "600")
        for response in malformed:
            self.assertEqual(response.status_code, 400)
            self.assert_headers(response)
        for response in forbidden:
            self.assertEqual(response.status_code, 403)
            self.assert_headers(response)
        self.assertEqual(other_path.status_code, 404)
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        for response in (admitted, limited):
            self.assert_cors(response)

    def test_allowed_post_origin_is_reflected_on_fixed_errors_and_absent_is_allowed(self) -> None:
        app = self.app(limit=10)
        with self.client(app) as client:
            ready = client.post(
                "/api/player-history/query",
                json={"riot_id": "Player1#TW01"},
                headers={"Origin": "https://aram.test"},
            )
            bad = client.post(
                "/api/player-history/query?unexpected=1",
                json={"riot_id": "Player1#TW01"},
                headers={"Origin": "https://aram.test"},
            )
            absent = client.post(
                "/api/player-history/query", json={"riot_id": "Player1#TW01"}
            )
        for response in (ready, bad):
            self.assert_cors(response)
        self.assertNotIn("access-control-allow-origin", absent.headers)
        self.assertEqual((ready.status_code, bad.status_code, absent.status_code), (200, 400, 200))

    def test_ready_not_found_and_candidate_eligibility_have_fixed_allowlist(self) -> None:
        app = self.app()
        with self.client(app) as client:
            ready = client.post(
                "/api/player-history/query", json={"riot_id": "Player1#TW01"}
            )
            missing = client.post(
                "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
            )
        for response in (ready, missing):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                set(response.json()),
                {"status", "snapshot", "observed_matches", "low_sample", "histories"},
            )
            self.assert_headers(response)
            encoded = response.content.decode()
            for canary in ("Player1", "Unknown", LOOKUP_SECRET.hex(), str(self.snapshot)):
                self.assertNotIn(canary, encoded)
        self.assertEqual(
            missing.json(),
            {
                "status": "not_found",
                "snapshot": None,
                "observed_matches": None,
                "low_sample": None,
                "histories": [],
            },
        )
        # Both are low sample/not-found and therefore eligible; no raw aliases are stored.
        connection = sqlite3.connect(self.quarantine)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM player_seed_quarantine").fetchone(),
            (2,),
        )
        connection.close()
        raw = self.quarantine.read_bytes()
        self.assertNotIn(b"player1", raw)
        self.assertNotIn(b"unknown", raw)

    def test_origin_rejected_before_limiter_and_nontrusted_ignores_xff(self) -> None:
        app = self.app(limit=1)
        with self.client(app) as client:
            forbidden = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"Origin": "https://evil.test"},
            )
            allowed = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"X-Forwarded-For": "not-an-ip"},
            )
            limited = client.post(
                "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
            )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)

    def test_trusted_and_untrusted_forwarding_is_canonical_and_before_charge(self) -> None:
        trusted_app = self.app(limit=1, trusted=frozenset({"198.51.100.10"}))
        with self.client(trusted_app) as client:
            malformed = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"X-Forwarded-For": "203.0.113.7,203.0.113.8"},
            )
            duplicate = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers=[
                    ("X-Forwarded-For", "203.0.113.7"),
                    ("X-Forwarded-For", "203.0.113.8"),
                ],
            )
            admitted = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )
            exhausted = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )
        self.assertEqual(
            [response.status_code for response in (malformed, duplicate, admitted, exhausted)],
            [400, 400, 200, 429],
        )

        untrusted_app = self.app(limit=1)
        with self.client(untrusted_app, host="198.51.100.20") as client:
            ignored = client.post(
                "/api/player-history/query",
                json={"riot_id": "Unknown#TW00"},
                headers=[
                    ("X-Forwarded-For", "not-an-ip"),
                    ("X-Forwarded-For", "also-not-an-ip"),
                ],
            )
            exhausted = client.post(
                "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
            )
        self.assertEqual((ignored.status_code, exhausted.status_code), (200, 429))

    def test_synchronized_concurrent_limit_restart_and_separate_ip(self) -> None:
        app = self.app(limit=5)
        barrier = threading.Barrier(32)

        def send_one(_index: int) -> int:
            barrier.wait(timeout=10)
            with self.client(app) as client:
                return client.post(
                    "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
                ).status_code

        with ThreadPoolExecutor(max_workers=32) as executor:
            statuses = list(executor.map(send_one, range(32)))
        self.assertEqual(statuses.count(200), 5)
        self.assertEqual(statuses.count(429), 27)

        restarted = self.app(limit=5)
        with self.client(restarted) as same_client, self.client(
            restarted, host="198.51.100.11"
        ) as separate_client:
            self.assertEqual(
                same_client.post(
                    "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
                ).status_code,
                429,
            )
            self.assertEqual(
                separate_client.post(
                    "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
                ).status_code,
                200,
            )

    def test_trusted_proxy_canonical_query_body_and_duplicate_rejections(self) -> None:
        app = self.app(trusted=frozenset({"198.51.100.10"}))
        cases = (
            ({"X-Forwarded-For": "203.0.113.7, 203.0.113.8"}, b'{"riot_id":"Unknown#TW00"}'),
            ({"X-Forwarded-For": "203.0.113.7:80"}, b'{"riot_id":"Unknown#TW00"}'),
            ({}, b'{"riot_id":"a#123","riot_id":"b#123"}'),
            ({}, b"x" * 2049),
        )
        with self.client(app) as client:
            for headers, body in cases:
                response = client.post(
                    "/api/player-history/query",
                    content=body,
                    headers={"Content-Type": "application/json", **headers},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"status": "bad_request"})
                self.assert_headers(response)
            query_string = client.post(
                "/api/player-history/query?riot_id=Secret#TW1",
                json={"riot_id": "Unknown#TW00"},
            )
            self.assertEqual(query_string.status_code, 400)
            self.assertNotIn("Secret", query_string.text)

    def test_snapshot_identity_change_returns_fixed_503(self) -> None:
        app = self.app()
        self.snapshot.write_bytes(self.snapshot.read_bytes() + b"x")
        with self.client(app) as client:
            response = client.post(
                "/api/player-history/query", json={"riot_id": "Unknown#TW00"}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotIn(str(self.snapshot), response.text)

    def test_full_sample_ready_never_enters_candidate_quarantine(self) -> None:
        full_snapshot = Path(self.temporary.name) / "full.sqlite"
        graph = build_player_history_graph_v1(
            [source_row(game_id, created_ms=game_id) for game_id in range(1, 21)],
            config=config(),
        )
        publish_player_history_snapshot_v1(full_snapshot, graph)
        app = self.app(snapshot=full_snapshot)
        with self.client(app) as client:
            response = client.post(
                "/api/player-history/query", json={"riot_id": "Player1#TW01"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["low_sample"])
        connection = sqlite3.connect(self.quarantine)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM player_seed_quarantine").fetchone(),
            (0,),
        )
        connection.close()

    def test_operational_path_aliases_fail_before_service_start(self) -> None:
        base = PlayerHistoryPublicConfig(
            snapshot_path=self.snapshot,
            lookup_secret=LOOKUP_SECRET,
            candidate_secret=CANDIDATE_SECRET,
            rate_secret=RATE_SECRET,
            candidate_public_key=self.private_key.public_key(),
            public_key_path=self.public_pem,
            quarantine_path=self.quarantine,
            rate_path=self.rate,
        )
        cases = (
            {"quarantine_path": self.snapshot},
            {"rate_path": self.snapshot},
            {"public_key_path": self.snapshot},
        )
        from dataclasses import replace

        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(
                PlayerHistoryPublicConfigurationError
            ):
                create_player_history_public_app(replace(base, **changes))

    def test_main_configures_one_bounded_uvicorn_worker(self) -> None:
        sentinel_config = object()
        sentinel_app = object()
        with (
            mock.patch(
                "aram_nn.site.player_history_public.load_player_history_public_config",
                return_value=(sentinel_config, "127.0.0.1", 8766),
            ),
            mock.patch(
                "aram_nn.site.player_history_public.create_player_history_public_app",
                return_value=sentinel_app,
            ),
            mock.patch("aram_nn.site.player_history_public.uvicorn.run") as run,
        ):
            result = CliRunner().invoke(main)
        self.assertEqual(result.exit_code, 0, result.output)
        run.assert_called_once_with(
            sentinel_app,
            host="127.0.0.1",
            port=8766,
            access_log=False,
            log_level="warning",
            limit_concurrency=32,
            backlog=64,
            workers=1,
        )


if __name__ == "__main__":
    unittest.main()
