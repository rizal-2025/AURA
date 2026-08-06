"""Provider-independent liveness and readiness boundary tests."""

from pathlib import Path
from types import SimpleNamespace
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

    def test_koyeb_image_is_single_worker_non_root_and_migration_free(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER aura", dockerfile)
        self.assertIn("${HOST:-0.0.0.0}", dockerfile)
        self.assertIn("${PORT:-8000}", dockerfile)
        self.assertIn("--workers 1", dockerfile)
        self.assertIn("--no-access-log", dockerfile)
        command = next(
            line for line in dockerfile.splitlines() if line.startswith("CMD ")
        )
        self.assertNotIn("migrat", command.casefold())

    def test_github_maintenance_workflows_are_manual_or_guarded(self):
        cleanup = (PROJECT_ROOT / ".github/workflows/demo-cleanup.yml").read_text(
            encoding="utf-8"
        )
        migration = (
            PROJECT_ROOT / ".github/workflows/demo-migration.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("cron: '17 * * * *'", cleanup)
        self.assertIn("AURA_DEMO_CLEANUP_SCHEDULE_ENABLED", cleanup)
        self.assertIn("workflow_dispatch:", cleanup)
        self.assertIn("workflow_dispatch:", migration)
        self.assertNotIn("pull_request:", cleanup + migration)
        self.assertNotIn("push:", cleanup + migration)
        self.assertIn("contents: read", cleanup + migration)
        self.assertIn("cancel-in-progress: false", cleanup + migration)
        self.assertIn("NEON_MAINTENANCE_DATABASE_URL", cleanup + migration)

    def test_demo_runtime_hides_internal_routes_from_openapi(self):
        demo_client = TestClient(
            create_app(
                SimpleNamespace(
                    APP_ENV="demo",
                    APP_NAME="AURA",
                    VERSION="test",
                )
            )
        )
        paths = demo_client.get("/openapi.json").json()["paths"]
        self.assertIn("/chat", paths)
        self.assertNotIn("/internal/demo/sessions", paths)
        self.assertNotIn("/internal/demo/chat", paths)
        self.assertNotEqual(
            demo_client.post("/internal/demo/sessions").status_code,
            404,
        )

    def test_managed_postgresql_pool_is_small_and_has_no_overflow(self):
        options = deployment_engine_options(
            "postgresql+psycopg://user:password@postgres:5432/aura_demo"
        )
        self.assertEqual(options["pool_size"], 2)
        self.assertEqual(options["max_overflow"], 0)
        self.assertEqual(options["pool_timeout"], 5)
        self.assertTrue(options["pool_pre_ping"])
        self.assertEqual(deployment_engine_options("sqlite:///local.db"), {})


if __name__ == "__main__":
    unittest.main()
