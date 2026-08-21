"""Windows task, operation-log, and cleanup-health hardening coverage."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.jobs import demo_cleanup
from app.services.demo_cleanup_service import DemoCleanupSummary


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


class _CleanupSuccessService:
    def __init__(self, **_values):
        pass

    async def run_once(self, *, batch_size: int) -> DemoCleanupSummary:
        return DemoCleanupSummary(batch_size, batch_size, 0, 0, 0, 0)


class _CleanupTotalFailureService(_CleanupSuccessService):
    async def run_once(self, *, batch_size: int) -> DemoCleanupSummary:
        return DemoCleanupSummary(batch_size, 0, 0, 0, batch_size, 0)


class _CleanupPartialFailureService(_CleanupSuccessService):
    async def run_once(self, *, batch_size: int) -> DemoCleanupSummary:
        return DemoCleanupSummary(batch_size, batch_size - 1, 0, 0, 1, 0)


def real_cleanup_payload(service_type: type[_CleanupSuccessService]) -> tuple[int, str]:
    with (
        patch.dict(
            "sys.modules",
            {"app.db.database": SimpleNamespace(SessionLocal=object())},
        ),
        patch(
            "app.core.config.get_environment_settings",
            return_value=SimpleNamespace(APP_ENV="demo"),
        ),
        patch(
            "app.services.demo_cleanup_service.DemoCleanupService",
            service_type,
        ),
        patch("builtins.print") as output,
    ):
        exit_code = demo_cleanup.main(["--once", "--batch-size", "3"])
    return exit_code, output.call_args.args[0]


class DemoCleanupWindowsStaticTests(unittest.TestCase):
    def test_task_action_has_deterministic_repository_working_directory(self):
        combined = read(TASKS) + read(COMMON)
        self.assertIn("$repositoryRoot = Assert-AuraRepositoryLayout", combined)
        self.assertIn("<WorkingDirectory>$workingDirectory</WorkingDirectory>", combined)
        self.assertIn(
            '-NoProfile -NonInteractive -ExecutionPolicy Bypass '
            '-File `"$CleanupScript`"',
            combined,
        )
        self.assertIn("-Mode Execute", combined)
        self.assertIn("-Confirmation RUN_AURA_DEMO_CLEANUP", combined)
        self.assertNotIn("Set-ExecutionPolicy", combined)

    def test_wrapper_fails_closed_and_enters_verified_repository(self):
        wrapper = read(WRAPPER)
        ordered = (
            "Assert-AuraProductionProfile",
            "AURA_CLEANUP_CONFIRMATION_REQUIRED",
            "Assert-AuraCleanupExecutionActivated",
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
            "CLEANUP_ACTIVATION_INCOMPLETE",
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
if (-not (Test-AuraCleanupTaskXml -Xml $first -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false -EffectiveEnabled $false)) { throw 'inspection' }
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
if (-not (Test-AuraCleanupTaskXml -Xml $serialized -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false -EffectiveEnabled $false)) { throw 'host-serialization' }
Write-Output 'TASK_XML_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "TASK_XML_OK")

    def test_task_action_requires_exact_bounded_execution_policy(self):
        body = r"""
$powerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$cleanup = 'C:\repo with spaces & safe\deploy\windows\Run-DemoCleanup.ps1'
$root = 'C:\repo with spaces & safe'
$canonical = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
$namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
function New-Variant { [xml]$document = $canonical; return $document }
function Manager($document) {
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace('t', $namespace)
    return ,$manager
}
function Get-Node($document, [string]$xpath) {
    $node = $document.SelectSingleNode($xpath, (Manager $document))
    if ($null -eq $node) { throw 'test-node-missing' }
    return $node
}
function Set-Value($document, [string]$xpath, [string]$value) {
    (Get-Node $document $xpath).InnerText = $value
}
function Valid($document) {
    return Test-AuraCleanupTaskXml -Xml $document.OuterXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false -EffectiveRunLevel 'Limited' -EffectiveStartWhenAvailable $false -EffectiveEnabled $false
}
$expected = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $cleanup + '" -Profile production -Mode Execute -Confirmation RUN_AURA_DEMO_CLEANUP'
$actual = [string](Get-Node ([xml]$canonical) '/t:Task/t:Actions/t:Exec/t:Arguments').InnerText
if ($actual -cne $expected) { throw 'approved-arguments-not-exact' }
if (-not (Valid ([xml]$canonical))) { throw 'approved-action-rejected' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' ($expected.Replace('-ExecutionPolicy Bypass ', ''))
if (Valid $case) { throw 'missing-execution-policy-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' ($expected.Replace('-ExecutionPolicy Bypass', '-ExecutionPolicy RemoteSigned'))
if (Valid $case) { throw 'wrong-execution-policy-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' '-NoProfile -NonInteractive -Command "Set-ExecutionPolicy Bypass -Scope LocalMachine"'
if (Valid $case) { throw 'persistent-policy-command-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Command' 'C:\Program Files\PowerShell\7\pwsh.exe'
if (Valid $case) { throw 'wrong-powershell-host-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' ($expected.Replace($cleanup, 'C:\wrong\Run-DemoCleanup.ps1'))
if (Valid $case) { throw 'wrong-script-path-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' ($expected + ' -Command "Write-Output unsafe"')
if (Valid $case) { throw 'extra-command-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:Arguments' ($expected.Replace('-Profile production', '-Profile staging'))
if (Valid $case) { throw 'wrong-profile-accepted' }

$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:WorkingDirectory' 'C:\wrong'
if (Valid $case) { throw 'wrong-working-directory-accepted' }

if (-not (Valid ([xml]$canonical))) { throw 'canonical-action-regressed' }
Write-Output 'TASK_EXECUTION_POLICY_MATRIX_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(
            result.stdout.strip(),
            "TASK_EXECUTION_POLICY_MATRIX_OK",
        )

    def test_task_validator_normalizes_only_proven_effective_defaults(self):
        body = r"""
$powerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$cleanup = 'C:\repo\deploy\windows\Run-DemoCleanup.ps1'
$root = 'C:\repo'
$canonical = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
$namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
function New-Variant { [xml]$document = $canonical; return $document }
function Manager($document) {
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace('t', $namespace)
    return ,$manager
}
function Remove-Value($document, [string]$xpath) {
    $node = $document.SelectSingleNode($xpath, (Manager $document))
    if ($null -eq $node) { throw 'test-node-missing' }
    [void]$node.ParentNode.RemoveChild($node)
}
function Set-Value($document, [string]$xpath, [string]$value) {
    $node = $document.SelectSingleNode($xpath, (Manager $document))
    if ($null -eq $node) { throw 'test-node-missing' }
    $node.InnerText = $value
}
function Valid($document, [object]$runLevel = 'Limited', [object]$start = $false) {
    return Test-AuraCleanupTaskXml -Xml $document.OuterXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false -EffectiveRunLevel $runLevel -EffectiveStartWhenAvailable $start -EffectiveEnabled $false
}
function Valid-WithoutEffectiveEvidence($document) {
    return Test-AuraCleanupTaskXml -Xml $document.OuterXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
}
if (-not (Valid (New-Variant))) { throw 'explicit-defaults' }
$case = New-Variant
Remove-Value $case '/t:Task/t:Principals/t:Principal/t:RunLevel'
if (-not (Valid $case)) { throw 'omitted-run-level' }
$case = New-Variant
Remove-Value $case '/t:Task/t:Settings/t:StartWhenAvailable'
if (-not (Valid $case)) { throw 'omitted-start-when-available' }
$case = New-Variant
Remove-Value $case '/t:Task/t:Principals/t:Principal/t:RunLevel'
Remove-Value $case '/t:Task/t:Settings/t:StartWhenAvailable'
if (-not (Valid $case)) { throw 'both-defaults-omitted' }
if (Valid-WithoutEffectiveEvidence $case) { throw 'unproven-omissions-accepted' }
if (Valid (New-Variant) 'Highest') { throw 'effective-elevation-accepted' }
if (Valid (New-Variant) 'Unknown') { throw 'unknown-effective-run-level-accepted' }
if (Valid (New-Variant) 'Limited' $true) { throw 'effective-start-true-accepted' }
if (Valid (New-Variant) 'Limited' 'false') { throw 'unknown-effective-start-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Principals/t:Principal/t:RunLevel' 'HighestAvailable'
if (Valid $case) { throw 'highest-available-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Principals/t:Principal/t:RunLevel' 'leastprivilege'
if (Valid $case) { throw 'malformed-run-level-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Settings/t:StartWhenAvailable' 'true'
if (Valid $case) { throw 'start-true-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Settings/t:StartWhenAvailable' 'False'
if (Valid $case) { throw 'malformed-start-accepted' }
$case = New-Variant
Remove-Value $case '/t:Task/t:Actions/t:Exec/t:Command'
if (Valid $case) { throw 'missing-command-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Principals/t:Principal/t:UserId' 'S-1-5-32-544'
if (Valid $case) { throw 'principal-mismatch-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Actions/t:Exec/t:WorkingDirectory' 'C:\wrong'
if (Valid $case) { throw 'working-directory-mismatch-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Triggers/t:CalendarTrigger/t:StartBoundary' '2024-01-01T00:18:00'
if (Valid $case) { throw 'trigger-mismatch-accepted' }
$case = New-Variant
Set-Value $case '/t:Task/t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval' 'PT2H'
if (Valid $case) { throw 'repetition-mismatch-accepted' }
Write-Output 'TASK_DEFAULT_NORMALIZATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(
            result.stdout.strip(),
            "TASK_DEFAULT_NORMALIZATION_OK",
        )

    def test_task_validator_normalizes_enabled_only_with_effective_proof(self):
        body = r"""
$powerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$cleanup = 'C:\repo\deploy\windows\Run-DemoCleanup.ps1'
$root = 'C:\repo'
$canonical = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
$namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
function New-Variant { [xml]$document = $canonical; return $document }
function Manager($document) {
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace('t', $namespace)
    return ,$manager
}
function Enabled-Node($document) {
    return $document.SelectSingleNode('/t:Task/t:Settings/t:Enabled', (Manager $document))
}
function Set-Enabled($document, [string]$value) {
    (Enabled-Node $document).InnerText = $value
}
function Remove-Enabled($document) {
    $node = Enabled-Node $document
    [void]$node.ParentNode.RemoveChild($node)
}
function Valid($document, [bool]$expected, [object]$effective) {
    return Test-AuraCleanupTaskXml -Xml $document.OuterXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $expected -EffectiveRunLevel 'Limited' -EffectiveStartWhenAvailable $false -EffectiveEnabled $effective
}
function Valid-WithoutEnabledEvidence($document, [bool]$expected) {
    return Test-AuraCleanupTaskXml -Xml $document.OuterXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $expected -EffectiveRunLevel 'Limited' -EffectiveStartWhenAvailable $false
}
$case = New-Variant
Set-Enabled $case 'true'
if (-not (Valid $case $true $true)) { throw 'explicit-true-effective-true-rejected' }
$case = New-Variant
Remove-Enabled $case
if (-not (Valid $case $true $true)) { throw 'omitted-true-effective-true-rejected' }
$case = New-Variant
if (Valid $case $true $false) { throw 'explicit-false-expected-true-accepted' }
if (Valid $case $true $true) { throw 'xml-effective-disagreement-accepted' }
$case = New-Variant
Remove-Enabled $case
if (Valid $case $true $false) { throw 'omitted-effective-false-accepted' }
if (Valid-WithoutEnabledEvidence $case $true) { throw 'omitted-without-evidence-accepted' }
$case = New-Variant
Set-Enabled $case 'True'
if (Valid $case $true $true) { throw 'malformed-enabled-accepted' }
$case = New-Variant
if (-not (Valid $case $false $false)) { throw 'explicit-false-effective-false-rejected' }
if (Valid $case $false 'false') { throw 'unknown-effective-enabled-accepted' }
if (Valid-WithoutEnabledEvidence $case $false) { throw 'explicit-false-without-evidence-accepted' }
$case = New-Variant
Set-Enabled $case 'true'
if (Valid $case $false $true) { throw 'enabled-accepted-as-disabled' }
if (Valid $case $false $false) { throw 'enabled-xml-effective-false-accepted' }
$case = New-Variant
Remove-Enabled $case
if (Valid $case $false $false) { throw 'omitted-false-accepted' }
if (Valid $case $false $true) { throw 'omitted-enabled-accepted-as-disabled' }
Write-Output 'TASK_ENABLED_NORMALIZATION_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(
            result.stdout.strip(),
            "TASK_ENABLED_NORMALIZATION_OK",
        )

    def test_activation_marker_schema_is_atomic_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            body = r"""
$script:AuraRunRoot = $env:AURA_TEST_RUN_ROOT
function Initialize-AuraDataDirectories { New-Item -ItemType Directory -Path $script:AuraRunRoot -Force | Out-Null }
function Set-AuraOperatorProtectedAcl { param($Path,[switch]$Container) }
function Assert-AuraOperatorRuntimeContainerAcl { param($Path) }
function Assert-AuraOperatorSecretAcl { param($Path) }
$expected = Join-Path $script:AuraRunRoot 'cleanup-activation-production.json'
if (Test-Path -LiteralPath $expected) { throw 'preexisting' }
$written = Write-AuraCleanupActivationMarker -State activating -ActivatedAtUtc ([DateTime]'2026-08-09T01:02:03Z')
if ($written.Version -ne 2 -or $written.State -cne 'activating' -or $written.TaskName -cne 'AURA Demo Cleanup') { throw 'contents' }
$raw = Get-Content -Raw -LiteralPath $expected | ConvertFrom-Json
if ($raw.version -ne 2 -or $raw.state -cne 'activating' -or $raw.profile -cne 'production' -or $raw.activatedAtUtc -cne '2026-08-09T01:02:03.0000000Z') { throw 'schema' }
$active = Set-AuraCleanupActivationMarkerActive -ActivatedAtUtc ([DateTime]'2026-08-09T01:03:04Z')
if ($active.State -cne 'active' -or (Read-AuraCleanupActivationMarker).State -cne 'active') { throw 'transition' }
Remove-AuraCleanupActivationMarker
if (Test-Path -LiteralPath $expected) { throw 'remove' }
[IO.File]::WriteAllText($expected, '{"version":1,"profile":"production","state":"active","activatedAtUtc":"2026-08-09T01:02:03.0000000Z","taskName":"AURA Demo Cleanup"}', [Text.Encoding]::ASCII)
if ((Read-AuraCleanupActivationMarker).Version -ne 1) { throw 'v1-compatibility' }
Remove-AuraCleanupActivationMarker
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
$script:Marker = [PSCustomObject]@{ ActivatedAtUtc=$now.AddHours(-5); State='activating' }
$script:Task = [PSCustomObject]@{ Disabled=$true; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_ACTIVATION_INCOMPLETE') { throw 'activating-disabled' }
$script:Task = [PSCustomObject]@{ Disabled=$false; DefinitionMatches=$true }
if ((Get-AuraCleanupHealth -NowUtc $now).Status -ne 'CLEANUP_ACTIVATION_INCOMPLETE') { throw 'activating-enabled' }
$script:Marker = [PSCustomObject]@{ ActivatedAtUtc=$now.AddHours(-5); State='active' }
$script:Task = $null
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
function Get-ScheduledTask { param($TaskName,$ErrorAction) if ($null -ne $script:TaskState) { [PSCustomObject]@{ State=$script:TaskState; Principal=[PSCustomObject]@{ RunLevel='Limited' }; Settings=[PSCustomObject]@{ StartWhenAvailable=$false; Enabled=($script:TaskState -ne 'Disabled') } } } }
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
$script:Marker = $null
$script:FailEnable = $false
$script:FailMarkerCreate = $false
$script:FailTransition = $false
$script:FailValidation = $false
$script:Events = [System.Collections.Generic.List[string]]::new()
function Read-AuraCleanupActivationMarker { param($Profile) $script:Marker }
function Get-AuraCleanupTaskSnapshot {
    param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot)
    if ($script:TaskState -eq 'Ready') { $script:Events.Add("post-enable-state-$($script:Marker.State)") }
    [PSCustomObject]@{ Disabled=($script:TaskState -eq 'Disabled'); DefinitionMatches=(-not ($script:FailValidation -and $script:TaskState -eq 'Ready')) }
}
function Assert-AuraCleanupActivationWindow { }
function Enable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('enable'); if ($script:FailEnable) { throw 'enable-failed' }; $script:TaskState='Ready' }
function Disable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('disable'); $script:TaskState='Disabled' }
function Write-AuraCleanupActivationMarker { param($Profile,$State) $script:Events.Add('marker-activating'); if ($script:FailMarkerCreate) { throw 'marker-create-failed' }; $script:Marker=[PSCustomObject]@{ State='activating' } }
function Set-AuraCleanupActivationMarkerActive { param($Profile) $script:Events.Add('marker-active'); if ($script:FailTransition) { throw 'marker-transition-failed' }; $script:Marker=[PSCustomObject]@{ State='active' }; $script:Marker }
function Remove-AuraCleanupActivationMarker { param($Profile) $script:Events.Add('remove-marker'); $script:Marker=$null }
$parameters = @{ PowerShellPath='C:\powershell.exe'; CleanupScript='C:\repo\Run-DemoCleanup.ps1'; RepositoryRoot='C:\repo' }
if ((Enable-AuraCleanupTaskActivation @parameters) -ne 'AURA_CLEANUP_ACTIVATED') { throw 'activate' }
if ($script:Marker.State -ne 'active' -or $script:TaskState -ne 'Ready' -or ($script:Events -join ',') -ne 'marker-activating,enable,post-enable-state-activating,marker-active') { throw 'order' }
$script:TaskState='Disabled'; $script:Marker=$null; $script:Events.Clear(); $script:FailMarkerCreate=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'marker-create-accepted' } catch { if ($_.Exception.Message -eq 'marker-create-accepted') { throw } }
if ($null -ne $script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'marker-activating') { throw 'marker-create' }
$script:TaskState='Disabled'; $script:Marker=$null; $script:Events.Clear(); $script:FailMarkerCreate=$false; $script:FailEnable=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'enable-failure-accepted' } catch { if ($_.Exception.Message -eq 'enable-failure-accepted') { throw } }
if ($null -ne $script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'marker-activating,enable,disable,remove-marker') { throw 'enable-rollback' }
$script:TaskState='Disabled'; $script:Marker=$null; $script:Events.Clear(); $script:FailEnable=$false; $script:FailValidation=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'validation-failure-accepted' } catch { if ($_.Exception.Message -eq 'validation-failure-accepted') { throw } }
if ($null -ne $script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'marker-activating,enable,post-enable-state-activating,disable,remove-marker') { throw 'validation-rollback' }
$script:TaskState='Disabled'; $script:Marker=$null; $script:Events.Clear(); $script:FailValidation=$false; $script:FailTransition=$true
try { Enable-AuraCleanupTaskActivation @parameters; throw 'transition-failure-accepted' } catch { if ($_.Exception.Message -eq 'transition-failure-accepted') { throw } }
if ($null -ne $script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'marker-activating,enable,post-enable-state-activating,marker-active,disable,remove-marker') { throw 'transition-rollback' }
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

    def test_execute_guard_requires_active_marker_and_exact_enabled_task(self):
        body = r"""
$script:Marker=$null; $script:Task=[PSCustomObject]@{ Disabled=$false; DefinitionMatches=$true }
function Read-AuraCleanupActivationMarker { param($Profile) $script:Marker }
function Assert-AuraRepositoryLayout { 'C:\repo' }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) $script:Task }
try { Assert-AuraCleanupExecutionActivated; throw 'absent-accepted' } catch { if ($_.Exception.Message -eq 'absent-accepted') { throw } }
$script:Marker=[PSCustomObject]@{ State='activating' }
try { Assert-AuraCleanupExecutionActivated; throw 'activating-accepted' } catch { if ($_.Exception.Message -eq 'activating-accepted') { throw } }
$script:Marker=[PSCustomObject]@{ State='active' }
$script:Task=[PSCustomObject]@{ Disabled=$true; DefinitionMatches=$true }
try { Assert-AuraCleanupExecutionActivated; throw 'disabled-accepted' } catch { if ($_.Exception.Message -eq 'disabled-accepted') { throw } }
$script:Task=[PSCustomObject]@{ Disabled=$false; DefinitionMatches=$false }
try { Assert-AuraCleanupExecutionActivated; throw 'mismatch-accepted' } catch { if ($_.Exception.Message -eq 'mismatch-accepted') { throw } }
$script:Task=[PSCustomObject]@{ Disabled=$false; DefinitionMatches=$true }
if ((Assert-AuraCleanupExecutionActivated).State -cne 'active') { throw 'active-rejected' }
Write-Output 'EXECUTION_GUARD_OK'
"""
        result = self.invoke(body)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "EXECUTION_GUARD_OK")

    def test_deactivation_disables_before_removing_marker(self):
        body = r"""
$script:TaskState='Ready'; $script:Marker=[PSCustomObject]@{ State='active' }; $script:Events=[System.Collections.Generic.List[string]]::new()
function Read-AuraCleanupActivationMarker { param($Profile) $script:Marker }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) [PSCustomObject]@{ Disabled=($script:TaskState -eq 'Disabled'); DefinitionMatches=$true } }
function Disable-ScheduledTask { param($TaskName,$ErrorAction) $script:Events.Add('disable'); $script:TaskState='Disabled' }
function Remove-AuraCleanupActivationMarker { param($Profile) $script:Events.Add('remove-marker'); $script:Marker=$null }
$parameters = @{ PowerShellPath='C:\powershell.exe'; CleanupScript='C:\repo\Run-DemoCleanup.ps1'; RepositoryRoot='C:\repo' }
if ((Disable-AuraCleanupTaskActivation @parameters) -ne 'AURA_CLEANUP_DEACTIVATED') { throw 'deactivate' }
if ($null -ne $script:Marker -or $script:TaskState -ne 'Disabled' -or ($script:Events -join ',') -ne 'disable,remove-marker') { throw 'order' }
$script:Marker=[PSCustomObject]@{ State='activating' }; $script:Events.Clear()
if ((Disable-AuraCleanupTaskActivation @parameters) -ne 'AURA_CLEANUP_DEACTIVATED') { throw 'incomplete-deactivate' }
if ($null -ne $script:Marker -or ($script:Events -join ',') -ne 'remove-marker') { throw 'incomplete-order' }
$script:Marker=[PSCustomObject]@{ State='active' }
function Get-AuraCleanupTaskSnapshot { param($TaskName,$PowerShellPath,$CleanupScript,$RepositoryRoot) $null }
try { Disable-AuraCleanupTaskActivation @parameters; throw 'missing-accepted' } catch { if ($_.Exception.Message -eq 'missing-accepted') { throw } }
if ($null -eq $script:Marker) { throw 'drift-hidden' }
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
                "@echo off\r\nif defined AURA_TEST_CHILD_SENTINEL echo invoked>\"%AURA_TEST_CHILD_SENTINEL%\"\r\necho %AURA_TEST_PAYLOAD%\r\nexit /b %AURA_TEST_CHILD_EXIT%\r\n",
                encoding="ascii",
            )
            common = r"""
$ErrorActionPreference='Stop'
function Assert-AuraProductionProfile { param($Profile) if ($env:AURA_TEST_PREFLIGHT_FAIL -eq '1') { throw 'preflight' } }
function Assert-AuraCleanupExecutionActivated { param($Profile) if ($env:AURA_TEST_ACTIVATION_STATE -cne 'active') { throw 'not-active' } }
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
            payloads = (
                real_cleanup_payload(_CleanupSuccessService),
                real_cleanup_payload(_CleanupTotalFailureService),
                real_cleanup_payload(_CleanupPartialFailureService),
            )

            def run_wrapper(code: int, payload: str, **environment: str):
                return subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / WRAPPER.name), "-Mode", "Execute", "-Confirmation", "RUN_AURA_DEMO_CLEANUP"],
                    cwd=root,
                    env={
                        **os.environ,
                        "AURA_TEST_ACTIVATION_STATE": "active",
                        "AURA_TEST_CHILD_EXIT": str(code),
                        "AURA_TEST_PAYLOAD": payload,
                        **environment,
                    },
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

            for code, payload in payloads:
                with self.subTest(code=code):
                    result = run_wrapper(code, payload)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            malformed_payloads = (
                (0, "not-json"),
                (0, '{"status":"unknown","mode":"execute","eligible_sessions":0,"attempted_sessions":0,"successful_cleanup_count":0,"failed_cleanup_count":0}'),
                (0, '{"status":"ok","mode":"execute","eligible_sessions":0,"attempted_sessions":0,"successful_cleanup_count":0}'),
                (0, '{"status":"ok","mode":"execute","eligible_sessions":"0","attempted_sessions":0,"successful_cleanup_count":0,"failed_cleanup_count":0}'),
                (2, payloads[0][1]),
            )
            for child_code, payload in malformed_payloads:
                with self.subTest(malformed_child_code=child_code, payload=payload):
                    result = run_wrapper(child_code, payload)
                    self.assertNotEqual(result.returncode, 0)

            sentinel = root / "child-invoked.txt"
            transition = run_wrapper(
                0,
                payloads[0][1],
                AURA_TEST_ACTIVATION_STATE="activating",
                AURA_TEST_CHILD_SENTINEL=str(sentinel),
            )
            self.assertNotEqual(transition.returncode, 0)
            self.assertFalse(sentinel.exists(), transition.stdout + transition.stderr)
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


@unittest.skipUnless(os.name == "nt", "Real NTFS ACL tests require Windows")
class RuntimeContainerAclBehavioralTests(unittest.TestCase):
    _PREAMBLE = r"""
