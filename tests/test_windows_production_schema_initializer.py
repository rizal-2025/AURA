"""Executable regression tests for the Windows production schema wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
INITIALIZER = WINDOWS_ROOT / "Initialize-AuraPostgreSQLProductionSchema.ps1"


def schema_payload(*, actual_tables: int = 0) -> str:
    return json.dumps(
        {
            "status": "ready",
            "operation": "plan",
            "classification": "additive-empty-schema",
            "expectedTableCount": 10,
            "actualTableCount": actual_tables,
            "expectedColumnCount": 88,
            "matchingColumnCount": 0,
            "matchingPrimaryKeyCount": 0,
            "matchingTableStructureCount": 0,
        }
    )


@unittest.skipUnless(os.name == "nt", "PowerShell regression requires Windows")
class ProductionSchemaProcessResultTests(unittest.TestCase):
    def invoke(self, *, exit_code: int, stdout: str) -> subprocess.CompletedProcess[str]:
        script = (
            f". '{COMMON}';"
            "$ErrorActionPreference='Stop';"
            "try {"
            "ConvertFrom-AuraSchemaProcessResult "
            "-Profile production -Operation plan "
            "-ExitCode ([int]$env:AURA_DUMMY_EXIT_CODE) "
            "-StandardOutput $env:AURA_DUMMY_STDOUT "
            "-StandardError 'suppressed warning' | Out-Null;"
            "Write-Output 'AURA_DUMMY_RESULT_ACCEPTED'"
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
                "AURA_DUMMY_EXIT_CODE": str(exit_code),
                "AURA_DUMMY_STDOUT": stdout,
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_valid_plan_with_stderr_warning_succeeds(self):
        result = self.invoke(exit_code=0, stdout=schema_payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AURA_DUMMY_RESULT_ACCEPTED", result.stdout)
        self.assertNotIn("suppressed warning", result.stdout + result.stderr)

    def test_nonzero_exit_uses_production_safe_code(self):
        result = self.invoke(exit_code=1, stdout=schema_payload())
        self.assertEqual(result.returncode, 7)
        self.assertIn("AURA_PRODUCTION_SCHEMA_PLAN_OPERATION_FAILED", result.stdout)

    def test_unexpected_schema_uses_production_safe_code(self):
        result = self.invoke(exit_code=0, stdout=schema_payload(actual_tables=1))
        self.assertEqual(result.returncode, 7)
        self.assertIn("AURA_PRODUCTION_SCHEMA_STATE_INVALID", result.stdout)


class ProductionSchemaInitializerStaticTests(unittest.TestCase):
    def test_initializer_is_fixed_empty_only_and_secret_safe(self):
        script = INITIALIZER.read_text(encoding="utf-8")
        for expected in (
            "Import-AuraConfiguration -Profile production",
            "aura_demo_public",
            "aura_public_runtime",
            "Read-Host",
            "-AsSecureString",
            "additive-empty-schema",
            "actualTableCount -ne 0",
            "-Profile production",
        ):
            self.assertIn(expected, script)
        self.assertNotIn("aura_demo_staging", script)
        self.assertNotIn("aura_test", script)
        self.assertNotIn("PGPASSWORD", script)

    def test_temporary_credential_cleanup_is_unconditional(self):
        script = INITIALIZER.read_text(encoding="utf-8")
        outer_try = script.index("try {", script.index("$tempPath ="))
        finalizer = script.index("} finally {", outer_try)
        cleanup = script.index(
            "Remove-Item -LiteralPath $tempPath -Force",
            finalizer,
        )
        success = script.index("AURA_PRODUCTION_SCHEMA_INITIALIZED", cleanup)
        self.assertLess(outer_try, finalizer)
        self.assertLess(finalizer, cleanup)
        self.assertLess(cleanup, success)


if __name__ == "__main__":
    unittest.main()
