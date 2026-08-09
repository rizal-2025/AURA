"""Windows task, operation-log, and cleanup-health hardening coverage."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
WRAPPER = WINDOWS_ROOT / "Run-DemoCleanup.ps1"
TASKS = WINDOWS_ROOT / "Register-AuraTasks.ps1"
STATUS = WINDOWS_ROOT / "Get-AuraPublicDemoStatus.ps1"
ACTIVATE = WINDOWS_ROOT / "Activate-AuraDemoCleanup.ps1"
DEACTIVATE = WINDOWS_ROOT / "Deactivate-AuraDemoCleanup.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DemoCleanupWindowsStaticTests(unittest.TestCase):
    def test_task_action_has_deterministic_repository_working_directory(self):
        combined = read(TASKS) + read(COMMON)
        self.assertIn("$repositoryRoot = Assert-AuraRepositoryLayout", combined)
        self.assertIn("<WorkingDirectory>$workingDirectory</WorkingDirectory>", combined)
        self.assertIn('-File `"$CleanupScript`"', combined)
        self.assertIn("-Mode Execute", combined)
        self.assertIn("-Confirmation RUN_AURA_DEMO_CLEANUP", combined)

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
        combined = read(TASKS) + read(WRAPPER) + read(ACTIVATE) + read(DEACTIVATE)
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
            "CLEANUP_TASK_MISSING",
            "CLEANUP_TASK_DISABLED",
        ):
            self.assertIn(value, common + status)
        self.assertIn("cleanup_health", status)
        self.assertIn("cleanup_last_success_age", status)
        self.assertIn("cleanup_last_attempt_age", status)
        self.assertIn("cleanup_last_dry_run_age", status)
        self.assertIn("$cleanupHealth.ReadyCompatible", status)
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

    def test_hourly_task_xml_is_serializable_exact_and_non_interactive(self):
        body = r"""
$powerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$cleanup = 'C:\repo\deploy\windows\Run-DemoCleanup.ps1'
$root = 'C:\repo'
$first = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
$second = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
if ($first -cne $second) { throw 'not-deterministic' }
if (-not (Test-AuraCleanupTaskXml -Xml $first -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false)) { throw 'inspection' }
[xml]$document = $first
$manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
$manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
function Value($xpath) { $document.SelectSingleNode($xpath, $manager).InnerText }
if ((Value '//t:Repetition/t:Interval') -cne 'PT1H') { throw 'interval' }
if ((Value '//t:Repetition/t:Duration') -cne 'P1D') { throw 'duration' }
if ((Value '//t:CalendarTrigger/t:StartBoundary') -cne '2024-01-01T00:17:00') { throw 'boundary' }
if ((Value '//t:Principal/t:UserId') -cne 'S-1-5-18') { throw 'principal' }
if ((Value '//t:Principal/t:RunLevel') -cne 'LeastPrivilege') { throw 'run-level' }
if ((Value '//t:Settings/t:Enabled') -cne 'false') { throw 'enabled' }
if ((Value '//t:Settings/t:MultipleInstancesPolicy') -cne 'IgnoreNew') { throw 'overlap' }
if ((Value '//t:Settings/t:StartWhenAvailable') -cne 'false') { throw 'immediate-start' }
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$definition = $service.NewTask(0)
$definition.XmlText = $first
if ($definition.Principal.LogonType -ne 5) { throw 'service-account-logon' }
if ($definition.Triggers.Item(1).Repetition.Interval -cne 'PT1H') { throw 'host-interval' }
$serialized = $definition.XmlText
if (-not (Test-AuraCleanupTaskXml -Xml $serialized -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false)) { throw 'host-serialization' }
Write-Output 'TASK_XML_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "TASK_XML_OK")

    def test_activation_marker_schema_is_atomic_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraRunRoot = $env:AURA_TEST_RUN_ROOT
