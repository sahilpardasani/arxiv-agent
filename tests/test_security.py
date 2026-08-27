import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True


class SecurityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy_env = patch.dict(os.environ, {"ENABLE_LEGACY_MODE": "false"}, clear=False)
        cls.legacy_env.start()
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.legacy_env.stop()

    def test_working_directory_is_not_public(self):
        for path in ("/static/.env", "/static/main.py", "/static/.git/config"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_trigger_fails_closed_without_server_token(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/api/trigger-analysis")
        self.assertEqual(response.status_code, 503)

    def test_trigger_rejects_wrong_token(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "correct"}, clear=False):
            response = self.client.post(
                "/api/trigger-analysis",
                headers={"Authorization": "Bearer wrong"},
            )
        self.assertEqual(response.status_code, 401)

    def test_trigger_accepts_correct_token_without_running_pipeline(self):
        with (
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "correct"}, clear=False),
            patch.object(main, "legacy_mode_enabled", return_value=False),
            patch.object(main, "enqueue_analysis", return_value=(True, "job-1")),
        ):
            response = self.client.post(
                "/api/trigger-analysis",
                headers={"Authorization": "Bearer correct"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_trigger_fails_closed_when_queue_is_unavailable(self):
        with (
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "correct"}, clear=False),
            patch.object(main, "legacy_mode_enabled", return_value=False),
            patch.object(main, "enqueue_analysis", side_effect=RuntimeError("down")),
        ):
            response = self.client.post("/api/trigger-analysis", headers={"Authorization": "Bearer correct"})
        self.assertEqual(response.status_code, 503)

    def test_authenticated_trigger_dispatches_in_legacy_mode(self):
        with (
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "correct"}, clear=False),
            patch.object(main, "legacy_mode_enabled", return_value=True),
            patch.object(main.legacy_runtime, "dispatch", return_value=True) as dispatch,
        ):
            response = self.client.post("/api/trigger-analysis", headers={"Authorization": "Bearer correct"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "started")
        dispatch.assert_called_once_with("manual")

    def test_security_headers_are_present(self):
        response = self.client.get("/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_large_request_body_is_rejected(self):
        response = self.client.post("/api/trigger-analysis", content=b"x" * 1_048_577)
        self.assertEqual(response.status_code, 413)

    def test_invalid_date_is_rejected(self):
        response = self.client.get("/api/papers", params={"date": "../../etc/passwd"})
        self.assertEqual(response.status_code, 422)

    def test_public_api_is_rate_limited(self):
        main._local_request_history.clear()
        with patch.dict(os.environ, {"API_RATE_LIMIT": "120"}, clear=False):
            statuses = [self.client.get("/api/dates").status_code for _ in range(121)]
        self.assertEqual(statuses[-1], 429)
        main._local_request_history.clear()

    def test_local_rate_limit_state_has_a_hard_key_cap(self):
        main._local_request_history.clear()
        with patch.dict(os.environ, {"LOCAL_RATE_LIMIT_MAX_KEYS": "100"}, clear=False):
            for index in range(100):
                self.assertIsNone(main._local_rate_limit(f"client:{index}", 10, 60))
            self.assertEqual(main._local_rate_limit("client:overflow", 10, 60), 60)
        self.assertEqual(len(main._local_request_history), 100)
        main._local_request_history.clear()


if __name__ == "__main__":
    unittest.main()
