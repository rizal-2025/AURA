"""Real Windows Task Scheduler normalization coverage for demo cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMON = PROJECT_ROOT / "deploy" / "windows" / "AuraWindows.Common.ps1"


@unittest.skipUnless(os.name == "nt", "Real Task Scheduler tests require Windows")
class DemoCleanupWindowsRegisteredTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("Real Task Scheduler registration requires elevation")

    def invoke(self, body: str, task_name: str) -> subprocess.CompletedProcess[str]:
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
            env={**os.environ, "AURA_TEST_TASK_NAME": task_name},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_registered_task_accepts_windows_default_omissions(self):
        task_name = f"AURA Demo Cleanup Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^AURA Demo Cleanup Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
if (@(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue).Count -ne 0) { throw 'production-task-present' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath((Join-Path $root 'deploy\windows\Run-DemoCleanup.ps1'))
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
        task_name = f"AURA Demo Cleanup Test {uuid.uuid4().hex}"
        body = r"""
$taskName = $env:AURA_TEST_TASK_NAME
if ($taskName -notmatch '^AURA Demo Cleanup Test [a-f0-9]{32}$') { throw 'test-task-name-invalid' }
if (@(Get-ScheduledTask -TaskName 'AURA Demo Cleanup' -ErrorAction SilentlyContinue).Count -ne 0) { throw 'production-task-present' }
if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) { throw 'activation-marker-present' }
$failure = $null
try {
    $root = Assert-AuraRepositoryLayout
    $cleanup = [IO.Path]::GetFullPath((Join-Path $root 'deploy\windows\Run-DemoCleanup.ps1'))
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
if ($null -ne $failure) { throw $failure }
"""
        result = self.invoke(body, task_name)
        self.assert_ok(result)
        self.assertIn("REGISTERED_HIGHEST_REJECTED", result.stdout)