function Initialize-AuraDataDirectories { New-Item -ItemType Directory -Path $script:AuraRunRoot -Force | Out-Null }
function Set-AuraOperatorProtectedAcl { param($Path,[switch]$Container) }
function Assert-AuraOperatorSecretAcl { param($Path) }
$expected = Join-Path $script:AuraRunRoot 'cleanup-activation-production.json'
if (Test-Path -LiteralPath $expected) { throw 'preexisting' }
$written = Write-AuraCleanupActivationMarker -ActivatedAtUtc ([DateTime]'2026-08-09T01:02:03Z')
if ($written.State -cne 'active' -or $written.TaskName -cne 'AURA Demo Cleanup') { throw 'contents' }
$raw = Get-Content -Raw -LiteralPath $expected | ConvertFrom-Json
if ($raw.version -ne 1 -or $raw.profile -cne 'production' -or $raw.activatedAtUtc -cne '2026-08-09T01:02:03.0000000Z') { throw 'schema' }
Remove-AuraCleanupActivationMarker
if (Test-Path -LiteralPath $expected) { throw 'remove' }
Write-Output 'MARKER_OK'
"""
            result = self.invoke(body, AURA_TEST_RUN_ROOT=directory)
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "MARKER_OK")

    def test_cleanup_health_full_activation_truth_table(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraLogRoot = $env:AURA_TEST_LOG_ROOT
New-Item -ItemType Directory -Path $script:AuraLogRoot -Force | Out-Null
$script:Marker = $null
$script:Task = $null
function Read-AuraCleanupActivationMarker { param($Profile) $script:Marker }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) $script:Task }
function Assert-AuraRepositoryLayout { 'C:\repo' }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_NOT_CONFIGURED') { throw 'pre-absent' }
$script:Task = [PSCustomObject]@{ Disabled=$true; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_NOT_CONFIGURED') { throw 'pre-staged' }
$script:Task = [PSCustomObject]@{ Disabled=$false; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth).Status -ne 'CLEANUP_ACTIVATION_INCONSISTENT') { throw 'pre-enabled' }
$script:Task = $null
$now = [DateTime]::UtcNow
$script:Marker = [PSCustomObject]@{ ActivatedAtUtc=$now.AddHours(-5); State='active' }
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_TASK_MISSING') { throw 'missing' }
$script:Task = [PSCustomObject]@{ Disabled=$true; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_TASK_DISABLED') { throw 'disabled' }
$script:Task = [PSCustomObject]@{ Disabled=$false; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_NEVER_RAN') { throw 'never' }
$path = Join-Path $script:AuraLogRoot 'operations-20260809.log'
function Add-CleanupRecord([DateTime]$Timestamp, [string]$Mode, [string]$Result) {
    $line = 'timestamp={0} profile=production stage=CLEANUP mode={1} eligible_sessions=1 attempted_sessions=1 successful_cleanup_count=1 failed_cleanup_count=0 result={2} elapsed_ms=10' -f $Timestamp.ToUniversalTime().ToString('o'), $Mode, $Result
    Add-Content -LiteralPath $path -Value $line -Encoding ascii
}
Add-CleanupRecord -Timestamp $now.AddMinutes(-5) -Mode 'dry-run' -Result success
$dryOnly = Get-AuraCleanupHealth -NowUtc $now
if ($dryOnly.Status -ne 'CLEANUP_NEVER_RAN' -or $dryOnly.LastAttemptAge -ne 'never' -or $dryOnly.LastDryRunAge -ne 'fresh' -or $dryOnly.LastSuccessAge -ne 'never') { throw 'dry-run' }
Add-CleanupRecord -Timestamp $now.AddHours(-4) -Mode 'execute' -Result success
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_STALE') { throw 'stale' }
Add-CleanupRecord -Timestamp $now.AddMinutes(-2) -Mode 'execute' -Result success
$healthy = Get-AuraCleanupHealth -NowUtc $now
if ($healthy.Status -ne 'CLEANUP_HEALTHY' -or -not $healthy.ReadyCompatible) { throw 'healthy' }
Add-CleanupRecord -Timestamp $now.AddMinutes(-1) -Mode 'execute' -Result failure
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_FAILED') { throw 'failed' }
Write-Output 'CLEANUP_HEALTH_OK'
"""
            result = self.invoke(body, AURA_TEST_LOG_ROOT=directory)
            self.assert_ok(result)
            self.assertEqual(result.stdout.strip(), "CLEANUP_HEALTH_OK")

    def test_staged_registration_is_idempotent_fail_closed_and_rolls_back(self):
        body = r"""
$script:TaskState = $null
$script:TaskXml = $null
$script:RegisterCount = 0
$script:UnregisterCount = 0
$script:Corrupt = $false
$script:FailRegisterAfterCreate = $false
$script:ActivationMarker = $null
function Read-AuraCleanupActivationMarker { param($Profile) $script:ActivationMarker }
function Get-ScheduledTask { param($TaskName,$ErrorAction) if ($null -ne $script:TaskState) { [PSCustomObject]@{ State=$script:TaskState } } }
function Export-ScheduledTask { param($TaskName,$ErrorAction) if ($script:Corrupt) { '<Task />' } else { $script:TaskXml } }
function Register-ScheduledTask { param($TaskName,$Xml,$ErrorAction) $script:RegisterCount++; $script:TaskXml=$Xml; $script:TaskState='Disabled'; if ($script:FailRegisterAfterCreate) { throw 'partial-register' } }
function Unregister-ScheduledTask { param($TaskName,$Confirm,$ErrorAction) $script:UnregisterCount++; $script:TaskState=$null; $script:TaskXml=$null }
$parameters = @{ PowerShellPath='C:\powershell.exe'; CleanupScript='C:\repo\Run-DemoCleanup.ps1'; RepositoryRoot='C:\repo' }
if ((Register-AuraCleanupTaskStaged @parameters) -ne 'AURA_CLEANUP_TASK_STAGED_DISABLED') { throw 'stage' }
if ($script:TaskState -ne 'Disabled' -or $script:RegisterCount -ne 1) { throw 'not-disabled' }
if ((Register-AuraCleanupTaskStaged @parameters) -ne 'AURA_CLEANUP_TASK_ALREADY_STAGED') { throw 'idempotent' }
if ($script:RegisterCount -ne 1) { throw 'registered-twice' }
$script:ActivationMarker=[PSCustomObject]@{ State='active' }; $script:TaskState='Ready'; $script:TaskXml=New-AuraCleanupTaskXml @parameters -Enabled $true
if ((Register-AuraCleanupTaskStaged @parameters) -ne 'AURA_CLEANUP_TASK_ALREADY_ACTIVE') { throw 'active' }
if ($script:RegisterCount -ne 1 -or $script:TaskState -ne 'Ready') { throw 'active-replaced' }
$script:ActivationMarker=$null; $script:TaskState='Disabled'
$script:TaskXml = '<Task />'
try { Register-AuraCleanupTaskStaged @parameters; throw 'mismatch-accepted' } catch { if ($_.Exception.Message -eq 'mismatch-accepted') { throw } }
$script:TaskState=$null; $script:TaskXml=$null; $script:Corrupt=$true
try { Register-AuraCleanupTaskStaged @parameters; throw 'corruption-accepted' } catch { if ($_.Exception.Message -eq 'corruption-accepted') { throw } }
if ($script:UnregisterCount -ne 1 -or $null -ne $script:TaskState) { throw 'rollback' }
$script:Corrupt=$false; $script:FailRegisterAfterCreate=$true
try { Register-AuraCleanupTaskStaged @parameters; throw 'partial-accepted' } catch { if ($_.Exception.Message -eq 'partial-accepted') { throw } }
if ($script:UnregisterCount -ne 2 -or $null -ne $script:TaskState) { throw 'partial-rollback' }
Write-Output 'REGISTRATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "REGISTRATION_OK")

    def test_activation_marker_order_and_rollback(self):
        body = r"""
