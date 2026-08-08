"""Regression coverage for the one-command Production lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
START = WINDOWS_ROOT / "Start-AuraPublicDemo.ps1"
STOP = WINDOWS_ROOT / "Stop-AuraPublicDemo.ps1"
STATUS = WINDOWS_ROOT / "Get-AuraPublicDemoStatus.ps1"
READINESS = WINDOWS_ROOT / "Test-PublicDemoReadiness.ps1"
BACKUP = WINDOWS_ROOT / "Invoke-AuraProductionBackup.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ProductionOperationsStaticTests(unittest.TestCase):
    def test_daily_commands_are_production_only_and_idempotent(self):
        start = read(START)
        stop = read(STOP)
        status = read(STATUS)
        for script in (start, stop, status, read(READINESS), read(BACKUP)):
            self.assertIn("Assert-AuraProductionProfile", script)
        self.assertIn("AURA_PUBLIC_DEMO_ALREADY_READY profile=production", start)
        self.assertIn("AURA_PUBLIC_DEMO_STOPPED profile=production", stop)
        self.assertIn("State -in @('stale', 'absent')", read(WINDOWS_ROOT / "Stop-Aura.ps1"))
        self.assertIn("State -in @('stale', 'absent')", read(WINDOWS_ROOT / "Stop-TailscaleFunnel.ps1"))

    def test_start_order_and_stop_order_are_fixed(self):
        start = read(START)
        ordered = (
            "Assert-AuraProductionProfile",
            "Assert-AuraRepositoryLayout",
            "AURA_PRODUCTION_CONFIG_MISSING",
            "Assert-AuraOperatorSecretAcl -Path $configPath",
            "AURA_PRODUCTION_PGPASS_MISSING",
            "Assert-AuraOperatorSecretAcl -Path $pgPassPath",
            "Import-AuraConfiguration",
            "Assert-AuraProductionConfiguration",
            "Test-AuraPostgreSQLServiceRunning",
            "Test-AuraProductionDatabaseReadiness",
            "Get-AuraOwnedProcessState -Kind aura",
            "Start-Aura.ps1",
            "Get-AuraGatewayListenerProcessInfo",
            "Test-AuraFirewallRules",
            "Get-AuraOwnedProcessState -Kind funnel",
            "Start-TailscaleFunnel.ps1",
            "Test-AuraPublicHealth",
        )
        positions = [start.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        stop = read(STOP)
        self.assertLess(stop.index("Stop-TailscaleFunnel.ps1"), stop.index("Stop-Aura.ps1"))
        self.assertLess(stop.index("Test-AuraPublicHealth"), stop.index("Stop-Aura.ps1"))

    def test_ownership_is_exact_creation_bound_and_fails_closed(self):
        common = read(COMMON)
        combined = common + read(START) + read(STOP)
        for expected in (
            "Test-AuraExpectedProcessInfo",
            "ExecutablePath",
            "creationTimeUtc",
            "Get-AuraProcessCreationTimeUtc",
            "Get-AuraGatewayListenerProcessInfo",
            "ParentProcessId",
            "CN=Python Software Foundation",
            "AURA_PROCESS_METADATA_INVALID",
            "Assert-AuraOwnedProcessStillMatches",
            "HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS",
            "AURA_PROCESS_OWNERSHIP_UNCERTAIN",
        ):
            self.assertIn(expected, combined)
        combined_stop = read(WINDOWS_ROOT / "Stop-Aura.ps1") + read(
            WINDOWS_ROOT / "Stop-TailscaleFunnel.ps1"
        )
        self.assertNotIn("Get-Process -Name", combined_stop)
        self.assertNotIn("Stop-Process -Name", combined_stop)

    def test_listener_funnel_and_public_health_are_multi_signal(self):
        combined = read(COMMON) + read(WINDOWS_ROOT / "Start-TailscaleFunnel.ps1")
        self.assertIn("Test-AuraExactLoopbackListener", combined)
        self.assertIn("Test-AuraExpectedProcessInfo", combined)
        self.assertIn("Get-AuraFunnelBaseUri", combined)
        self.assertIn("Test-AuraPublicHealth", combined)
        lifecycle = "\n".join(
            read(path)
            for path in (
                START,
                STOP,
                WINDOWS_ROOT / "Start-TailscaleFunnel.ps1",
                WINDOWS_ROOT / "Stop-TailscaleFunnel.ps1",
            )
        ).casefold()
        self.assertNotIn("funnel reset", lifecycle)
        self.assertNotIn("serve reset", lifecycle)
        self.assertNotIn("--bg", lifecycle)

    def test_status_start_and_readiness_share_exact_firewall_and_database_gates(self):
        common = read(COMMON)
        self.assertIn("function Test-AuraFirewallRegistryRuleValues", common)
        self.assertIn("Registry::HKEY_LOCAL_MACHINE", common)
        self.assertIn("function Test-AuraPostgreSQLLoopbackListener", common)
        for script in (read(START), read(STATUS), read(READINESS)):
            self.assertIn("Test-AuraFirewallRules", script)
            self.assertIn("Test-AuraPostgreSQLLoopbackListener", script)

    def test_status_and_readiness_are_read_only_and_output_safe(self):
        combined = "\n".join(read(path) for path in (START, STATUS, READINESS))
        for forbidden in (
            "Method Post",
            "internal/demo/sessions",
            "demo_cleanup",
            "DEMO_BFF_SERVICE_TOKEN",
            "X-Demo-Client-Subject",
            "DNSName",
        ):
            self.assertNotIn(forbidden.casefold(), combined.casefold())
        status = read(STATUS)
        for state in ("ready", "offline", "degraded"):
            self.assertIn(f"'{state}'", status)
        for safe_check in (
            "postgresql_running",
            "aura_process_present",
            "owned_pid_valid",
            "listener_loopback",
            "local_health",
            "funnel_process_present",
            "public_health",
            "firewall_valid",
            "config_acl_valid",
            "pgpass_acl_valid",
            "backup_age",
        ):
            self.assertIn(safe_check, status)
        self.assertNotIn("ProcessInfo.ProcessId)", "\n".join(
            line for line in status.splitlines() if "Write-Output" in line
        ))

    def test_backup_wrapper_validates_readiness_archive_acl_and_safe_output(self):
        backup = read(BACKUP)
        for expected in (
            "Test-AuraProductionDatabaseReadiness",
            "Backup-DemoDatabase.ps1",
            "Assert-AuraOperatorSecretAcl",
            "pg_restore.exe",
            "--list",
            "timestamp_class=utc",
            "archive_valid=yes",
            "acl_protected=yes",
        ):
            self.assertIn(expected, backup)
        output = next(line for line in backup.splitlines() if "AURA_PRODUCTION_BACKUP" in line)
        self.assertNotIn("FullName", output)
        self.assertNotIn("database", output.casefold())

    def test_backup_age_policy_is_documented(self):
        runbook = read(PROJECT_ROOT / "docs" / "windows-production-operations.md")
        manual = read(PROJECT_ROOT / "docs" / "PUBLIC-DEMO-MANUAL.md")
        for text in (runbook, manual):
            self.assertIn("24", text)
            self.assertIn("48", text)
            for classification in ("fresh", "warning", "stale", "missing"):
                self.assertIn(classification, text)


@unittest.skipUnless(os.name == "nt", "PowerShell behavior tests require Windows")
class ProductionOperationsPowerShellTests(unittest.TestCase):
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

    def test_exact_process_command_rejects_extra_arguments(self):
        body = r"""
