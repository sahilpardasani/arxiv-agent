import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main


class FrontendRegressionTests(unittest.TestCase):
    # A plain TestClient request does not start the app lifespan. This keeps the
    # MCP session manager available for the security suite's lifecycle tests.
    client = TestClient(main.app)

    def test_dashboard_serves_accessible_discovery_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        for expected in (
            'href="#papers"',
            'for="searchInput"',
            'type="search"',
            'aria-label="Filter papers"',
            'role="status"',
            'aria-live="polite"',
        ):
            self.assertIn(expected, html)

    def test_dashboard_keeps_security_and_keyboard_affordances(self):
        html = Path("simple_dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn(" onchange=", html)
        self.assertIn("function escapeHtml", html)
        self.assertIn("rel=\"noopener noreferrer\"", html)
        self.assertIn("event.key === '/'", html)


if __name__ == "__main__":
    unittest.main()
