"""Windows task, operation-log, and cleanup-health hardening coverage."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
WRAPPER = WINDOWS_ROOT / "Run-DemoCleanup.ps1"
TASKS = WINDOWS_ROOT / "Register-AuraTasks.ps1"
STATUS = WINDOWS_ROOT / "Get-AuraPublicDemoStatus.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DemoCleanupWindowsStaticTests(unittest.TestCase):
    def test_task_action_has_deterministic_repository_working_directory(self):
        tasks = read(TASKS)
        self.assertIn("$repositoryRoot = Assert-AuraRepositoryLayout", tasks)
        self.assertIn("-WorkingDirectory $repositoryRoot", tasks)
        self.assertIn('-File `"$cleanupScript`"', tasks)
        self.assertIn("-Mode Execute", tasks)
        self.assertIn("-Confirmation RUN_AURA_DEMO_CLEANUP", tasks)

    def test_wrapper_fails_closed_and_enters_verified_repository(self):
        wrapper = read(WRAPPER)
        ordered = (
            "Assert-AuraProductionProfile",
            "AURA_CLEANUP_CONFIRMATION_REQUIRED",
            "Assert-AuraRepositoryLayout",
            "Push-Location -LiteralPath $repositoryRoot",
            "Assert-AuraOperatorSecretAcl -Path $configPath",
            "Assert-AuraOperatorSecretAcl -Path $pgPassPath",
            "Import-AuraConfiguration -Profile production",
            "Assert-AuraProductionConfiguration",
            "Test-AuraPostgreSQLServiceRunning",
            "Test-AuraPostgreSQLLoopbackListener",
            "Test-AuraProductionDatabaseReadiness",
            "app.jobs.demo_cleanup",
        )
        positions = [wrapper.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("[string]$Mode = 'DryRun'", wrapper)
        self.assertIn("'--dry-run'", wrapper)
        self.assertIn("Write-AuraCleanupOperationLog", wrapper)

    def test_task_and_wrapper_do_not_embed_secret_values(self):
        combined = read(TASKS) + read(WRAPPER)
        for forbidden in (
            "postgresql+psycopg://",
            "DEMO_BFF_SERVICE_TOKEN=",
            "AUTH_JWT_SECRET=",
            "PGPASSWORD",
        ):
            self.assertNotIn(forbidden.casefold(), combined.casefold())

    def test_status_exposes_cleanup_health_without_requiring_configuration(self):
        common = read(COMMON)
        status = read(STATUS)
        for value in (
            "CLEANUP_NOT_CONFIGURED",
            "CLEANUP_NEVER_RAN",
            "CLEANUP_HEALTHY",
            "CLEANUP_STALE",
            "CLEANUP_FAILED",
        ):
            self.assertIn(value, common + status)
        self.assertIn("cleanup_health", status)
        self.assertIn("cleanup_last_success_age", status)
        not_configured_block = status[status.index("$cleanupHealth =") :]
        self.assertNotIn("$reasons.Add('CLEANUP_NOT_CONFIGURED')", not_configured_block)


@unittest.skipUnless(os.name == "nt", "PowerShell behavior tests require Windows")
class DemoCleanupWindowsPowerShellTests(unittest.TestCase):
    def invoke(self, body: str, **environment: str) -> subprocess.CompletedProcess[str]:
        command = f". '{COMMON}'; $ErrorActionPreference='Stop'; {body}"
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            env={**os.environ, **environment},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_cleanup_operation_log_records_success_failure_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraLogRoot = $env:AURA_TEST_LOG_ROOT
function Initialize-AuraDataDirectories { New-Item -ItemType Directory -Path $script:AuraLogRoot -Force | Out-Null }
function Remove-AuraExpiredFiles { param($Root,$Filter,$RetentionDays,$PreservePath) }
Write-AuraCleanupOperationLog -Profile production -Mode dry-run -EligibleSessions 3 -AttemptedSessions 0 -SuccessfulCleanupCount 0 -FailedCleanupCount 0 -Result success -ElapsedMs 10
Write-AuraCleanupOperationLog -Profile production -Mode execute -EligibleSessions 2 -AttemptedSessions 2 -SuccessfulCleanupCount 2 -FailedCleanupCount 0 -Result success -ElapsedMs 20
Write-AuraCleanupOperationLog -Profile production -Mode execute -EligibleSessions 2 -AttemptedSessions 2 -SuccessfulCleanupCount 1 -FailedCleanupCount 1 -Result partial_failure -ElapsedMs 30
Write-AuraCleanupOperationLog -Profile production -Mode execute -EligibleSessions 0 -AttemptedSessions 0 -SuccessfulCleanupCount 0 -FailedCleanupCount 0 -Result failure -ElapsedMs 40
$lines = @(Get-Content -LiteralPath (Get-ChildItem -LiteralPath $script:AuraLogRoot -File | Select-Object -First 1).FullName)
if ($lines.Count -ne 4) { throw 'line-count' }
if ($lines[0] -notmatch 'mode=dry-run eligible_sessions=3 attempted_sessions=0 .* result=success elapsed_ms=10$') { throw 'dry-run' }
if ($lines[1] -notmatch 'mode=execute eligible_sessions=2 attempted_sessions=2 successful_cleanup_count=2 failed_cleanup_count=0 result=success') { throw 'success' }
if ($lines[2] -notmatch 'failed_cleanup_count=1 result=partial_failure') { throw 'failure' }
if ($lines[3] -notmatch 'result=failure elapsed_ms=40$') { throw 'total-failure' }
Write-Output 'CLEANUP_LOG_OK'
"""
            result = self.invoke(body, AURA_TEST_LOG_ROOT=directory)
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "CLEANUP_LOG_OK")

    def test_cleanup_health_classifies_absent_never_stale_healthy_and_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraLogRoot = $env:AURA_TEST_LOG_ROOT
New-Item -ItemType Directory -Path $script:AuraLogRoot -Force | Out-Null
function Get-ScheduledTask { param($TaskName,$ErrorAction) $null }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_NOT_CONFIGURED') { throw 'absent' }
function Get-ScheduledTask { param($TaskName,$ErrorAction) [PSCustomObject]@{ State='Disabled' } }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_NOT_CONFIGURED') { throw 'disabled' }
function Get-ScheduledTask { param($TaskName,$ErrorAction) [PSCustomObject]@{ State='Ready' } }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_NEVER_RAN') { throw 'never' }
$now = [DateTime]::UtcNow
$path = Join-Path $script:AuraLogRoot 'operations-20260809.log'
function Add-CleanupRecord([DateTime]$Timestamp, [string]$Result) {
    $line = 'timestamp={0} profile=production stage=CLEANUP mode=execute eligible_sessions=1 attempted_sessions=1 successful_cleanup_count=1 failed_cleanup_count=0 result={1} elapsed_ms=10' -f $Timestamp.ToUniversalTime().ToString('o'), $Result
    Add-Content -LiteralPath $path -Value $line -Encoding ascii
}
Add-CleanupRecord -Timestamp $now.AddHours(-4) -Result success
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_STALE') { throw 'stale' }
Add-CleanupRecord -Timestamp $now.AddMinutes(-2) -Result success
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_HEALTHY') { throw 'healthy' }
Add-CleanupRecord -Timestamp $now.AddMinutes(-1) -Result failure
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_FAILED') { throw 'failed' }
Write-Output 'CLEANUP_HEALTH_OK'
"""
            result = self.invoke(body, AURA_TEST_LOG_ROOT=directory)
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "CLEANUP_HEALTH_OK")


if __name__ == "__main__":
    unittest.main()
