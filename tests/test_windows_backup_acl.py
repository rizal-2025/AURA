"""Executable regression coverage for protected Windows backup ACLs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMON = PROJECT_ROOT / "deploy" / "windows" / "AuraWindows.Common.ps1"


@unittest.skipUnless(os.name == "nt", "Windows ACL regression requires Windows")
class WindowsBackupAclTests(unittest.TestCase):
    def invoke(self, target: Path, *, container: bool) -> subprocess.CompletedProcess[str]:
        container_switch = " -Container" if container else ""
        script = (
            f". '{COMMON}';"
            "$ErrorActionPreference='Stop';"
            "try {"
            "Set-AuraOperatorProtectedAcl -Path $env:AURA_DUMMY_ACL_PATH"
            f"{container_switch};"
            "$acl = Get-Acl -LiteralPath $env:AURA_DUMMY_ACL_PATH;"
            "$allowed = @(("
            "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value),"
            "'S-1-5-18','S-1-5-32-544');"
            "$unexpected = @($acl.Access | Where-Object {"
            "$_.AccessControlType -eq 'Allow' -and "
            "$_.IdentityReference.Translate("
            "[Security.Principal.SecurityIdentifier]).Value -notin $allowed"
            "});"
            "if (-not $acl.AreAccessRulesProtected -or $unexpected.Count -ne 0) {"
            "throw 'AURA_DUMMY_ACL_INVALID'"
            "};"
            "Write-Output 'AURA_DUMMY_ACL_PROTECTED'"
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
            env={**os.environ, "AURA_DUMMY_ACL_PATH": str(target)},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_directory_acl_is_protected_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(Path(directory), container=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "AURA_DUMMY_ACL_PROTECTED")
            self.assertEqual(result.stderr, "")

    def test_file_acl_is_protected_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.dump"
            path.write_bytes(b"not a database backup")
            result = self.invoke(path, container=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "AURA_DUMMY_ACL_PROTECTED")
            self.assertEqual(result.stderr, "")

    def test_system_validation_keeps_the_generated_acl_shape_exact(self):
        common = COMMON.read_text(encoding="utf-8")
        for expected in (
            "$currentSid -ceq $systemSid",
            "$rules.Count -ne 3",
            "AURA_SECRET_ACL_SYSTEM_SHAPE_INVALID",
            "AURA_SECRET_ACL_SYSTEM_OPERATOR_INVALID",
            "^S-1-5-21-(?:\\d+-){3}\\d+$",
            "FileSystemRights]::FullControl",
            "$rule.IsInherited",
            "$seen.ContainsKey($sid)",
        ):
            self.assertIn(expected, common)


if __name__ == "__main__":
    unittest.main()