function Get-AuraPythonPath { 'C:\trusted\python.exe' }
$valid = [PSCustomObject]@{ Name='python.exe'; ExecutablePath='C:\trusted\python.exe'; CommandLine='"C:\trusted\python.exe" -m app.self_host --profile production' }
$extra = [PSCustomObject]@{ Name='python.exe'; ExecutablePath='C:\trusted\python.exe'; CommandLine='"C:\trusted\python.exe" -m app.self_host --profile production --extra' }
if (-not (Test-AuraExpectedProcessInfo -ProcessInfo $valid -Kind aura -Profile production)) { throw 'valid-rejected' }
if (Test-AuraExpectedProcessInfo -ProcessInfo $extra -Kind aura -Profile production) { throw 'extra-accepted' }
Write-Output 'EXACT_COMMAND_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "EXACT_COMMAND_OK")

    def test_exact_loopback_listener_rejects_wrong_owner_and_non_loopback(self):
        body = r"""
function Get-NetTCPConnection { [PSCustomObject]@{ LocalAddress='127.0.0.1'; OwningProcess=71 } }
if (-not (Test-AuraExactLoopbackListener -Port 8000 -OwningProcess 71)) { throw 'valid-rejected' }
if (Test-AuraExactLoopbackListener -Port 8000 -OwningProcess 72) { throw 'owner-accepted' }
function Get-NetTCPConnection { [PSCustomObject]@{ LocalAddress='192.168.10.2'; OwningProcess=71 } }
if (Test-AuraExactLoopbackListener -Port 8000 -OwningProcess 71) { throw 'lan-accepted' }
Write-Output 'LOOPBACK_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "LOOPBACK_OK")

    def test_public_health_contract_classifies_exact_content_only(self):
        body = r"""