$script:TaskState = 'Disabled'
$script:Marker = $false
$script:FailEnable = $false
$script:FailMarker = $false
$script:Events = [System.Collections.Generic.List[string]]::new()
function Read-AuraCleanupActivationMarker { param($Profile) if ($script:Marker) { [PSCustomObject]@{ State='active' } } }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) [PSCustomObject]@{ Disabled=($script:TaskState -eq 'Disabled'); DefinitionMatches=$true } }
function Assert-AuraCleanupActivationWindow { }
function Enable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('enable'); if ($script:FailEnable) { throw 'enable-failed' }; $script:TaskState='Ready' }
function Disable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('disable'); $script:TaskState='Disabled' }
function Write-AuraCleanupActivationMarker { param($Profile) $script:Events.Add('marker'); if ($script:FailMarker) { throw 'marker-failed' }; $script:Marker=$true }
$parameters = @{ PowerShellPath='C:\powershell.exe'; CleanupScript='C:\repo\Run-DemoCleanup.ps1'; RepositoryRoot='C:\repo' }
if ((Enable-AuraCleanupTaskActivation @parameters) -ne 'AURA_CLEANUP_ACTIVATED') { throw 'activate' }
if (-not $script:Marker -or $script:TaskState -ne 'Ready' -or ($script:Events -join ',') -ne 'enable,marker') { throw 'order' }
$script:TaskState='Disabled'; $script:Marker=$false; $script:Events.Clear(); $script:FailMarker=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'marker-failure-accepted' } catch { if ($_.Exception.Message -eq 'marker-failure-accepted') { throw } }
if ($script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'enable,marker,disable') { throw 'marker-rollback' }
$script:TaskState='Disabled'; $script:Marker=$false; $script:Events.Clear(); $script:FailMarker=$false; $script:FailEnable=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'enable-failure-accepted' } catch { if ($_.Exception.Message -eq 'enable-failure-accepted') { throw } }
if ($script:Marker -or ($script:Events -join ',') -ne 'enable') { throw 'enable-marker' }
Write-Output 'ACTIVATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "ACTIVATION_OK")

    def test_activation_refuses_an_imminent_hourly_trigger(self):
        body = r"""
try { Assert-AuraCleanupActivationWindow -NowLocal ([DateTime]'2026-08-09T10:17:30'); throw 'minute-accepted' } catch { if ($_.Exception.Message -eq 'minute-accepted') { throw } }
try { Assert-AuraCleanupActivationWindow -NowLocal ([DateTime]'2026-08-09T10:15:30'); throw 'imminent-accepted' } catch { if ($_.Exception.Message -eq 'imminent-accepted') { throw } }
Assert-AuraCleanupActivationWindow -NowLocal ([DateTime]'2026-08-09T10:14:59')
Write-Output 'ACTIVATION_WINDOW_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "ACTIVATION_WINDOW_OK")

    def test_deactivation_disables_before_removing_marker(self):
        body = r"""
