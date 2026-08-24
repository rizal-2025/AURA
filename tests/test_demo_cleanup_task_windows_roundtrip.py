"""Real Windows Task Scheduler normalization coverage for demo cleanup."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMON = PROJECT_ROOT / "deploy" / "windows" / "AuraWindows.Common.ps1"
NOOP_ACTION = (
    PROJECT_ROOT / "tests" / "fixtures" / "windows" / "Noop-ScheduledTaskAction.ps1"
)
EXECUTION_POLICY_PROBE = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "windows"
    / "ExecutionPolicyProbe-ScheduledTaskAction.ps1"
)
SYSTEM_SECRET_ACL_PROBE = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "windows"
    / "SystemSecretAclProbe-ScheduledTaskAction.ps1"
)


@unittest.skipUnless(os.name == "nt", "Real Task Scheduler tests require Windows")
class DemoCleanupWindowsRegisteredTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("Real Task Scheduler registration requires elevation")

    def invoke(
        self,
        body: str,
        task_name: str,
        cwd: Path = PROJECT_ROOT,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
            cwd=cwd,
            env={
                **os.environ,
                "AURA_TEST_TASK_NAME": task_name,
                "AURA_TEST_NOOP_ACTION": str(NOOP_ACTION),
                "AURA_TEST_EXECUTION_POLICY_PROBE": str(EXECUTION_POLICY_PROBE),
                **(environment or {}),
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_elevated_readiness_import_is_external_cwd_independent(self):
        task_name = f"Codex Enabled Normalization Test {uuid.uuid4().hex}"
        body = r"""
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -ne 1) { throw 'production-task-count-invalid' }
$stateBefore = [string]$productionBefore[0].State
$enabledBefore = [bool]$productionBefore[0].Settings.Enabled
if ($stateBefore -cne 'Disabled' -or $enabledBefore) { throw 'production-task-not-disabled' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$root = Assert-AuraRepositoryLayout
$result = Invoke-AuraRepositoryPythonOperation -Operation readiness-import
if ($result.ExitCode -ne 0) { throw 'elevated-import-failed' }
if (-not [string]::IsNullOrEmpty($result.StandardError)) { throw 'elevated-import-stderr' }
if ([IO.Path]::GetFullPath($result.WorkingDirectory) -cne [IO.Path]::GetFullPath($root)) { throw 'elevated-working-directory-invalid' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne 1) { throw 'production-task-count-changed' }
if ([string]$productionAfter[0].State -cne $stateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $enabledBefore) { throw 'production-task-state-changed' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
Write-Output ('ELEVATED_REPOSITORY_IMPORT_OK root=' + $result.WorkingDirectory)
"""
        result = self.invoke(body, task_name, cwd=PROJECT_ROOT.parent)
        self.assert_ok(result)
        self.assertIn("ELEVATED_REPOSITORY_IMPORT_OK", result.stdout)
        self.assertIn(str(PROJECT_ROOT), result.stdout)

    def test_bounded_execution_policy_runs_disposable_task(self):
        now = datetime.now().astimezone()
        next_boundary = now.replace(minute=17, second=0, microsecond=0)
        if next_boundary <= now:
            next_boundary += timedelta(hours=1)
        if now.minute == 17 or next_boundary - now <= timedelta(minutes=2):
            self.skipTest("Too close to the task's minute-17 trigger boundary")

        task_name = f"AURA Execution Policy Test {uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(
            prefix="aura-execution-policy-"
        ) as directory:
            body = r"""
$positiveName = $env:AURA_TEST_TASK_NAME
$negativeName = $positiveName + ' Negative'
if ($positiveName -notmatch '^AURA Execution Policy Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -ne 1) { throw 'production-task-count-invalid' }
$productionStateBefore = [string]$productionBefore[0].State
$productionEnabledBefore = [bool]$productionBefore[0].Settings.Enabled
if ($productionStateBefore -cne 'Disabled' -or $productionEnabledBefore) { throw 'production-task-not-disabled' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }

$testRoot = [IO.Path]::GetFullPath($env:AURA_TEST_EXECUTION_POLICY_ROOT).TrimEnd('\')
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not ($testRoot + '\').StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'test-root-not-temporary' }
Set-AuraOperatorProtectedAcl -Path $testRoot -Container
$sourceProbe = [IO.Path]::GetFullPath($env:AURA_TEST_EXECUTION_POLICY_PROBE)
if (-not (Test-Path -LiteralPath $sourceProbe -PathType Leaf)) { throw 'probe-fixture-missing' }
$probe = Join-Path $testRoot 'ExecutionPolicyProbe-ScheduledTaskAction.ps1'
Copy-Item -LiteralPath $sourceProbe -Destination $probe -ErrorAction Stop
$resultPath = Join-Path $testRoot 'execution-policy-probe-result.txt'
$root = Assert-AuraRepositoryLayout
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$persistentScopes = @('MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine')
function Get-PersistentPolicies {
    $values = @{}
    foreach ($entry in Get-ExecutionPolicy -List) {
        if ([string]$entry.Scope -in $persistentScopes) {
            $values[[string]$entry.Scope] = [string]$entry.ExecutionPolicy
        }
    }
    return $values
}
function Assert-PoliciesEqual($before, $after) {
    foreach ($scope in $persistentScopes) {
        if ($before[$scope] -cne $after[$scope]) { throw ('persistent-policy-changed-' + $scope) }
    }
}
function Remove-TestTask([string]$name) {
    $remaining = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 1) {
        if ([string]$remaining[0].State -cne 'Disabled') {
            Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
        }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    }
}
function Wait-TestTask([string]$name, [DateTime]$previousRun) {
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        $currentTask = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        $currentInfo = Get-ScheduledTaskInfo -TaskName $name -ErrorAction Stop
        if ($currentInfo.LastRunTime -gt $previousRun -and [string]$currentTask.State -cne 'Running') {
            return $currentInfo
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('test-task-timeout-' + $name)
}

$policiesBefore = Get-PersistentPolicies
$failure = $null
try {
    $positiveXml = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $probe -RepositoryRoot $root -Enabled $true
    [xml]$negativeDocument = $positiveXml
    $manager = [Xml.XmlNamespaceManager]::new($negativeDocument.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $argumentsNode = $negativeDocument.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments', $manager)
    if ($null -eq $argumentsNode) { throw 'negative-arguments-missing' }
    $argumentsNode.InnerText = $argumentsNode.InnerText.Replace(
        '-ExecutionPolicy Bypass',
        '-ExecutionPolicy Restricted'
    )
    if (Test-AuraCleanupTaskXml -Xml $negativeDocument.OuterXml -PowerShellPath $powerShell -CleanupScript $probe -RepositoryRoot $root -Enabled $true -EffectiveRunLevel 'Limited' -EffectiveStartWhenAvailable $false -EffectiveEnabled $true) { throw 'restricted-negative-control-accepted' }

    Register-ScheduledTask -TaskName $negativeName -Xml $negativeDocument.OuterXml -ErrorAction Stop | Out-Null
    $negativeBefore = (Get-ScheduledTaskInfo -TaskName $negativeName -ErrorAction Stop).LastRunTime
    Start-ScheduledTask -TaskName $negativeName -ErrorAction Stop
    $negativeInfo = Wait-TestTask -name $negativeName -previousRun $negativeBefore
    if ($negativeInfo.LastTaskResult -eq 0) { throw 'restricted-negative-control-succeeded' }
    if (Test-Path -LiteralPath $resultPath) { throw 'restricted-negative-control-ran-script' }
    Remove-TestTask $negativeName

    Register-ScheduledTask -TaskName $positiveName -Xml $positiveXml -ErrorAction Stop | Out-Null
    $positiveTask = Get-ScheduledTask -TaskName $positiveName -ErrorAction Stop
    $positiveSnapshot = Get-AuraCleanupTaskSnapshot -TaskName $positiveName -PowerShellPath $powerShell -CleanupScript $probe -RepositoryRoot $root
    if ($null -eq $positiveSnapshot -or $positiveSnapshot.Disabled -or -not $positiveSnapshot.DefinitionMatches) { throw 'bounded-positive-task-not-canonical' }
    $positiveBefore = (Get-ScheduledTaskInfo -TaskName $positiveName -ErrorAction Stop).LastRunTime
    Start-ScheduledTask -TaskName $positiveName -ErrorAction Stop
    $positiveInfo = Wait-TestTask -name $positiveName -previousRun $positiveBefore
    if ($positiveInfo.LastTaskResult -ne 0) { throw 'bounded-positive-task-failed' }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { throw 'bounded-positive-probe-missing' }
    if ([IO.File]::ReadAllText($resultPath, [Text.Encoding]::ASCII) -cne 'AURA_EXECUTION_POLICY_PROBE_OK') { throw 'bounded-positive-probe-invalid' }
    if (-not [bool]$positiveTask.Settings.Enabled) { throw 'bounded-positive-task-not-enabled' }

    $policiesAfter = Get-PersistentPolicies
    Assert-PoliciesEqual $policiesBefore $policiesAfter
    Write-Output (
        'EXECUTION_POLICY_TASK_BEHAVIOR_OK negative_result={0} positive_result={1}' -f `
        $negativeInfo.LastTaskResult, $positiveInfo.LastTaskResult
    )
} catch { $failure = $_ } finally {
    Remove-TestTask $negativeName
    Remove-TestTask $positiveName
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
}
if (@(Get-ScheduledTask -TaskName $negativeName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'negative-task-cleanup-failed' }
if (@(Get-ScheduledTask -TaskName $positiveName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'positive-task-cleanup-failed' }
Assert-PoliciesEqual $policiesBefore (Get-PersistentPolicies)
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne 1) { throw 'production-task-count-changed' }
if ([string]$productionAfter[0].State -cne $productionStateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $productionEnabledBefore) { throw 'production-task-state-changed' }
if ($null -ne $failure) { throw $failure }
"""
            result = self.invoke(
                body,
                task_name,
                environment={"AURA_TEST_EXECUTION_POLICY_ROOT": directory},
            )
        self.assert_ok(result)
        self.assertRegex(
            result.stdout,
            r"EXECUTION_POLICY_TASK_BEHAVIOR_OK negative_result=\d+ positive_result=0",
        )

    def test_system_accepts_only_exact_generated_operator_secret_acl(self):
        now = datetime.now().astimezone()
        next_boundary = now.replace(minute=17, second=0, microsecond=0)
        if next_boundary <= now:
            next_boundary += timedelta(hours=1)
        if now.minute == 17 or next_boundary - now <= timedelta(minutes=2):
            self.skipTest("Too close to the task's minute-17 trigger boundary")

        task_name = f"AURA Secret ACL Test {uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="aura-system-secret-acl-") as directory:
            body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^AURA Secret ACL Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -ne 1) { throw 'production-task-count-invalid' }
$productionStateBefore = [string]$productionBefore[0].State
$productionEnabledBefore = [bool]$productionBefore[0].Settings.Enabled
if ($productionStateBefore -cne 'Disabled' -or $productionEnabledBefore) { throw 'production-task-not-disabled' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }

$testRoot = [IO.Path]::GetFullPath($env:AURA_TEST_SYSTEM_SECRET_ACL_ROOT).TrimEnd('\')
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not ($testRoot + '\').StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'test-root-not-temporary' }
Set-AuraOperatorProtectedAcl -Path $testRoot -Container
$sourceProbe = [IO.Path]::GetFullPath($env:AURA_TEST_SYSTEM_SECRET_ACL_PROBE)
if (-not (Test-Path -LiteralPath $sourceProbe -PathType Leaf)) { throw 'probe-fixture-missing' }
$probe = Join-Path $testRoot 'SystemSecretAclProbe-ScheduledTaskAction.ps1'
Copy-Item -LiteralPath $sourceProbe -Destination $probe -ErrorAction Stop
$validPath = Join-Path $testRoot 'valid-secret.txt'
$invalidPath = Join-Path $testRoot 'invalid-secret.txt'
$resultPath = Join-Path $testRoot 'system-secret-acl-probe-result.txt'
[IO.File]::WriteAllText($validPath, 'synthetic-valid', [Text.Encoding]::ASCII)
[IO.File]::WriteAllText($invalidPath, 'synthetic-invalid', [Text.Encoding]::ASCII)
Set-AuraOperatorProtectedAcl -Path $validPath
Set-AuraOperatorProtectedAcl -Path $invalidPath
$invalidAcl = Get-Acl -LiteralPath $invalidPath
$users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$extraRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $users,
    [Security.AccessControl.FileSystemRights]::Read,
    [Security.AccessControl.AccessControlType]::Allow
)
$invalidAcl.AddAccessRule($extraRule)
[IO.File]::SetAccessControl($invalidPath, $invalidAcl)

$root = Assert-AuraRepositoryLayout
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$xml = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $probe -RepositoryRoot $root -Enabled $true
$failure = $null
try {
    Register-ScheduledTask -TaskName $taskName -Xml $xml -ErrorAction Stop | Out-Null
    $before = (Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastRunTime
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 200
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
        if ($info.LastRunTime -gt $before -and [string]$task.State -cne 'Running') { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($info.LastRunTime -le $before) { throw 'system-secret-acl-task-timeout' }
    $probeResult = if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        [IO.File]::ReadAllText($resultPath, [Text.Encoding]::ASCII)
    } else { 'result-missing' }
    if ($info.LastTaskResult -ne 0) {
        throw ('system-secret-acl-task-failed-' + $info.LastTaskResult + '-' + $probeResult)
    }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { throw 'system-secret-acl-result-missing' }
    if ($probeResult -cne 'AURA_SYSTEM_SECRET_ACL_PROBE_OK') { throw 'system-secret-acl-result-invalid' }
    Write-Output 'SYSTEM_SECRET_ACL_TASK_BEHAVIOR_OK'
} catch { $failure = $_ } finally {
    $remaining = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 1) {
        if ([string]$remaining[0].State -cne 'Disabled') {
            Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null
        }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    }
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'system-secret-acl-task-cleanup-failed' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne 1) { throw 'production-task-count-changed' }
if ([string]$productionAfter[0].State -cne $productionStateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $productionEnabledBefore) { throw 'production-task-state-changed' }
if ($null -ne $failure) { throw $failure }
"""
            result = self.invoke(
                body,
                task_name,
                environment={
                    "AURA_TEST_SYSTEM_SECRET_ACL_ROOT": directory,
                    "AURA_TEST_SYSTEM_SECRET_ACL_PROBE": str(SYSTEM_SECRET_ACL_PROBE),
                },
            )
        self.assert_ok(result)
        self.assertIn("SYSTEM_SECRET_ACL_TASK_BEHAVIOR_OK", result.stdout)

    def test_registered_task_accepts_windows_default_omissions(self):
        task_name = f"Codex Enabled Normalization Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^Codex Enabled Normalization Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -gt 1) { throw 'production-task-ambiguous' }
$productionStateBefore = if ($productionBefore.Count -eq 1) { [string]$productionBefore[0].State } else { 'Missing' }
$productionEnabledBefore = if ($productionBefore.Count -eq 1) { [bool]$productionBefore[0].Settings.Enabled } else { $null }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath($env:AURA_TEST_NOOP_ACTION)
    if (-not (Test-Path -LiteralPath $cleanup -PathType Leaf)) { throw 'noop-action-missing' }
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $xml = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    Register-ScheduledTask -TaskName $taskName -Xml $xml -ErrorAction Stop | Out-Null
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if ([string]$task.State -cne 'Disabled' -or [bool]$task.Settings.Enabled) { throw 'test-task-enabled' }
    $actual = [string](Export-ScheduledTask -TaskName $taskName -ErrorAction Stop)
    [xml]$document = $actual
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $runLevelOmitted = $null -eq $document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:RunLevel', $manager)
    $startOmitted = $null -eq $document.SelectSingleNode('/t:Task/t:Settings/t:StartWhenAvailable', $manager)
    if (-not $runLevelOmitted -and -not $startOmitted) { throw 'windows-default-omission-not-observed' }
    $snapshot = Get-AuraCleanupTaskSnapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $snapshot -or -not $snapshot.Disabled -or -not $snapshot.DefinitionMatches) { throw 'normalized-task-rejected' }
    $sidText = [string]$task.Principal.UserId
    try { $sid = [Security.Principal.SecurityIdentifier]::new($sidText) } catch { $sid = [Security.Principal.NTAccount]::new($sidText).Translate([Security.Principal.SecurityIdentifier]) }
    if ($sid.Value -cne 'S-1-5-18') { throw 'effective-principal-invalid' }
    if ([string]$task.Principal.LogonType -cne 'ServiceAccount') { throw 'effective-logon-invalid' }
    if ([string]$task.Principal.RunLevel -cne 'Limited') { throw 'effective-run-level-invalid' }
    if ([bool]$task.Settings.StartWhenAvailable -or [bool]$task.Settings.Enabled) { throw 'effective-settings-invalid' }
    Write-Output ('REGISTERED_DEFAULTS_OK run_level_omitted=' + $runLevelOmitted + ' start_when_available_omitted=' + $startOmitted)
} catch { $failure = $_ } finally {
    $remaining = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 1) {
        if ([string]$remaining[0].State -cne 'Disabled') { Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    }
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'test-task-cleanup-failed' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne $productionBefore.Count) { throw 'production-task-count-changed' }
if ($productionAfter.Count -eq 1 -and ([string]$productionAfter[0].State -cne $productionStateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $productionEnabledBefore)) { throw 'production-task-state-changed' }
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REGISTERED_DEFAULTS_OK", result.stdout)
        self.assertRegex(
            result.stdout,
            r"run_level_omitted=True|start_when_available_omitted=True",
        )

    def test_registered_highest_run_level_is_rejected(self):
        task_name = f"Codex Enabled Normalization Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^Codex Enabled Normalization Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -gt 1) { throw 'production-task-ambiguous' }
$productionStateBefore = if ($productionBefore.Count -eq 1) { [string]$productionBefore[0].State } else { 'Missing' }
$productionEnabledBefore = if ($productionBefore.Count -eq 1) { [bool]$productionBefore[0].Settings.Enabled } else { $null }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath($env:AURA_TEST_NOOP_ACTION)
    if (-not (Test-Path -LiteralPath $cleanup -PathType Leaf)) { throw 'noop-action-missing' }
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    [xml]$document = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:RunLevel', $manager).InnerText = 'HighestAvailable'
    Register-ScheduledTask -TaskName $taskName -Xml $document.OuterXml -ErrorAction Stop | Out-Null
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if ([string]$task.State -cne 'Disabled' -or [bool]$task.Settings.Enabled) { throw 'test-task-enabled' }
    $snapshot = Get-AuraCleanupTaskSnapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $snapshot -or -not $snapshot.Disabled -or $snapshot.DefinitionMatches) { throw 'highest-run-level-accepted' }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $registered = $service.GetFolder('\').GetTask('\' + $taskName)
    if ([int]$registered.Definition.Principal.RunLevel -ne 1) { throw 'highest-run-level-not-effective' }
    Write-Output 'REGISTERED_HIGHEST_REJECTED'
} catch { $failure = $_ } finally {
    $remaining = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 1) {
        if ([string]$remaining[0].State -cne 'Disabled') { Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    }
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'test-task-cleanup-failed' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne $productionBefore.Count) { throw 'production-task-count-changed' }
if ($productionAfter.Count -eq 1 -and ([string]$productionAfter[0].State -cne $productionStateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $productionEnabledBefore)) { throw 'production-task-state-changed' }
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REGISTERED_HIGHEST_REJECTED", result.stdout)

    def test_disabled_to_enabled_roundtrip_accepts_proven_enabled_omission(self):
        now = datetime.now().astimezone()
        next_boundary = now.replace(minute=17, second=0, microsecond=0)
        if next_boundary <= now:
            next_boundary += timedelta(hours=1)
        if now.minute == 17 or next_boundary - now <= timedelta(minutes=2):
            self.skipTest("Too close to the task's minute-17 trigger boundary")

        task_name = f"Codex Enabled Normalization Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^Codex Enabled Normalization Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$productionBefore = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionBefore.Count -gt 1) { throw 'production-task-ambiguous' }
$productionStateBefore = if ($productionBefore.Count -eq 1) { [string]$productionBefore[0].State } else { 'Missing' }
$productionEnabledBefore = if ($productionBefore.Count -eq 1) { [bool]$productionBefore[0].Settings.Enabled } else { $null }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath($env:AURA_TEST_NOOP_ACTION)
    if (-not (Test-Path -LiteralPath $cleanup -PathType Leaf)) { throw 'noop-action-missing' }
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $xml = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    Register-ScheduledTask -TaskName $taskName -Xml $xml -ErrorAction Stop | Out-Null

    $disabledTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $disabledXml = [string](Export-ScheduledTask -TaskName $taskName -ErrorAction Stop)
    if ([string]$disabledTask.State -cne 'Disabled' -or [bool]$disabledTask.Settings.Enabled) { throw 'disabled-state-invalid' }
    $disabledSnapshot = Get-AuraCleanupTaskSnapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $disabledSnapshot -or -not $disabledSnapshot.Disabled -or -not $disabledSnapshot.DefinitionMatches) { throw 'disabled-snapshot-invalid' }
    $disabledAcceptedAsEnabled = Test-AuraCleanupTaskXml -Xml $disabledXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $true -EffectiveRunLevel $disabledTask.Principal.RunLevel -EffectiveStartWhenAvailable $disabledTask.Settings.StartWhenAvailable -EffectiveEnabled $disabledTask.Settings.Enabled
    if ($disabledAcceptedAsEnabled) { throw 'disabled-task-accepted-as-enabled' }

    Assert-AuraCleanupActivationWindow
    Enable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null

    $enabledTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $enabledXml = [string](Export-ScheduledTask -TaskName $taskName -ErrorAction Stop)
    if ([string]$enabledTask.State -ceq 'Disabled' -or -not [bool]$enabledTask.Settings.Enabled) { throw 'enabled-state-invalid' }
    [xml]$enabledDocument = $enabledXml
    $manager = [Xml.XmlNamespaceManager]::new($enabledDocument.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    if ($null -ne $enabledDocument.SelectSingleNode('/t:Task/t:Settings/t:Enabled', $manager)) { throw 'enabled-default-omission-not-observed' }
    $enabledSnapshot = Get-AuraCleanupTaskSnapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $enabledSnapshot -or $enabledSnapshot.Disabled -or -not $enabledSnapshot.DefinitionMatches) { throw 'enabled-snapshot-invalid' }

    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $registered = $service.GetFolder('\').GetTask('\' + $taskName)
    if (-not [bool]$registered.Enabled -or -not [bool]$registered.Definition.Settings.Enabled) { throw 'com-enabled-state-invalid' }
    Write-Output 'REGISTERED_ENABLED_OMISSION_OK'
} catch { $failure = $_ } finally {
    $remaining = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 1) {
        if ([string]$remaining[0].State -cne 'Disabled') { Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    }
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'test-task-cleanup-failed' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
$productionAfter = @(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue)
if ($productionAfter.Count -ne $productionBefore.Count) { throw 'production-task-count-changed' }
if ($productionAfter.Count -eq 1 -and ([string]$productionAfter[0].State -cne $productionStateBefore -or [bool]$productionAfter[0].Settings.Enabled -ne $productionEnabledBefore)) { throw 'production-task-state-changed' }
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REGISTERED_ENABLED_OMISSION_OK", result.stdout)

    def test_versioned_upgrade_replaces_only_a_disposable_disabled_task(self):
        task_name = f"AURA Cleanup Upgrade Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
$neighborName = $taskName + ' Neighbor'
if ($taskName -notmatch '^AURA Cleanup Upgrade Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$env:AURA_TEST_ALLOW_CLEANUP_TASK_UPGRADE = '1'
function Get-TaskFingerprint([string]$name) {
    $tasks = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 0) { return '__ABSENT__' }
    if ($tasks.Count -ne 1) { throw ('task-ambiguous-' + $name) }
    return [string](Export-ScheduledTask -TaskName $name -ErrorAction Stop)
}
function Remove-DisposableTask([string]$name) {
    $tasks = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 1) {
        if ([string]$tasks[0].State -cne 'Disabled') {
            Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
        }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    }
}
$productionBefore = Get-TaskFingerprint 'AURA Demo Cleanup'
$backupBefore = Get-TaskFingerprint 'AURA Demo Backup'
$apiBefore = Get-TaskFingerprint 'AURA API Production'
if ($productionBefore -ceq '__ABSENT__') { throw 'production-task-missing' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath($env:AURA_TEST_NOOP_ACTION)
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    [xml]$oldDocument = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    $manager = [Xml.XmlNamespaceManager]::new($oldDocument.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $arguments = $oldDocument.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments', $manager)
    $arguments.InnerText = Get-AuraCleanupTaskPrePr43Arguments -CleanupScript $cleanup
    Register-ScheduledTask -TaskName $taskName -Xml $oldDocument.OuterXml -ErrorAction Stop | Out-Null
    $neighborXml = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    Register-ScheduledTask -TaskName $neighborName -Xml $neighborXml -ErrorAction Stop | Out-Null
    $neighborBefore = Get-TaskFingerprint $neighborName
    $old = Get-AuraCleanupTaskPrePr43Snapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $old -or -not $old.Disabled -or -not $old.DefinitionMatches) { throw 'old-task-not-recognized' }
    $lastRunBefore = (Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastRunTime
    $result = Upgrade-AuraCleanupTaskVersioned -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($result -cne 'AURA_CLEANUP_TASK_UPGRADED_DISABLED') { throw 'upgrade-result-invalid' }
    $fresh = Get-AuraCleanupTaskSnapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $fresh -or -not $fresh.Disabled -or -not $fresh.DefinitionMatches) { throw 'upgraded-task-invalid' }
    if ($fresh.Xml -notmatch [regex]::Escape((Get-AuraCleanupTaskArguments -CleanupScript $cleanup))) { throw 'current-action-missing' }
    $lastRunAfter = (Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastRunTime
    if ($lastRunAfter -ne $lastRunBefore) { throw 'upgrade-triggered-task' }
    if ((Get-TaskFingerprint $neighborName) -cne $neighborBefore) { throw 'neighbor-task-changed' }
    if ((Get-TaskFingerprint 'AURA Demo Backup') -cne $backupBefore) { throw 'backup-task-changed' }
    if ((Get-TaskFingerprint 'AURA API Production') -cne $apiBefore) { throw 'api-task-changed' }
    if ((Get-TaskFingerprint 'AURA Demo Cleanup') -cne $productionBefore) { throw 'production-task-changed' }
    if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
    Write-Output 'REAL_WINDOWS_VERSION_UPGRADE_OK'
} catch { $failure = $_ } finally {
    Remove-DisposableTask $neighborName
    Remove-DisposableTask $taskName
    Remove-Item Env:AURA_TEST_ALLOW_CLEANUP_TASK_UPGRADE -ErrorAction SilentlyContinue
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'upgrade-task-cleanup-failed' }
if (@(Get-ScheduledTask -TaskName $neighborName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'neighbor-task-cleanup-failed' }
if ((Get-TaskFingerprint 'AURA Demo Cleanup') -cne $productionBefore) { throw 'production-task-finally-changed' }
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REAL_WINDOWS_VERSION_UPGRADE_OK", result.stdout)

    def test_versioned_upgrade_real_validation_failure_restores_old_xml(self):
        task_name = f"AURA Cleanup Upgrade Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^AURA Cleanup Upgrade Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
$env:AURA_TEST_ALLOW_CLEANUP_TASK_UPGRADE = '1'
function Get-TaskFingerprint([string]$name) {
    $tasks = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 0) { return '__ABSENT__' }
    if ($tasks.Count -ne 1) { throw ('task-ambiguous-' + $name) }
    return [string](Export-ScheduledTask -TaskName $name -ErrorAction Stop)
}
function Remove-DisposableTask([string]$name) {
    $tasks = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 1) {
        if ([string]$tasks[0].State -cne 'Disabled') {
            Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
        }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    }
}
$productionBefore = Get-TaskFingerprint 'AURA Demo Cleanup'
$backupBefore = Get-TaskFingerprint 'AURA Demo Backup'
$apiBefore = Get-TaskFingerprint 'AURA API Production'
if ($productionBefore -ceq '__ABSENT__') { throw 'production-task-missing' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$originalSnapshot = ${function:Get-AuraCleanupTaskSnapshot}
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath($env:AURA_TEST_NOOP_ACTION)
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    [xml]$oldDocument = New-AuraCleanupTaskXml -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root -Enabled $false
    $manager = [Xml.XmlNamespaceManager]::new($oldDocument.NameTable)
    $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $arguments = $oldDocument.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments', $manager)
    $arguments.InnerText = Get-AuraCleanupTaskPrePr43Arguments -CleanupScript $cleanup
    Register-ScheduledTask -TaskName $taskName -Xml $oldDocument.OuterXml -ErrorAction Stop | Out-Null
    $capturedOld = Get-TaskFingerprint $taskName
    $old = Get-AuraCleanupTaskPrePr43Snapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $old -or -not $old.Disabled -or -not $old.DefinitionMatches) { throw 'old-task-not-recognized' }
    $lastRunBefore = (Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastRunTime
    function Get-AuraCleanupTaskSnapshot {
        param(
            [string]$TaskName = 'AURA Demo Cleanup',
            [Parameter(Mandatory)][string]$PowerShellPath,
            [Parameter(Mandatory)][string]$CleanupScript,
            [Parameter(Mandatory)][string]$RepositoryRoot
        )
        $snapshot = & $originalSnapshot -TaskName $TaskName -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot
        if ($null -ne $snapshot -and $snapshot.Xml -match '(?i)-ExecutionPolicy\s+Bypass') {
            return [PSCustomObject]@{State=$snapshot.State;Disabled=$snapshot.Disabled;DefinitionMatches=$false;Xml=$snapshot.Xml}
        }
        return $snapshot
    }
    try {
        Upgrade-AuraCleanupTaskVersioned -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
        throw 'validation-failure-not-injected'
    } catch {
        if ($_.Exception.Message -cne 'AURA_CLEANUP_TASK_UPGRADE_VALIDATION_FAILED') { throw }
    }
    Set-Item -Path Function:\Get-AuraCleanupTaskSnapshot -Value $originalSnapshot
    $restored = Get-AuraCleanupTaskPrePr43Snapshot -TaskName $taskName -PowerShellPath $powerShell -CleanupScript $cleanup -RepositoryRoot $root
    if ($null -eq $restored -or -not $restored.Disabled -or -not $restored.DefinitionMatches) { throw 'old-task-not-restored' }
    if ($restored.Xml -cne $capturedOld) { throw 'captured-old-xml-not-restored' }
    $lastRunAfter = (Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastRunTime
    if ($lastRunAfter -ne $lastRunBefore) { throw 'rollback-triggered-task' }
    if ((Get-TaskFingerprint 'AURA Demo Backup') -cne $backupBefore) { throw 'backup-task-changed' }
    if ((Get-TaskFingerprint 'AURA API Production') -cne $apiBefore) { throw 'api-task-changed' }
    if ((Get-TaskFingerprint 'AURA Demo Cleanup') -cne $productionBefore) { throw 'production-task-changed' }
    if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-created' }
    Write-Output 'REAL_WINDOWS_VERSION_UPGRADE_ROLLBACK_OK'
} catch { $failure = $_ } finally {
    Set-Item -Path Function:\Get-AuraCleanupTaskSnapshot -Value $originalSnapshot
    Remove-DisposableTask $taskName
    Remove-Item Env:AURA_TEST_ALLOW_CLEANUP_TASK_UPGRADE -ErrorAction SilentlyContinue
}
if (@(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).Count -ne 0) { throw 'rollback-task-cleanup-failed' }
if ((Get-TaskFingerprint 'AURA Demo Cleanup') -cne $productionBefore) { throw 'production-task-finally-changed' }
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REAL_WINDOWS_VERSION_UPGRADE_ROLLBACK_OK", result.stdout)