$testRoot = [IO.Path]::GetFullPath($env:AURA_TEST_ACL_ROOT).TrimEnd('\')
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not $testRoot.StartsWith(
    $temporaryRoot,
    [StringComparison]::OrdinalIgnoreCase
)) { throw 'AURA_TEST_ACL_ROOT_NOT_TEMPORARY' }
$testItem = Get-Item -LiteralPath $testRoot -Force
if (
    -not $testItem.PSIsContainer `
    -or ($testItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
) { throw 'AURA_TEST_ACL_ROOT_INVALID' }

$script:TestCurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$script:TestSystemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$script:TestAdministratorsSid = [Security.Principal.SecurityIdentifier]::new(
    'S-1-5-32-544'
)
$script:TestUsersSid = [Security.Principal.SecurityIdentifier]::new(
    'S-1-5-32-545'
)
$script:TestInheritance = (
    [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [Security.AccessControl.InheritanceFlags]::ObjectInherit
)
$script:TestOperatorRights = (
    [Security.AccessControl.FileSystemRights]::Modify -bor
    [Security.AccessControl.FileSystemRights]::Synchronize
)

function New-TestRuntimeRule {
    param(
        [Parameter(Mandatory)]
        [Security.Principal.SecurityIdentifier]$Sid,
        [Parameter(Mandatory)]
        [Security.AccessControl.FileSystemRights]$Rights,
        [Security.AccessControl.InheritanceFlags]$Inheritance =
            $script:TestInheritance,
        [Security.AccessControl.PropagationFlags]$Propagation =
            [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]$Type =
            [Security.AccessControl.AccessControlType]::Allow
    )
    return [Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $Rights,
        $Inheritance,
        $Propagation,
        $Type
    )
}

function Set-TestReviewedRuntimeAcl {
    param([Parameter(Mandatory)][string]$Path)

    # Exercise the repository's real protected-container provisioning helper,
    # then narrow the operator to the reviewed runtime Modify contract.
    Set-AuraOperatorProtectedAcl -Path $Path -Container
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    [void]$acl.AddAccessRule((New-TestRuntimeRule `
        -Sid $script:TestSystemSid `
        -Rights ([Security.AccessControl.FileSystemRights]::FullControl)))
    [void]$acl.AddAccessRule((New-TestRuntimeRule `
        -Sid $script:TestAdministratorsSid `
        -Rights ([Security.AccessControl.FileSystemRights]::FullControl)))
    [void]$acl.AddAccessRule((New-TestRuntimeRule `
        -Sid $script:TestCurrentSid `
        -Rights $script:TestOperatorRights))
    [IO.Directory]::SetAccessControl($Path, $acl)
}

function Set-TestDirectoryAcl {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Acl
    )
    [IO.Directory]::SetAccessControl($Path, $Acl)
}

function Assert-TestRuntimeAclRejected {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected
    )
    try {
        Assert-AuraOperatorRuntimeContainerAcl -Path $Path
        throw 'AURA_TEST_UNSAFE_ACL_ACCEPTED'
    } catch {
        $actual = $_.Exception.Message
        if ($actual -ceq 'AURA_TEST_UNSAFE_ACL_ACCEPTED') { throw }
        if ($actual -cne $Expected) {
            throw "AURA_TEST_ACL_ERROR_MISMATCH expected=$Expected actual=$actual"
        }
    }
    Write-Output "AURA_TEST_ACL_REJECTED=$Expected"
}

Set-TestReviewedRuntimeAcl -Path $testRoot
Assert-AuraOperatorRuntimeContainerAcl -Path $testRoot
"""

    def invoke_acl_case(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="aura-runtime-acl-") as directory:
            resolved = Path(directory).resolve()
            self.assertTrue(
                resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
            )
            command = (
                f". '{COMMON}'; $ErrorActionPreference='Stop'; "
                f"{self._PREAMBLE}; try {{ {body} }} finally {{ "
                "Set-TestReviewedRuntimeAcl -Path $testRoot }"
            )
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
                env={**os.environ, "AURA_TEST_ACL_ROOT": str(resolved)},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

    def assert_acl_case(self, body: str, expected: str) -> str:
        result = self.invoke_acl_case(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn(expected, result.stdout)
        return result.stdout

    def test_exact_reviewed_runtime_acl_is_accepted(self):
        self.assert_acl_case(
            "Write-Output 'AURA_TEST_RUNTIME_ACL_ACCEPTED'",
            "AURA_TEST_RUNTIME_ACL_ACCEPTED",
        )

    def test_inheritance_enabled_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.SetAccessRuleProtection($false, $true)
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_INHERITANCE_ENABLED'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_INHERITANCE_ENABLED",
        )

    def test_unexpected_principal_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestSystemSid)
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestUsersSid `
    -Rights ([Security.AccessControl.FileSystemRights]::FullControl)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_UNEXPECTED_IDENTITY'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_UNEXPECTED_IDENTITY",
        )

    def test_deny_ace_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestSystemSid)
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestSystemSid `
    -Rights ([Security.AccessControl.FileSystemRights]::FullControl) `
    -Type ([Security.AccessControl.AccessControlType]::Deny)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_DENY_OR_UNKNOWN_TYPE_FOUND'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_DENY_OR_UNKNOWN_TYPE_FOUND",
        )

    def test_rights_mismatch_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestCurrentSid)
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestCurrentSid `
    -Rights ([Security.AccessControl.FileSystemRights]::FullControl)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_RIGHTS_INVALID'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_RIGHTS_INVALID",
        )

    def test_inheritance_flags_mismatch_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestCurrentSid)
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestCurrentSid `
    -Rights $script:TestOperatorRights `
    -Inheritance ([Security.AccessControl.InheritanceFlags]::ContainerInherit)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_INHERITANCE_FLAGS_INVALID'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_INHERITANCE_FLAGS_INVALID",
        )

    def test_propagation_flags_mismatch_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestCurrentSid)
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestCurrentSid `
    -Rights $script:TestOperatorRights `
    -Propagation ([Security.AccessControl.PropagationFlags]::InheritOnly)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_PROPAGATION_FLAGS_INVALID'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_PROPAGATION_FLAGS_INVALID",
        )

    def test_required_principal_missing_is_rejected_by_count_gate(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
$acl.PurgeAccessRules($script:TestSystemSid)
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
Assert-TestRuntimeAclRejected -Path $testRoot `
    -Expected 'AURA_RUNTIME_ACL_ACE_COUNT_INVALID'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_ACE_COUNT_INVALID",
        )

    def test_duplicate_or_overlapping_operator_ace_is_rejected(self):
        self.assert_acl_case(
            r"""
$acl = Get-Acl -LiteralPath $testRoot
[void]$acl.AddAccessRule((New-TestRuntimeRule `
    -Sid $script:TestCurrentSid `
    -Rights ([Security.AccessControl.FileSystemRights]::FullControl)))
Set-TestDirectoryAcl -Path $testRoot -Acl $acl
$operatorRules = @((Get-Acl -LiteralPath $testRoot).Access | Where-Object {
    $_.IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value -ceq $script:TestCurrentSid.Value
})
$representation = if ($operatorRules.Count -gt 1) { 'separate' } else {
    'canonicalized'
}
try {
    Assert-AuraOperatorRuntimeContainerAcl -Path $testRoot
    throw 'AURA_TEST_UNSAFE_ACL_ACCEPTED'
} catch {
    $actual = $_.Exception.Message
    if ($actual -ceq 'AURA_TEST_UNSAFE_ACL_ACCEPTED') { throw }
    if ($actual -cnotin @(
        'AURA_RUNTIME_ACL_ACE_COUNT_INVALID',
        'AURA_RUNTIME_ACL_DUPLICATE_ACE',
        'AURA_RUNTIME_ACL_RIGHTS_INVALID'
    )) { throw "AURA_TEST_OVERLAPPING_ACL_ERROR_UNEXPECTED actual=$actual" }
    Write-Output (
        'AURA_TEST_OVERLAPPING_ACE_REJECTED={0} representation={1}' -f `
        $actual, $representation
    )
}
""",
            "AURA_TEST_OVERLAPPING_ACE_REJECTED=",
        )

    def test_regular_file_is_rejected(self):
        self.assert_acl_case(
            r"""
$file = Join-Path $testRoot 'not-a-directory.txt'
[IO.File]::WriteAllText($file, 'disposable ACL test', [Text.Encoding]::ASCII)
Assert-TestRuntimeAclRejected -Path $file `
    -Expected 'AURA_RUNTIME_ACL_PATH_TYPE_INVALID'
""",
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_PATH_TYPE_INVALID",
        )

    def test_reparse_point_directory_is_rejected_when_supported(self):
        result = self.invoke_acl_case(
            r"""
$target = Join-Path $testRoot 'junction-target'
$junction = Join-Path $testRoot 'junction-under-test'
[void](New-Item -ItemType Directory -Path $target)
try {
    [void](New-Item -ItemType Junction -Path $junction -Target $target `
        -ErrorAction Stop)
} catch {
    Write-Output (
        'AURA_TEST_REPARSE_SKIPPED={0}:{1}' -f `
        $_.Exception.GetType().Name, $_.FullyQualifiedErrorId
    )
    return
}
try {
    Assert-TestRuntimeAclRejected -Path $junction `
        -Expected 'AURA_RUNTIME_ACL_PATH_TYPE_INVALID'
} finally {
    Remove-Item -LiteralPath $junction -Force -ErrorAction SilentlyContinue
}
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        if "AURA_TEST_REPARSE_SKIPPED=" in result.stdout:
            reason = result.stdout.split("AURA_TEST_REPARSE_SKIPPED=", 1)[1].strip()
            self.skipTest(f"Junction creation unavailable: {reason}")
        self.assertIn(
            "AURA_TEST_ACL_REJECTED=AURA_RUNTIME_ACL_PATH_TYPE_INVALID",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
