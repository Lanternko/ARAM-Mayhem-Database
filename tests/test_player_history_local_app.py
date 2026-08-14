from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aram_nn.site.player_history_local_app import create_player_history_local_app
from aram_nn.site.player_history_snapshot import (
    build_player_history_graph_v1,
    publish_player_history_snapshot_v1,
)
from test_player_history_snapshot import LOOKUP_SECRET, config, source_row


PORT = 8765


class PlayerHistoryLocalAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temporary.name) / "snapshot.sqlite"
        graph = build_player_history_graph_v1([source_row()], config=config())
        publish_player_history_snapshot_v1(self.snapshot, graph)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _app(self):
        return create_player_history_local_app(
            snapshot_path=self.snapshot, lookup_secret=LOOKUP_SECRET, port=PORT
        )

    def _client(self, app, *, host="127.0.0.1", client_host="127.0.0.1"):
        return TestClient(
            app,
            base_url=f"http://{host}:{PORT}",
            client=(client_host, 50000),
        )

    def _bootstrapped(self):
        app, token = self._app()
        client = self._client(app)
        response = client.get(f"/bootstrap/{token}", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return app, token, client, response

    def assert_security_headers(self, response) -> None:
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_bootstrap_cookie_redirect_replay_and_invalid_are_uniform(self) -> None:
        app, token, client, response = self._bootstrapped()
        self.assertEqual(response.headers["location"], "/")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assert_security_headers(response)

        replay = self._client(app).get(f"/bootstrap/{token}")
        invalid = self._client(app).get("/bootstrap/not-the-token")
        self.assertEqual((replay.status_code, replay.content), (404, b""))
        self.assertEqual((invalid.status_code, invalid.content), (404, b""))
        self.assert_security_headers(replay)

    def test_auth_host_client_and_origin_fail_before_query(self) -> None:
        app, token = self._app()
        local = self._client(app)
        missing = local.get("/")
        wrong_host = self._client(app, host="evil.test").get("/")
        wrong_client = self._client(app, client_host="10.0.0.2").get("/")
        for response in (missing, wrong_host, wrong_client):
            self.assertEqual((response.status_code, response.content), (404, b""))
            self.assert_security_headers(response)

        local.get(f"/bootstrap/{token}", follow_redirects=False)
        bad_origin = local.post(
            "/query",
            data={"riot_id": "Player1#TW01"},
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual((bad_origin.status_code, bad_origin.content), (404, b""))

    def test_valid_ui_flow_has_safe_result_and_security_headers(self) -> None:
        _app, _token, client, _response = self._bootstrapped()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("本機對戰紀錄", page.text)
        result = client.post(
            "/query",
            data={"riot_id": "Player1#TW01"},
            headers={"Origin": f"http://127.0.0.1:{PORT}"},
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn("觀測場數：1", result.text)
        for canary in (
            "Player1",
            "TW01",
            "00000000-0000",
            LOOKUP_SECRET.hex(),
            str(self.snapshot),
            "lookup_key",
            "event_key",
        ):
            self.assertNotIn(canary, result.text)
        self.assert_security_headers(result)

    def test_body_input_caps_rate_limit_and_only_allowed_routes(self) -> None:
        app, _token, client, _response = self._bootstrapped()
        self.assertEqual(
            {route.path for route in app.routes},
            {"/bootstrap/{supplied_token}", "/", "/query"},
        )
        oversized = client.post(
            "/query",
            content=b"x" * 2049,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(oversized.status_code, 404)

        # Oversized input consumed one limiter slot; nineteen more are allowed.
        for _ in range(19):
            response = client.post("/query", json={"riot_id": "Unknown#TW00"})
            self.assertEqual(response.status_code, 200)
        limited = client.post("/query", json={"riot_id": "Unknown#TW00"})
        self.assertEqual(limited.status_code, 429)
        self.assert_security_headers(limited)


if __name__ == "__main__":
    unittest.main()