$script:TaskState='Ready'; $script:Marker=$true; $script:Events=[System.Collections.Generic.List[string]]::new()
function Read-AuraCleanupActivationMarker { param($Profile) if ($script:Marker) { [PSCustomObject]@{ State='active' } } }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) [PSCustomObject]@{ Disabled=($script:TaskState -eq 'Disabled'); DefinitionMatches=$true } }
function Disable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('disable'); $script:TaskState='Disabled' }
function Remove-AuraCleanupActivationMarker { param($Profile) $script:Events.Add('remove-marker'); $script:Marker=$false }
$parameters = @{ PowerShellPath='C:\powershell.exe'; CleanupScript='C:\repo\Run-DemoCleanup.ps1'; RepositoryRoot='C:\repo' }
if ((Disable-AuraCleanupTaskActivation @parameters) -ne 'AURA_CLEANUP_DEACTIVATED') { throw 'deactivate' }
if ($script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'disable,remove-marker') { throw 'order' }
$script:Marker=$true
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) $null }
try { Disable-AuraCleanupTaskActivation @parameters; throw 'missing-accepted' } catch { if ($_.Exception.Message -eq 'missing-accepted') { throw } }
if (-not $script:Marker) { throw 'drift-hidden' }
Write-Output 'DEACTIVATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "DEACTIVATION_OK")

    def test_wrapper_propagates_exact_child_exit_codes_and_preflight_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(WRAPPER, root / WRAPPER.name)
            (root / "production.conf").write_text("test", encoding="ascii")
            (root / "production.pgpass").write_text("test", encoding="ascii")
            (root / "fake-cleanup.cmd").write_text(
                "@echo off\r\necho %AURA_TEST_PAYLOAD%\r\nexit /b %AURA_TEST_CHILD_EXIT%\r\n",
                encoding="ascii",
            )
            common = r"""
$ErrorActionPreference='Stop'
function Assert-AuraProductionProfile { param($Profile) if ($env:AURA_TEST_PREFLIGHT_FAIL -eq '1') { throw 'preflight' } }
function Assert-AuraRepositoryLayout { $PSScriptRoot }
function Initialize-AuraDataDirectories { }
function Get-AuraSecretPath { param($Profile) (Join-Path $PSScriptRoot 'production.conf') }
function Get-AuraPgPassPath { param($Profile) (Join-Path $PSScriptRoot 'production.pgpass') }
function Assert-AuraOperatorSecretAcl { param($Path) }
function Import-AuraConfiguration { param($Profile) @{} }
function Assert-AuraProductionConfiguration { }
function Test-AuraPostgreSQLServiceRunning { $true }
function Test-AuraPostgreSQLLoopbackListener { $true }
function Test-AuraProductionDatabaseReadiness { $true }
function Get-AuraPythonPath { Join-Path $PSScriptRoot 'fake-cleanup.cmd' }
function Restore-AuraProcessEnvironment { param($Previous) }
function Write-AuraCleanupOperationLog { param($Profile,$Mode,$EligibleSessions,$AttemptedSessions,$SuccessfulCleanupCount,$FailedCleanupCount,$Result,$ElapsedMs) }
"""
            (root / COMMON.name).write_text(common, encoding="utf-8")
            payloads = {
                0: '{"status":"ok","mode":"execute","eligible_sessions":0,"attempted_sessions":0,"successful_cleanup_count":0,"failed_cleanup_count":0}',
                1: '{"status":"error","mode":"execute","code":"DEMO_CLEANUP_FAILED","eligible_sessions":0,"attempted_sessions":0,"successful_cleanup_count":0,"failed_cleanup_count":0}',
                2: '{"status":"error","mode":"execute","code":"DEMO_CLEANUP_PARTIAL_FAILURE","eligible_sessions":1,"attempted_sessions":1,"successful_cleanup_count":0,"failed_cleanup_count":1}',
            }
            for code, payload in payloads.items():
                with self.subTest(code=code):
                    result = subprocess.run(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / WRAPPER.name), "-Mode", "Execute", "-Confirmation", "RUN_AURA_DEMO_CLEANUP"],
                        cwd=root,
                        env={**os.environ, "AURA_TEST_CHILD_EXIT": str(code), "AURA_TEST_PAYLOAD": payload},
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            failed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / WRAPPER.name)],
                cwd=root,
                env={**os.environ, "AURA_TEST_PREFLIGHT_FAIL": "1"},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(failed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
