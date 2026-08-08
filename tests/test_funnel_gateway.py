"""Exact public route inventory and safety properties for the Funnel gateway."""

import json
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from app.funnel_main import create_funnel_app


EXPECTED_ROUTES = {
    ("GET", "/health"),
    ("POST", "/internal/demo/sessions"),
    ("GET", "/internal/demo/sessions/current"),
    ("POST", "/internal/demo/chat"),
    ("GET", "/internal/demo/reservations"),
    ("POST", "/internal/demo/reset"),
}


class FunnelGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_funnel_app(
            SimpleNamespace(APP_NAME="AURA", VERSION="test")
        )
        cls.client = TestClient(cls.app)

    def test_route_inventory_is_exact(self):
        def concrete_routes(router):
            for route in router.routes:
                included = getattr(route, "original_router", None)
                if included is not None:
                    yield from concrete_routes(included)
                elif hasattr(route, "methods"):
                    yield route

        actual = {
            (method, route.path)
            for route in concrete_routes(self.app)
            for method in route.methods
        }
        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_documentation_and_non_demo_surfaces_are_absent(self):
        for path in (
            "/",
            "/ready",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/chat",
            "/reservations",
            "/telegram",
            "/admin",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_slash_variants_do_not_redirect(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("location", response.headers)

    def test_health_is_fixed_and_detail_free(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    def test_unauthenticated_requests_do_not_reach_database(self):
        reached = False

        def forbidden_database_dependency():
            nonlocal reached
            reached = True
            raise AssertionError("database dependency reached")

        from app.db.database import get_db

        self.app.dependency_overrides[get_db] = forbidden_database_dependency
        try:
            response = self.client.post("/internal/demo/sessions")
        finally:
            self.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(reached)

    def test_session_boundary_logs_only_fixed_safe_diagnostics(self):
        marker = "never-log-unauthenticated-header"
        with self.assertLogs("AURA", level="INFO") as captured:
            response = self.client.post(
                "/internal/demo/sessions",
                headers={"X-BFF-Service-Token": marker},
            )
        diagnostics = []
        for record in captured.records:
            try:
                value = json.loads(record.getMessage())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("route") == "session_create":
                diagnostics.append(value)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0]["code"], "REQUEST_RECEIVED")
        self.assertEqual(diagnostics[1]["code"], "RESPONSE_4XX")
        for diagnostic in diagnostics:
            self.assertEqual(
                set(diagnostic), {"route", "stage", "elapsedMs", "code"}
            )
        rendered = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn(marker, rendered)
        self.assertNotIn("/internal/demo/sessions", rendered)


if __name__ == "__main__":
    unittest.main()
