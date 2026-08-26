import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from data_store import PaperRepository


class ScalabilityTests(unittest.TestCase):
    def test_missing_snapshot_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(PaperRepository(Path(directory)).ready())

    def test_snapshot_is_parsed_once_under_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.json"
            path.write_text(json.dumps({"papers": [{"paper": {"title": "x"}}]}))
            repository = PaperRepository(Path(directory))
            original = Path.read_bytes
            reads = 0

            def counted(target):
                nonlocal reads
                reads += 1
                return original(target)

            with patch.object(Path, "read_bytes", counted):
                with ThreadPoolExecutor(max_workers=20) as pool:
                    results = list(pool.map(lambda _: repository.current(), range(100)))
            self.assertEqual(reads, 1)
            self.assertTrue(all(result["papers"] for result in results))

    def test_concurrent_cached_reads_are_successful(self):
        main._local_request_history.clear()
        client = TestClient(main.app)
        with patch.dict("os.environ", {"API_RATE_LIMIT": "1000"}, clear=False):
            with ThreadPoolExecutor(max_workers=20) as pool:
                statuses = list(pool.map(lambda _: client.get("/api/papers").status_code, range(100)))
        self.assertEqual(set(statuses), {200})

    def test_forwarded_ip_ignored_from_untrusted_peer(self):
        client = TestClient(main.app)
        response = client.get("/health", headers={"X-Forwarded-For": "203.0.113.2"})
        self.assertEqual(response.status_code, 200)

    def test_nonexistent_dates_do_not_grow_response_cache(self):
        client = TestClient(main.app)
        main._response_cache.clear()
        main._local_request_history.clear()
        with patch.dict("os.environ", {"API_RATE_LIMIT": "1000"}, clear=False):
            for day in range(1, 29):
                response = client.get("/api/papers", params={"date": f"1900-01-{day:02d}"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total_papers"], 0)
        self.assertEqual(main._response_cache, {})


if __name__ == "__main__":
    unittest.main()
