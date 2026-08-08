"""Executable regression tests for trusted PostgreSQL CLI discovery on Windows."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMON = PROJECT_ROOT / "deploy" / "windows" / "AuraWindows.Common.ps1"


@unittest.skipUnless(os.name == "nt", "PowerShell regression requires Windows")
class PostgreSQLToolDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self._temporary_directory.name)
        self.install_root = self.temporary_root / "PostgreSQL"
        self.install_root.mkdir()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def make_tool(
        self,
        version: str,
        tool_name: str = "pg_dump.exe",
        *,
        runtime: bool = False,
    ) -> Path:
        relative = Path("pgAdmin 4", "runtime") if runtime else Path("bin")
        path = self.install_root / version / relative / tool_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic executable placeholder")
        return path.resolve()

    def invoke(
        self,
        *,
        tool_name: str = "pg_dump.exe",
        path_candidates: tuple[Path, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f". '{COMMON}';"
            "$ErrorActionPreference='Stop';"
            "$candidates = @();"
            "if (-not [string]::IsNullOrWhiteSpace($env:AURA_DUMMY_PATH_CANDIDATES)) {"
            "$candidates = @($env:AURA_DUMMY_PATH_CANDIDATES.Split(';'))"
            "};"
            "try {"
            "$selected = Select-AuraPostgreSQLTool "
            "-ToolName $env:AURA_DUMMY_TOOL_NAME "
            "-InstallRoot $env:AURA_DUMMY_INSTALL_ROOT "
            "-PathCandidates $candidates;"
            "Write-Output $selected"
            "} catch { Write-Output $_.Exception.Message; exit 7 }"
        )
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            cwd=PROJECT_ROOT,
            env={
                **os.environ,
                "AURA_DUMMY_INSTALL_ROOT": str(self.install_root),
                "AURA_DUMMY_TOOL_NAME": tool_name,
                "AURA_DUMMY_PATH_CANDIDATES": ";".join(map(str, path_candidates)),
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def assert_selected(
        self,
        result: subprocess.CompletedProcess[str],
        expected: Path,
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), expected)
        self.assertEqual(result.stderr, "")

    def test_trusted_tool_found_via_path_is_selected_first(self):
        path_tool = self.make_tool("16")
        self.make_tool("18")
        result = self.invoke(path_candidates=(path_tool,))
        self.assert_selected(result, path_tool)

    def test_missing_path_falls_back_to_highest_official_install(self):
        self.make_tool("15")
        expected = self.make_tool("18")
        result = self.invoke()
        self.assert_selected(result, expected)

    def test_numeric_version_order_is_used(self):
        self.make_tool("9.6")
        expected = self.make_tool("18")
        self.make_tool("17.5")
        result = self.invoke()
        self.assert_selected(result, expected)

    def test_pgadmin_runtime_is_not_preferred_over_official_bin(self):
        runtime = self.make_tool("18", runtime=True)
        expected = self.make_tool("17")
        result = self.invoke(path_candidates=(runtime,))
        self.assert_selected(result, expected)

    def test_pgadmin_runtime_is_a_last_resort(self):
        expected = self.make_tool("18", runtime=True)
        result = self.invoke()
        self.assert_selected(result, expected)

    def test_missing_tool_fails_closed(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "AURA_POSTGRESQL_TOOL_NOT_FOUND")
        self.assertEqual(result.stderr, "")

    def test_malicious_or_unexpected_path_is_rejected(self):
        outside = self.temporary_root / "attacker" / "pg_dump.exe"
        outside.parent.mkdir()
        outside.write_bytes(b"synthetic untrusted placeholder")
        unexpected = self.install_root / "18" / "tools" / "pg_dump.exe"
        unexpected.parent.mkdir(parents=True)
        unexpected.write_bytes(b"synthetic unexpected placeholder")

        for candidate in (outside, unexpected):
            with self.subTest(candidate=candidate.parent.name):
                result = self.invoke(path_candidates=(candidate.resolve(),))
                self.assertEqual(result.returncode, 7)
                self.assertEqual(
                    result.stdout.strip(), "AURA_POSTGRESQL_TOOL_NOT_FOUND"
                )
                self.assertEqual(result.stderr, "")

    def test_supported_executable_name_is_exact(self):
        self.make_tool("18", tool_name="psql.exe")
        result = self.invoke(tool_name="pg_dump.exe")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "AURA_POSTGRESQL_TOOL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
