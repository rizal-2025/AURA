"""Provider-independent liveness and readiness boundary tests."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.db.database import deployment_engine_options
from app.core.self_host_validation import (
    SelfHostConfigurationError,
    validate_self_host_runtime,
)


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

    def test_self_host_profiles_are_loopback_only(self):
        for profile, port in (("production", 8000), ("staging", 8001)):
            with self.subTest(profile=profile):
                runtime = validate_self_host_runtime(
                    profile=profile,
                    app_env="demo",
                    bind_host="127.0.0.1",
                    port=str(port),
                    database_url=(
                        "postgresql+psycopg://runtime:synthetic@127.0.0.1:5432/"
                        f"aura_demo_{profile}"
                    ),
                )
                self.assertEqual(runtime.host, "127.0.0.1")
                self.assertEqual(runtime.port, port)

    def test_self_host_rejects_non_loopback_and_wrong_profile_port(self):
        base = {
            "profile": "production",
            "app_env": "demo",
            "bind_host": "127.0.0.1",
            "port": "8000",
            "database_url": (
                "postgresql+psycopg://runtime:synthetic@127.0.0.1:5432/"
                "aura_demo_public"
            ),
        }
        for override in (
            {"bind_host": "0.0.0.0"},
            {"bind_host": "192.168.1.20"},
            {"port": "8001"},
            {"app_env": "production"},
            {"database_url": (
                "postgresql+psycopg://runtime:synthetic@db.example:5432/"
                "aura_demo_public"
            )},
        ):
            with self.subTest(override=tuple(override)):
                with self.assertRaises(SelfHostConfigurationError):
                    validate_self_host_runtime(**{**base, **override})

    def test_cloud_maintenance_workflows_are_removed(self):
        self.assertFalse((PROJECT_ROOT / ".github/workflows/demo-cleanup.yml").exists())
        self.assertFalse((PROJECT_ROOT / ".github/workflows/demo-migration.yml").exists())

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

    def test_local_postgresql_pool_is_small_and_has_no_overflow(self):
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
