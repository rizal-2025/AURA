"""Provider-independent liveness and readiness boundary tests."""

from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.db.database import deployment_engine_options


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

    def test_railway_service_configs_are_bounded_and_migration_free(self):
        api = tomllib.loads(
            (PROJECT_ROOT / "deploy/railway/aura-api.toml").read_text(
                encoding="utf-8"
            )
        )
        cleanup = tomllib.loads(
            (PROJECT_ROOT / "deploy/railway/aura-cleanup.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(api["build"]["builder"], "DOCKERFILE")
        self.assertEqual(api["deploy"]["numReplicas"], 1)
        self.assertEqual(api["deploy"]["healthcheckPath"], "/ready")
        self.assertIn("--host ::", api["deploy"]["startCommand"])
        self.assertIn("${PORT}", api["deploy"]["startCommand"])
        self.assertNotIn("migrat", api["deploy"]["startCommand"].casefold())
        self.assertEqual(cleanup["deploy"]["cronSchedule"], "17 * * * *")
        self.assertEqual(
            cleanup["deploy"]["startCommand"],
            "python -m app.jobs.demo_cleanup --once --batch-size 100",
        )
        self.assertEqual(cleanup["deploy"]["restartPolicyType"], "NEVER")

    def test_managed_postgresql_pool_is_small_and_has_no_overflow(self):
        options = deployment_engine_options(
            "postgresql+psycopg://user:password@postgres:5432/aura_demo"
        )
        self.assertEqual(options["pool_size"], 5)
        self.assertEqual(options["max_overflow"], 0)
        self.assertEqual(options["pool_timeout"], 5)
        self.assertTrue(options["pool_pre_ping"])
        self.assertEqual(deployment_engine_options("sqlite:///local.db"), {})


if __name__ == "__main__":
    unittest.main()
