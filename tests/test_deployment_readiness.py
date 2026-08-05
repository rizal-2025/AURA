"""Provider-independent liveness and readiness boundary tests."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


class DeploymentReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            create_app(
                SimpleNamespace(
                    APP_ENV="test",
                    APP_NAME="AURA",
                    VERSION="test",
                )
            )
        )

    def test_liveness_has_a_fixed_public_response(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    def test_readiness_is_hidden_and_reports_ready(self):
        with patch("app.main.database_is_ready", return_value=True):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
        self.assertNotIn(
            "/ready",
            self.client.get("/openapi.json").json()["paths"],
        )

    def test_readiness_failure_is_safe_and_detail_free(self):
        with patch("app.main.database_is_ready", return_value=False):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        rendered = response.text.casefold()
        for forbidden in ("database_url", "postgresql", "sql", "password"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