function Invoke-WebRequest { [PSCustomObject]@{ StatusCode=200; Content='{"status":"healthy"}'; Headers=@{ 'Content-Type'='application/json' } } }
if (-not (Test-AuraHealthContract -Uri ([Uri]'https://example.invalid/health'))) { throw 'valid-rejected' }
function Invoke-WebRequest { [PSCustomObject]@{ StatusCode=200; Content='{"status":"ok"}'; Headers=@{ 'Content-Type'='application/json' } } }
if (Test-AuraHealthContract -Uri ([Uri]'https://example.invalid/health')) { throw 'invalid-accepted' }
Write-Output 'HEALTH_CLASSIFICATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "HEALTH_CLASSIFICATION_OK")

    def test_postgresql_listener_allows_both_loopbacks_but_rejects_wildcard(self):
        body = r"""
function Get-NetTCPConnection {
    @(
        [PSCustomObject]@{ LocalAddress='127.0.0.1' },
        [PSCustomObject]@{ LocalAddress='::1' }
    )
}
if (-not (Test-AuraPostgreSQLLoopbackListener)) { throw 'loopbacks-rejected' }
function Get-NetTCPConnection {
    @(
        [PSCustomObject]@{ LocalAddress='127.0.0.1' },
        [PSCustomObject]@{ LocalAddress='0.0.0.0' }
    )
}
if (Test-AuraPostgreSQLLoopbackListener) { throw 'wildcard-accepted' }
Write-Output 'POSTGRES_LOOPBACK_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "POSTGRES_LOOPBACK_OK")

    def test_firewall_registry_fallback_requires_three_exact_rules(self):
        body = r"""
$values = @(
    'v2.30|Action=Block|Active=TRUE|Dir=In|Protocol=6|LPort=8000|Name=AURA block direct API 8000|EmbedCtxt=AURA Self-Host|',
    'v2.30|Action=Block|Active=TRUE|Dir=In|Protocol=6|LPort=8001|Name=AURA block direct API 8001|EmbedCtxt=AURA Self-Host|',
    'v2.30|Action=Block|Active=TRUE|Dir=In|Protocol=6|LPort=5432|Name=AURA block direct PostgreSQL 5432|EmbedCtxt=AURA Self-Host|'
)
if (-not (Test-AuraFirewallRegistryRuleValues -Values $values)) { throw 'valid-rejected' }
if (Test-AuraFirewallRegistryRuleValues -Values @($values[0],$values[1])) { throw 'missing-accepted' }
if (Test-AuraFirewallRegistryRuleValues -Values @($values + $values[0])) { throw 'duplicate-accepted' }
$scoped = $values[0].Replace('|Name=', '|App=C:\unsafe.exe|Name=')
if (Test-AuraFirewallRegistryRuleValues -Values @($scoped,$values[1],$values[2])) { throw 'scoped-accepted' }
Write-Output 'FIREWALL_REGISTRY_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "FIREWALL_REGISTRY_OK")

    def test_stale_pid_is_removed_but_ambiguous_pid_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "owned.pid"
            pid_path.write_text("987654", encoding="ascii")
            body = r"""
function Assert-AuraOperatorSecretAcl { param([string]$Path) }
function Get-AuraOwnershipPath { $env:AURA_TEST_PID_PATH }
function Get-CimInstance { $null }
$state = Get-AuraOwnedProcessState -Kind aura -Profile production -RepairStaleMetadata
if ($state.State -ne 'stale' -or (Test-Path -LiteralPath $env:AURA_TEST_PID_PATH)) { throw 'stale-not-repaired' }
[IO.File]::WriteAllText($env:AURA_TEST_PID_PATH, '987654')
function Get-CimInstance { [PSCustomObject]@{ Name='python.exe'; ExecutablePath='C:\wrong\python.exe'; CommandLine='python.exe something'; CreationDate=[DateTime]::UtcNow; ProcessId=987654 } }
function Test-AuraExpectedProcessInfo { $false }
$ambiguous = Get-AuraOwnedProcessState -Kind aura -Profile production -RepairStaleMetadata
if ($ambiguous.State -ne 'ambiguous' -or -not (Test-Path -LiteralPath $env:AURA_TEST_PID_PATH)) { throw 'ambiguous-not-closed' }
Write-Output 'STALE_AND_AMBIGUOUS_OK'
"""
            result = self.invoke(body, AURA_TEST_PID_PATH=str(pid_path))
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "STALE_AND_AMBIGUOUS_OK")

    def test_backup_age_classification_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraBackupRoot = $env:AURA_TEST_BACKUP_ROOT
$now = [DateTime]::UtcNow
if ((Get-AuraBackupAgeClassification -NowUtc $now) -ne 'missing') { throw 'missing' }
$path = Join-Path $script:AuraBackupRoot 'aura_demo_public_20260101T000000Z.dump'
[IO.File]::WriteAllBytes($path, [byte[]](1,2,3))
[IO.File]::SetLastWriteTimeUtc($path, $now.AddHours(-24))
if ((Get-AuraBackupAgeClassification -NowUtc $now) -ne 'fresh') { throw 'fresh' }
[IO.File]::SetLastWriteTimeUtc($path, $now.AddHours(-25))
if ((Get-AuraBackupAgeClassification -NowUtc $now) -ne 'warning') { throw 'warning' }
[IO.File]::SetLastWriteTimeUtc($path, $now.AddHours(-49))
if ((Get-AuraBackupAgeClassification -NowUtc $now) -ne 'stale') { throw 'stale' }
Write-Output 'BACKUP_AGE_OK'
"""
            result = self.invoke(body, AURA_TEST_BACKUP_ROOT=directory)
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "BACKUP_AGE_OK")


if __name__ == "__main__":
    unittest.main()
