[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PythonPath,
    [Parameter(Mandatory)][ValidateSet('broad','postgresql','postgresql-full','critical')][string]$Suite,
    [Parameter(Mandatory)][int]$Seed,
    [Parameter(Mandatory)][string]$Report
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ((Get-Location).Path -ne $repositoryRoot) { throw 'WORKTREE_CWD_REQUIRED' }
. (Join-Path $repositoryRoot 'deploy\windows\AuraWindows.Common.ps1')
$python = [IO.Path]::GetFullPath($PythonPath)
$signature = Get-AuthenticodeSignature -LiteralPath $python
if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate.Subject.StartsWith('CN=Python Software Foundation')) {
    throw 'TEST_PYTHON_SIGNATURE_INVALID'
}
$pgPassPath = Assert-AuraPathWithin -Path (Join-Path $script:AuraSecretRoot 'test.pgpass') -Root $script:AuraSecretRoot
Assert-AuraOperatorSecretAcl -Path $pgPassPath
# Reuse the official child environment without invoking full discovery or
# assuming the worktree owns a .venv. No secret values are read or printed.
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repositoryRoot 'deploy\windows\Run-AuraPostgreSQLTests.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'TEST_RUNNER_PARSE_FAILED' }
$factory = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'New-AuraPostgreSQLTestProcess'}, $true)
if ($null -eq $factory) { throw 'OFFICIAL_TEST_FACTORY_MISSING' }
. ([scriptblock]::Create($factory.Extent.Text))
if ($Report.Contains('"')) { throw 'INVALID_REPORT_PATH' }
$arguments = '-B -m tools.demo_hardening_qualification --suite ' + $Suite + ' --seed ' + $Seed + ' --report "' + $Report + '"'
$process = New-AuraPostgreSQLTestProcess -Arguments $arguments
$process.WaitForExit()
$code = $process.ExitCode
$process.Dispose()
exit $code
