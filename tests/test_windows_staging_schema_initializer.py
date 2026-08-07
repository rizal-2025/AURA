"""Executable regression tests for the Windows staging schema wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
INITIALIZER = WINDOWS_ROOT / "Initialize-AuraPostgreSQLStagingSchema.ps1"


def schema_payload(
    *,
    operation: str = "plan",
    status: str = "ready",
    classification: str = "additive-empty-schema",
    expected_tables: int = 10,
    actual_tables: int = 0,
) -> str:
    converged = classification == "converged"
    return json.dumps(
        {
            "status": status,
            "operation": operation,
            "classification": classification,
            "expectedTableCount": expected_tables,
            "actualTableCount": actual_tables,
            "expectedColumnCount": 88,
            "matchingColumnCount": 88 if converged else 0,
            "matchingPrimaryKeyCount": 10 if converged else 0,
            "matchingTableStructureCount": 10 if converged else 0,
        }
    )


@unittest.skipUnless(os.name == "nt", "PowerShell regression requires Windows")
class SchemaProcessResultTests(unittest.TestCase):
    def invoke(
        self,
        *,
        operation: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f". '{COMMON}';"
            "$ErrorActionPreference='Stop';"
            "try {"
            "ConvertFrom-AuraSchemaProcessResult "
            "-Operation $env:AURA_DUMMY_OPERATION "
            "-ExitCode ([int]$env:AURA_DUMMY_EXIT_CODE) "
            "-StandardOutput $env:AURA_DUMMY_STDOUT "
            "-StandardError $env:AURA_DUMMY_STDERR | Out-Null;"
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
                "AURA_DUMMY_OPERATION": operation,
                "AURA_DUMMY_EXIT_CODE": str(exit_code),
                "AURA_DUMMY_STDOUT": stdout,
                "AURA_DUMMY_STDERR": stderr,
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_exit_zero_valid_json_with_stderr_warning_succeeds(self):
        warning = "Synthetic harmless deprecation warning"
        result = self.invoke(
            operation="plan",
            exit_code=0,
            stdout=schema_payload(),
            stderr=warning,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AURA_DUMMY_RESULT_ACCEPTED", result.stdout)
        self.assertNotIn(warning, result.stdout + result.stderr)

    def test_nonzero_exit_fails_closed_even_with_success_json(self):
        result = self.invoke(
            operation="plan",
            exit_code=1,
            stdout=schema_payload(),
            stderr="",
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("AURA_STAGING_SCHEMA_PLAN_OPERATION_FAILED", result.stdout)

    def test_malformed_stdout_fails_closed(self):
        result = self.invoke(
            operation="plan",
            exit_code=0,
            stdout="not-json",
            stderr="",
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("AURA_STAGING_SCHEMA_RESULT_INVALID", result.stdout)

    def test_failed_and_blocked_statuses_fail_closed(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                result = self.invoke(
                    operation="plan",
                    exit_code=0,
                    stdout=schema_payload(status=status),
                    stderr="",
                )
                self.assertEqual(result.returncode, 7)
                self.assertIn(
                    "AURA_STAGING_SCHEMA_PLAN_OPERATION_FAILED",
                    result.stdout,
                )

    def test_unexpected_schema_fails_closed(self):
        result = self.invoke(
            operation="plan",
            exit_code=0,
            stdout=schema_payload(actual_tables=1),
            stderr="",
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("AURA_STAGING_SCHEMA_STATE_INVALID", result.stdout)

    def test_apply_and_verify_require_exact_ten_table_convergence(self):
        for operation in ("apply-empty-schema", "verify"):
            with self.subTest(operation=operation):
                result = self.invoke(
                    operation=operation,
                    exit_code=0,
                    stdout=schema_payload(
                        operation=operation,
                        status="verified",
                        classification="converged",
                        actual_tables=10,
                    ),
                    stderr="warning remains suppressed",
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class SchemaTemporaryCredentialCleanupTests(unittest.TestCase):
    def test_temporary_credential_cleanup_is_unconditional_finally_contract(self):
        script = INITIALIZER.read_text(encoding="utf-8")
        outer_try = script.index("try {", script.index("$tempPath ="))
        finalizer = script.index("} finally {", outer_try)
        cleanup = script.index(
            "Remove-Item -LiteralPath $tempPath -Force",
            finalizer,
        )
        success = script.index("AURA_STAGING_SCHEMA_INITIALIZED", cleanup)
        self.assertLess(outer_try, finalizer)
        self.assertLess(finalizer, cleanup)
        self.assertLess(cleanup, success)


if __name__ == "__main__":
    unittest.main()
